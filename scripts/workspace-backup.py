"""Offline backup/restore entrypoint for managed reception data roots.

Run with the packaged backend environment or the project's Python venv, using -I.
No live integration, service, credentials, or authorization is initialized.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/reception/src'))
from reception_service.workspace_backup import backup_workspace, restore_workspace
from reception_service.legacy_workspace_backup import LegacyShutdownEvidence, export_legacy_workspace


def main():
    parser = argparse.ArgumentParser(description='Offline backup and restore of a cleanly stopped managed reception workspace.')
    parser.add_argument('action', choices=('backup', 'restore', 'legacy-export'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--stopped-processes-evidence', help='Describe verification that all owning Electron, Python and workctl processes stopped')
    parser.add_argument('--stopped-operations-evidence', help='Describe verification that no operation is executing')
    args = parser.parse_args()
    try:
        if args.action == 'legacy-export':
            evidence = LegacyShutdownEvidence(args.stopped_processes_evidence, args.stopped_operations_evidence)
            target = export_legacy_workspace(args.source, args.destination, evidence=evidence)
        else:
            target = (backup_workspace if args.action == 'backup' else restore_workspace)(args.source, args.destination)
    except (OSError, RuntimeError, ValueError) as exc:
        message = 'Legacy export safety or validation check failed' if args.action == 'legacy-export' else str(exc)
        print(json.dumps({'status': 'refused', 'action': args.action, 'message': message}, ensure_ascii=True))
        return 1
    if args.action == 'legacy-export':
        manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        print(json.dumps({'status': 'completed', 'action': args.action, 'files': len(manifest['files']), 'automatic_authority_restored': False}))
    else:
        print(json.dumps({'status': 'completed', 'action': args.action, 'destination': str(target), 'automatic_authority_restored': False}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
