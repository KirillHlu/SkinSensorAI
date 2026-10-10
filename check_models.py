from models import DNOClassifier, AcneClassifier
import os

print('Init DNO...')
d = DNOClassifier(models_dir='models')
print('DNO model loaded:', bool(d.model))
print('Init Acne...')
a = AcneClassifier(models_dir='models')
print('Acne model loaded:', bool(a.model))

# Try a quick predict for acne model if there is an upload
if a.model:
    uploads = os.path.join(os.path.dirname(__file__), 'static','uploads')
    files = [f for f in os.listdir(uploads) if f.lower().endswith(('png','jpg','jpeg'))]
    if files:
        img = os.path.join(uploads, files[0])
        print('Testing acne.predict on', img)
        res = a.predict(img)
        print('Predict result:', res)
    else:
        print('No images to test in uploads')
