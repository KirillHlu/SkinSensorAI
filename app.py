import os
import uuid
import sqlite3
import base64
import json
import requests
import warnings
from contextlib import closing
from io import BytesIO
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, make_response, send_from_directory, flash, jsonify, session, abort, send_file
import time
from werkzeug.security import generate_password_hash, check_password_hash

from PIL import Image, UnidentifiedImageError
from security import init_security, session_secret, safe_next, reserve_global_quota
from models import DNOClassifier, AcneClassifier
from i18n import init_i18n, assistant_language_instruction, translate as t

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SKINSENSOR_DATA_DIR", BASE_DIR / "instance"))
UPLOADS = DATA_DIR / "uploads"
LEGACY_UPLOADS = BASE_DIR / "static" / "uploads"
DB_PATH = Path(os.environ.get("SKINSENSOR_DB_PATH", BASE_DIR / "web_skin_analysis.db"))
ALLOWED_EXT = {"png", "jpg", "jpeg"}

app = Flask(__name__, static_folder=None)
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
app.secret_key = session_secret(os.environ.get('FLASK_SECRET') or CONFIG.get('flask', {}).get('secret_key'), DATA_DIR)
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SKINSENSOR_HTTPS') == '1'

# ensure uploads exist
UPLOADS.mkdir(parents=True, exist_ok=True)

# Initialize classifiers (will log warnings if models missing)
dno = DNOClassifier(models_dir=str(BASE_DIR / 'models'))
acne = AcneClassifier(models_dir=str(BASE_DIR / 'models'))

# --- GigaChat / consultations setup ---

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
    if not ca_bundle and (BASE_DIR / 'gigachat-ca.crt').is_file():
        ca_bundle = str(BASE_DIR / 'gigachat-ca.crt')
    kwargs['verify'] = ca_bundle or True
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
init_security(app, DB_PATH)


@app.route('/')
def index():
    return render_template('index.html')


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
        if len(name or '') > 100 or len(prefs or '') > 4000 or (age and (not age.isdigit() or not 1 <= int(age) <= 120)):
            conn.close()
            abort(400)
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

        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(file.stream) as image:
                    if image.format not in ('JPEG', 'PNG') or image.width * image.height > app.config['MAX_IMAGE_PIXELS']:
                        abort(400, description='Invalid or oversized image.')
                    image.load()
                    # Re-encode only pixels: discard EXIF, extra frames and trailing payloads.
                    clean = Image.new('RGB', image.size)
                    clean.paste(image.convert('RGB'))
                    encoded = BytesIO()
                    clean.save(encoded, format='JPEG', quality=90)
            payload = encoded.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            abort(400, description='Invalid image. Upload a JPEG or PNG.')
        if len(payload) > app.config['MAX_CONTENT_LENGTH']:
            abort(413)
        if mode not in ('quick', 'detailed'):
            abort(400)
        check_upload_quota(user_id, len(payload))
        limited = reserve_global_quota()
        if limited is not None:
            return limited
        filename = uuid.uuid4().hex + '.jpg'
        save_path = UPLOADS / filename
        save_path.write_bytes(payload)

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
            'uploads/' + filename
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
    if not text or not text.strip():
        return jsonify({'ok': False, 'error': t('Empty message')}), 400
    if len(text) > 4000:
        return jsonify({'ok': False, 'error': 'Message is too long (maximum 4000 characters).'}), 400
    limited = reserve_global_quota()
    if limited is not None:
        return limited
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
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        name = request.form.get('name', '')
        if len(email) > 254 or '@' not in email or not 12 <= len(password) <= 128 or len(name) > 100:
            abort(400, description='Use a valid email, a 12-128 character password and a name up to 100 characters.')
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
        limited = reserve_global_quota()
        if limited is not None:
            conn.close()
            return limited
        user_id = email
        pw_hash = generate_password_hash(password)
        try:
            cursor.execute('INSERT INTO users (user_id, email, password_hash, name) VALUES (?, ?, ?, ?)', (user_id, email, pw_hash, name))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            abort(400, description='Account could not be created.')
        finally:
            conn.close()
        session.clear()
        session['auth_user'] = user_id
        flash('Регистрация успешна', 'success')
        resp = make_response(redirect(url_for('dashboard')))
        # clear anonymous cookie to avoid confusion
        resp.set_cookie('user_id', '', expires=0)
        return resp
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if len(email) > 254 or len(password) > 128:
            abort(400)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT password_hash FROM users WHERE email = ?', (email,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0] or not check_password_hash(row[0], password):
            flash('Неверный email или пароль', 'danger')
            return redirect(url_for('login'))
        session.clear()
        session['auth_user'] = email
        flash('Вы вошли в систему', 'success')
        next_url = safe_next(request.args.get('next'))
        resp = make_response(redirect(next_url or url_for('dashboard')))
        resp.set_cookie('user_id', '', expires=0)
        return resp
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
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


