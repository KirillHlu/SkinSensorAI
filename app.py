import os
import uuid
import sqlite3
import base64
import json
import requests
import urllib3
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, make_response, send_from_directory, flash, jsonify, session
import sys
import platform
import time
from werkzeug.security import generate_password_hash, check_password_hash

from werkzeug.utils import secure_filename
from models import DNOClassifier, AcneClassifier
from i18n import init_i18n, assistant_language_instruction, translate as t

BASE_DIR = Path(__file__).resolve().parent
UPLOADS = BASE_DIR / "static" / "uploads"
DB_PATH = BASE_DIR / "web_skin_analysis.db"
ALLOWED_EXT = {"png", "jpg", "jpeg"}

app = Flask(__name__)
init_i18n(app)


def load_config():
    config_path = BASE_DIR / 'config.json'
    try:
        with config_path.open(encoding='utf-8') as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        config = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Некорректный JSON в {config_path}: {exc}') from exc
    return config


CONFIG = load_config()
app.secret_key = os.environ.get('FLASK_SECRET') or CONFIG.get('flask', {}).get('secret_key', 'dev-secret')

# ensure uploads exist
UPLOADS.mkdir(parents=True, exist_ok=True)

# Initialize classifiers (will log warnings if models missing)
dno = DNOClassifier(models_dir=str(BASE_DIR / 'models'))
acne = AcneClassifier(models_dir=str(BASE_DIR / 'models'))

# --- GigaChat / consultations setup ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYSTEM_PROMPT = '''Ты - консультант по уходу за кожей и профессиональный дерматологический помощник.
Отвечай понятно и по делу на языке, указанном в инструкции о языке сайта ниже. Учитывай историю диалога и результаты анализа кожи пользователя.
В сообщении ниже раздел «АКТУАЛЬНЫЕ РЕЗУЛЬТАТЫ АНАЛИЗА» содержит реальные данные из базы этого пользователя.
Если пользователь спрашивает «видишь ли ты анализ» или просит посмотреть анализы, ответь прямо по этим данным:
назови дату, тип кожи, уверенность и признаки акне. Не говори, что нужен файл или ссылка: изображения уже были обработаны сайтом.
Старые ответы ассистента в истории могут быть ошибочными — не считай их источником фактов и доверяй актуальным результатам анализа.
Не ставь окончательный медицинский диагноз, давай безопасные рекомендации и советуй дерматолога при необходимости.
Не выдумывай данные, которых нет в актуальных результатах.'''


def get_gigachat_credentials():
    gigachat = CONFIG.get('gigachat', {})
    client_id = os.environ.get('GIGACHAT_CLIENT_ID') or gigachat.get('client_id')
    client_secret = os.environ.get('GIGACHAT_CLIENT_SECRET') or gigachat.get('client_secret')
    return client_id, client_secret


GIGACHAT_CLIENT_ID, GIGACHAT_CLIENT_SECRET = get_gigachat_credentials()
GIGACHAT_OAUTH_URL = CONFIG.get('gigachat', {}).get(
    'oauth_url', 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'
)
GIGACHAT_CHAT_URL = CONFIG.get('gigachat', {}).get(
    'chat_url', 'https://gigachat.devices.sberbank.ru/api/v1/chat/completions'
)


# helper functions for messages
def db_add_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)', (str(user_id), role, content))
    # keep last 50
    cursor.execute('''
        DELETE FROM messages WHERE user_id = ? AND id NOT IN (
            SELECT id FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT 50
        )
    ''', (str(user_id), str(user_id)))
    conn.commit()
    conn.close()


def db_get_history(user_id, limit=30):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT role, content FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', (str(user_id), limit))
    rows = cursor.fetchall()
    conn.close()
    return [{'role': r[0], 'content': r[1]} for r in reversed(rows)]


def db_get_analysis_context(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT mode, dno_prediction, dno_confidence, acne_detected,
               acne_confidence, recommendation, created_at
        FROM analysis_history
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    ''', (str(user_id), limit))
    rows = cursor.fetchall()
    conn.close()

    analyses = []
    for row in reversed(rows):
        analyses.append(
            f"- Дата: {row[6]}; режим: {row[0]}; "
            f"тип кожи: {row[1] or 'нет данных'} "
            f"(уверенность {row[2]:.2f}); "
            f"признаки акне: {'обнаружены' if row[3] else 'не обнаружены'} "
            f"(уверенность {row[4]:.2f}); рекомендации анализа: "
            f"{row[5] or 'нет данных'}"
        )
    if not analyses:
        return 'Результатов анализа кожи пока нет.'
    return '\n'.join(analyses)


