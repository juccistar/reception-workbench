"""Bounded private WhatsApp image transfer and source-bound asset resolution."""
import base64
import binascii
import hashlib
import time
import uuid
import threading

from .errors import ServiceError
from .image_assets import decode_image, ImageAssetError, MAX_BYTES

CHUNK = 256 * 1024


class WhatsAppMediaMixin:
    def _wa_drop_transfer(self, transfer_id):
        transfer = self._wa_transfers.pop(transfer_id, None)
        if transfer and transfer.get('timer'):
            transfer['timer'].cancel()
        return transfer

    def _wa_expire_transfer(self, transfer_id, identity):
        with self._lock:
            transfer = self._wa_transfers.get(transfer_id)
            if not transfer or transfer['identity'] != identity:
                return
            self._wa_drop_transfer(transfer_id)
            if self._closing or self._stop.is_set():
                return
            try:
                key, row = self._wa_media_source(*identity)
                if row['media'].get('status') == 'pending':
                    row['media'] = {'status': 'failed', 'source_version': identity[-1], 'error_code': 'media_unavailable'}
                    self.repo.put('wa_message', key, row)
                    self._changed()
            except ServiceError:
                pass

    def _wa_media_source(self, account_id, connection_epoch, chat_id, message_id, source_version):
        from .whatsapp import _key
        self._wa_check_connection(account_id, connection_epoch)
        key = _key(self.seller_id, account_id, message_id)
        row = self.repo.get('wa_message', key)
        if (not row or row.get('seller_id') != self.seller_id or row.get('account_id') != account_id
                or row.get('chat_id') != chat_id or row.get('message_id') != message_id
                or source_version != _key(account_id, connection_epoch, chat_id, message_id, str(row.get('timestamp_ms')))
                or (row.get('media') or {}).get('source_version') != source_version):
            raise ServiceError('source_changed', 'WhatsApp 图片来源已变化')
        return key, row

    def _wa_media_begin(self, account_id, connection_epoch, chat_id, message_id, source_version, mime_type):
        with self._lock:
            _, row = self._wa_media_source(account_id, connection_epoch, chat_id, message_id, source_version)
            if row['media'].get('status') != 'pending' or mime_type not in ('image/png', 'image/jpeg', 'image/webp', 'application/octet-stream'):
                raise ServiceError('invalid_params', 'WhatsApp 图片不可用')
            transfers = self._wa_transfers
            now = time.monotonic()
            for key in list(transfers):
                if transfers[key]['deadline'] <= now:
                    self._wa_expire_transfer(key, transfers[key]['identity'])
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            if len(transfers) >= 4 or any(t['identity'] == identity for t in transfers.values()):
                raise ServiceError('transfer_unavailable', 'WhatsApp 图片传输繁忙')
            transfer_id = uuid.uuid4().hex
            timer = threading.Timer(60, self._wa_expire_transfer, args=(transfer_id, identity))
            timer.daemon = True
            transfers[transfer_id] = dict(identity=identity, mime=mime_type, data=bytearray(), deadline=now + 60, timer=timer)
            timer.start()
            return {'transfer_id': transfer_id}

    def _wa_media_transfer(self, transfer_id, identity):
        transfer = self._wa_transfers.get(transfer_id)
        if not transfer or transfer['identity'] != identity:
            raise ServiceError('transfer_unavailable', 'WhatsApp 图片传输已失效')
        if time.monotonic() >= transfer['deadline']:
            self._wa_expire_transfer(transfer_id, identity)
            raise ServiceError('transfer_unavailable', 'WhatsApp 图片传输已失效')
        try:
            self._wa_media_source(*identity)
        except ServiceError:
            self._wa_drop_transfer(transfer_id)
            raise
        return transfer

    def _wa_media_chunk(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, offset, data_base64):
        with self._lock:
            t = self._wa_media_transfer(transfer_id, (account_id, connection_epoch, chat_id, message_id, source_version))
            try:
                if type(offset) is not int or offset != len(t['data']) or not isinstance(data_base64, str) or len(data_base64) > ((CHUNK + 2) // 3) * 4:
                    raise ValueError()
                data = base64.b64decode(data_base64, validate=True)
                if not data or len(data) > CHUNK or len(t['data']) + len(data) > MAX_BYTES or sum(len(x['data']) for x in self._wa_transfers.values()) + len(data) > 24 * 1024 * 1024:
                    raise ValueError()
                t['data'].extend(data)
                return {'next_offset': len(t['data'])}
            except (ValueError, binascii.Error):
                self._wa_drop_transfer(transfer_id)
                raise ServiceError('invalid_media', 'WhatsApp 图片分块无效') from None

    def _wa_media_finish(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, size, sha256):
        with self._lock:
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            t = self._wa_media_transfer(transfer_id, identity)
            try:
                data = bytes(t['data'])
                if type(size) is not int or size != len(data) or not isinstance(sha256, str) or hashlib.sha256(data).hexdigest() != sha256:
                    raise ValueError()
                image = decode_image(data, t['mime'])
                key, row = self._wa_media_source(*identity)
                if row['media'].get('status') != 'pending':
                    raise ValueError()
                media_id = uuid.uuid4().hex
                asset = {'seller_id': self.seller_id, 'account_id': account_id, 'connection_epoch': connection_epoch, 'chat_id': chat_id, 'message_id': message_id, 'source_version': source_version, 'media_id': media_id, 'sha256': sha256, 'mime': image['mime'], 'original_b64': base64.b64encode(data).decode('ascii')}
                with self.repo.transaction():
                    self.repo.put('wa_media_asset', media_id, asset)
                    row['media'] = {'status': 'ready', 'source_version': source_version, 'media_id': media_id, 'sha256': sha256}
                    self.repo.put('wa_message', key, row)
                    self._schedule_whatsapp()
                self._changed()
                return {'status': 'ready', 'media_id': media_id, 'sha256': sha256}
            except (ValueError, ImageAssetError):
                raise ServiceError('invalid_media', 'WhatsApp 图片校验失败') from None
            finally:
                self._wa_drop_transfer(transfer_id)

    def _wa_media_fail(self, transfer_id, account_id, connection_epoch, chat_id, message_id, source_version, error_code):
        with self._lock:
            identity = (account_id, connection_epoch, chat_id, message_id, source_version)
            transfer = self._wa_transfers.get(transfer_id)
            if transfer and transfer['identity'] == identity:
                self._wa_drop_transfer(transfer_id)
            key, row = self._wa_media_source(*identity)
            if row['media'].get('status') == 'pending':
                row['media'] = {'status': 'failed', 'source_version': source_version, 'error_code': 'media_unavailable'}
                self.repo.put('wa_message', key, row)
            return {'status': 'failed'}

    def _wa_resolve_current_image(self, ref):
        with self._lock:
            asset = self.repo.get('wa_media_asset', ref.get('image_id'))
            if not asset:
                raise ServiceError('source_changed', 'WhatsApp 图片来源已变化')
            return self._wa_resolve_image(ref, self._wa_account, self._wa_epoch, asset['chat_id'])

    def _wa_resolve_image(self, ref, account_id, connection_epoch, chat_id):
        from .whatsapp import _key
        with self._lock:
            self._wa_check_connection(account_id, connection_epoch)
            asset = self.repo.get('wa_media_asset', ref.get('image_id'))
            if not asset or any(asset.get(k) != v for k, v in {'seller_id': self.seller_id, 'account_id': account_id, 'connection_epoch': connection_epoch, 'chat_id': chat_id, 'source_version': ref.get('source_version'), 'sha256': ref.get('sha256')}.items()):
                raise ServiceError('source_changed', 'WhatsApp 图片来源已变化')
            _, row = self._wa_media_source(account_id, connection_epoch, chat_id, asset['message_id'], asset['source_version'])
            if row.get('media', {}).get('media_id') != ref['image_id'] or ref.get('source_id') != 'wa_' + _key(account_id, asset['message_id'])[:20] or ref.get('role') != ('seller' if row['from_me'] else 'buyer'):
                raise ServiceError('source_changed', 'WhatsApp 图片来源已变化')
            return {k: asset[k] for k in ('sha256', 'mime', 'original_b64')}
