"""Verify the current portable ZIP in place without extracting or executing files."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(stream):
    value = hashlib.sha256()
    count = 0
    while chunk := stream.read(1024 * 1024):
        value.update(chunk)
        count += len(chunk)
    return count, value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', type=Path, default=ROOT / 'artifacts/delivery.json')
    parser.add_argument('--expected-version', default=None)
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()
    delivery_file = args.delivery if args.delivery.is_absolute() else ROOT / args.delivery
    delivery = json.loads(delivery_file.read_text(encoding='utf-8'))
    archive = Path(delivery['archive']).resolve()
    assert any(archive.is_relative_to((ROOT / name).resolve()) for name in ('artifacts', 'relase'))
    assert Path(delivery['archive']).absolute() == archive, 'Archive path must not traverse links'
    with archive.open('rb') as stream:
        archive_bytes, archive_hash = digest(stream)
    assert archive_hash == delivery['archive_sha256']
    prefix = 'ReceptionWorkbench/'
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        names = [entry.filename for entry in entries]
        assert len(names) == len(set(name.casefold() for name in names))
        for entry in entries:
            name = entry.filename
            assert name.startswith(prefix) and '\\' not in name
            assert not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts
            assert not entry.is_dir() and not stat.S_ISLNK(entry.external_attr >> 16)
        manifest_info = bundle.getinfo(prefix + 'manifest.json')
        assert manifest_info.file_size <= 1024 * 1024
        manifest = json.loads(bundle.read(manifest_info))
        expected_version = args.expected_version or json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
        assert len(expected_version) <= 40 and all(part.isdigit() for part in expected_version.split('.')) and len(expected_version.split('.')) == 3
        assert manifest['version'] == expected_version
        indexed = manifest['files']
        expected_names = [prefix + entry['path'] for entry in indexed] + [prefix + 'manifest.json']
        assert len(expected_names) == len(set(expected_names))
        assert set(names) == set(expected_names)
        for entry in indexed:
            with bundle.open(prefix + entry['path']) as stream:
                byte_count, value = digest(stream)  # Reading to EOF also checks the entry CRC.
            assert byte_count == entry['bytes'] and value == entry['sha256'], entry['path']
    result = {'passed': True, 'version': expected_version, 'entries': len(entries),
              'manifest_files': len(indexed), 'all_crcs_verified': True,
              'all_file_hashes_verified': True, 'archive_bytes': archive_bytes,
              'archive_sha256': archive_hash}
    output = args.output or ROOT / 'artifacts/verification' / ('release-' + expected_version + '-zip.json')
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
