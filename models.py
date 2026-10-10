import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Try to import heavy libs, but allow graceful degradation
try:
    import tensorflow as tf
    from PIL import Image
    import numpy as np
except Exception:
    tf = None
    Image = None
    np = None

try:
    import torch
    import torchvision.transforms as transforms
    from torchvision import models
    import cv2
except Exception:
    torch = None
    transforms = None
    models = None
    cv2 = None


class DNOClassifier:
    def __init__(self, models_dir=None):
        self.model = None
        self.models_dir = models_dir or os.path.join(os.path.dirname(__file__), 'models')
        self.IMG_SIZE = (224, 224)
        self.DNO_CLASSES = {0: 'нормальная кожа', 1: 'жирная кожа', 2: 'сухая кожа'}
        self._load_model()

    def _load_model(self):
        if tf is None:
            logger.warning('TensorFlow не доступен — DNO модель не будет загружена')
            return
        dno_path = os.path.join(self.models_dir, 'best_model_dno.keras')
        if os.path.exists(dno_path):
            try:
                self.model = tf.keras.models.load_model(dno_path)
                logger.info('DNO model loaded')
            except Exception as e:
                logger.error(f'Ошибка загрузки DNO модели: {e}')
        else:
            logger.warning(f'DNO model not found at {dno_path}')

    def predict(self, image_path):
        if self.model is None or tf is None:
            return None
        try:
            img = tf.keras.preprocessing.image.load_img(image_path, target_size=self.IMG_SIZE)
            img_array = tf.keras.preprocessing.image.img_to_array(img)
            img_batch = np.expand_dims(img_array, axis=0)
            img_batch = tf.keras.applications.mobilenet_v2.preprocess_input(img_batch)
            pred = self.model.predict(img_batch, verbose=0)[0]
            class_idx = int(pred.argmax())
            return {'prediction': self.DNO_CLASSES.get(class_idx, 'неизвестно'), 'confidence': float(pred[class_idx])}
        except Exception as e:
            logger.error(f'DNO predict error: {e}')
            return None


class AcneClassifier:
    def __init__(self, models_dir=None):
        self.model = None
        self.device = None
        self.classes = ["Acne", "normal"]
        self.models_dir = models_dir or os.path.join(os.path.dirname(__file__), 'models')
        self.transform = None
        self.recommendations = {
            "Acne": [
                "🔹 Используйте средства с салициловой кислотой или бензоил пероксидом",
                "🔹 Не выдавливайте прыщи - это может привести к рубцам",
                "🔹 Регулярно меняйте наволочку (каждые 3-4 дня)",
            ],
            "normal": [
                "✅ Продолжайте ухаживать за кожей в том же режиме",
                "✅ Используйте увлажняющие средства с SPF защитой",
            ]
        }
        self._load_model()

    def _load_model(self):
        if torch is None:
            logger.warning('PyTorch не доступен — Acne модель не будет загружена')
            return
        possible = [
            os.path.join(self.models_dir, 'skin_classifier.pth'),
            os.path.join(self.models_dir, 'skin_classifier.pt'),
        ]
        model_path = None
        for p in possible:
            if os.path.exists(p):
                model_path = p
                break
        if model_path is None:
            logger.warning('PyTorch модель не найдена в models_dir')
            return
        try:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            self.model = models.resnet50(weights=None)
            num_features = self.model.fc.in_features
            self.model.fc = torch.nn.Sequential(
                torch.nn.Dropout(0.2),
                torch.nn.Linear(num_features, 512),
                torch.nn.ReLU(),
                torch.nn.Dropout(0.2),
                torch.nn.Linear(512, len(self.classes))
            )
            state = torch.load(model_path, map_location=self.device)
            self.model.load_state_dict(state)
            self.model = self.model.to(self.device)
            self.model.eval()
            self.transform = transforms.Compose([
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
            logger.info('Acne model loaded')
        except Exception as e:
            logger.error(f'Ошибка загрузки Acne модели: {e}')
            self.model = None

    def predict(self, image_path, confidence_threshold=0.7):
        if self.model is None or torch is None:
            return None
        try:
            # Prefer PIL for reading files (better Unicode/path handling on Windows)
            img_pil = None
            try:
                if Image is not None:
                    img_pil = Image.open(image_path).convert('RGB')
            except Exception:
                img_pil = None

            if img_pil is None and cv2 is not None:
                img_cv = cv2.imread(image_path)
                if img_cv is None:
                    return None
                img_cv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
                from PIL import Image as PILImage
                img_pil = PILImage.fromarray(img_cv)

            if img_pil is None:
                return None

            tensor = self.transform(img_pil).unsqueeze(0).to(self.device)
            with torch.no_grad():
                outputs = self.model(tensor)
                probs = torch.softmax(outputs, dim=1)
                confidence, predicted = torch.max(probs, 1)
            class_idx = int(predicted.item())
            predicted_class = self.classes[class_idx]
            confidence_value = float(confidence.item())
            return {
                'predicted_class': predicted_class,
                'confidence': confidence_value,
                'is_confident': confidence_value >= confidence_threshold,
                'recommendations': self.recommendations.get(predicted_class, [])
            }
        except Exception as e:
            logger.error(f'Acne predict error: {e}')
            return None
