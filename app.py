import os
import time
import sqlite3
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, has_request_context
import requests
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler

# Desativa avisos locais de SSL (caso precise em ambiente interno)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_neon_key_fallback')
APP_VERSION = "v1.1.0"

DB_FILE = 'autopubli_v4.db'
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==========================================
# CONFIGURAÇÕES DA META & FUNIL (NUVEM)
# ==========================================
# Puxa as credenciais direto do painel seguro do Render
META_APP_ID = os.environ.get('META_APP_ID', '')
META_APP_SECRET = os.environ.get('META_APP_SECRET', '')
WEBHOOK_VERIFY_TOKEN = os.environ.get('WEBHOOK_VERIFY_TOKEN', '')
FUNNEL_LINK = "https://seu-link-de-vendas.com/oferta"
PUBLIC_BASE_URL = (os.environ.get('PUBLIC_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')

# IMPORTANTE: seu app usa o fluxo clássico "Instagram Graph API via Facebook
# Login" (escopos pages_show_list / instagram_basic / instagram_content_publish
# / instagram_manage_messages / pages_read_engagement). Nesse fluxo, TODAS as
# chamadas — OAuth, descoberta da conta do Instagram, publicação e mensagens —
# são feitas contra graph.facebook.com, nunca graph.instagram.com (esse host é
# de um produto diferente, "Instagram API with Instagram Login", que usa um
# outro tipo de token e não entende tokens de usuário do Facebook). Foi essa
# mistura de hosts que causava o erro "Cannot parse access token".
GRAPH_API_VERSION = os.environ.get('GRAPH_API_VERSION', 'v20.0')
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
OAUTH_SCOPES = "instagram_basic,instagram_content_publish,instagram_manage_messages,instagram_manage_comments,pages_show_list,pages_read_engagement"

# ==========================================
# 1. BANCO DE DADOS
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT,
        profile_picture_url TEXT, access_token TEXT, ig_user_id TEXT,
        dm_message TEXT, comment_message TEXT,
        status TEXT DEFAULT 'ativa', last_error TEXT,
        total_published INTEGER DEFAULT 0, total_pending INTEGER DEFAULT 0,
        last_published_at TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, total_videos INTEGER DEFAULT 0, completed_count INTEGER DEFAULT 0, pending_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0, status TEXT DEFAULT 'pendente')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, account_id INTEGER,
        filename TEXT, original_filename TEXT, media_type TEXT DEFAULT 'video',
        caption TEXT, scheduled_time TEXT, status TEXT DEFAULT 'aguardando',
        error_message TEXT, processed_at TEXT, position INTEGER,
        FOREIGN KEY(batch_id) REFERENCES batches(id), FOREIGN KEY(account_id) REFERENCES accounts(id)
    )''')
    migrations = {
        'accounts': {
            'profile_picture_url': 'TEXT',
            'dm_message': 'TEXT',
            'comment_message': 'TEXT',
        },
        'publications': {'media_type': "TEXT DEFAULT 'video'"},
    }
    for table, columns in migrations.items():
        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        for column, definition in columns.items():
            if column not in existing_columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
    conn.close()

init_db()

@app.context_processor
def inject_app_version():
    return {'APP_VERSION': APP_VERSION}

# ==========================================
# 2. HELPERS DE AUTENTICAÇÃO / DESCOBERTA (Graph API v20.0)
# ==========================================
def _friendly_meta_error(payload):
    """Extrai uma mensagem amigável de um erro padrão da Graph API."""
    err = payload.get('error', {}) if isinstance(payload, dict) else {}
    code = err.get('code')
    subcode = err.get('error_subcode')
    msg = err.get('message', 'Erro desconhecido ao falar com a Meta.')

    if code == 190:
        return ("Token inválido, expirado ou mal formatado (erro 190). Gere um novo token no "
                "Graph API Explorer selecionando seu App, ou refaça o login em /login_meta. "
                f"Detalhe da Meta: {msg}")
    if subcode == 33 or code == 100:
        return (f"A Meta não encontrou o recurso solicitado. Verifique se o usuário do token "
                f"administra a Página e se ela está vinculada ao Instagram. Detalhe: {msg}")
    return f"Erro da Meta: {msg}"


def _get_json(response):
    """Faz o parse seguro de uma resposta HTTP, nunca deixando estourar exceção."""
    try:
        return response.json()
    except ValueError:
        return {'error': {'message': f'Resposta não-JSON da Meta (HTTP {response.status_code}).'}}


def _get_instagram_profile(ig_user_id, access_token):
    """Busca o perfil diretamente para não depender do objeto aninhado da Página."""
    response = requests.get(
        f"{GRAPH_URL}/{ig_user_id}",
        params={'fields': 'id,username,name,profile_picture_url', 'access_token': access_token},
        timeout=20,
    )
    profile = _get_json(response)
    if 'error' in profile:
        return None, _friendly_meta_error(profile)

    username = (profile.get('username') or '').strip()
    if not username:
        return None, ("A Meta não retornou o username da conta do Instagram. "
                      "Confirme que a Página está vinculada à conta comercial correta.")

    return {
        'id': profile.get('id') or ig_user_id,
        'username': username,
        'name': (profile.get('name') or '').strip(),
        'profile_picture_url': (profile.get('profile_picture_url') or '').strip(),
    }, None


def exchange_for_long_lived_token(short_token):
    """
    Troca um token curto (ou até um token já de usuário do Graph Explorer) por um
    token de longa duração (~60 dias) via fb_exchange_token.
    Retorna (token, erro). Se a troca falhar, devolve o token original como
    fallback (ele ainda pode funcionar por algumas horas) e o erro para log/flash.
    """
    try:
        params = {
            'grant_type': 'fb_exchange_token',
            'client_id': META_APP_ID,
            'client_secret': META_APP_SECRET,
            'fb_exchange_token': short_token,
        }
        r = requests.get(f"{GRAPH_URL}/oauth/access_token", params=params, timeout=20)
        data = _get_json(r)
        if 'access_token' in data:
            return data['access_token'], None
        return short_token, _friendly_meta_error(data)
    except requests.exceptions.Timeout:
        return short_token, "Tempo esgotado ao converter o token para longa duração."
    except requests.exceptions.RequestException as e:
        return short_token, f"Erro de conexão ao converter token: {e}"


def discover_instagram_accounts(user_access_token):
    """
    Dado um token de USUÁRIO do Facebook (curto, longo, ou colado manualmente
    do Graph Explorer), descobre todas as Páginas administradas por esse
    usuário e, para cada uma, a Conta Comercial/Criador do Instagram vinculada.

    Usa o Page Access Token (não o token de usuário) como access_token salvo,
    pois é ele quem carrega as permissões instagram_content_publish /
    instagram_manage_messages para aquela conta específica e tende a durar
    mais tempo em produção.

    Retorna (lista_de_contas, erro_amigavel_ou_None).
    lista_de_contas = [{ig_user_id, username, name, page_id, access_token}]
    """
    if not user_access_token or not user_access_token.strip():
        return [], "Nenhum token foi informado."

    try:
        params = {
            'fields': 'id,name,access_token,instagram_business_account{id}',
            'access_token': user_access_token.strip(),
            'limit': 100,
        }
        r = requests.get(f"{GRAPH_URL}/me/accounts", params=params, timeout=20)
        data = _get_json(r)

        if 'error' in data:
            return [], _friendly_meta_error(data)

        pages = data.get('data', [])
        if not pages:
            return [], ("Nenhuma Página do Facebook foi encontrada para este token. Confirme que "
                         "o token foi gerado com o escopo 'pages_show_list' e que o usuário "
                         "administra ao menos uma Página conectada ao Instagram.")

        contas = []
        for page in pages:
            ig_account = page.get('instagram_business_account')
            if not ig_account or not ig_account.get('id'):
                continue

            page_access_token = page.get('access_token') or user_access_token.strip()
            profile, profile_error = _get_instagram_profile(
                ig_account['id'],
                page_access_token,
            )
            if profile_error:
                return [], profile_error

            # O nome da Página é apenas um último recurso para contas sem nome
            # público; username nunca deve ser substituído por um placeholder.
            profile_name = profile['name'] or (page.get('name') or '').strip() or profile['username']
            contas.append({
                'ig_user_id': profile['id'],
                'username': profile['username'],
                'name': profile_name,
                'profile_picture_url': profile['profile_picture_url'],
                'page_id': page.get('id'),
                # Page Access Token: é este que deve ser usado para publicar
                # Reels e enviar/responder DMs nessa conta do Instagram.
                'access_token': page_access_token,
            })

        if not contas:
            return [], ("Encontramos suas Páginas do Facebook, mas nenhuma delas está vinculada a "
                         "uma conta comercial ou de criador do Instagram. Vincule o Instagram à "
                         "Página em Configurações da Página > Instagram, e tente novamente.")

        return contas, None

    except requests.exceptions.Timeout:
        return [], "Tempo esgotado ao contatar a Meta (/me/accounts). Tente novamente em instantes."
    except requests.exceptions.RequestException as e:
        return [], f"Erro de conexão com a Meta: {e}"


def salvar_contas_descobertas(contas, apelido_manual=None):
    """Insere/atualiza no SQLite as contas descobertas. Retorna (novas, atualizadas)."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    novas, atualizadas = [], []
    for conta in contas:
        cursor.execute("SELECT id FROM accounts WHERE ig_user_id = ?", (conta['ig_user_id'],))
        existente = cursor.fetchone()
        display_name = apelido_manual if (apelido_manual and len(contas) == 1) else conta['name']
        if existente:
            cursor.execute(
                "UPDATE accounts SET name = ?, username = ?, profile_picture_url = ?, access_token = ?, "
                "status = 'ativa', last_error = NULL WHERE ig_user_id = ?",
                (display_name, conta['username'], conta.get('profile_picture_url', ''),
                 conta['access_token'], conta['ig_user_id'])
            )
            atualizadas.append(conta['username'])
        else:
            cursor.execute(
                "INSERT INTO accounts (name, username, profile_picture_url, access_token, ig_user_id, status) "
                "VALUES (?, ?, ?, ?, ?, 'ativa')",
                (display_name, conta['username'], conta.get('profile_picture_url', ''),
                 conta['access_token'], conta['ig_user_id'])
            )
            novas.append(conta['username'])
    conn.commit()
    conn.close()
    return novas, atualizadas

# ==========================================
# 3. LOGIN AUTOMÁTICO (OAUTH 2.0)
# ==========================================
@app.route('/login_meta')
def login_meta():
    """Redireciona o usuário para a tela oficial de login da Meta"""
    if not META_APP_ID:
        flash("META_APP_ID não configurado nas variáveis de ambiente do Render.", "error")
        return redirect(url_for('contas'))
    redirect_uri = url_for('meta_callback', _external=True, _scheme='https')
    url = (
        f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={OAUTH_SCOPES}"
        f"&response_type=code"
    )
    return redirect(url)


@app.route('/callback')
def meta_callback():
    """Recebe o code da Meta, troca por token de 60 dias e descobre a(s) conta(s) do Instagram"""
    code = request.args.get('code')
    error = request.args.get('error_description') or request.args.get('error')
    if error:
        flash(f"Conexão cancelada ou negada pela Meta: {error}", "error")
        return redirect(url_for('contas'))
    if not code:
        flash("Conexão cancelada pelo usuário.", "error")
        return redirect(url_for('contas'))

    redirect_uri = url_for('meta_callback', _external=True, _scheme='https')

    try:
        # 1. Troca o code pelo token curto
        params = {
            'client_id': META_APP_ID,
            'redirect_uri': redirect_uri,
            'client_secret': META_APP_SECRET,
            'code': code,
        }
        r = requests.get(f"{GRAPH_URL}/oauth/access_token", params=params, timeout=20)
        res = _get_json(r)
        short_token = res.get('access_token')

        if not short_token:
            flash(f"Falha ao autorizar com a Meta: {_friendly_meta_error(res)}", "error")
            return redirect(url_for('contas'))

        # 2. Converte para token longo (60 dias)
        long_token, exchange_error = exchange_for_long_lived_token(short_token)
        if exchange_error:
            # Não é fatal: seguimos com o token curto, mas avisamos o usuário.
            flash(f"Aviso: não foi possível estender o token para 60 dias ({exchange_error}). "
                  f"Usando token de curta duração por enquanto.", "error")

        # 3. Descobre as Páginas do usuário e as contas do Instagram vinculadas
        contas, discover_error = discover_instagram_accounts(long_token)
        if discover_error:
            flash(f"Login na Meta funcionou, mas falhou ao localizar sua conta do Instagram: {discover_error}", "error")
            return redirect(url_for('contas'))

        # 4. Salva no banco
        novas, atualizadas = salvar_contas_descobertas(contas)
        if novas:
            flash(f"Conta(s) sincronizada(s) automaticamente: @{', @'.join(novas)}.", "success")
        if atualizadas:
            flash(f"Conta(s) reconectada(s), token renovado por 60 dias: @{', @'.join(atualizadas)}.", "success")

    except requests.exceptions.Timeout:
        flash("Tempo esgotado ao falar com a Meta durante o login. Tente novamente.", "error")
    except requests.exceptions.RequestException as e:
        flash(f"Erro de conexão com a Meta durante o login: {e}", "error")
    except Exception as e:
        flash(f"Falha de sistema na Automação: {str(e)}", "error")

    return redirect(url_for('contas'))

# ==========================================
# 4. WEBHOOK (AUTOMAÇÃO DE DM / FUNIL)
# ==========================================
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == WEBHOOK_VERIFY_TOKEN: return challenge, 200
    return "Falha", 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    data = request.json
    try:
        if data.get('object') == 'instagram':
            for entry in data.get('entry', []):
                ig_user_id = entry.get('id')
                for messaging_event in entry.get('messaging', []):
                    if 'message' in messaging_event:
                        sender_id = messaging_event['sender']['id']
                        text_received = messaging_event['message'].get('text', '')
                        enviar_resposta_automatica(ig_user_id, sender_id, text_received)
                    elif 'reaction' in messaging_event:
                        sender_id = messaging_event['sender']['id']
                        enviar_resposta_automatica(ig_user_id, sender_id, "Reação")
                for change in entry.get('changes', []):
                    if change.get('field') != 'comments':
                        continue
                    comment = change.get('value') or {}
                    comment_id = comment.get('id')
                    if comment_id:
                        responder_comentario_automatico(ig_user_id, comment_id, comment.get('text', ''))
    except Exception as e: print(f"Erro Webhook: {e}")
    return jsonify({"status": "ok"}), 200

def enviar_resposta_automatica(ig_user_id, recipient_id, mensagem_recebida):
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT access_token, username, dm_message FROM accounts WHERE ig_user_id = ? AND status = 'ativa'", (ig_user_id,))
    account = cursor.fetchone(); conn.close()
    if not account: return

    texto_resposta = account['dm_message'] or (
        f"Fala, tudo bem? Vi que você interagiu com a nossa conta "
        f"(@{account['username']}). Acesse nosso material exclusivo aqui:\n\n👉 {FUNNEL_LINK}"
    )

    payload = {"recipient": {"id": recipient_id}, "message": {"text": texto_resposta}}
    try:
        res_post = requests.post(
            f"{GRAPH_URL}/{ig_user_id}/messages",
            params={'access_token': account['access_token']},
            json=payload,
            timeout=20,
        )
        res = _get_json(res_post)
        if 'recipient_id' in res:
            print(f"✅ Bot disparou DM na conta @{account['username']}")
        else:
            print(f"❌ Falha ao disparar DM na conta @{account['username']}: {_friendly_meta_error(res)}")
    except requests.exceptions.RequestException as e:
        print(f"❌ Falha de conexão ao disparar DM: {e}")

def responder_comentario_automatico(ig_user_id, comment_id, comentario):
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute(
        "SELECT access_token, comment_message FROM accounts "
        "WHERE ig_user_id = ? AND status = 'ativa'", (ig_user_id,)
    )
    account = cursor.fetchone(); conn.close()
    if not account or not account['comment_message']:
        return

    try:
        response = requests.post(
            f"{GRAPH_URL}/{comment_id}/replies",
            data={
                'message': account['comment_message'],
                'access_token': account['access_token'],
            },
            timeout=20,
        )
        result = _get_json(response)
        if 'id' in result:
            print(f"OK: resposta automatica enviada para comentario na conta {ig_user_id}")
        else:
            print(f"Falha ao responder comentario: {_friendly_meta_error(result)}")
    except requests.exceptions.RequestException as e:
        print(f"Falha de conexao ao responder comentario: {e}")

# ==========================================
# 5. WORKER DE AGENDAMENTO DE POSTAGENS
# ==========================================
def process_queue():
    with app.app_context():
        try:
            conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            cursor.execute("SELECT p.*, a.access_token, a.ig_user_id, a.username, a.status as account_status FROM publications p JOIN accounts a ON p.account_id = a.id WHERE p.status = 'aguardando' AND p.scheduled_time <= ? ORDER BY p.scheduled_time ASC", (now,))
            pendentes = cursor.fetchall()

            for pub in pendentes:
                pub_id, account_id = pub['id'], pub['account_id']
                if pub['account_status'] != 'ativa':
                    cursor.execute("UPDATE publications SET status='erro', error_message=? WHERE id=?", (f"Conta offline", pub_id)); conn.commit(); continue

                cursor.execute("UPDATE publications SET status='processando' WHERE id=?", (pub_id,)); conn.commit()

                try:
                    upload_url = f"{GRAPH_URL}/{pub['ig_user_id']}/media"

                    # No Render, pegamos a URL pública real do app automaticamente via requisição ou variável
                    public_url = (
                        request.host_url.rstrip('/')
                        if has_request_context()
                        else (PUBLIC_BASE_URL or "https://seu-app.onrender.com")
                    )
                    media_url = f"{public_url}/uploads/{pub['filename']}"
                    if pub['media_type'] == 'image':
                        payload = {
                            'image_url': media_url,
                            'caption': pub['caption'] or '',
                            'access_token': pub['access_token'],
                        }
                    else:
                        payload = {
                            'media_type': 'REELS',
                            'video_url': media_url,
                            'caption': pub['caption'] or '',
                            'access_token': pub['access_token'],
                        }
                    res_upload = requests.post(upload_url, data=payload, timeout=60)
                    res = _get_json(res_upload)

                    if 'id' not in res:
                        raise Exception(_friendly_meta_error(res))

                    if pub['media_type'] == 'video':
                        time.sleep(35)

                    res_publish = requests.post(
                        f"{GRAPH_URL}/{pub['ig_user_id']}/media_publish",
                        data={'creation_id': res['id'], 'access_token': pub['access_token']},
                        timeout=60,
                    )
                    pub_res = _get_json(res_publish)

                    if 'id' in pub_res:
                        cursor.execute("UPDATE publications SET status='publicado', processed_at=?, error_message=NULL WHERE id=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), pub_id))
                        cursor.execute("UPDATE accounts SET total_published=total_published+1, last_published_at=?, status='ativa', last_error=NULL WHERE id=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), account_id))
                    else:
                        raise Exception(_friendly_meta_error(pub_res))

                except Exception as e:
                    cursor.execute("UPDATE publications SET status='erro', error_message=?, processed_at=? WHERE id=?", (str(e), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), pub_id))
                    cursor.execute("UPDATE accounts SET status='erro', last_error=? WHERE id=?", (str(e), account_id))
                conn.commit()

                cursor.execute("SELECT batch_id FROM publications WHERE id=?", (pub_id,))
                b_row = cursor.fetchone()
                if b_row and b_row['batch_id']:
                    b_id = b_row['batch_id']
                    cursor.execute("UPDATE batches SET completed_count=(SELECT COUNT(*) FROM publications WHERE batch_id=? AND status='publicado'), error_count=(SELECT COUNT(*) FROM publications WHERE batch_id=? AND status='erro'), pending_count=(SELECT COUNT(*) FROM publications WHERE batch_id=? AND status IN ('aguardando','processando')) WHERE id=?", (b_id, b_id, b_id, b_id)); conn.commit()
            conn.close()
        except Exception as e: print(f"Worker Error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=process_queue, trigger='interval', seconds=30)
scheduler.start()

# ==========================================
# 6. ROTAS DO PAINEL WEB
# ==========================================
@app.route('/uploads/<filename>')
def uploaded_file(filename): return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/')
def dashboard():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='ativa' THEN 1 ELSE 0 END) as ativa, SUM(CASE WHEN status='erro' THEN 1 ELSE 0 END) as erro FROM accounts")
    acc_stats = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='publicado' THEN 1 ELSE 0 END) as pub, SUM(CASE WHEN status IN ('aguardando','processando') THEN 1 ELSE 0 END) as fila, SUM(CASE WHEN status='processando' THEN 1 ELSE 0 END) as proc, SUM(CASE WHEN status='erro' THEN 1 ELSE 0 END) as err, SUM(CASE WHEN status='cancelado' THEN 1 ELSE 0 END) as canc FROM publications")
    pub_stats = cursor.fetchone()
    cursor.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1"); ultimo_lote = cursor.fetchone()
    cursor.execute("SELECT * FROM accounts WHERE status='ativa'"); contas_ativas = [dict(row) for row in cursor.fetchall()]; conn.close()

    stats = {"contas_total": acc_stats['total'] or 0, "contas_ativas": acc_stats['ativa'] or 0, "contas_erro": acc_stats['erro'] or 0, "total": pub_stats['total'] or 0, "publicado": pub_stats['pub'] or 0, "fila": pub_stats['fila'] or 0, "processando": pub_stats['proc'] or 0, "erro": pub_stats['err'] or 0, "cancelado": pub_stats['canc'] or 0}
    return render_template('dashboard.html', stats=stats, ultimo_lote=ultimo_lote, contas_ativas=contas_ativas, active_page='dashboard')

@app.route('/agendar_lote', methods=['POST'])
def agendar_lote():
    account_ids = request.form.getlist('account_ids')
    media_files = request.files.getlist('media')
    allowed_extensions = {'.mp4': 'video', '.jpg': 'image', '.jpeg': 'image', '.png': 'image'}
    media_files = [
        media for media in media_files
        if media.filename and os.path.splitext(media.filename.lower())[1] in allowed_extensions
    ]
    if not account_ids or not media_files: return redirect(url_for('dashboard'))

    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("INSERT INTO batches (created_at, total_videos, pending_count, status) VALUES (?, ?, ?, 'processando')", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), len(media_files), len(media_files)))
    batch_id = cursor.lastrowid

    tempo_atual = datetime.strptime(request.form.get('data_hora'), '%Y-%m-%dT%H:%M')
    for index, media in enumerate(media_files):
        extension = os.path.splitext(media.filename.lower())[1]
        media_type = allowed_extensions[extension]
        filename = f"media_{batch_id}_{index}_{int(time.time())}{extension}"
        media.save(os.path.join(UPLOAD_FOLDER, filename))
        cursor.execute(
            "INSERT INTO publications (batch_id, account_id, filename, original_filename, media_type, "
            "caption, scheduled_time, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, account_ids[index % len(account_ids)], filename, media.filename, media_type,
             request.form.get('legenda', ''), tempo_atual.strftime('%Y-%m-%d %H:%M'), index + 1)
        )
        tempo_atual += timedelta(minutes=int(request.form.get('intervalo', 5)))

    conn.commit(); conn.close()
    return redirect(url_for('fila'))

@app.route('/fila')
def fila():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT p.*, a.username as account_username, a.name as account_name FROM publications p JOIN accounts a ON p.account_id = a.id ORDER BY p.scheduled_time DESC LIMIT 200")
    queue_dicts = [dict(row) for row in cursor.fetchall()]; conn.close()
    return render_template('fila.html', queue=queue_dicts, queue_json=json.dumps(queue_dicts), active_page='fila')

@app.route('/contas')
def contas():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts")
    accs = [dict(row) for row in cursor.fetchall()]; conn.close()
    return render_template('contas.html', accounts=accs, active_page='contas')

@app.route('/automacoes', methods=['GET', 'POST'])
def automacoes():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if request.method == 'POST':
        account_id = request.form.get('account_id', type=int)
        dm_message = request.form.get('dm_message', '').strip()
        comment_message = request.form.get('comment_message', '').strip()
        if not account_id:
            conn.close()
            flash("Selecione uma conta válida para salvar as automações.", "error")
            return redirect(url_for('automacoes'))
        cursor.execute(
            "UPDATE accounts SET dm_message = ?, comment_message = ? WHERE id = ?",
            (dm_message or None, comment_message or None, account_id),
        )
        conn.commit()
        conn.close()
        flash("Automações salvas para a conta selecionada.", "success")
        return redirect(url_for('automacoes'))

    cursor.execute("SELECT * FROM accounts ORDER BY name, username")
    accounts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return render_template('automacoes.html', accounts=accounts, active_page='automacoes')

@app.route('/contas/automatizar', methods=['POST'])
def automatizar_conta():
    """
    Inserção manual de token (ex: colado do Graph API Explorer).
    Fluxo:
      1. Tenta estender o token para longa duração (fb_exchange_token).
      2. Usa /me/accounts para achar as Páginas do usuário e suas contas do
         Instagram vinculadas (isso é o que estava faltando: chamar
         graph.instagram.com/me diretamente nunca funciona com um token de
         usuário do Facebook).
      3. Salva o Page Access Token de cada conta encontrada.
    """
    token = (request.form.get('access_token') or '').strip()
    apelido = request.form.get('name', '').strip() or None

    if not token:
        flash("Cole um token de acesso válido antes de enviar.", "error")
        return redirect(url_for('contas'))

    try:
        # 1. Tenta estender para 60 dias (não é fatal se falhar)
        long_token, exchange_error = exchange_for_long_lived_token(token)

        # 2. Descobre Páginas + contas do Instagram vinculadas
        contas_encontradas, discover_error = discover_instagram_accounts(long_token)

        if discover_error:
            flash(f"Token Inválido: {discover_error}", "error")
            return redirect(url_for('contas'))

        # 3. Salva
        novas, atualizadas = salvar_contas_descobertas(contas_encontradas, apelido_manual=apelido)

        if novas:
            flash(f"Conta(s) adicionada(s) ao hub: @{', @'.join(novas)}.", "success")
        if atualizadas:
            flash(f"Conta(s) já existente(s), token atualizado: @{', @'.join(atualizadas)}.", "success")
        if not novas and not atualizadas:
            flash("Nenhuma conta nova encontrada para este token.", "error")
        if exchange_error:
            flash(f"Aviso: token salvo pode ser de curta duração ({exchange_error}).", "error")

    except Exception as e:
        flash(f"Falha inesperada ao processar o token: {str(e)}", "error")

    return redirect(url_for('contas'))

@app.route('/contas/<int:id>/toggle', methods=['POST'])
def toggle_conta(id):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT status FROM accounts WHERE id = ?", (id,))
    row = cursor.fetchone()
    if row: cursor.execute("UPDATE accounts SET status = ? WHERE id = ?", ('desativada' if row[0] == 'ativa' else 'ativa', id)); conn.commit()
    conn.close(); return redirect(url_for('contas'))

@app.route('/contas/<int:id>/remover', methods=['POST'])
def remover_conta(id):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("DELETE FROM accounts WHERE id = ?", (id,)); conn.commit(); conn.close()
    return redirect(url_for('contas'))

if __name__ == '__main__':
    print("🚀 Auto Publi Pro [Render Edition] rodando na porta 5000...")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