def gigachat_post(url, **kwargs):
    ca_bundle = os.environ.get('GIGACHAT_CA_BUNDLE')
    if ca_bundle:
        kwargs['verify'] = ca_bundle
    for attempt in range(2):
        try:
            return requests.post(url, **kwargs)
        except requests.ConnectionError:
            if attempt == 1:
                raise
            time.sleep(1)


def ask_gigachat(user_id, user_message):
    if not GIGACHAT_CLIENT_ID or not GIGACHAT_CLIENT_SECRET:
        return t("GigaChat не настроен на сервере. Задайте GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET.")

    history = db_get_history(user_id, limit=20)
    analysis_context = db_get_analysis_context(user_id)
    messages = [{
        'role': 'system',
        'content': f"{SYSTEM_PROMPT}\n\nАКТУАЛЬНЫЕ РЕЗУЛЬТАТЫ АНАЛИЗА:\n{analysis_context}" + assistant_language_instruction()
    }]
    for m in history:
        messages.append({'role': m['role'], 'content': m['content']})
    if not history or history[-1]['role'] != 'user' or history[-1]['content'] != user_message:
        messages.append({'role': 'user', 'content': user_message})

    try:
        auth_key = base64.b64encode(
            f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode()
        ).decode()
        token_resp = gigachat_post(
            GIGACHAT_OAUTH_URL,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
                'RqUID': str(uuid.uuid4()),
                'Authorization': f'Basic {auth_key}',
            },
            data={'scope': 'GIGACHAT_API_PERS'},
            verify=False,
            timeout=30,
        )
        token_resp.raise_for_status()
        token = token_resp.json().get('access_token')
        if not token:
            return t('Ошибка GigaChat: OAuth-сервер не вернул access_token.')

        chat_resp = gigachat_post(
            GIGACHAT_CHAT_URL,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Authorization': f'Bearer {token}',
            },
            json={'model': 'GigaChat', 'messages': messages},
            verify=False,
            timeout=60,
        )
        chat_resp.raise_for_status()
        choices = chat_resp.json().get('choices', [])
        if not choices or not choices[0].get('message', {}).get('content'):
            return t('Ошибка GigaChat: ответ не содержит текста консультации.')
        return choices[0]['message']['content'].strip()
    except requests.ConnectionError:
        return t('GigaChat временно недоступен. Повторите сообщение через несколько секунд.')
    except requests.RequestException as exc:
        return t('Ошибка при обращении к GigaChat:') + ' ' + str(exc)
    except OSError as exc:
        return t('Ошибка сетевого соединения с GigaChat:') + ' ' + str(exc)
    except ValueError as exc:
        return t('Ошибка формата ответа GigaChat:') + ' ' + str(exc)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            password_hash TEXT,
            name TEXT,
            age INTEGER,
            preferences TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # messages (for consultations/chat)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # analysis history
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analysis_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            mode TEXT,
            dno_prediction TEXT DEFAULT '',
            dno_confidence REAL DEFAULT 0,
            acne_detected INTEGER DEFAULT 0,
            acne_confidence REAL DEFAULT 0,
            recommendation TEXT DEFAULT '',
            image_path TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # detailed analysis by zones
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS detailed_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER,
            zone_name TEXT,
            dno_prediction TEXT DEFAULT '',
            dno_confidence REAL DEFAULT 0,
            acne_detected INTEGER DEFAULT 0,
            acne_confidence REAL DEFAULT 0,
            FOREIGN KEY (analysis_id) REFERENCES analysis_history (id)
        )
    ''')

    # reminders
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            text TEXT,
            time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        )
    ''')

    # Ensure columns for email/password exist (for legacy DBs)
    cursor.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in cursor.fetchall()]
    if 'email' not in cols:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except Exception:
            pass
    if 'password_hash' not in cols:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        except Exception:
            pass

    conn.commit()
    conn.close()


def get_or_set_user_id(resp=None):
    # anonymous id stored in cookie (used if user not logged in)
    user_id = request.cookies.get('user_id')
    if not user_id:
        user_id = str(uuid.uuid4())
        if resp is None:
            resp = make_response()
        resp.set_cookie('user_id', user_id, max_age=60*60*24*365)
    return user_id, resp


def current_user_id():
    # return authenticated user_id (email) if logged in, else anonymous cookie id
    auth = session.get('auth_user')
    if auth:
        return auth
    return request.cookies.get('user_id')


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('auth_user'):
            flash('Пожалуйста, войдите в систему чтобы продолжить', 'warning')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapper


# Initialize DB at startup
init_db()


