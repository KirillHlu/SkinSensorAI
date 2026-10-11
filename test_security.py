"""Security regressions using only temporary files, fake users and mocked ML/API."""
from contextlib import closing
from io import BytesIO
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from flask import Flask
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import generate_password_hash
from security import DEFAULT_LIMITS, safe_next, session_secret

ROOT = Path(__file__).resolve().parent
PASSWORD = 'test-password-only-123'


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash(PASSWORD)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'test.db'
        models = types.ModuleType('models')
        models.DNOClassifier = Mock(return_value=Mock(model=None, predict=Mock(return_value=None)))
        models.AcneClassifier = Mock(return_value=Mock(model=None, predict=Mock(return_value=None)))
        spec = importlib.util.spec_from_file_location('security_test_app', ROOT / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'models': models}), patch.dict(os.environ, {
            'SKINSENSOR_DATA_DIR': str(self.root / 'private'),
            'SKINSENSOR_DB_PATH': str(self.db),
            'FLASK_SECRET': 'test-key-' * 8,
        }):
            spec.loader.exec_module(self.module)
        self.app = self.module.app
        self.app.config.update(TESTING=True, RATE_LIMITS={})
        self.module.LEGACY_UPLOADS = self.root / 'legacy'
        self.module.LEGACY_UPLOADS.mkdir()
        Image.new('RGB', (8, 8)).save(self.module.UPLOADS / 'owner.jpg')
        Image.new('RGB', (8, 8)).save(self.module.LEGACY_UPLOADS / 'legacy.jpg')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for user in ('owner@example.invalid', 'other@example.invalid'):
                conn.execute('INSERT INTO users(user_id,email,password_hash) VALUES (?,?,?)', (user,user,self.password_hash))
            conn.execute("INSERT INTO messages(user_id,role,content) VALUES ('owner@example.invalid','user','PRIVATE SYNTHETIC MESSAGE')")
            for image in ('uploads/owner.jpg', 'static/uploads/legacy.jpg'):
                conn.execute("INSERT INTO analysis_history(user_id,mode,image_path) VALUES ('owner@example.invalid','quick',?)", (image,))
        self.client = self.app.test_client()

    def authenticate(self, client=None, user='owner@example.invalid'):
        with (client or self.client).session_transaction() as session:
            session['auth_user'] = user

    def post(self, path, data=None, client=None, **kwargs):
        client = client or self.client
        with client.session_transaction() as session:
            session.setdefault('_csrf', 'test-csrf-token')
            token = session['_csrf']
        headers = kwargs.pop('headers', {})
        headers['X-CSRF-Token'] = token
        return client.post(path, data=data, headers=headers, **kwargs)

    def query(self, sql):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql).fetchall()

    def photo(self, tail=b''):
        output = BytesIO()
        Image.new('RGB', (8, 8)).save(output, format='PNG')
        return BytesIO(output.getvalue() + tail), 'photo.png'

    def test_forged_cookie_cannot_read_results_or_history(self):
        self.client.set_cookie('user_id', 'owner@example.invalid')
        self.assertEqual(self.client.get('/result/1').status_code, 302)
        self.client.set_cookie('user_id', 'owner@example.invalid')
        self.assertEqual(self.client.get('/consult/history').status_code, 401)
        self.authenticate(user='other@example.invalid')
        self.assertEqual(self.client.get('/result/1').status_code, 404)
        self.assertEqual(self.client.get('/result/99999').status_code, 404)
        self.authenticate()
        self.assertEqual(self.client.get('/result/1').status_code, 200)

    def test_old_development_secret_cannot_forge_a_session(self):
        weak_app = Flask('weak_test_app')
        weak_app.secret_key = 'dev-secret'
        cookie = SecureCookieSessionInterface().get_signing_serializer(weak_app).dumps({'auth_user':'owner@example.invalid'})
        self.client.set_cookie('session', cookie)
        self.assertEqual(self.client.get('/result/1').status_code, 302)

    def test_login_does_not_move_another_users_data(self):
        self.client.set_cookie('user_id', 'owner@example.invalid')
        response = self.post('/login', {'email':'other@example.invalid', 'password':PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.query('SELECT user_id FROM messages'), [('owner@example.invalid',)])
        self.assertEqual(set(self.query('SELECT user_id FROM analysis_history')), {('owner@example.invalid',)})
        self.assertEqual(self.client.get('/consult/history').json['history'], [])

    def test_registration_does_not_move_another_users_data(self):
        self.client.set_cookie('user_id', 'owner@example.invalid')
        response = self.post('/register', {'email':'new@example.invalid','password':PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.query('SELECT user_id FROM messages'), [('owner@example.invalid',)])
        self.assertEqual(set(self.query('SELECT user_id FROM analysis_history')), {('owner@example.invalid',)})

    def test_photos_are_owner_only_including_legacy_files(self):
        for path in ('/result/1/image', '/result/2/image'):
            self.assertEqual(self.client.get(path).status_code, 302)
        self.authenticate(user='other@example.invalid')
        for path in ('/result/1/image', '/result/2/image'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.authenticate()
        for path in ('/result/1/image', '/result/2/image'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/jpeg')
            self.assertIn('no-store', response.headers['Cache-Control'])
        for path in ('/uploads/owner.jpg','/static/uploads/legacy.jpg',
                     '/static/css/../uploads/legacy.jpg','/static/%75ploads/legacy.jpg',
                     '/instance/uploads/owner.jpg','/static/../config.json'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        html = self.client.get('/result/2').get_data(as_text=True)
        self.assertIn('/result/2/image', html)
        self.assertNotIn('/static/uploads/', html)

    def test_path_resolution_rejects_traversal(self):
        for path in ('/etc/passwd','uploads/../../config.json','uploads/..\\config.json',
                     'static/uploads/../config.json','uploads/'):
            self.assertIsNone(self.module.resolve_image_path(path), path)

    def test_old_windows_image_paths_still_work_for_the_owner(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE analysis_history SET image_path = ? WHERE id = 2', ('static\\uploads\\legacy.jpg',))
        self.authenticate()
        self.assertEqual(self.client.get('/result/2/image').status_code, 200)
        self.authenticate(user='other@example.invalid')
        self.assertEqual(self.client.get('/result/2/image').status_code, 404)

    def test_fake_image_rejected_before_storage_or_inference(self):
        self.authenticate()
        before = set(self.module.UPLOADS.iterdir())
        response = self.post('/analyze', {'image':(BytesIO(b'<html>not a photo</html>'),'fake.jpg')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(self.module.UPLOADS.iterdir()), before)
        self.module.dno.predict.assert_not_called()

    def test_valid_upload_is_private_and_strips_trailing_content(self):
        self.authenticate()
        response = self.post('/analyze', {'image':self.photo(b'SECRET TRAILING PAYLOAD'),'mode':'quick'})
        self.assertEqual(response.status_code, 302)
        stored = self.query('SELECT image_path FROM analysis_history ORDER BY id DESC LIMIT 1')[0][0]
        path = self.module.resolve_image_path(stored)
        self.assertEqual(path.parent, self.module.UPLOADS)
        self.assertNotIn(b'SECRET TRAILING PAYLOAD', path.read_bytes())
        with Image.open(path) as image:
            self.assertEqual(image.format, 'JPEG')
        self.assertEqual(self.client.get(response.location + '/image').status_code, 200)

    def test_request_and_image_dimension_limits(self):
        self.authenticate()
        self.app.config['MAX_CONTENT_LENGTH'] = 1024
        self.assertEqual(self.post('/analyze', {'image':(BytesIO(b'x' * 2048),'large.jpg')}).status_code, 413)
        self.app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
        self.app.config['MAX_IMAGE_PIXELS'] = 10
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 400)
        self.module.dno.predict.assert_not_called()

    def test_storage_quotas_reject_before_saving(self):
        self.authenticate()
        self.app.config['MAX_USER_UPLOAD_STORAGE'] = 1
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 413)
        self.app.config['MAX_USER_UPLOAD_STORAGE'] = 999999
        self.app.config['MAX_UPLOAD_STORAGE'] = 1
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 413)
        self.assertEqual(len(list(self.module.UPLOADS.iterdir())), 1)

    def test_csrf_required_on_every_mutating_route(self):
        self.authenticate()
        for path in ('/login','/register','/profile','/analyze','/consult/send','/logout'):
            self.assertEqual(self.client.post(path, data={'message':'test'}).status_code, 400, path)
        self.assertEqual(len(self.query('SELECT * FROM messages')), 1)
        html = self.client.get('/profile').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
        self.assertEqual(self.client.post('/profile', data={'csrf_token':token,'name':'updated'}).status_code, 302)

    def test_csrf_rotated_at_login_and_logout_is_post_only(self):
        self.client.get('/login')
        with self.client.session_transaction() as session:
            old_token = session['_csrf']
        self.post('/login', {'email':'owner@example.invalid','password':PASSWORD})
        self.assertEqual(self.client.post('/logout', data={'csrf_token':old_token}).status_code, 400)
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.post('/logout').status_code, 302)
        self.assertEqual(self.client.get('/dashboard').status_code, 302)

    def test_open_redirect_variations_blocked(self):
        for target in ('https://example.invalid','//example.invalid','/\\example.invalid',
                       '/%2fexample.invalid','/%252fexample.invalid','/\r\nLocation: x','javascript:alert(1)'):
            self.assertIsNone(safe_next(target), target)
        self.assertEqual(safe_next('/dashboard?tab=history'), '/dashboard?tab=history')
        response = self.post('/login?next=https://example.invalid', {'email':'owner@example.invalid','password':PASSWORD})
        self.assertEqual(response.location, '/dashboard')

    def test_ip_rate_limit_cannot_be_reset_by_cookie_or_forwarded_header(self):
        self.app.config['RATE_LIMITS'] = {'login':[('ip',1,60)]}
        self.assertEqual(self.post('/login', {'email':'missing','password':'wrong'}).status_code, 302)
        other = self.app.test_client()
        response = self.post('/login', {'email':'another','password':'wrong'}, client=other, headers={'X-Forwarded-For':'203.0.113.3'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)

    def test_ip_email_rate_limit_normalizes_email_and_expires(self):
        self.app.config['RATE_LIMITS'] = {'login':[('ip_email',1,60)]}
        with patch('security.time.time', return_value=1000):
            self.post('/login', {'email':'owner@example.invalid','password':'wrong'})
            response = self.post('/login', {'email':' OWNER@example.invalid ','password':'wrong'})
            self.assertEqual(response.status_code, 429)
        with patch('security.time.time', return_value=1061):
            self.assertEqual(self.post('/login', {'email':'owner@example.invalid','password':'wrong'}).status_code, 302)

    def test_failed_logins_cannot_lock_out_owner_on_another_ip(self):
        self.app.config['RATE_LIMITS'] = DEFAULT_LIMITS
        attacker = self.app.test_client()
        for _ in range(10):
            response = self.post('/login', {'email':'owner@example.invalid','password':'wrong'},
                                 client=attacker, environ_overrides={'REMOTE_ADDR':'192.0.2.10'})
            self.assertEqual(response.status_code, 302)
        blocked = self.post('/login', {'email':'owner@example.invalid','password':'wrong'},
                            client=self.app.test_client(),
                            headers={'X-Forwarded-For':'192.0.2.20'},
                            environ_overrides={'REMOTE_ADDR':'192.0.2.10'})
        self.assertEqual(blocked.status_code, 429)
        victim = self.app.test_client()
        response = self.post('/login', {'email':'owner@example.invalid','password':PASSWORD},
                             client=victim, environ_overrides={'REMOTE_ADDR':'192.0.2.20'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/dashboard')
        with victim.session_transaction() as session:
            self.assertEqual(session['auth_user'], 'owner@example.invalid')

    def test_consultation_rate_limit_stops_extra_api_calls(self):
        self.authenticate()
        self.app.config['RATE_LIMITS'] = {'consult_send':[('user',1,60)]}
        with patch.object(self.module,'ask_gigachat',return_value='synthetic response') as api:
            self.assertEqual(self.post('/consult/send', {'message':'hello'}).status_code, 200)
            limited = self.post('/consult/send', {'message':'hello'})
            self.assertEqual(limited.status_code, 429)
            self.assertFalse(limited.json['ok'])
            self.assertEqual(api.call_count, 1)

    def test_global_limit_spans_accounts(self):
        self.app.config['RATE_LIMITS'] = {'consult_send':[('global',1,60)]}
        with patch.object(self.module,'ask_gigachat',return_value='synthetic response') as api:
            self.authenticate()
            self.post('/consult/send', {'message':'hello'})
            self.authenticate(user='other@example.invalid')
            self.assertEqual(self.post('/consult/send', {'message':'hello'}, environ_overrides={'REMOTE_ADDR':'203.0.113.3'}).status_code, 429)
            self.assertEqual(api.call_count, 1)

    def test_300_rejected_messages_do_not_exhaust_shared_daily_quota(self):
        self.app.config['RATE_LIMITS'] = DEFAULT_LIMITS
        clients = [self.app.test_client() for _ in range(3)]
        for i, client in enumerate(clients):
            self.authenticate(client, user=f'audit-{i}@example.invalid')
        started = time.time()
        with patch.object(self.module, 'ask_gigachat', return_value='test reply') as api:
            for minute in range(10):
                with patch('security.time.time', return_value=started + minute * 61):
                    for client in clients:
                        for _ in range(10):
                            self.assertEqual(self.post('/consult/send', {'message':''}, client=client).status_code, 400)
            api.assert_not_called()
            with patch('security.time.time', return_value=started + 610):
                fresh = self.app.test_client()
                self.authenticate(fresh, user='other@example.invalid')
                response = self.post('/consult/send', {'message':'A normal question'}, client=fresh,
                                     environ_overrides={'REMOTE_ADDR':'203.0.113.99'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(api.call_count, 1)

    def test_invalid_messages_still_consume_personal_spam_limit(self):
        self.authenticate()
        self.app.config['RATE_LIMITS'] = {'consult_send':[('user',1,60),('global',1,86400)]}
        self.assertEqual(self.post('/consult/send', {'message':''}).status_code, 400)
        self.assertEqual(self.post('/consult/send', {'message':''}).status_code, 429)
        self.authenticate(user='other@example.invalid')
        with patch.object(self.module, 'ask_gigachat', return_value='reply'):
            self.assertEqual(self.post('/consult/send', {'message':'normal'}).status_code, 200)

    def test_invalid_or_busy_chat_requests_preserve_global_capacity(self):
        self.authenticate()
        self.app.config['RATE_LIMITS'] = {'consult_send':[('global',1,86400)]}
        with patch.object(self.module, 'ask_gigachat', return_value='reply') as api:
            for text in ('', '   ', 'x'*4001):
                self.assertEqual(self.post('/consult/send', {'message':text}).status_code, 400)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("INSERT INTO security_leases VALUES ('heavy','busy',9999999999)")
            self.assertEqual(self.post('/consult/send', {'message':'normal'}).status_code, 429)
            api.assert_not_called()
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('DELETE FROM security_leases')
            self.assertEqual(self.post('/consult/send', {'message':'normal'}).status_code, 200)
            self.assertEqual(self.post('/consult/send', {'message':'normal'}).status_code, 429)
            self.assertEqual(api.call_count, 1)

    def test_rejected_upload_does_not_consume_global_analysis_quota(self):
        self.authenticate()
        self.app.config['RATE_LIMITS'] = {'analyze':[('global',1,86400)]}
        self.assertEqual(self.post('/analyze', {'image':(BytesIO(b'invalid'),'bad.jpg')}).status_code, 400)
        self.app.config['MAX_USER_UPLOAD_STORAGE'] = 1
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 413)
        self.app.config['MAX_USER_UPLOAD_STORAGE'] = 20*1024*1024
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 302)
        self.assertEqual(self.post('/analyze', {'image':self.photo()}).status_code, 429)
        self.assertEqual(self.module.dno.predict.call_count, 1)

    def test_rejected_registration_does_not_consume_global_quota(self):
        self.app.config['RATE_LIMITS'] = {'register':[('global',1,86400)]}
        self.assertEqual(self.post('/register', {'email':'bad','password':'x'}).status_code, 400)
        self.post('/register', {'email':'owner@example.invalid','password':PASSWORD})
        self.assertEqual(self.post('/register', {'email':'new@example.invalid','password':PASSWORD}).status_code, 302)
        self.assertEqual(self.post('/register', {'email':'second@example.invalid','password':PASSWORD}).status_code, 429)
        self.assertEqual(self.query("SELECT count(*) FROM users WHERE email='second@example.invalid'"), [(0,)])

    def test_repeated_image_decoding_is_rate_limited(self):
        self.authenticate()
        self.app.config['RATE_LIMITS'] = {'result_image':[('user',1,60)]}
        self.assertEqual(self.client.get('/result/1/image').status_code, 200)
        self.assertEqual(self.client.get('/result/1/image').status_code, 429)

    def test_long_messages_do_not_reach_api_or_database(self):
        self.authenticate()
        with patch.object(self.module,'ask_gigachat') as api:
            self.assertEqual(self.post('/consult/send', {'message':'x'*4001}).status_code, 400)
            api.assert_not_called()
        self.assertEqual(len(self.query('SELECT * FROM messages')), 1)

    def test_concurrent_work_lease_and_error_cleanup(self):
        self.authenticate()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO security_leases VALUES ('heavy','other-request',9999999999)")
        self.assertEqual(self.post('/consult/send', {'message':'hello'}).status_code, 429)
        self.assertEqual(self.query('SELECT token FROM security_leases'), [('other-request',)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE security_leases SET expires = 0')
        with patch.object(self.module,'ask_gigachat',side_effect=RuntimeError('synthetic error')):
            with self.assertRaises(RuntimeError):
                self.post('/consult/send', {'message':'hello'})
        self.assertEqual(self.query('SELECT * FROM security_leases'), [])

    def test_debug_and_private_files_are_not_public(self):
        for path in ('/debug','/config.json','/web_skin_analysis.db','/.git/config','/instance/session.key'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        with self.client.get('/static/css/style.css') as response:
            self.assertEqual(response.status_code, 200)

    def test_headers_and_session_cookie(self):
        self.app.config['SESSION_COOKIE_SECURE'] = True
        response = self.client.get('/login', base_url='https://localhost')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('max-age=', response.headers['Strict-Transport-Security'])
        nonce = re.search(r"'nonce-([^']+)'",response.headers['Content-Security-Policy'])[1]
        self.assertIn(f'<script nonce="{nonce}">', response.get_data(as_text=True))
        cookie = response.headers['Set-Cookie']
        for flag in ('Secure','HttpOnly','SameSite=Lax'):
            self.assertIn(flag, cookie)

    def test_gigachat_certificate_verification_cannot_be_disabled(self):
        with patch.dict(os.environ, {'GIGACHAT_CA_BUNDLE':'test-ca.crt'}), patch.object(self.module.requests,'post') as post:
            self.module.gigachat_post('https://example.invalid',verify=False)
            self.assertEqual(post.call_args.kwargs['verify'], 'test-ca.crt')
        with patch.dict(os.environ, {'GIGACHAT_CA_BUNDLE':''}), patch.object(self.module,'BASE_DIR',self.root), patch.object(self.module.requests,'post') as post:
            self.module.gigachat_post('https://example.invalid',verify=False)
            self.assertIs(post.call_args.kwargs['verify'], True)

    def test_default_secret_is_random_persistent_and_private(self):
        directory = self.root/'keys'
        secret = session_secret('dev-secret',directory)
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(secret, session_secret(None,directory))

    def test_existing_account_not_overwritten_and_weak_password_rejected(self):
        self.assertEqual(self.post('/register', {'email':'new@example.invalid','password':'x'}).status_code, 400)
        self.post('/register', {'email':'owner@example.invalid','password':PASSWORD+'different'})
        self.assertEqual(self.query("SELECT password_hash FROM users WHERE user_id='owner@example.invalid'")[0][0], self.password_hash)


if __name__ == '__main__':
    unittest.main()
