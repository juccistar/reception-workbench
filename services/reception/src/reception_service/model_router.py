"""Private model configuration routing with authority-file revision checks.

Only the host may supply credentials; immutable snapshots never contain them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time

from .model_providers.encoding import failure, image_blocks, validate_request_budget
from .model_providers.runtime import ModelRuntime
from .model_providers.owned_sync import owned_sync
from .media_resources import PROCESS_MEDIA_BUDGET, ResourceLimitError

from .errors import ServiceError

_PURPOSES = ('reception', 'translation')
_CAPABILITIES = frozenset(('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation'))
_LIMIT = 1024 * 1024


def _failure(code='model_configuration_invalid'):
    return ServiceError(code, '模型配置不可用或已变化，请重新检查配置', stage='model_configuration')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _read_authority(path):
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError('authority too large')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if not isinstance(value, dict) or value.get('format') != 'model-settings-v1':
            raise ValueError('invalid authority')
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise _failure() from None


def _selected(settings, purpose, required):
    try:
        if not isinstance(settings, dict) or settings.get('format') != 'model-settings-v1':
            raise ValueError()
        profiles = settings['profiles']
        if not isinstance(profiles, list) or len(profiles) > 32:
            raise ValueError()
        ids = [p['profile_id'] for p in profiles if isinstance(p, dict)]
        if len(ids) != len(profiles) or len(set(ids)) != len(ids):
            raise ValueError()
        route = settings['routes'][purpose]
        profile_id = route['profile_id']
        profile = next(p for p in profiles if p['profile_id'] == profile_id)
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError()
        revisions = (route['route_revision'], profile['config_revision'],
                     profile['credential_revision'], profile['capability_revision'])
        if any(type(n) is not int or n < 0 for n in revisions):
            raise ValueError()
        if profile['protocol'] not in ('chat-completions', 'anthropic'):
            raise ValueError()
        if any(not isinstance(profile.get(k), str) or not profile[k] for k in ('base_url', 'model')):
            raise ValueError()
        if profile.get('has_key') is not True:
            raise ValueError()
        caps = profile['capabilities']
        if any(not isinstance(caps.get(k), dict) or caps[k].get('status') != 'passed' for k in required):
            raise ValueError()
        return profile, revisions
    except (KeyError, TypeError, ValueError, StopIteration):
        raise _failure('model_not_ready') from None


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    purpose: str
    profile_id: str
    route_revision: int
    config_revision: int
    credential_revision: int
    capability_revision: int
    required_capabilities: tuple[str, ...]
    allow_unchecked: bool = False


class ModelRouter:
    def __init__(self):
        self._lock = threading.RLock()
        self._authority = None
        self._settings = None
        self._credentials = {}
        self._closed = False
        self._runtime = None
        self._transport = None
        self._active = {purpose: set() for purpose in _PURPOSES}

    def configure(self, packet):
        if not isinstance(packet, dict) or packet.get('format') != 'model-runtime-v1':
            raise _failure()
        path, settings, credentials = packet.get('authority_file'), packet.get('settings'), packet.get('credentials')
        if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(credentials, dict):
            raise _failure()
        authority = _read_authority(path)
        if not isinstance(settings, dict):
            raise _failure()
        # Compare the complete public document so that injected metadata cannot
        # substitute an endpoint while keeping the same revisions.
        public = {k: v for k, v in authority.items() if k != 'encrypted_keys'}
        if public != settings:
            raise _failure('model_configuration_stale')
        allowed = set()
        try:
            for purpose in _PURPOSES:
                selected = settings['routes'][purpose]['profile_id']
                if selected is not None:
                    if not isinstance(selected, str) or not selected:
                        raise ValueError()
                    allowed.add(selected)
            if any(k not in allowed or not isinstance(v, str) or not v for k, v in credentials.items()):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise _failure() from None
        with self._lock:
            if self._closed:
                raise _failure()
            old = self._settings
            affected = set()
            for purpose in _PURPOSES:
                def dependency(doc):
                    if doc is None:
                        return None
                    try:
                        profile, revisions = _selected(doc, purpose, ())
                        return (profile['profile_id'], profile['protocol'], profile['base_url'], profile['model'], revisions)
                    except ServiceError:
                        return None
                old_dep, new_dep = dependency(old), dependency(settings)
                old_key = self._credentials.get(old_dep[0]) if old_dep else None
                new_key = credentials.get(new_dep[0]) if new_dep else None
                if old_dep != new_dep or old_key != new_key or (self._authority is not None and self._authority != path):
                    affected.add(purpose)
            for purpose in affected:
                for event in self._active[purpose]:
                    event.set()
            if self._runtime is not None and self._transport is not None:
                old_profiles = {p.get('profile_id'): p for p in (old or {}).get('profiles', [])}
                new_profiles = {p.get('profile_id'): p for p in settings.get('profiles', [])}
                selected_ids = {settings['routes'][p]['profile_id'] for p in _PURPOSES}
                retired = []
                for profile_id, old_profile in old_profiles.items():
                    new_profile = new_profiles.get(profile_id)
                    fields = ('protocol', 'base_url', 'model', 'config_revision', 'credential_revision')
                    if (profile_id not in selected_ids or new_profile is None or
                        any(old_profile.get(k) != new_profile.get(k) for k in fields) or
                        self._credentials.get(profile_id) != credentials.get(profile_id)):
                        retired.append(profile_id)
                transport = self._transport
                async def retire():
                    for profile_id in retired:
                        await transport.invalidate_profile(profile_id)
                if retired:
                    self._runtime.schedule(retire)
            self._authority = path
            self._settings = deepcopy(settings)
            self._credentials = dict(credentials)
            return affected

    def resolve(self, purpose, required_capabilities=(), *, allow_unchecked=False):
        if purpose not in _PURPOSES or type(allow_unchecked) is not bool or (allow_unchecked and purpose != 'reception'):
            raise _failure()
        try:
            required = tuple(sorted(set(required_capabilities)))
        except (TypeError, ValueError):
            raise _failure() from None
        if any(k not in _CAPABILITIES for k in required):
            raise _failure()
        with self._lock:
            if self._closed or self._settings is None:
                raise _failure()
            profile, revisions = _selected(self._settings, purpose, () if allow_unchecked else required)
            if profile['profile_id'] not in self._credentials:
                raise _failure('model_not_ready')
            snapshot = ModelSnapshot(purpose, profile['profile_id'], *revisions, required, allow_unchecked)
            self.assert_current(snapshot)
            return snapshot

    def assert_current(self, snapshot):
        if not isinstance(snapshot, ModelSnapshot):
            raise _failure()
        with self._lock:
            if self._closed or self._settings is None or not self._authority:
                raise _failure('model_configuration_stale')
            path = self._authority
        authority = _read_authority(path)
        with self._lock:
            if self._closed or self._settings is None or self._authority != path:
                raise _failure('model_configuration_stale')
            required = () if snapshot.allow_unchecked and snapshot.purpose == 'reception' else snapshot.required_capabilities
            local, local_rev = _selected(self._settings, snapshot.purpose, required)
            remote, remote_rev = _selected(authority, snapshot.purpose, required)
            expected = (snapshot.route_revision, snapshot.config_revision, snapshot.credential_revision, snapshot.capability_revision)
            fields = ('profile_id', 'protocol', 'base_url', 'model', 'credential_ref', 'has_key')
            if (local_rev != expected or remote_rev != expected or
                    local['profile_id'] != snapshot.profile_id or
                    any(local.get(k) != remote.get(k) for k in fields) or
                    snapshot.profile_id not in self._credentials):
                raise _failure('model_configuration_stale')
            return None

    def generate(self, purpose, system_prompt, user_payload, *, images=(), required_capabilities=(),
                 cancel=None, on_call_start=None, max_output_tokens=8192, deadline=None, _owned_input=None,
                 _allow_unchecked=False):
        started = time.monotonic()
        try:
            images = tuple(dict(item) for item in images)
            if sum(len(item['data']) for item in images) > 64 * 1024 * 1024:
                raise failure('model_invalid_input')
        except (TypeError, KeyError, ValueError):
            raise failure('model_invalid_input') from None
        reservation = validate_request_budget(system_prompt, user_payload, images)
        try:
            required = set(required_capabilities)
        except TypeError:
            raise failure('model_invalid_input') from None
        if purpose == 'reception':
            required.update(('text', 'structure'))
        elif purpose == 'translation':
            required.update(('text', 'translation'))
        if images:
            required.update(('single_image' if len(images) == 1 else 'multi_image', 'combined'))
        local_cancel = threading.Event()
        class Cancel:
            def is_set(self):
                return local_cancel.is_set() or (cancel is not None and cancel.is_set())
        combined_cancel = Cancel()
        with self._lock:
            snapshot = self.resolve(purpose, required, allow_unchecked=_allow_unchecked)
            profile, _ = _selected(self._settings, purpose, () if snapshot.allow_unchecked else snapshot.required_capabilities)
            profile = deepcopy(profile)
            key = self._credentials[snapshot.profile_id]
            if self._runtime is None:
                self._runtime = ModelRuntime()
            runtime = self._runtime
            self._active[purpose].add(local_cancel)
        from .model_attempts import AttemptLifecycle
        from .model_providers.lifecycle import WireAttempt
        from .model_providers.transport import ProviderTransport
        lifecycle = on_call_start if isinstance(on_call_start, AttemptLifecycle) else None
        wire = None
        def before_send():
            if combined_cancel.is_set():
                raise failure('model_cancelled')
            self.assert_current(snapshot)
            if combined_cancel.is_set():
                raise failure('model_cancelled')
            if on_call_start is not None:
                on_call_start()
            if combined_cancel.is_set():
                raise failure('model_cancelled')
            self.assert_current(snapshot)
            if lifecycle is not None:
                wire.deadline = min(wire.deadline, lifecycle.deadline)
            if time.monotonic() >= wire.deadline:
                raise failure('model_timeout')
            if not isinstance(self._transport, ProviderTransport):
                # Explicit test transports have no HTTP boundary; once their
                # callback returns treat their invocation conservatively as sent.
                wire.entered = True

        async def complete_attempt(outcome, no_http, retry_after=None):
            if lifecycle is None:
                return
            await owned_sync(lambda: lifecycle.complete(outcome, no_http=no_http, retry_after=retry_after), complete_on_cancel=True)

        async def operation():
            try:
                if _owned_input is None:
                    lease = await PROCESS_MEDIA_BUDGET.acquire_async(
                        reservation, deadline=operation_deadline, cancel=combined_cancel)
                else:
                    # One atomic resize avoids waiting for a second reservation
                    # while the input's own reservation is still held.
                    _owned_input.reserve_more(reservation)
                    lease = _owned_input.lease
            except ResourceLimitError as error:
                raise failure('model_resource_over_limit' if error.code == 'resource_too_large' else
                              'model_cancelled' if error.code == 'resource_cancelled' else 'model_resource_busy') from None
            try:
                return await leased_operation(lease)
            finally:
                if _owned_input is None:
                    lease.close()
        async def leased_operation(lease):
            nonlocal wire
            if combined_cancel.is_set():
                raise failure('model_cancelled')
            await owned_sync(lambda: self.assert_current(snapshot))
            blocks, manifest = image_blocks(images, profile['protocol'])
            if self._transport is None:
                self._transport = ProviderTransport()
            for attempt_index in range(2):
                wire = WireAttempt(min(operation_deadline, lifecycle.deadline) if lifecycle else operation_deadline)
                try:
                    result = await self._transport.generate(profile=profile, api_key=key, system_prompt=system_prompt,
                            user_payload=user_payload, images=images, encoded_images=blocks,
                            max_output_tokens=max_output_tokens, before_send=before_send, _resource_lease=lease, _attempt=wire)
                except BaseException:
                    no_http = not wire.entered
                    rejected = wire.status == 429
                    await complete_attempt('unsubmitted' if no_http or rejected else 'unknown', no_http,
                                           wire.retry_after if rejected else None)
                    # Only a received 429 is a retryable rejection. Transport
                    # errors and 5xx do not prove that generation was unsubmitted.
                    delay = max(0, wire.retry_after)
                    if (rejected and attempt_index == 0 and not combined_cancel.is_set()
                            and time.monotonic() + delay < wire.deadline):
                        await asyncio.sleep(delay)
                        if combined_cancel.is_set():
                            raise failure('model_cancelled')
                        continue
                    raise
                else:
                    await complete_attempt('completed', False)
                    break
            if combined_cancel.is_set():
                raise failure('model_cancelled')
            await owned_sync(lambda: self.assert_current(snapshot))
            if not isinstance(result, dict) or not isinstance(result.get('output'), dict):
                raise failure('model_invalid_response')
            evidence = None
            if images:
                rows = [{k: row[k] for k in ('alias', 'image_id', 'source_id', 'source_version', 'role', 'sha256')} for row in manifest]
                digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
                evidence = {'status': 'completed', 'images': rows, 'manifest_sha256': digest}
                if result.get('encoded_body_sha256') is not None:
                    evidence['encoded_body_sha256'] = result['encoded_body_sha256']
            return {'output': result['output'], 'model_snapshot': asdict(snapshot),
                    'image_input_evidence': evidence, 'usage': result.get('usage', {}),
                    'request_id': result.get('request_id'), 'model': result.get('model'),
                    'elapsed_ms': int((time.monotonic() - started) * 1000), 'stop_reason': result.get('stop_reason')}
        try:
            remaining = (180 if purpose == 'reception' else 30) if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise failure('model_timeout')
            operation_deadline = time.monotonic() + remaining
            if lifecycle is not None:
                lifecycle.bind(operation_deadline)
            completion = {'on_complete': _owned_input.transfer()} if _owned_input is not None else {}
            result = runtime.run(purpose, operation, cancel=combined_cancel, heavy=bool(images),
                                 deadline=remaining, on_abandon=local_cancel.set, **completion)
            with self._lock:
                if combined_cancel.is_set():
                    raise failure('model_cancelled')
                self.assert_current(snapshot)
            return result
        finally:
            with self._lock:
                self._active[purpose].discard(local_cancel)

    def close(self):
        with self._lock:
            self._closed = True
            for events in self._active.values():
                for event in events:
                    event.set()
            self._credentials.clear()
            self._settings = None
            self._authority = None
            runtime = self._runtime
        if runtime is not None:
            async def finish():
                if self._transport is not None:
                    await self._transport.aclose()
                    self._transport = None
            runtime.close(finish)