@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    user_id, resp = get_or_set_user_id(resp)
    return resp


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    # profile for authenticated users
    user_id = session.get('auth_user')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT email, name, age, preferences FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()

    if request.method == 'POST':
        name = request.form.get('name')
        age = request.form.get('age')
        prefs = request.form.get('preferences')
        if row:
            cursor.execute('UPDATE users SET name = ?, age = ?, preferences = ? WHERE user_id = ?', (name, age or None, prefs, user_id))
        else:
            cursor.execute('INSERT OR REPLACE INTO users (user_id, email, name, age, preferences) VALUES (?, ?, ?, ?, ?)', (user_id, user_id, name, age or None, prefs))
        conn.commit()
        conn.close()
        flash('Профиль сохранён', 'success')
        return redirect(url_for('profile'))

    conn.close()
    user = {'email': row[0], 'name': row[1], 'age': row[2], 'preferences': row[3]} if row else None
    return render_template('profile.html', user=user)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


@app.route('/analyze', methods=['GET', 'POST'])
@login_required
def analyze():
    # only authenticated users can analyze to keep results private
    user_id = session.get('auth_user')

    if request.method == 'POST':
        file = request.files.get('image')
        mode = request.form.get('mode', 'quick')
        if not file or file.filename == '':
            flash('Пожалуйста, загрузите изображение', 'danger')
            return redirect(url_for('analyze'))
        if not allowed_file(file.filename):
            flash('Неподдерживаемый формат файла', 'danger')
            return redirect(url_for('analyze'))

        filename = secure_filename(f"{uuid.uuid4().hex}_{file.filename}")
        save_path = UPLOADS / filename
        file.save(save_path)

        # Run classifiers (they will return None if models missing)
        dno_res = dno.predict(str(save_path)) if hasattr(dno, 'predict') else None
        acne_res = acne.predict(str(save_path)) if hasattr(acne, 'predict') else None

        # Build recommendation text
        recs = []
        if acne_res and acne_res.get('recommendations'):
            recs.extend(acne_res['recommendations'])
        if dno_res:
            recs.append(f"Тип кожи: {dno_res.get('prediction')} (уверенность {dno_res.get('confidence'):.2f})")

        recommendation = '\n'.join(recs) if recs else 'Нет рекомендаций (модели не загружены)'

        # save to db associated with authenticated user
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO analysis_history (user_id, mode, dno_prediction, dno_confidence, acne_detected, acne_confidence, recommendation, image_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            mode,
            dno_res.get('prediction') if dno_res else '',
            dno_res.get('confidence') if dno_res else 0,
            1 if (acne_res and acne_res.get('predicted_class') != 'normal') else 0,
            acne_res.get('confidence') if acne_res else 0,
            recommendation,
            str(save_path.relative_to(BASE_DIR))
        ))
        conn.commit()
        analysis_id = cursor.lastrowid
        conn.close()

        return redirect(url_for('result', analysis_id=analysis_id))

    return render_template('analyze.html')


# --- Consultations (chat) routes ---
@app.route('/consult')
@login_required
def consult():
    # Only authenticated users allowed — history is tied to their account
    user_id = session.get('auth_user')
    history = db_get_history(user_id, limit=50)
    return render_template('consult.html', history=history)


@app.route('/consult/send', methods=['POST'])
def consult_send():
    # AJAX endpoint — require authentication; return 401 JSON for AJAX callers
    if not session.get('auth_user'):
        return jsonify({'ok': False, 'error': t('Unauthorized')}), 401
    user_id = session.get('auth_user')
    text = request.form.get('message')
    if not text:
        return jsonify({'ok': False, 'error': t('Empty message')}), 400
    db_add_message(user_id, 'user', text)
    answer = ask_gigachat(user_id, text)
    db_add_message(user_id, 'assistant', answer)
    return jsonify({'ok': True, 'answer': answer})


@app.route('/consult/history')
def consult_history():
    if not session.get('auth_user'):
        return jsonify({'history': []}), 401
    user_id = session.get('auth_user')
    history = db_get_history(user_id, limit=100)
    return jsonify({'history': history})


