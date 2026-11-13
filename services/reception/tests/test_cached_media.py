import base64
import hashlib
import io
import threading
import unittest

from PIL import Image

from reception_service.cached_media import CachedMediaMixin
from reception_service.errors import ServiceError


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get(self, kind, key, default=None):
        return self.rows.get((kind, key), default)


class Host(CachedMediaMixin):
    account_verified = True
    seller_id = 'seller'
    _closing = False
    _lock = threading.RLock()


class CachedMediaTests(unittest.TestCase):
    def setUp(self):
        out = io.BytesIO()
        Image.new('RGBA', (4, 3), (0, 0, 0, 0)).save(out, 'PNG')
        self.data = out.getvalue()
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.ref = {'kind': 'supplement', 'conversation_id': 'c', 'attachment_id': 'a', 'sha256': self.sha}
        self.attachment = {'id': 'a', 'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'c',
                           'status': 'ready', 'origin': 'business_supplement', 'sha256': self.sha,
                           'size': len(self.data), 'media_type': 'image/png', 'original_b64': base64.b64encode(self.data).decode()}
        self.host = Host()
        self.host.repo = Repo({('conversation', 'c'): {'seller_id': 'seller', 'buyer_id': 'buyer'},
                               ('supplement_attachment', 'a'): self.attachment})

    def test_read_and_check_each_chunk(self):
        desc = self.host._image_original_describe(self.ref)
        self.assertEqual((desc['width'], desc['height']), (4, 3))
        chunk = self.host._image_original_chunk(self.ref, 0, self.sha)
        self.assertEqual(base64.b64decode(chunk['data_b64']), self.data)
        self.attachment['status'] = 'removed'
        with self.assertRaises(ServiceError):
            self.host._image_original_chunk(self.ref, 0, self.sha)

    def test_copy_converts_formats_and_applies_orientation_and_alpha(self):
        for fmt, mime in [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')]:
            image = Image.new('RGBA', (2, 3), (255, 0, 0, 0))
            image.putpixel((0, 0), (255, 0, 0, 255))
            out = io.BytesIO()
            options = {}
            if fmt == 'JPEG':
                image = image.convert('RGB')
                exif = Image.Exif()
                exif[274] = 6
                options['exif'] = exif
            image.save(out, fmt, **options)
            data = out.getvalue()
            sha = hashlib.sha256(data).hexdigest()
            ref = {**self.ref, 'sha256': sha}
            self.attachment.update(sha256=sha, size=len(data), media_type=mime, original_b64=base64.b64encode(data).decode())
            desc = self.host._image_copy_describe(ref)
            self.assertEqual((desc['width'], desc['height']), (3, 2) if fmt == 'JPEG' else (2, 3))
            result = self.host._image_copy_chunk(ref, desc['token'], 0, desc['sha256'])
            with Image.open(io.BytesIO(base64.b64decode(result['data_b64']))) as converted:
                self.assertEqual(converted.format, 'PNG')
                if fmt != 'JPEG':
                    self.assertEqual(converted.convert('RGBA').getpixel((1, 1))[3], 0)

    def test_copy_rejects_expired_or_changed_reference(self):
        desc = self.host._image_copy_describe(self.ref)
        self.host._cached_copy_sessions[desc['token']]['expires'] = 0
        with self.assertRaises(ServiceError):
            self.host._image_copy_chunk(self.ref, desc['token'], 0, desc['sha256'])
        desc = self.host._image_copy_describe(self.ref)
        self.attachment['status'] = 'removed'
        with self.assertRaises(ServiceError):
            self.host._image_copy_chunk(self.ref, desc['token'], 0, desc['sha256'])

    def test_maximum_original_bytes(self):
        for size, valid in [(6 * 1024 * 1024, True), (6 * 1024 * 1024 + 1, False)]:
            data = self.data + bytes(size - len(self.data))
            sha = hashlib.sha256(data).hexdigest()
            self.attachment.update(sha256=sha, size=size, original_b64=base64.b64encode(data).decode())
            ref = {**self.ref, 'sha256': sha}
            if valid:
                self.assertEqual(self.host._image_original_describe(ref)['size'], size)
            else:
                with self.assertRaises(ServiceError):
                    self.host._image_original_describe(ref)

    def test_product_owner_and_version(self):
        self.host.repo.rows[('library_product', 'p')] = {'seller_id': 'seller', 'product_id': '123'}
        record = {'seller_id': 'seller', 'product_id': '123', 'updated_at': 4,
                  'images': [{'status': 'ready', 'asset_sha256': self.sha}]}
        self.host.repo.rows[('product_main_image', 'p')] = record
        self.host.repo.rows[('image_asset', self.sha)] = {**self.attachment, 'mime': 'image/png'}
        ref = {'kind': 'product', 'local_product_id': 'p', 'version': 4, 'sha256': self.sha}
        self.host._image_original_describe(ref)
        with self.assertRaises(ServiceError):
            self.host._image_original_describe({**ref, 'version': 3})
        record['seller_id'] = 'another'
        with self.assertRaises(ServiceError):
            self.host._image_original_describe(ref)

    def test_product_text_copy_is_owned_bounded_and_derived(self):
        self.host.repo.rows[('library_product', 'p')] = {'seller_id': 'seller', 'product_id': '123'}
        self.assertEqual(self.host._copy_text_for_reference({'local_product_id': 'p', 'field': 'product_id'}),
                         {'field': 'product_id', 'text': '123'})
        self.assertEqual(self.host._copy_text_for_reference({'local_product_id': 'p', 'field': 'product_url'})['text'],
                         'https://www.alibaba.com/product-detail/_123.html')
        self.host.repo.rows[('library_product', 'p')]['product_id'] = '定制-123'
        self.assertEqual(self.host._copy_text_for_reference({'local_product_id': 'p', 'field': 'product_id'})['text'], '定制-123')
        with self.assertRaises(ServiceError):
            self.host._copy_text_for_reference({'local_product_id': 'p', 'field': 'product_url'})
        self.host.repo.rows[('library_product', 'p')]['seller_id'] = 'other'
        with self.assertRaises(ServiceError):
            self.host._copy_text_for_reference({'local_product_id': 'p', 'field': 'product_id'})
        for ref in ({'local_product_id': 'p', 'field': 'contact'}, {'local_product_id': 'p', 'field': 'product_id', 'text': 'injected'},
                    {'case_id': 'c', 'local_product_id': 'p', 'field': 'product_id'}, None):
            with self.subTest(ref=ref), self.assertRaises(ServiceError):
                self.host._copy_text_for_reference(ref)

    def test_reject_wrong_owner_or_corruption(self):
        self.attachment['buyer_id'] = 'other'
        with self.assertRaises(ServiceError):
            self.host._image_original_describe(self.ref)
        self.attachment['buyer_id'] = 'buyer'
        self.attachment['original_b64'] = base64.b64encode(b'wrong').decode()
        with self.assertRaises(ServiceError):
            self.host._image_original_describe(self.ref)


if __name__ == '__main__':
    unittest.main()
