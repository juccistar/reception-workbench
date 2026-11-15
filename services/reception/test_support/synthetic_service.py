"""Isolated JSONL fixture service, never included in the product package."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.errors import ServiceError
from reception_test_support.service import create_synthetic_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    lock = threading.Lock()
    def emit(value):
        with lock:
            print(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False), flush=True)
    service = create_synthetic_service(args.data_dir, emit=emit)
    try:
        for line in sys.stdin:
            request_id = None
            try:
                if len(line) > 8_400_000:
                    raise ValueError('request too large')
                request = json.loads(line)
                if not isinstance(request, dict) or set(request) - {'id', 'method', 'params'} or type(request.get('id')) not in (str, int) or not isinstance(request.get('method'), str):
                    raise ValueError('invalid request')
                request_id = request['id']
                result = service.call(request['method'], request.get('params', {}))
                emit({'id': request_id, 'result': result})
                if request['method'] == 'shutdown':
                    break
            except ServiceError as error:
                emit({'id': request_id, 'error': error.to_dict()})
            except Exception:
                emit({'id': request_id, 'error': {'code': 'invalid_request', 'message': 'synthetic request failed'}})
    finally:
        service.close()


if __name__ == '__main__':
    main()
