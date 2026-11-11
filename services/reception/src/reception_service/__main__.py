"""UTF-8 JSON-lines transport. EOF and shutdown revoke before waiting on I/O."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .errors import ServiceError
from .service import ReceptionService


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        if '--maintenance' in sys.argv or '--inspect-launch-owner' in sys.argv:
            sys.stdout.write(json.dumps({'status': 'refused', 'code': 'invalid_arguments',
                                         'message': '数据维护参数无效'}, ensure_ascii=True) + '\n')
            raise SystemExit(2)
        super().error(message)


def main():
    parser = _Parser(description='客户接待工作台 Python 服务')
    parser.add_argument('--mode', choices=('demo', 'live'), default='demo')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AlibabaReceptionWorkbenchV2')
    parser.add_argument('--seller-id', default='')
    parser.add_argument('--workctl', default='')
    parser.add_argument('--maintenance', choices=('prepare', 'inspect', 'backup', 'migrate', 'restore', 'open', 'recover-profile'))
    parser.add_argument('--source')
    parser.add_argument('--destination')
    parser.add_argument('--archive')
    parser.add_argument('--backup-dir')
    parser.add_argument('--allow-new', action='store_true')
    parser.add_argument('--inspect-launch-owner', action='store_true')
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    output_lock = threading.Lock()

    def emit(payload):
        line = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        with output_lock:
            sys.stdout.write(line + '\n')
            sys.stdout.flush()

    if args.inspect_launch_owner:
        if (args.maintenance or args.seller_id or args.workctl or args.source or args.destination
                or args.archive or args.backup_dir or args.allow_new or '--data-dir' not in sys.argv):
            emit({'status': 'unknown'})
            raise SystemExit(2)
        from .launch_ownership import inspect_launch_ownership
        emit(inspect_launch_ownership(args.data_dir))
        return

    if args.maintenance:
        from .storage_maintenance import run_maintenance
        from .storage_guard import StorageError
        from .profile_recovery import ProfileRecoveryError, recover_created_profile
        try:
            if args.maintenance == 'recover-profile':
                if (not all((args.source, args.seller_id, args.destination, args.backup_dir))
                        or args.archive or args.allow_new or args.workctl or '--data-dir' in sys.argv):
                    emit({'status': 'refused', 'code': 'invalid_arguments', 'message': '数据维护参数无效'})
                    raise SystemExit(2)
                program = (Path(sys.executable).resolve().parents[2] if getattr(sys, 'frozen', False)
                           else Path(__file__).resolve().parents[4])
                result = recover_created_profile(args.source, args.seller_id, args.destination,
                                                 args.backup_dir, program_directory=program)
            else:
                result = run_maintenance(args.maintenance, args.source, args.destination,
                                         args.archive, args.backup_dir, allow_new=args.allow_new)
        except Exception as exc:
            messages = {
                'storage_path_too_long': '数据目录路径过长，请选择较短的目录。',
                'storage_busy': '工作区正在使用，请正常退出该商家的旧窗口后重试。',
                'storage_backup_failed': '启动恢复备份未完成，请保留原目录并检查备份位置。',
                'storage_recovery_failed': '启动恢复校验未通过，请保留原目录和恢复快照。',
                'storage_recovery_incomplete': '启动恢复尚未完成，请保留原目录和恢复快照以便核查。',
                'storage_recovery_required': '工作区需要核查关闭状态，请保留原数据并使用原版本恢复。',
                'storage_recovery_unavailable': '当前环境不支持安全启动恢复，请保留原数据。',
                'storage_upgrade_requires_clean': '升级前需要正常关闭并备份，请先使用旧版本打开工作区并正常退出后重试。',
            }
            code = getattr(exc, 'code', None) if isinstance(exc, StorageError) else None
            if isinstance(exc, StorageError) and str(exc) == 'Upgrade requires clean shutdown and a safety backup; recover the workspace with the previous version first':
                code = 'storage_upgrade_requires_clean'
            if isinstance(exc, ProfileRecoveryError):
                code = exc.code
                message = '商家首次初始化恢复未完成，请保留原目录并检查恢复信息。'
            else:
                code = code if code in messages else 'maintenance_refused'
                message = messages.get(code, '数据维护未完成，请检查目录、备份及工作区状态。')
            payload = {'status': 'refused', 'code': code, 'message': message}
            if isinstance(exc, ProfileRecoveryError) and getattr(exc, 'backup', None):
                payload['recovery_backup'] = exc.backup
            emit(payload)
            raise SystemExit(1)
        emit(result)
        return

    service = ReceptionService(args.data_dir, args.mode, args.seller_id, emit=emit)
    if args.workctl:
        service.save_settings(workctl_path=args.workctl)

    def terminate(signum, frame):
        service.call('shutdown')
        raise SystemExit(0)

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    try:
        for line in sys.stdin:
            request_id = None
            try:
                if len(line) > 2_000_000:
                    raise ServiceError('request_too_large', '请求超过大小限制', stage='protocol')
                request = json.loads(line)
                if not isinstance(request, dict) or set(request) - {'id', 'method', 'params'}:
                    raise ServiceError('invalid_request', '请求结构无效', stage='protocol')
                request_id = request.get('id')
                if type(request_id) not in (str, int) or not isinstance(request.get('method'), str):
                    raise ServiceError('invalid_request', '请求编号或方法无效', stage='protocol')
                result = service.call(request['method'], request.get('params', {}))
                emit({'id': request_id, 'result': result})
                if request['method'] == 'shutdown':
                    break
            except (json.JSONDecodeError, UnicodeError):
                emit({'id': request_id, 'error': ServiceError('invalid_json', '请求必须是 UTF-8 JSON', stage='protocol').to_dict()})
            except ServiceError as error:
                emit({'id': request_id, 'error': error.to_dict()})
            except Exception:
                emit({'id': request_id, 'error': ServiceError('internal_error', '业务操作未完成', stage='service').to_dict()})
    finally:
        service.close()


if __name__ == '__main__':
    main()
