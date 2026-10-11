"""Configure the existing PythonAnywhere web app; preserve existing data."""
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

root = Path(__file__).resolve().parent
config_path = root / 'config.json'
if not config_path.exists():
    with config_path.open('x', encoding='utf-8') as stream:
        json.dump({'flask': {'secret_key': secrets.token_hex(32), 'debug': False}}, stream)
    config_path.chmod(0o600)

transfer = root / 'gigachat-transfer.json'
if transfer.exists():
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['gigachat'] = json.loads(transfer.read_text(encoding='utf-8-sig'))
    config_path.write_text(json.dumps(config), encoding='utf-8')
    config_path.chmod(0o600)
    transfer.unlink()
    print('GigaChat credentials imported without displaying them.', flush=True)

wsgi_path = Path('/var/www') / f'{getpass.getuser().lower()}_pythonanywhere_com_wsgi.py'
backup = wsgi_path.with_suffix('.py.before_skinsensor')
if wsgi_path.exists() and not backup.exists():
    shutil.copy2(wsgi_path, backup)
wsgi = f'''import os
import sys
os.environ['KERAS_BACKEND'] = 'torch'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['GIGACHAT_CA_BUNDLE'] = {str(root / 'gigachat-ca.crt')!r}
sys.path.insert(0, {str(root)!r})
import torch
torch.set_num_threads(1)
from app import app as application
from werkzeug.middleware.proxy_fix import ProxyFix
# PythonAnywhere guarantees only the last X-Forwarded-For entry.
# Enable this only in the hosted WSGI entrypoint, never for direct local access.
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=0, x_host=0, x_port=0, x_prefix=0)
application.config.update(DEBUG=False, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
'''
wsgi_path.write_text(wsgi, encoding='utf-8')
print('WSGI configured:', wsgi_path, flush=True)

if '--check' in sys.argv:
    exec(compile(wsgi, str(wsgi_path), 'exec'))
    import tempfile
    from PIL import Image
    from app import dno, acne
    print('Models loaded:', dno.model is not None, acne.model is not None, flush=True)
    with tempfile.TemporaryDirectory() as temp:
        test_image = str(Path(temp) / 'synthetic.png')
        Image.new('RGB', (224, 224), (128, 128, 128)).save(test_image)
        print('Synthetic inference:', dno.predict(test_image) is not None, acne.predict(test_image) is not None, flush=True)
    client = application.test_client()
    print('HTTP smoke tests:', {path: client.get(path).status_code for path in ['/', '/login', '/register', '/status']}, flush=True)
