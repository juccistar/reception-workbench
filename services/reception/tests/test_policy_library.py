import threading
import unittest

from reception_service.errors import ServiceError
from reception_service.policy_library import (adopt_builtin_policy, list_builtin_policies,
                                               normalize_policy, save_policy, select_policy_inputs)
from reception_service.repository import Repository


class Service:
    def __init__(self):
        self.account_verified = True
        self._lock = threading.RLock()
        self.repo = Repository(None, seller_id='seller-a')
        self.changes = 0

    def _changed(self):
        self.changes += 1


class PolicyLibraryTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()

    def tearDown(self):
        self.service.repo.close()

    def test_style_priority_selection_and_validation(self):
        service = self.service
        scoped = save_policy(service, 'policy', '专用', '专用语气', ['hat'], True, False,
                             category='style', enabled=True, usage='internal_guidance', priority=1)
        generic = save_policy(service, 'policy', '通用', '通用语气', [], True, False,
                              category='style', enabled=True, usage='internal_guidance', priority=2)
        case = {'facts': {'product': {'value': 'hat'}}, 'procurement_key': ''}
        self.assertEqual(select_policy_inputs([scoped, generic], case)['style'][0]['source_id'], generic['policy_id'] + ':internal')
        generic = save_policy(service, generic['kind'], generic['title'], generic['content'], [], True, False,
                              generic['policy_id'], generic['revision'], category='style', enabled=True,
                              usage='internal_guidance', priority=1)
        self.assertEqual(select_policy_inputs([scoped, generic], case)['style'][0]['source_id'], scoped['policy_id'] + ':internal')
        self.assertEqual(normalize_policy({'kind': 'policy'})['priority'], 0)
        with self.assertRaises(ServiceError):
            save_policy(service, 'policy', '错误', '内容', [], True, False, priority=101)

    def test_catalog_is_read_only_and_templates_require_real_content(self):
        items = list_builtin_policies(self.service)['items']
        self.assertEqual([x['template_id'] for x in items], ['B01', 'B02', 'B03', 'B04', 'B05', 'B06', 'T01', 'T02', 'T03'])
        self.assertTrue(all(not x['enabled'] and x['adopted_policy_id'] is None for x in items))
        self.assertIn('本政策不代表免费样品', items[2]['content'])
        self.assertEqual(self.service.repo.list('policy'), [])
        with self.assertRaises(ServiceError):
            adopt_builtin_policy(self.service, 'T01')
        p = adopt_builtin_policy(self.service, 'T01', content='面料：棉')
        self.assertEqual(p['content'], '面料：棉')
        self.assertEqual(select_policy_inputs([p], {})['knowledge'][0]['content'], '面料：棉')

    def test_adoption_idempotence_update_and_cas(self):
        first = adopt_builtin_policy(self.service, 'B01', enabled=False)
        self.assertEqual(adopt_builtin_policy(self.service, 'B01', content='changed', enabled=True), first)
        with self.assertRaises(ServiceError):
            adopt_builtin_policy(self.service, 'B01', update=True, content='changed')
        updated = adopt_builtin_policy(self.service, 'B01', content='edited', enabled=False, product_scope=['coat'], update=True, expected_revision=first['revision'])
        self.assertEqual(updated['policy_id'], first['policy_id'])
        self.assertEqual(updated['revision'], first['revision'] + 1)
        self.assertFalse(updated['enabled'])
        self.assertEqual(list_builtin_policies(self.service)['items'][0]['content'].startswith('使用即时聊天'), True)
        self.assertEqual(len(self.service.repo.list('policy')), 1)

    def test_updated_catalog_requires_review_and_preserves_merchant_copy(self):
        for template_id in ('B01', 'B05', 'B06'):
            with self.subTest(template_id=template_id):
                original = save_policy(self.service, 'policy', '商家名称', '商家自定义正文', ['coat'], True, False,
                                       category='reception', enabled=False, usage='internal_guidance',
                                       internal_notes='商家备注', template_id=template_id, template_version=1)
                entry = next(item for item in list_builtin_policies(self.service)['items']
                             if item['template_id'] == template_id)
                self.assertEqual(entry['template_version'], 2)
                self.assertTrue(entry['available_update'])
                self.assertEqual(self.service.repo.get('policy', original['policy_id']), original)
                self.assertEqual(adopt_builtin_policy(self.service, template_id), original)
                with self.assertRaises(ServiceError) as conflict:
                    adopt_builtin_policy(self.service, template_id, content='过期的审核结果', update=True,
                                         expected_revision=original['revision'] - 1)
                self.assertEqual(conflict.exception.code, 'revision_conflict')
                self.assertEqual(self.service.repo.get('policy', original['policy_id']), original)
                updated = adopt_builtin_policy(self.service, template_id, content='商家审核合并后的正文',
                                               update=True, expected_revision=original['revision'])
                for key in ('policy_id', 'title', 'enabled', 'product_scope', 'usage', 'internal_notes'):
                    self.assertEqual(updated[key], original[key], key)
                self.assertEqual(updated['content'], '商家审核合并后的正文')
                self.assertEqual(updated['template_version'], 2)
                entry = next(item for item in list_builtin_policies(self.service)['items']
                             if item['template_id'] == template_id)
                self.assertFalse(entry['available_update'])

    def test_template_update_preserves_local_fields_and_applies_explicit_choices(self):
        p = adopt_builtin_policy(self.service, 'B02')
        p = save_policy(self.service, p['kind'], '本地名称', '本地规则', ['coat'], False, False,
                        p['policy_id'], p['revision'], valid_until=9999999999999,
                        category='business', enabled=False, usage='internal_guidance',
                        customer_wording='可公开说明', internal_notes='仅内部查看')
        updated = adopt_builtin_policy(self.service, 'B02', content='审核后的正文',
                                       update=True, expected_revision=p['revision'])
        for key in ('kind', 'title', 'category', 'usage', 'customer_wording', 'internal_notes',
                    'valid_until', 'enabled', 'confirmed', 'product_scope'):
            self.assertEqual(updated[key], p[key], key)
        self.assertEqual(updated['content'], '审核后的正文')
        self.assertEqual(updated['template_version'], 1)
        explicit = adopt_builtin_policy(self.service, 'B02', content='再次审核', update=True,
                                        expected_revision=updated['revision'], enabled=True,
                                        confirmed=True, product_scope=['dress'])
        self.assertTrue(explicit['enabled'])
        self.assertTrue(explicit['confirmed'])
        self.assertEqual(explicit['product_scope'], ['dress'])
        with self.assertRaises(ServiceError):
            adopt_builtin_policy(self.service, 'B02', update=True, expected_revision=explicit['revision'])

    def test_template_save_cannot_enable_empty_facts_and_selection_defends_old_rows(self):
        p = adopt_builtin_policy(self.service, 'T01', content='面料：棉', enabled=False)
        def save(text, enabled):
            return save_policy(self.service, p['kind'], p['title'], text, [], True, True,
                               p['policy_id'], p['revision'], enabled=enabled, usage='customer_reference')
        for text in ('颜色：[待填写]\n面料：[待填写]', '颜色：\n面料：  ', '待填写'):
            with self.subTest(text=text), self.assertRaises(ServiceError):
                save(text, True)
        draft = save('颜色：\n面料：[待填写]', False)
        self.assertFalse(draft['public'])
        legacy = {**draft, 'enabled': True, 'public': True}
        self.assertEqual(select_policy_inputs([legacy], {})['knowledge'], [])
        self.assertFalse(normalize_policy(legacy)['public'])
        valid = save_policy(self.service, draft['kind'], draft['title'], '颜色：\n面料：棉', [], True, True,
                            draft['policy_id'], draft['revision'], enabled=True, usage='customer_reference')
        self.assertEqual(select_policy_inputs([valid], {})['knowledge'][0]['content'], '面料：棉')

    def test_legacy_and_separate_public_and_internal_sources(self):
        legacy = dict(policy_id='a', kind='policy', content='fact', confirmed=True, public=True, revision=1, product_scope=[])
        private = dict(legacy, policy_id='b', public=False)
        internal = dict(legacy, policy_id='c', public=False, enabled=True, usage='internal_guidance', category='business', content='secret rule', customer_wording='public statement', internal_notes='private note')
        selected = select_policy_inputs([legacy, private, internal], {}, at_ms=100)
        self.assertEqual([p['policy_id'] for p in selected['knowledge']], ['a', 'c:customer'])
        self.assertEqual(selected['guidance'][0]['policy_id'], 'c:internal')
        self.assertNotIn('secret rule', str(selected['knowledge']))
        self.assertNotIn('private note', str(selected))
        self.assertFalse(normalize_policy(internal)['public'])
        self.assertNotIn('b', selected['policy_versions'])

    def test_scope_expiry_style_conflicts_and_disabled_fingerprint(self):
        def p(pid, **kwargs):
            return dict({'policy_id': pid, 'kind': 'policy', 'content': pid, 'confirmed': True, 'public': False,
                         'enabled': True, 'usage': 'internal_guidance', 'category': 'style', 'revision': 1,
                         'product_scope': [], 'valid_until': None}, **kwargs)
        rows = [p('global'), p('scoped', product_scope=['coat']), p('disabled', enabled=False), p('expired', valid_until=100)]
        case = {'facts': {'product': {'value': 'winter coat'}}}
        result = select_policy_inputs(rows, case, at_ms=100)
        self.assertEqual(result['style'][0]['policy_id'], 'scoped:internal')
        self.assertEqual(len(result['style_conflicts']), 2)
        rows[2]['content'] = 'changed'; rows[2]['revision'] += 1
        self.assertEqual(select_policy_inputs(rows, case, at_ms=100), result)
        self.assertEqual(select_policy_inputs(rows, {}, at_ms=100)['style'][0]['policy_id'], 'global:internal')

    def test_disabled_edits_do_not_invalidate_and_scope_changes_target_cases(self):
        repo = self.service.repo
        for cid, product in [('one', 'coat'), ('two', 'dress')]:
            repo.put('case', cid, {'case_id': cid, 'conversation_id': cid, 'facts': {'product': {'value': product}}})
            repo.put('draft', cid, {'draft_id': cid, 'case_id': cid, 'conversation_id': cid, 'status': 'pending', 'body': 'human', 'validation': {'issues': []}})
        p = save_policy(self.service, 'policy', 'title', 'rule', ['coat'], True, False, enabled=False, usage='internal_guidance')
        p = save_policy(self.service, 'policy', 'title', 'edited', ['coat'], True, False, p['policy_id'], p['revision'], enabled=False, usage='internal_guidance')
        self.assertEqual(repo.get('draft', 'one')['status'], 'pending')
        p = save_policy(self.service, 'policy', 'title', 'edited', ['coat'], True, False, p['policy_id'], p['revision'], enabled=True, usage='internal_guidance')
        self.assertEqual(repo.get('draft', 'one')['status'], 'stale')
        self.assertEqual(repo.get('draft', 'one')['body'], 'human')
        self.assertEqual(repo.get('draft', 'two')['status'], 'pending')
        with self.assertRaises(ServiceError):
            save_policy(self.service, 'policy', 'title', 'edited', [], True, False, p['policy_id'], 1)
        self.service.account_verified = False
        with self.assertRaises(ServiceError):
            adopt_builtin_policy(self.service, 'B02')


if __name__ == '__main__':
    unittest.main()
