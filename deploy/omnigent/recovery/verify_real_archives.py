#!/usr/bin/env python3
"""Optional real-archive verification, separate from portable unittest discovery.

    python3 -B verify_real_archives.py ARCHIVE_DIR

ARCHIVE_DIR is required and must contain both named archives below. There is no
private default path or environment fallback. Temporary restored trees stay
beside this script and are deleted. Missing inputs fail; there are no skips.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from safe_restore import RestoreError, restore_archive

CASES = (
    ('served-reconstruction.tar.gz',
     'b058b9e129c81ff8aecaea8a24eae4504a528f36359ba0f7a4e9fad585007f0a',
     83527882, '646e56ce02cfa7c9c8be1269540f65068419b4a2c6d1caca87e72cb9d3e7684a'),
    ('rollback-serving-tree.tar.gz',
     'c73b2eb26184df45086aa3d0222e9175d895dc71526039808765b9379e193878',
     83528022, '192d0fe850366e92041f659a4e269ac6120f51ef6b4e75e0850e2a8d436b20bd'),
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def verify(archive_dir):
    for name, *_ in CASES:
        require((archive_dir / name).is_file(), 'REQUIRED_ARCHIVE_MISSING:' + name)
    receipts, inventories = [], []
    with tempfile.TemporaryDirectory(prefix='.real-restore-', dir=Path(__file__).resolve().parent) as tmp:
        root = Path(tmp)
        active = root / 'active'
        active.mkdir()
        sentinel = active / 'sentinel'
        sentinel.write_bytes(b'active remains unchanged\n')
        for i, (name, digest, size, index_sha) in enumerate(CASES):
            destination = root / f'restored-{i}'
            receipt = restore_archive(archive_dir / name, digest, destination)
            require(receipt['file_count'] == 1559, 'FILE_COUNT')
            require(receipt['expanded_bytes'] == size, 'EXPANDED_BYTES')
            require(hashlib.sha256((destination / 'index.html').read_bytes()).hexdigest() == index_sha,
                    'INDEX_SHA256')
            require(sum(path.is_file() for path in destination.rglob('*')) == 1559, 'DISK_FILE_COUNT')
            if i == 0:
                require(receipt['inventory_sha256'] ==
                        'f1a1915e8e1939bf19b9af2394d230f6656ae395657d4cfa19c61e9df9d08e34',
                        'TRUSTED_RECONSTRUCTION_INVENTORY_SHA256')
            require(sentinel.read_bytes() == b'active remains unchanged\n', 'ACTIVE_SENTINEL_CHANGED')
            require(not list(root.glob('.safe-restore-*')), 'STAGING_NOT_CLEANED')
            inventories.append(receipt.pop('inventory'))
            receipt.pop('destination')  # Temporary path is deliberately not a durable artifact.
            receipts.append({'archive': name, 'index_sha256': index_sha, **receipt})
        require(set(inventories[0]) == set(inventories[1]), 'ARCHIVE_PATH_SETS_DIFFER')
        changed = sorted(name for name in inventories[0] if inventories[0][name] != inventories[1][name])
        require(changed == ['index.html'], 'UNEXPECTED_ROLLBACK_DELTA')
    return {'passed': True, 'archive_cases': 2, 'changed_files': changed,
            'unchanged_non_index_files': 1558, 'archives': receipts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive_dir', type=Path)
    args = parser.parse_args()
    try:
        receipt = verify(args.archive_dir)
    except (RestoreError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
