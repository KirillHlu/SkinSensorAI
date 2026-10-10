"""Server-rendered interface translations with an explicit language allowlist."""
import json
import re
from pathlib import Path
from flask import request

LANGUAGES = {
    'ru': {'name': 'Русский', 'assistant': 'Russian'},
    'en': {'name': 'English', 'assistant': 'English'},
    'es': {'name': 'Español', 'assistant': 'Spanish'},
    'zh': {'name': '中文（简体）', 'assistant': 'Simplified Chinese'},
    'pt': {'name': 'Português (Brasil)', 'assistant': 'Brazilian Portuguese'},
    'de': {'name': 'Deutsch', 'assistant': 'German'},
    'ko': {'name': '한국어', 'assistant': 'Korean'},
    'kk': {'name': 'Қазақша', 'assistant': 'Kazakh'},
    'uk': {'name': 'Українська', 'assistant': 'Ukrainian'},
    'fr': {'name': 'Français', 'assistant': 'French'},
    'it': {'name': 'Italiano', 'assistant': 'Italian'},
}
ROOT = Path(__file__).resolve().parent
EN = json.loads((ROOT / 'translations.json').read_text(encoding='utf-8'))
CATALOGUES = {'en': EN}
for code in LANGUAGES:
    if code not in ('ru', 'en'):
        CATALOGUES[code] = json.loads((ROOT / 'locales' / f'{code}.json').read_text(encoding='utf-8'))
# Replace source fragments in a single pass: translated Kazakh or Ukrainian text
# must never be processed again as though it were Russian source text.
FRAGMENTS = re.compile('|'.join(re.escape(key) for key in sorted(EN, key=len, reverse=True)))
MODES = {'quick': 'Быстрый', 'detailed': 'Подробный'}


def current_language():
    language = request.cookies.get('language', 'ru')
    return language if language in LANGUAGES else 'ru'


def assistant_language_instruction():
    code = current_language()
    name = LANGUAGES[code]['assistant']
    return (f'\n\nWebsite language: {name} ({code}). Respond in {name}. '
            'Write the entire consultation, including headings and recommendations, in this language. '
            'This overrides any earlier language instruction. '
            'Keep this language even if the question or older conversation messages use another language.')


def translate(value):
    if value is None:
        return ''
    value = MODES.get(str(value), str(value))
    language = current_language()
    if language == 'ru':
        return {'Unauthorized': 'Пожалуйста, войдите в систему',
                'Empty message': 'Введите сообщение'}.get(value, value)
    catalogue = CATALOGUES[language]
    if value in catalogue:
        return catalogue[value]
    # Fixed model recommendations may be stored alongside confidence numbers.
    # User-written messages and profile fields are intentionally not passed here.
    return FRAGMENTS.sub(lambda match: catalogue.get(match[0], EN[match[0]]), value)


def init_i18n(app):
    @app.context_processor
    def language_context():
        return {'t': translate, 'language': current_language(), 'languages': LANGUAGES}
