# SkinSensorAI

SkinSensorAI helps you understand your skin with AI-powered photo analysis, skincare recommendations, and consultations in 11 languages.

## Features

- Skin-type classification and acne detection from a facial photograph.
- Accounts, analysis history, and consultations using GigaChat.
- Russian, English, Spanish, Simplified Chinese, Brazilian Portuguese, German, Korean, Kazakh, Ukrainian, French, and Italian interfaces.
- Responsive interface with light and dark themes.

## Local setup (Windows PowerShell)

Python 3.11 was used for local development.

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements_web.txt
Copy-Item config.example.json config.json
```

Edit your local `config.json`: set a random Flask secret key and your own GigaChat credentials. Alternatively, set the environment variables `FLASK_SECRET`, `GIGACHAT_CLIENT_ID`, and `GIGACHAT_CLIENT_SECRET`. Never commit credentials.

Add the model weights described in [models/README.md](models/README.md), then run:

```powershell
.\venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000` in your browser. The application creates its local SQLite database and upload directory automatically. Without model weights, predictions are unavailable. Consultations require valid GigaChat credentials.

## Tests

```powershell
.\venv\Scripts\python.exe -m unittest test_i18n -v
```

The localization tests use a temporary database and mocked ML models and GigaChat responses.

## Repository contents

`app.py` contains Flask routes; `models.py` loads classifiers; `i18n.py`, `translations.json`, and `locales/` provide translations. Templates and assets are in `templates/` and `static/`.

Local credentials, virtual environments, databases, uploaded photos, and model weights are excluded from Git. This repository contains the application source, not user data.

## Scope

SkinSensorAI provides skincare guidance and does not replace a dermatologist. Publishing this repository does not deploy the running application; production hosting requires a separate deployment configuration.

## GitHub Codespaces

Open this repository in Codespaces on the 2-core machine. Initial setup installs CPU-only PyTorch and TensorFlow, creates a private random Flask session secret, and starts Gunicorn on port 5000. Open the forwarded port from the Ports panel; it stays private by default.

Upload `best_model_dno.keras` and `skin_classifier.pth` into `models/` inside your Codespace (do not commit them). Set `GIGACHAT_CLIENT_ID` and `GIGACHAT_CLIENT_SECRET` as Codespaces secrets restricted to this repository, then restart the Codespace. Without these, the interface works but image predictions and consultations are unavailable respectively.

To start the server manually, run `bash .devcontainer/start.sh`. Check `.codespaces/server.log` for startup errors. To restart after adding models, stop and restart the Codespace.

Stop the Codespace when finished to conserve compute quota. Storage continues to count while it is stopped. This is a development/demo environment, not always-on hosting. Do not enable a paid plan to run this demo unless you intend to pay for it.

## PythonAnywhere

The deployment uses Python 3.11, preinstalled CPU PyTorch/torchvision, and `keras==3.14.0` with the PyTorch backend. This avoids installing TensorFlow in the free account's disk quota. Install Keras with `python3.11 -m pip install --user --no-cache-dir keras==3.14.0`; do not install the full local requirements into this account.

Clone the source into `~/SkinSensorAI` and upload both model weights into `models/`. Place the official GigaChat root CA certificate at `gigachat-ca.crt` (see [Sber's certificate instructions](https://developers.sber.ru/docs/ru/gigachat/certificates)). Keep GigaChat credentials in the private `config.json`, outside Git. An optional `gigachat-transfer.json` containing only `client_id` and `client_secret` is imported and removed by the setup script.

Run `python3.11 ~/SkinSensorAI/setup_pythonanywhere.py --check`. This backs up the existing default-domain WSGI file, configures one-thread inference and secure session cookies, creates a random session secret only if no config exists, and checks inference with a synthetic image. It preserves existing database contents. Set both the Web tab's source and working directories to `~/SkinSensorAI` (using the full absolute path), enable Force HTTPS, and click Reload.

The free web app requires monthly renewal using the Web tab's “Run until 1 month from today” button. Uploads and SQLite data remain private deployment files and must not be committed.

## Security protections

Account ownership comes exclusively from the signed login session. Legacy `user_id` cookies are ignored and removed; login/registration never reassign another user's data. Existing accounts, analyses and chats are preserved.

New images are stored in `instance/uploads/`. The owner-only `/result/<id>/image` endpoint also supports existing `static/uploads/` images. Public static serving uses an explicit asset allowlist. **Do not configure a web server or hosting static mapping for the entire `static/` directory**: that would bypass Flask's protections for legacy images. Map only public assets such as `static/css/` if needed. Back up the database and both upload directories together.

Every state-changing form and chat request requires a session-bound CSRF token. Logout is POST-only, post-login redirects accept local paths only, `/debug` is removed, debug mode is off, and responses set CSP, framing, MIME-sniffing and cache protections. HTTPS responses include HSTS. Local HTTP development works by default; enable `SKINSENSOR_HTTPS=1` behind HTTPS (the PythonAnywhere WSGI setup already enables secure session cookies).

The application replaces missing/short development session secrets with a persistent random key in `instance/session.key`. Preserve this private key across restarts and never commit it. GigaChat TLS verification is always enabled: use the official root certificate in `gigachat-ca.crt` or `GIGACHAT_CA_BUNDLE`.

Default limits in `security.py` are stored in SQLite and shared across workers/restarts:

- Login: 20 attempts per IP and 10 per IP/email pair per 15 minutes. Failed attempts from one IP do not lock the account on other IPs; users sharing an IP share its limits. Registration: 5 per IP per hour, 100 globally per day.
- Analysis: 10 per account and 30 per IP per hour, 100 globally per day.
- Chat: 10 per minute and 100 per day per account, 30 per IP per minute, 300 globally per day. Messages are capped at 4,000 characters.
- Image reads: 60 per account and 120 per IP per minute. Expensive work is serialized with an expiring SQLite lease.
- Request bodies: 8 MiB. Images: JPEG/PNG only, at most 16 million pixels, decoded and re-encoded without original metadata. Upload storage: 20 MiB per account and 200 MiB total, including legacy images.

Per-account and IP limits count rejected attempts to deter spam. Shared daily quotas are reserved only after input validation and admission to processing; rejected or busy requests do not consume them. An admitted operation still counts if downstream processing fails.

Limits use the server's `REMOTE_ADDR`; local runs ignore forwarded headers. The PythonAnywhere WSGI entrypoint trusts exactly the last `X-Forwarded-For` entry supplied by its load balancer, as documented by [PythonAnywhere](https://help.pythonanywhere.com/pages/WebAppClientIPAddresses). Earlier entries supplied by clients are ignored. Do not use that proxy configuration on a server directly exposed to clients. Application limits supplement, rather than replace, hosting-level traffic protection.

Run `python -m unittest test_security test_i18n -v`. Security tests use temporary databases, synthetic pictures and mocked ML/GigaChat; they do not touch real user data or make paid API calls.
