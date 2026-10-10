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
