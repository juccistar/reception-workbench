"""Strict, offline command line for account backups and restores."""
from __future__ import annotations

import argparse
import json
import sys


class _InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _InvalidArguments()


def run_cli(argv):
    def emit(payload):
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument('--whatsapp-maintenance', required=True, choices=('backup', 'restore'))
    parser.add_argument('--source')
    parser.add_argument('--archive')
    parser.add_argument('--destination', required=True)
    try:
        # Reject duplicate selectors and path options, including their = forms.
        flags = [arg.split('=', 1)[0] for arg in argv if arg.startswith('--')]
        if len(flags) != len(set(flags)):
            raise _InvalidArguments()
        args = parser.parse_args(argv)
        if not args.destination or (args.whatsapp_maintenance == 'backup' and (not args.source or args.archive is not None)) or (args.whatsapp_maintenance == 'restore' and (not args.archive or args.source is not None)):
            raise _InvalidArguments()
    except _InvalidArguments:
        emit({'status': 'refused', 'code': 'invalid_arguments', 'message': '账号维护参数无效'})
        return 2

    try:
        from .whatsapp_account_maintenance import backup_account, restore_account
        if args.whatsapp_maintenance == 'backup':
            backup_account(args.source, args.destination)
        else:
            restore_account(args.archive, args.destination)
    except Exception:
        emit({'status': 'refused', 'code': 'account_maintenance_refused', 'message': '账号维护未完成，请检查目录和工作区状态。'})
        return 1
    emit({'status': 'ok', 'operation': args.whatsapp_maintenance, 'reception_enabled': False})
    return 0
