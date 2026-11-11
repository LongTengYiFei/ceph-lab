#!/usr/bin/env python3
"""Small end-to-end tiering check, including direct reads that bypass overlay.

Creates only unique objmeta-validation-* objects and removes them afterwards.
The existing pools, tier mode, limits and flags are not changed.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

import rados

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
BYPASS = rados.LIBRADOS_OPERATION_IGNORE_CACHE | rados.LIBRADOS_OPERATION_IGNORE_OVERLAY


def compare_direct(ioctx, oid, data):
    with rados.ReadOpCtx() as operation:
        operation.cmpext(data)
        ioctx.operate_read_op(operation, oid, BYPASS)


def absent_direct(ioctx, oid, payload):
    try:
        compare_direct(ioctx, oid, payload)
    except rados.ObjectNotFound:
        return True
    return False


def cache_command(action, oid, *, missing_ok=False):
    result = subprocess.run([str(LAB / 'rados.sh'), '-p', 'objmeta-cache', action, oid],
                            capture_output=True, text=True, timeout=120)
    if result.returncode and not (missing_ok and 'No such file or directory' in result.stderr):
        raise RuntimeError(f'{action}/{oid}: {result.stderr}')


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    records = []
    cluster = rados.Rados(conffile='/etc/ceph/ceph.conf', name='client.admin')
    cluster.connect()
    data = cluster.open_ioctx('objmeta-data')
    hot = cluster.open_ioctx('objmeta-cache')
    created = {}
    result = {'started_utc': datetime.now(timezone.utc).isoformat(), 'objects': records, 'passed': False}
    try:
        for index in range(8):
            oid = f'objmeta-validation-{stamp}-{index}'
            seed = hashlib.shake_256((oid + ':seed').encode()).digest(256 * 1024)
            payload = hashlib.shake_256((oid + ':updated').encode()).digest(256 * 1024)
            created[oid] = payload
            # A cold write may proxy to the backend and then promote in Quincy.
            # Establish a cached, clean object before testing a dirty overwrite.
            data.write_full(oid, seed)
            assert data.read(oid, len(seed)) == seed
            compare_direct(hot, oid, seed)
            cache_command('cache-flush', oid)
            compare_direct(data, oid, seed)
            data.write_full(oid, payload)
            assert data.read(oid, len(payload)) == payload
            compare_direct(hot, oid, payload)
            # IGNORE_CACHE | IGNORE_OVERLAY proves this is the actual backend,
            # not another request silently served by the dirty cached version.
            compare_direct(data, oid, seed)
            cache_command('cache-flush', oid)
            compare_direct(data, oid, payload)
            cache_command('cache-evict', oid)
            assert absent_direct(hot, oid, payload)
            # The normal backend-pool interface still reads correctly after eviction.
            promoted = False
            for attempt in range(12):
                assert data.read(oid, len(payload)) == payload
                try:
                    compare_direct(hot, oid, payload)
                    promoted = True
                    break
                except rados.ObjectNotFound:
                    time.sleep(0.25)
            assert promoted, f'{oid} was not promoted back to the cache'
            records.append({'object': oid, 'bytes': len(payload),
                            'sha256': hashlib.sha256(payload).hexdigest(),
                            'cached_after_write': True, 'backend_has_previous_value_before_flush': True,
                            'backend_matches_after_flush': True, 'cache_absent_after_evict': True,
                            'overlay_read_matches': True, 'promoted_after_read': promoted})
            print(f'{oid}: write/read/flush/evict/promote OK', flush=True)
        result['passed'] = True
    finally:
        cleanup = []
        cleanup_errors = []
        for oid, payload in created.items():
            try:
                try:
                    data.remove_object(oid)
                except rados.ObjectNotFound:
                    pass
                cache_command('cache-flush', oid, missing_ok=True)
                cache_command('cache-evict', oid, missing_ok=True)
                assert absent_direct(data, oid, payload) and absent_direct(hot, oid, payload)
                cleanup.append(oid)
            except Exception as error:
                cleanup_errors.append({'object': oid, 'error': str(error)})
        result['cleaned_objects'] = cleanup
        result['cleanup_errors'] = cleanup_errors
        result['passed'] = result['passed'] and not cleanup_errors
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (HERE / 'io-validation.json').write_text(json.dumps(result, indent=2) + '\n')
        data.close()
        hot.close()
        cluster.shutdown()
        if cleanup_errors:
            raise RuntimeError(f'object cleanup failed: {cleanup_errors}')


if __name__ == '__main__':
    main()
