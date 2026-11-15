import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from reception_service.whatsapp_account_service import WhatsAppAccountService, _key


class AccountSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = WhatsAppAccountService(Path(self.tmp.name), str(uuid.uuid4()), lambda event: None)
        self.s._wa_set_account('account', 'epoch', 'connected')
        self.message('buyer', False)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def message(self, mid, outgoing):
        self.s._wa_ingest('account', 'epoch', [{'id': mid, 'chat_id': 'chat', 'body': 'text',
            'timestamp_ms': 1, 'is_history': False, 'is_group': False, 'from_me': outgoing}])

    def pending(self):
        self.s.repo.put('operation', 'op', {'operation_id': 'op', 'account_id': 'account',
            'connection_epoch': 'epoch', 'chat_id': 'chat', 'status': 'unknown',
            'submission_started': True, 'message_id': None, 'ack': None, 'body': 'reply'})

    def test_pending_binding_does_not_change_active_revision(self):
        self.pending()
        binding = self.s.save_whatsapp_binding('chat', 'merchant', 'conversation', 0, 'account', 'epoch')
        self.assertEqual(binding['revision'], 0)
        self.assertEqual(binding['requested_binding_revision'], 1)
        self.s._wa_transport_result('op', 'account', 'epoch', 'submitted', 'sent', 1)
        binding = self.s.repo.get('binding', _key('account', 'chat'))
        self.assertEqual((binding['merchant_id'], binding['conversation_id'], binding['revision']), ('merchant', 'conversation', 1))

    def test_outgoing_race_uses_message_id(self):
        self.pending()
        self.message('sent', True)
        self.assertFalse(self.s._conversation('chat')['human_owned'])
        self.s._wa_transport_result('op', 'account', 'epoch', 'submitted', 'sent', 0)
        self.assertFalse(self.s._conversation('chat')['human_owned'])

    def test_other_outgoing_during_send_takes_over(self):
        self.pending()
        self.message('manual', True)
        self.s._wa_transport_result('op', 'account', 'epoch', 'submitted', 'sent', 0)
        self.assertTrue(self.s._conversation('chat')['human_owned'])

    def test_repeated_configuration_does_not_revoke_but_invalid_does(self):
        class Router:
            def configure(self, packet):
                if packet.get('bad'):
                    raise ValueError('invalid')
            def close(self):
                pass
        with patch('reception_service.model_router.ModelRouter', Router):
            self.s._configure_models(format='synthetic')
            self.s.enabled, self.s.allowed = True, ['merchant']
            self.s._configure_models(format='synthetic')
            self.assertTrue(self.s.enabled)
            with self.assertRaises(ValueError):
                self.s._configure_models(bad=True)
            self.assertFalse(self.s.enabled)

    def test_private_disable_does_not_require_identity(self):
        self.s.enabled = True
        self.s.set_whatsapp_reception(False)
        self.assertFalse(self.s.enabled)
