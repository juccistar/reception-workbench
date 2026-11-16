"""Attachment isolation, bounded parsing and generation fencing using synthetic data."""
import base64
import io
import json
import os
import random
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
from reception_service.errors import ServiceError
from reception_service.model_payload import project_model_payload
from reception_service.service import ReceptionService
from reception_service.business_supplement import supplement_fingerprint
from reception_service.photos import with_direct_photo_reads


def b64(data):
    return base64.b64encode(data).decode('ascii')


def pdf(pages=1, text=True):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 10 100 Td (hello) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class SupplementAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule = patch.object(ReceptionService, '_schedule', return_value=None)
        self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(lambda: self.service.close())
        self.cid = self.service.repo.list('conversation')[0]['conversation_id']

    def upload(self, name, data):
        return self.service.upload_supplement_attachment(self.cid, name, b64(data))

    def test_text_persists_projects_and_removal_fences_existing_body(self):
        s = self.service
        saved = s.save_conversation_supplement(self.cid, 'original', None, 'save')
        case = s.repo.get('conversation', self.cid)['case_id']
        old = supplement_fingerprint(s.get_context(case))
        note = self.upload('note.txt', '商家文本'.encode())
        self.assertEqual(note['body'], 'original')
        self.assertNotEqual(old, supplement_fingerprint(s.get_context(case)))
        with self.assertRaises(ServiceError):
            s.save_conversation_supplement(self.cid, 'new', saved['revision'], 'stale')
        payload = project_model_payload(s.get_context(case))['supplement']
        self.assertEqual(payload['attachments'][0]['text'], '商家文本')
        self.assertEqual(payload['attachments'][0]['origin'], 'business_supplement')
        self.assertNotIn('original_b64', str(payload))
        attachment_id = note['attachments'][0]['id']
        s.close()
        self.service = s = ReceptionService(Path(self.temp.name))
        self.assertEqual(s.get_conversation(self.cid)['supplement']['attachments'][0]['id'], attachment_id)
        removed = s.remove_supplement_attachment(self.cid, attachment_id)
        self.assertEqual(removed['body'], 'original')
        self.assertEqual(removed['attachments'], [])
        self.assertIsNone(s.repo.get('supplement_attachment', attachment_id))

    def test_image_is_supplement_origin_and_polish_has_only_that_image(self):
        data = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(data, 'PNG')
        item = self.upload('sample.png', data.getvalue())['attachments'][0]
        context, _ = self.service._manual_context(self.cid, None, 'hello', 1)
        refs = context['supplement_image_refs']
        self.assertEqual(refs[0]['attachment_id'], item['id'])
        self.assertEqual(context['photo_context']['direct_images'], [])
        inputs = self.service._direct_photo_inputs(context)
        self.addCleanup(inputs.owner.close_if_owned)
        self.assertEqual(inputs[0]['attachment']['origin'], 'business_supplement')
        self.assertNotIn('message', inputs[0])
        model = project_model_payload(context)
        self.assertEqual(model['image_manifest'][0]['origin'], 'business_supplement')
        self.assertEqual(with_direct_photo_reads(context, [])['supplement_image_reads'], [])
        self.assertEqual(with_direct_photo_reads(context, refs)['supplement_image_reads'], refs)
        self.service.remove_supplement_attachment(self.cid, item['id'])
        with self.assertRaises(ServiceError):
            self.service._direct_photo_inputs(context)

    def test_removal_during_generation_discards_old_result(self):
        data = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(data, 'PNG')
        item = self.upload('sample.png', data.getvalue())['attachments'][0]
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        received_refs = []
        def generate(context, request_id, *, on_call_start=None, cancel_event=None, photo_inputs=None):
            on_call_start()
            received_refs.append(context['supplement_image_refs'])
            entered.set()
            release.wait(5)
            body = 'old reply' if context['supplement_image_refs'] else 'current reply'
            return {'reply_messages': [{'body': body, 'reply_sources': []}],
                    'verified_image_reads': context['supplement_image_refs']}
        with patch.object(self.service.integrations, 'generate_manual', side_effect=generate):
            job_id = self.service.generate_manual_suggestion(self.cid, 'attachment-race')['job_id']
            self.assertTrue(entered.wait(5))
            self.service.remove_supplement_attachment(self.cid, item['id'])
            release.set()
            for _ in range(300):
                job = self.service.repo.get('job', job_id)
                if job['status'] not in ('queued', 'running'):
                    break
                time.sleep(.01)
            self.assertIn(job['status'], ('cancelled', 'stale'))
            self.assertIsNone(self.service.get_conversation(self.cid)['manual_suggestion'])
            fresh_id = self.service.generate_manual_suggestion(self.cid, 'attachment-current')['job_id']
            for _ in range(300):
                fresh_job = self.service.repo.get('job', fresh_id)
                if fresh_job['status'] not in ('queued', 'running'):
                    break
                time.sleep(.01)
            self.assertEqual(fresh_job['status'], 'succeeded', fresh_job)
            self.assertEqual(self.service.get_conversation(self.cid)['manual_suggestion']['body'], 'current reply')
            self.assertEqual(len(received_refs), 2)
            self.assertEqual(received_refs[0][0]['attachment_id'], item['id'])
            self.assertEqual(received_refs[1], [])

    def test_pdf_extracts_text_and_corruption_is_shown_and_blocks_generation(self):
        note = self.upload('valid.pdf', pdf())
        attachment_id = note['attachments'][0]['id']
        context, _ = self.service._manual_context(self.cid, None)
        self.assertIn('hello', context['supplement']['attachments'][0]['text'])
        row = self.service.repo.get('supplement_attachment', attachment_id)
        row['original_b64'] = b64(b'corrupt')
        self.service.repo.put('supplement_attachment', attachment_id, row)
        self.assertEqual(self.service.get_conversation(self.cid)['supplement']['attachments'][0]['status'], 'failed')
        with self.assertRaises(ServiceError):
            self.service._manual_context(self.cid, None)
        self.service.remove_supplement_attachment(self.cid, attachment_id)
        self.assertEqual(self.service.get_conversation(self.cid)['supplement']['attachments'], [])

    def test_invalid_and_bounded_files_are_rejected(self):
        for name, data in [('x.exe', b'abc'), ('x.txt', b'\xff'), ('x.txt', b'a' * 16001),
                           ('x.png', b'not png'), ('x.pdf', b'%PDF-bad'), ('x.pdf', pdf(11)),
                           ('x.pdf', pdf(1, False))]:
            with self.subTest(name=name, size=len(data)):
                with self.assertRaises(ServiceError):
                    self.upload(name, data)
        for i in range(10):
            self.upload(f'{i}.txt', b'hello')
        with self.assertRaises(ServiceError):
            self.upload('eleven.txt', b'hello')

    def test_transparent_png_attachment_keeps_alpha_and_original_bytes(self):
        with io.BytesIO() as out, Image.new('RGBA', (400, 300), (255, 255, 255, 0)) as image:
            image.paste((255, 255, 255, 255), (100, 100, 200, 200))
            image.save(out, 'PNG')
            original = out.getvalue()
        item = self.upload('transparent.png', original)['attachments'][0]
        self.assertTrue(item['preview_data_url'].startswith('data:image/png;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(item['preview_data_url'].split(',', 1)[1]))) as preview:
            self.assertEqual(preview.getchannel('A').getextrema(), (0, 255))
            self.assertLessEqual(max(preview.size), 256)
        stored = self.service.repo.get('supplement_attachment', item['id'])
        self.assertEqual(base64.b64decode(stored['original_b64']), original)
        self.assertLessEqual(len(base64.b64decode(item['preview_data_url'].split(',', 1)[1])), 48 * 1024)

    def test_static_gif_is_accepted_and_animated_gif_is_rejected(self):
        still = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(still, 'GIF')
        self.assertEqual(self.upload('still.gif', still.getvalue())['attachments'][0]['media_type'], 'image/gif')
        animated = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(animated, 'GIF', save_all=True,
            append_images=[Image.new('RGB', (4, 4), 'blue')], duration=100, loop=0)
        with self.assertRaises(ServiceError):
            self.upload('animated.gif', animated.getvalue())

    def test_large_upload_reaches_bounded_parser_over_json_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            request = {'id': 'large', 'method': 'upload_supplement_attachment', 'params': {
                'conversation_id': 'synthetic', 'name': 'large.txt', 'content_b64': b64(b'a' * (20 * 1024 * 1024))}}
            generic = {'id': 'generic', 'method': 'get_settings', 'params': {'padding': 'a' * 2_000_001}}
            payload = json.dumps(request) + '\n' + json.dumps(generic) + '\n' + json.dumps({'id': 'stop', 'method': 'shutdown', 'params': {}}) + '\n'
            env = dict(os.environ, PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, '-B', '-m', 'reception_service', '--data-dir', folder],
                                    input=payload, text=True, encoding='utf-8', capture_output=True, env=env, timeout=30)
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            response = next(row for row in responses if row.get('id') == 'large')
            self.assertEqual(response['error']['code'], 'attachment_parse_failed')
            self.assertTrue(any(row.get('error', {}).get('code') == 'request_too_large' for row in responses))
            self.assertEqual(result.returncode, 0)

    def test_five_image_previews_stay_bounded_and_preserve_originals(self):
        raw = random.Random(42).randbytes(512 * 512 * 3)
        output = io.BytesIO()
        Image.frombytes('RGB', (512, 512), raw).save(output, 'PNG')
        original = output.getvalue()
        for i in range(5):
            note = self.upload(f'{i}.png', original)
        for attachment in note['attachments']:
            preview = attachment['preview_data_url'].split(',', 1)[1]
            self.assertLessEqual(len(base64.b64decode(preview)), 48 * 1024)
            stored = self.service.repo.get('supplement_attachment', attachment['id'])
            self.assertEqual(base64.b64decode(stored['original_b64']), original)
        self.assertLess(len(json.dumps(note, ensure_ascii=False).encode('utf-8')), 400_000)

    def test_total_limit_rejects_without_changing_existing_attachment(self):
        with patch('reception_service.conversation_assistance.MAX_TOTAL', 5):
            first = self.upload('one.txt', b'123')
            with self.assertRaises(ServiceError):
                self.upload('two.txt', b'456')
            note = self.service.get_conversation(self.cid)['supplement']
            self.assertEqual(note['revision'], first['revision'])
            self.assertEqual(len(note['attachments']), 1)

    def test_attachment_cannot_be_removed_from_other_conversation(self):
        attachment = self.upload('note.md', b'hello')['attachments'][0]
        with self.assertRaises(ServiceError):
            self.service.remove_supplement_attachment('different-conversation', attachment['id'])
        self.assertIsNotNone(self.service.repo.get('supplement_attachment', attachment['id']))
