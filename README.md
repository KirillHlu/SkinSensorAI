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
