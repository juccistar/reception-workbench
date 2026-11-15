"""Synthetic old-cache white artwork; never enters the packaged backend."""
import base64
import hashlib
import io
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/reception/test_support'))
import synthetic_service
from reception_service.image_assets import decode_image
from reception_service.photos import photo_version
from reception_service.repository import encoded, now_ms

_original_factory = synthetic_service.create_synthetic_service


def create_image_window_service(*args, **kwargs):
    service = _original_factory(*args, **kwargs)
    image = Image.new('RGBA', (600, 320), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((35, 45, 565, 275), 24, outline=(255, 255, 255, 255), width=8)
    draw.text((65, 115), 'WHITE LOGO', fill=(255, 255, 255, 255), font=ImageFont.load_default(size=62))
    data = io.BytesIO()
    image.save(data, format='PNG')
    original = data.getvalue()
    asset = decode_image(original, 'image/png')
    # Reproduce the old persisted preview: both white art and transparency were
    # flattened onto white, while original bytes remain intact in the asset.
    old = io.BytesIO()
    Image.new('RGB', image.size, 'white').save(old, format='JPEG')
    asset['preview_data_url'] = 'data:image/jpeg;base64,' + base64.b64encode(old.getvalue()).decode('ascii')
    with service._lock, service.repo.transaction():
        conversation = service._require('conversation', 'demo-emma')
        message = {'message_id': 'transparent-white-artwork-041', 'conversation_id': 'demo-emma',
                   'seller_id': service.seller_id, 'buyer_id': conversation['buyer_id'], 'role': 'buyer',
                   'own': False, 'text': '[image]', 'attachments': [], 'timestamp_ms': now_ms()}
        service._ingest(conversation, [message], has_more=False, full=False)
        service.repo.put('image_asset', asset['sha256'], asset)
        state = {key: message[key] for key in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')}
        state.update(status='ready', source_version=photo_version(message), sha256=asset['sha256'])
        service.repo.put('message_image', encoded(['demo-emma', message['message_id']]), state)
    original_call = service.call
    def call(method, params=None):
        if method == '_fixture_image_integrity':
            row = service.repo.get('image_asset', asset['sha256'])
            return {'sha256': hashlib.sha256(base64.b64decode(row['original_b64'])).hexdigest(),
                    'original_unchanged': row['original_b64'] == asset['original_b64'],
                    'persisted_preview_still_legacy': row['preview_data_url'] == asset['preview_data_url']}
        return original_call(method, params or {})
    service.call = call
    return service


synthetic_service.create_synthetic_service = create_image_window_service
synthetic_service.main()