# --- Authentication routes ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')
        if not email or not password:
            flash('Email и пароль обязательны', 'danger')
            return redirect(url_for('register'))
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE email = ?', (email,))
        if cursor.fetchone():
            conn.close()
            flash('Пользователь с таким email уже существует', 'warning')
            return redirect(url_for('register'))
        user_id = email
        pw_hash = generate_password_hash(password)
        cursor.execute('INSERT OR REPLACE INTO users (user_id, email, password_hash, name) VALUES (?, ?, ?, ?)', (user_id, email, pw_hash, name))
        conn.commit()
        conn.close()
        # set session and migrate any anonymous data from cookie to this account
        session['auth_user'] = user_id
        anon_id = request.cookies.get('user_id')
        if anon_id and anon_id != user_id:
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute('UPDATE messages SET user_id = ? WHERE user_id = ?', (user_id, anon_id))
                cur.execute('UPDATE analysis_history SET user_id = ? WHERE user_id = ?', (user_id, anon_id))
                conn.commit()
                conn.close()
                flash('Данные из анонимной сессии объединены с аккаунтом', 'info')
            except Exception:
                pass
        flash('Регистрация успешна', 'success')
        resp = make_response(redirect(url_for('dashboard')))
        # clear anonymous cookie to avoid confusion
        resp.set_cookie('user_id', '', expires=0)
        return resp
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM users WHERE email = ?', (email,))
        row = cursor.fetchone()
        conn.close()
        if not row or not check_password_hash(row[0], password):
            flash('Неверный email или пароль', 'danger')
            return redirect(url_for('login'))
        # login successful
        session['auth_user'] = email
        # migrate anonymous data if present
        anon_id = request.cookies.get('user_id')
        if anon_id and anon_id != email:
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute('UPDATE messages SET user_id = ? WHERE user_id = ?', (email, anon_id))
                cur.execute('UPDATE analysis_history SET user_id = ? WHERE user_id = ?', (email, anon_id))
                conn.commit()
                conn.close()
                flash('Данные из анонимной сессии объединены с аккаунтом', 'info')
            except Exception:
                pass
        flash('Вы вошли в систему', 'success')
        next_url = request.args.get('next')
        resp = make_response(redirect(next_url or url_for('dashboard')))
        resp.set_cookie('user_id', '', expires=0)
        return resp
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('auth_user', None)
    flash('Вы вышли', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session.get('auth_user')
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, mode, dno_prediction, acne_detected, created_at FROM analysis_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    analyses = [{'id': r[0], 'mode': r[1], 'dno_prediction': r[2], 'acne_detected': r[3], 'created_at': r[4]} for r in rows]
    return render_template('dashboard.html', analyses=analyses)


@app.route('/status')
def status():
    # return simple JSON showing if models are loaded
    return jsonify({
        'dno_loaded': bool(getattr(dno, 'model', None)),
        'acne_loaded': bool(getattr(acne, 'model', None))
    })


@app.route('/debug')
def debug_info():
    # Provide detailed diagnostics to help figure out missing-model problems
    dno_path = os.path.join(str(BASE_DIR), 'models', 'best_model_dno.keras')
    acne_path1 = os.path.join(str(BASE_DIR), 'models', 'skin_classifier.pth')
    acne_path2 = os.path.join(str(BASE_DIR), 'models', 'skin_classifier.pt')

    info = {
        'timestamp': time.time(),
        'python_executable': sys.executable,
        'cwd': os.getcwd(),
        'base_dir': str(BASE_DIR),
        'venv_site_packages': next((p for p in sys.path if 'site-packages' in p), None),
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'tensorflow': None,
        'torch': None,
        'dno_model_path_checked': dno_path,
        'dno_model_exists': os.path.exists(dno_path),
        'dno_model_size_bytes': os.path.getsize(dno_path) if os.path.exists(dno_path) else None,
        'acne_model_paths': [acne_path1, acne_path2],
        'acne_model_exists': os.path.exists(acne_path1) or os.path.exists(acne_path2),
        'acne_model_size_bytes': None,
        'dno_loaded': bool(getattr(dno, 'model', None)),
        'acne_loaded': bool(getattr(acne, 'model', None)),
    }

    try:
        import tensorflow as _tf
        info['tensorflow'] = _tf.__version__
    except Exception:
        info['tensorflow'] = None
    try:
        import torch as _torch
        info['torch'] = _torch.__version__
    except Exception:
        info['torch'] = None

    # acne model size check
    if os.path.exists(acne_path1):
        info['acne_model_size_bytes'] = os.path.getsize(acne_path1)
    elif os.path.exists(acne_path2):
        info['acne_model_size_bytes'] = os.path.getsize(acne_path2)

    return jsonify(info)


@app.route('/result/<int:analysis_id>')
def result(analysis_id):
    # only owner can view
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, mode, dno_prediction, dno_confidence, acne_detected, acne_confidence, recommendation, image_path, created_at FROM analysis_history WHERE id = ?', (analysis_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        flash('Анализ не найден', 'danger')
        return redirect(url_for('index'))

    owner = row[1]
    current = session.get('auth_user') or request.cookies.get('user_id')
    if owner != current:
        flash('У вас нет доступа к этому анализу', 'danger')
        return redirect(url_for('index'))

    data = {
        'id': row[0], 'user_id': row[1], 'mode': row[2], 'dno_prediction': row[3], 'dno_confidence': row[4],
        'acne_detected': row[5], 'acne_confidence': row[6], 'recommendation': row[7], 'image_path': row[8], 'created_at': row[9]
    }
    return render_template('result.html', analysis=data)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOADS, filename)


if __name__ == '__main__':
    app.run(
        debug=CONFIG.get('flask', {}).get('debug', True),
        port=CONFIG.get('flask', {}).get('port', 5000)
    )