@app.route('/result/<int:analysis_id>')
@login_required
def result(analysis_id):
    # only owner can view
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, mode, dno_prediction, dno_confidence, acne_detected, acne_confidence, recommendation, image_path, created_at FROM analysis_history WHERE id = ? AND user_id = ?', (analysis_id, session['auth_user']))
    row = cursor.fetchone()
    conn.close()
    if not row:
        abort(404)

    data = {
        'id': row[0], 'user_id': row[1], 'mode': row[2], 'dno_prediction': row[3], 'dno_confidence': row[4],
        'acne_detected': row[5], 'acne_confidence': row[6], 'recommendation': row[7], 'image_path': row[8], 'created_at': row[9]
    }
    return render_template('result.html', analysis=data)

def resolve_image_path(stored):
    """Resolve only known upload locations, never arbitrary DB paths or symlinks."""
    # Older local Windows records used backslashes when saving relative paths.
    stored = stored.replace('\\', '/')
    roots = {'uploads/': UPLOADS, 'static/uploads/': LEGACY_UPLOADS}
    for prefix, directory in roots.items():
        if stored.startswith(prefix):
            name = stored[len(prefix):]
            if not name or '/' in name or '\\' in name or name in ('.', '..'):
                return None
            candidate = directory / name
            if candidate.is_symlink() or candidate.resolve().parent != directory.resolve():
                return None
            return candidate
    return None


def check_upload_quota(user_id, incoming):
    total = sum(p.stat().st_size for folder in (UPLOADS, LEGACY_UPLOADS)
                if folder.exists() for p in folder.iterdir() if p.is_file())
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute('SELECT image_path FROM analysis_history WHERE user_id = ?', (user_id,)).fetchall()
    owned = {resolve_image_path(row[0]) for row in rows if row[0]}
    user_total = sum(p.stat().st_size for p in owned if p and p.is_file())
    if total + incoming > app.config['MAX_UPLOAD_STORAGE'] or user_total + incoming > app.config['MAX_USER_UPLOAD_STORAGE']:
        abort(413, description='Image storage quota reached.')


@app.route('/result/<int:analysis_id>/image')
@login_required
def result_image(analysis_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute('SELECT image_path FROM analysis_history WHERE id = ? AND user_id = ?',
                           (analysis_id, session['auth_user'])).fetchone()
    path = resolve_image_path(row[0]) if row and row[0] else None
    if not path or not path.is_file():
        abort(404)
    # Old uploads might not have been validated. Serve only decoded image pixels.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ('PNG', 'JPEG') or image.width * image.height > app.config['MAX_IMAGE_PIXELS']:
                    abort(404)
                clean = Image.new('RGB', image.size)
                clean.paste(image.convert('RGB'))
                output = BytesIO()
                clean.save(output, format='JPEG', quality=90)
        output.seek(0)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        abort(404)
    return send_file(output, mimetype='image/jpeg', download_name='analysis.jpg', max_age=0)


@app.route('/static/<path:filename>', endpoint='static')
def public_asset(filename):
    # An explicit public asset list also blocks legacy photos and path traversal.
    if filename not in ('logo.png', 'logo.svg', 'css/style.css', 'skin_algorithm_slide.html'):
        abort(404)
    return send_from_directory(BASE_DIR / 'static', filename)


if __name__ == '__main__':
    app.run(
        debug=False,
        port=CONFIG.get('flask', {}).get('port', 5000)
    )
