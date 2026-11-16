"""Account-owned, source-bound WhatsApp image transfer and resolution."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
import uuid

from .errors import ServiceError
from .image_assets import decode_image, ImageAssetError, MAX_BYTES

_CHUNK = 256 * 1024
_TOTAL = 24 * 1024 * 1024


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _error(code='source_changed'):
    raise ServiceError(code, 'WhatsApp 图片来源或传输已变化', stage='whatsapp_account')


class AccountMediaMixin:
    def _init_account_media(self):
        self._account_media_transfers = {}
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS account_media (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, epoch TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL, source_version TEXT NOT NULL, segment_id TEXT NOT NULL, status TEXT NOT NULL, mime TEXT, sha256 TEXT, data BLOB)')

    def _account_media_drop(self, transfer_id):
        transfer = self._account_media_transfers.pop(transfer_id, None)
        if transfer:
            transfer['timer'].cancel()
        return transfer

    def _cancel_account_media(self):
        """Call before closing the repository and on every connection change."""
        with self.repo.lock:
            for transfer_id in list(self._account_media_transfers):
                self._account_media_drop(transfer_id)

    def _account_media_source(self, account, epoch, chat, message, version):
        self._identity(account, epoch)
        row = self.repo.get('message', _key(account, message))
        binding = self.repo.get('binding', _key(account, chat))
        if (not row or row.get('account_id') != account or row.get('chat_id') != chat
                or row.get('id') != message or row.get('has_image') is not True
                or type(row.get('from_me')) is not bool or type(row.get('timestamp_ms')) is not int
                or version != _key(account, epoch, chat, message, str(row['timestamp_ms']))
                or not binding or not row.get('segment_id') or binding.get('segment_id') != row['segment_id']):
            _error()
        return row

    def _account_media_terminate(self, transfer_id, identity):
        transfer = self._account_media_transfers.get(transfer_id)
        if not transfer or transfer['identity'] != identity:
            return
        try:
            self._account_media_mark_failed(identity)
            self._changed()
        finally:
            self._account_media_drop(transfer_id)

    def _account_media_expire(self, transfer_id, identity):
        with self.repo.lock:
            try:
                self._account_media_terminate(transfer_id, identity)
            except Exception:
                # A closed repository or changed source cannot receive a late write.
                pass

    def _account_media_mark_failed(self, identity):
        account, epoch, chat, message, version = identity
        row = self._account_media_source(*identity)
        media_id = _key(*identity)
        self.repo.db.execute('INSERT INTO account_media (id,account_id,epoch,chat_id,message_id,source_version,segment_id,status) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status WHERE account_media.status=\'pending\'',
                             (media_id, account, epoch, chat, message, version, row['segment_id'], 'failed'))

    def _wa_media_begin(self, account_id, connection_epoch, chat_id, message_id, source_version, mime_type):
        with self.repo.lock:
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            row = self._account_media_source(*identity)
            if mime_type not in ('image/png', 'image/jpeg', 'image/webp', 'application/octet-stream'):
                _error('invalid_params')
            for tid, transfer in list(self._account_media_transfers.items()):
                if transfer['deadline'] <= time.monotonic() or transfer['identity'][:2] != (self.account, self.epoch):
                    try:
                        self._account_media_terminate(tid, transfer['identity'])
                    except ServiceError:
                        pass
            if len(self._account_media_transfers) >= 4 or any(t['identity'] == identity for t in self._account_media_transfers.values()):
                _error('transfer_unavailable')
            media_id = _key(*identity)
            existing = self.repo.db.execute('SELECT status FROM account_media WHERE id=?', (media_id,)).fetchone()
            if existing and existing[0] == 'ready':
                _error('transfer_unavailable')
            self.repo.db.execute('INSERT INTO account_media (id,account_id,epoch,chat_id,message_id,source_version,segment_id,status) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status',
                                 (media_id, account_id, connection_epoch, chat_id, message_id, source_version, row['segment_id'], 'pending'))
            tid = uuid.uuid4().hex
            timer = threading.Timer(60, self._account_media_expire, args=(tid, identity))
            timer.daemon = True
            self._account_media_transfers[tid] = {'identity': identity, 'mime': mime_type, 'data': bytearray(), 'deadline': time.monotonic() + 60, 'timer': timer}
            timer.start()
            return {'transfer_id': tid}

    def _account_media_transfer(self, transfer_id, identity):
        transfer = self._account_media_transfers.get(transfer_id)
        if not transfer or transfer['identity'] != identity:
            _error('transfer_unavailable')
        if time.monotonic() >= transfer['deadline']:
            self._account_media_terminate(transfer_id, identity)
            _error('transfer_unavailable')
        try:
            self._account_media_source(*identity)
        except ServiceError:
            self._account_media_drop(transfer_id)
            raise
        return transfer

    def _wa_media_chunk(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, offset, data_base64):
        with self.repo.lock:
            transfer = self._account_media_transfer(transfer_id, (account_id, connection_epoch, chat_id, message_id, source_version))
            try:
                if type(offset) is not int or offset != len(transfer['data']) or not isinstance(data_base64, str) or len(data_base64) > ((_CHUNK + 2) // 3) * 4:
                    raise ValueError()
                data = base64.b64decode(data_base64, validate=True)
                if not data or len(data) > _CHUNK or len(transfer['data']) + len(data) > MAX_BYTES or sum(len(t['data']) for t in self._account_media_transfers.values()) + len(data) > _TOTAL:
                    raise ValueError()
                transfer['data'].extend(data)
                return {'next_offset': len(transfer['data'])}
            except (ValueError, binascii.Error):
                self._account_media_terminate(transfer_id, transfer['identity'])
                _error('invalid_media')

    def _wa_media_finish(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, size, sha256):
        with self.repo.lock:
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            transfer = self._account_media_transfer(transfer_id, identity)
            try:
                data = bytes(transfer['data'])
                if type(size) is not int or size != len(data) or not isinstance(sha256, str) or hashlib.sha256(data).hexdigest() != sha256:
                    raise ValueError()
                image = decode_image(data, transfer['mime'])
                row = self._account_media_source(*identity)
                media_id = _key(*identity)
                result = self.repo.db.execute("UPDATE account_media SET status='ready',mime=?,sha256=?,data=? WHERE id=? AND status='pending' AND segment_id=?", (image['mime'], sha256, data, media_id, row['segment_id']))
                if result.rowcount != 1:
                    _error()
                self._changed()
                # Only an already-pending live message can wake the reply scheduler.
                conversation = self._conversation(chat_id)
                if (not row.get('is_history') and not row['from_me'] and conversation.get('pending')
                        and conversation.get('pending_message_id') == message_id):
                    schedule = getattr(self, '_schedule_account_replies', None)
                    if callable(schedule):
                        schedule()
                return {'status': 'ready', 'media_id': media_id, 'sha256': sha256}
            except (ValueError, ImageAssetError):
                self._account_media_terminate(transfer_id, identity)
                _error('invalid_media')
            finally:
                self._account_media_drop(transfer_id)

    def _wa_media_fail(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, error_code):
        with self.repo.lock:
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            transfer = self._account_media_transfers.get(transfer_id)
            if transfer_id and not transfer:
                self._account_media_source(*identity)
                active = any(t['identity'] == identity for t in self._account_media_transfers.values())
                status = self.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*identity),)).fetchone()
                if not active and status and status[0] == 'failed':
                    return {'status': 'failed'}
                _error('transfer_unavailable')
            if (transfer and transfer['identity'] != identity) or (not transfer_id and any(t['identity'] == identity for t in self._account_media_transfers.values())):
                _error('transfer_unavailable')
            if transfer:
                self._account_media_terminate(transfer_id, identity)
            else:
                self._account_media_mark_failed(identity)
                self._changed()
            return {'status': 'failed'}

    def _account_image_context(self, chat_id, segment_id):
        with self.repo.lock:
            self._identity(self.account, self.epoch)
            binding = self.repo.get('binding', _key(self.account, chat_id))
            if not binding or binding.get('segment_id') != segment_id:
                _error()
            rows = self.repo.list_owned('message', self.account, chat_id, segment_id)
            rows.sort(key=lambda r: (r['timestamp_ms'], r['id']))
            refs, statuses = [], []
            for row in rows[-30:]:
                if row.get('has_image') is not True or row.get('from_me') is not False:
                    continue
                version = _key(self.account, self.epoch, chat_id, row['id'], str(row['timestamp_ms']))
                media_id = _key(self.account, self.epoch, chat_id, row['id'], version)
                asset = self.repo.db.execute('SELECT status,sha256 FROM account_media WHERE id=? AND segment_id=?', (media_id, segment_id)).fetchone()
                source_id = 'wa_' + _key(self.account, row['id'])[:20]
                media = {'status': asset[0] if asset else 'pending', 'source_version': version}
                if asset and asset[0] == 'ready':
                    media.update(media_id=media_id, sha256=asset[1])
                    refs.append({'source_id': source_id, 'source_version': version, 'image_id': media_id, 'sha256': asset[1], 'role': 'buyer'})
                statuses.append({'source_id': source_id, 'media': media})
            return refs, statuses

    def _account_resolve_image(self, ref, account_id, connection_epoch, chat_id):
        with self.repo.lock:
            self._identity(account_id, connection_epoch)
            if not isinstance(ref, dict) or ref.get('role') != 'buyer':
                _error()
            asset = self.repo.db.execute('SELECT account_id,epoch,chat_id,message_id,source_version,segment_id,status,mime,sha256,data FROM account_media WHERE id=?', (ref.get('image_id'),)).fetchone()
            if not asset:
                _error()
            account, epoch, chat, message, version, segment, status, mime, digest, data = asset
            if ((account, epoch, chat) != (account_id, connection_epoch, chat_id) or status != 'ready'
                    or ref.get('source_version') != version or ref.get('sha256') != digest
                    or ref.get('source_id') != 'wa_' + _key(account, message)[:20]):
                _error()
            row = self._account_media_source(account, epoch, chat, message, version)
            if row['segment_id'] != segment or row['from_me'] is not False or not isinstance(data, bytes):
                _error()
            try:
                image = decode_image(data, mime)
                if image['sha256'] != digest:
                    raise ValueError()
            except (ValueError, ImageAssetError):
                _error()
            return {'sha256': digest, 'mime': image['mime'], 'original_b64': base64.b64encode(data).decode('ascii')}
