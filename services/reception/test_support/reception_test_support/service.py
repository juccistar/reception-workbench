"""Synthetic service for isolated development and test harnesses only."""
from pathlib import Path
import copy
from .demo import DemoIntegration

from reception_service.repository import now_ms
from reception_service.service import ReceptionService


class SyntheticReceptionService(ReceptionService):
    def _seed(self):
        with self._lock, self.repo.transaction():
            stamp = now_ms() - 600_000
            samples = [
                ('emma', 'Emma Wilson', 'Hi, I am interested in stainless steel bottles. Can I order a sample with our logo?'),
                ('sofia', 'Sofia Martinez', 'We need 500 canvas tote bags for our shop. Delivery to Madrid, Spain.'),
                ('noah', 'Noah Brown', 'I am looking for ceramic mugs. Could you share the available shapes?'),
            ]
            for index, (key, name, text) in enumerate(samples):
                cid, buyer, mid = f'demo-{key}', f'buyer-{key}', f'demo-{key}-message-1'
                info = {'conversation_id': cid, 'buyer_id': buyer, 'customer_name': name}
                message = {'message_id': mid, 'seller_id': self.seller_id, 'buyer_id': buyer,
                           'conversation_id': cid, 'role': 'buyer', 'text': text,
                           'timestamp_ms': stamp + index * 1000, 'attachments': [], 'own': False}
                self._ingest(info, [message], has_more=False, full=True)
            policy = {'policy_id': 'demo-sample-logo-policy', 'kind': 'policy', 'title': '演示：样品与 Logo 政策',
                      'content': 'DEMO POLICY: Samples and custom logos can be requested for evaluation. Sample cost, logo setup cost, stock and delivery time must be checked before confirmation. No price or delivery commitment is approved.',
                      'product_scope': [], 'confirmed': True, 'public': True, 'revision': 1,
                      'updated_at': now_ms(), 'valid_until': None}
            self.repo.put('policy', policy['policy_id'], policy, history=True)


def _factory(mode, data_dir, settings):
    return DemoIntegration(data_dir, settings)

def _initialize_fixture(service):
    if not service.repo.list('conversation'):
        service._seed()
    if hasattr(service.integrations, 'messages') and not service.integrations.messages:
        service.integrations.messages = copy.deepcopy(service.repo.list('message'))

def create_synthetic_service(data_root: Path, emit=None):
    return SyntheticReceptionService(Path(data_root), 'demo', emit=emit,
                                     _integration_factory=_factory, _fixture_initializer=_initialize_fixture)
