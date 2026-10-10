"""Integration checks with a temporary database and stubbed ML models."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock
from i18n import LANGUAGES, CATALOGUES, EN, translate

ROOT = Path(__file__).resolve().parent

class LanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / 'test.db'
        connect = sqlite3.connect
        cls.db_patch = patch('sqlite3.connect', lambda *a, **k: connect(cls.database, **k))
        cls.db_patch.start()
        models = types.ModuleType('models')
        models.DNOClassifier = Mock(return_value=Mock(model=None, predict=Mock(return_value=None)))
        models.AcneClassifier = models.DNOClassifier
        spec = importlib.util.spec_from_file_location('skin_test_app', ROOT / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'models': models}):
            spec.loader.exec_module(cls.module)
        cls.module.app.config.update(TESTING=True, SECRET_KEY='test-key')
        with closing(sqlite3.connect(cls.database)) as conn, conn:
            conn.execute("INSERT INTO users(user_id,email,name) VALUES ('test','test@example.test','Alex')")
            conn.execute("INSERT INTO analysis_history(user_id,mode,dno_prediction,dno_confidence,acne_detected,acne_confidence,recommendation) VALUES ('test','quick','сухая кожа',0.9,0,0.8,?)", ('Тип кожи: сухая кожа (уверенность 0.90)\nНет рекомендаций (модели не загружены)',))

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.module.app.test_client()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("DELETE FROM messages")

    def test_all_pages_all_languages(self):
        for language in LANGUAGES:
            self.client.set_cookie('language', language)
            with self.client.session_transaction() as session:
                session['auth_user']='test'
            for path in ('/','/login','/register','/analyze','/profile','/dashboard','/consult','/result/1'):
                response=self.client.get(path)
                self.assertEqual(response.status_code,200,path)
                html=response.get_data(as_text=True)
                self.assertIn(f'<html lang="{language}">',html)
                # Native language names intentionally include several scripts.
                body_without_picker = re.sub(r'<select id="languageSelect".*?</select>', '', html, flags=re.S)
                if language not in ('ru', 'kk', 'uk'):
                    self.assertIsNone(re.search('[А-Яа-яЁё]', body_without_picker), (language, path))
                self.assertEqual(len(re.findall(r'<option value="(?:' + '|'.join(LANGUAGES) + r')" lang=', html)), len(LANGUAGES))
                self.assertRegex(html, rf'<option value="{language}" lang="{language}" selected>')
            self.assertNotIn('face-preview',self.client.get('/').get_data(as_text=True))

    def test_language_defaults_and_invalid_cookie(self):
        self.assertIn('<html lang="ru">',self.client.get('/').get_data(as_text=True))
        self.client.set_cookie('language','invalid')
        self.assertIn('<html lang="ru">',self.client.get('/').get_data(as_text=True))
        self.client.set_cookie('language','en')
        self.assertIn('SkinSensorAI',self.client.get('/').get_data(as_text=True))
        self.assertIn('Welcome back',self.client.get('/login').get_data(as_text=True))

    def test_errors_and_flash_messages(self):
        for language, catalogue in CATALOGUES.items():
            self.client = self.module.app.test_client()
            self.client.set_cookie('language',language)
            response=self.client.get('/analyze',follow_redirects=True)
            # Text may contain HTML-escaped apostrophes.
            import html
            self.assertIn(catalogue['Пожалуйста, войдите в систему чтобы продолжить'], html.unescape(response.get_data(as_text=True)))
            self.assertEqual(self.client.post('/consult/send').json['error'],catalogue['Unauthorized'])
            with self.client.session_transaction() as session:
                session['auth_user']='test'
            self.assertEqual(self.client.post('/consult/send',data={'message':''}).json['error'],catalogue['Empty message'])
            response=self.client.post('/analyze',follow_redirects=True)
            self.assertIn(catalogue['Пожалуйста, загрузите изображение'],html.unescape(response.get_data(as_text=True)))

    def test_catalogues_and_stored_recommendations(self):
        for language, catalogue in CATALOGUES.items():
            self.assertEqual(set(catalogue),set(EN),language)
            self.assertTrue(all(isinstance(value,str) and value.strip() for value in catalogue.values()))
            with self.module.app.test_request_context('/',headers={'Cookie':f'language={language}'}):
                self.assertEqual(translate('quick'),catalogue['Быстрый'])
                self.assertEqual(translate('detailed'),catalogue['Подробный'])
                source='Тип кожи: сухая кожа (уверенность 0.90)'
                expected=catalogue['Тип кожи:']+' '+catalogue['сухая кожа']+' '+catalogue['(уверенность']+' 0.90)'
                self.assertEqual(translate(source),expected)

    def test_each_assistant_language(self):
        with self.client.session_transaction() as session:
            session['auth_user']='test'
        for language, details in LANGUAGES.items():
            self.client.set_cookie('language',language)
            token=Mock(); token.json.return_value={'access_token':'test'}
            answer=Mock(); answer.json.return_value={'choices':[{'message':{'content':'Test reply'}}]}
            with patch.object(self.module,'GIGACHAT_CLIENT_ID','test'), patch.object(self.module,'GIGACHAT_CLIENT_SECRET','test'), patch.object(self.module,'gigachat_post',side_effect=[token,answer]) as post:
                self.assertEqual(self.client.post('/consult/send',data={'message':'Test'}).status_code,200)
                prompt = post.call_args.kwargs['json']['messages'][0]['content']
                self.assertIn('Respond in '+details['assistant'],prompt)
                self.assertIn(f"Website language: {details['assistant']} ({language})",prompt)
                self.assertNotIn('Отвечай по-русски',prompt)

    def test_assistant_language_and_preserved_history(self):
        self.client.set_cookie('language','en')
        with self.client.session_transaction() as session:
            session['auth_user']='test'
        token=Mock();token.json.return_value={'access_token':'test'}
        answer=Mock();answer.json.return_value={'choices':[{'message':{'content':'Your skin advice.'}}]}
        with patch.object(self.module,'GIGACHAT_CLIENT_ID','test'), patch.object(self.module,'GIGACHAT_CLIENT_SECRET','test'), patch.object(self.module,'gigachat_post',side_effect=[token,answer]) as post:
            response=self.client.post('/consult/send',data={'message':'Мой вопрос'})
            self.assertEqual(response.json['answer'],'Your skin advice.')
            self.assertIn('Respond in English',post.call_args.kwargs['json']['messages'][0]['content'])
        self.assertIn('Мой вопрос',self.client.get('/consult').get_data(as_text=True))
        self.client.set_cookie('language','ru')
        self.assertIn('Your skin advice.',self.client.get('/consult').get_data(as_text=True))

if __name__=='__main__':
    unittest.main()
