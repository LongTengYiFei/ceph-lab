#!/usr/bin/env python3
"""Serial baseline/snapshot workload; record logical RocksDB inputs on all OSDs."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import traceback

import rados

EXPERIMENT = Path(__file__).resolve().parent
LAB = Path(os.environ.get('CEPH_LAB_DIR', EXPERIMENT.parent))
BASE_COMMIT = 'b12291d110049b2f35e32e0de30d70e9a4c060d2'
HOST_OSDS = {'node1': [0, 1, 2, 3], 'node2': [4, 5, 6, 7], 'node3': [8, 9, 10, 11]}
METRICS = {
    'submitted_bytes': ('rocksdb', 'submitted_bytes'),
    'submitted_ops': ('rocksdb', 'submitted_ops'),
    'submitted_txns': ('rocksdb', 'submitted_txns'),
    'issued_deferred_write_bytes': ('bluestore', 'issued_deferred_write_bytes'),
    'write_pad_bytes': ('bluestore', 'write_pad_bytes'),
    'write_new': ('bluestore', 'write_new'),
    'allocated': ('bluestore', 'allocated'),
    'stored': ('bluestore', 'stored'),
    'bluefs_write_bytes': ('bluefs', 'write_bytes'),
    'bluefs_bytes_written_wal': ('bluefs', 'bytes_written_wal'),
    'bluefs_bytes_written_sst': ('bluefs', 'bytes_written_sst'),
}


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def ceph(*args, as_json=False):
    command = [str(LAB / 'ceph.sh'), *map(str, args)]
    if as_json:
        command += ['-f', 'json']
    result = subprocess.run(command, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.returncode}: {result.stderr}')
    return json.loads(result.stdout) if as_json else result.stdout.strip()


def clean_status():
    status = ceph('status', as_json=True)
    osds, pgs = status['osdmap'], status['pgmap']
    clean_pgs = sum(p['count'] for p in pgs.get('pgs_by_state', [])
                    if p['state_name'] == 'active+clean')
    return (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds'] == 12
            and clean_pgs == pgs.get('num_pgs', 0)), status


def wait_clean():
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        ok, status = clean_status()
        if ok:
            return status
        time.sleep(1)
    raise RuntimeError(f'cluster did not settle: {status}')


def probe(action):
    def host_probe(item):
        host, ids = item
        command = shlex.join(['sudo', '-n', '/usr/bin/python3',
                              str(LAB / 'Exp_SnapshotWriteAmplification' / 'snapwrite_osd_probe.py'), action,
                              *map(str, ids)])
        ssh_prefix = ['sudo', '-n', '-u', 'cyf'] if os.geteuid() == 0 else []
        result = subprocess.run([*ssh_prefix, 'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 '-p', '23579', f'cyf@{host}', command],
                                text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError(f'{host} probe: {result.stderr}')
        return json.loads(result.stdout)
    start_ns = time.time_ns()
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(host_probe, HOST_OSDS.items()))
    osds = {key: val for result in results for key, val in result.items()}
    assert set(osds) == {str(i) for i in range(12)}
    return {'start_ns': start_ns, 'end_ns': time.time_ns(), 'osds': osds}


def delta(before, after):
    per_osd = {}
    for osd_id in before['osds']:
        b, a = before['osds'][osd_id], after['osds'][osd_id]
        assert a['pid'] == b['pid'], f'osd.{osd_id} restarted during measurement'
        values = {name: a['perf'][group][key] - b['perf'][group][key]
                  for name, (group, key) in METRICS.items()}
        for name in ('submitted_bytes', 'submitted_ops', 'submitted_txns',
                     'issued_deferred_write_bytes'):
            assert values[name] >= 0, f'counter decreased: osd.{osd_id} {name}'
        per_osd[osd_id] = values
    return {'sum': {name: sum(o[name] for o in per_osd.values()) for name in METRICS},
            'per_osd': per_osd,
            'window_seconds': (after['end_ns'] - before['start_ns']) / 1e9}


def data_for(round_id, count):
    return [hashlib.shake_256(f'snapWrite-v1:{round_id}:{i}'.encode()).digest(4096)
            for i in range(count)]


def write_round(ioctx, names, payloads, concurrency):
    pending = deque()
    started = time.monotonic()
    checked = 0
    for name, data in zip(names, payloads):
        pending.append(ioctx.aio_write(name, data, 0))
        if len(pending) >= concurrency:
            completion = pending.popleft()
            completion.wait_for_complete()
            assert completion.get_return_value() == 0
            checked += 1
    while pending:
        completion = pending.popleft()
        completion.wait_for_complete()
        assert completion.get_return_value() == 0
        checked += 1
    ioctx.aio_flush()
    assert checked == len(names)
    return time.monotonic() - started


def verify(ioctx, names, payloads):
    indexes = sorted(set((0, len(names) - 1, *(i * len(names) // 16 for i in range(16)))))
    for i in indexes:
        assert ioctx.read(names[i], 4097, 0) == payloads[i], f'readback mismatch: {names[i]}'
    return len(indexes)


def idle_window(directory, label, seconds):
    before = probe('dump')
    save(directory / f'{label}-before.json', before)
    time.sleep(seconds)
    after = probe('dump')
    save(directory / f'{label}-after.json', after)
    return delta(before, after)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects', type=int, default=10000)
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--pg-num', type=int, default=32)
    parser.add_argument('--concurrency', type=int, default=32)
    parser.add_argument('--idle-seconds', type=float, default=5)
    parser.add_argument('--label', default='formal')
    parser.add_argument('--output-dir', type=Path, default=EXPERIMENT / 'output')
    parser.add_argument('--expected-osd-commit', help='Optional exact runtime commit check')
    args = parser.parse_args()
    assert args.objects > 0 and args.rounds > 0 and args.repetitions > 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = args.output_dir / f'{stamp}-{args.label}'
    directory.mkdir(parents=True, exist_ok=False)
    raw = directory / 'raw'
    raw.mkdir()
    if os.geteuid() == 0 and 'SUDO_UID' in os.environ:
        uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        for path in (directory.parent, directory, raw):
            os.chown(path, uid, gid)
    print(f'RESULT_DIR={directory}', flush=True)
    state = {'state': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
             'parameters': {**vars(args), 'output_dir': str(args.output_dir)},
             'base_commit': BASE_COMMIT, 'groups': [],
             'created_pools': [], 'round_results': [], 'cleanup': {}}
    save(directory / 'results.json', state)
    cluster = None
    changed_flags = []
    config_changed = False
    config_original = []
    try:
        state['initial_status'] = wait_clean()
        state['initial_pools'] = ceph('osd', 'pool', 'ls', as_json=True)
        versions = ceph('versions', as_json=True)
        assert len(versions['osd']) == 1, 'all OSDs must run the same instrumented version'
        osd_version = next(iter(versions['osd']))
        assert osd_version.startswith('ceph version 17.2.7'), osd_version
        if args.expected_osd_commit:
            assert args.expected_osd_commit in osd_version, osd_version
        state['source_commit'] = re.search(r'\(([0-9a-f]{40})\)', osd_version).group(1)
        state['versions'] = versions
        # Check the feature itself: applying the patch can produce a new commit ID.
        probe('dump')
        config_original = [r for r in ceph('config', 'dump', as_json=True)
                           if r['name'] == 'mon_allow_pool_size_one' and r['section'] == 'mon']
        state['original_size_one_config'] = config_original
        dump = ceph('osd', 'dump', as_json=True)
        flags = dump['flags'].split(',')
        state['original_flags'] = flags
        for flag in ('noscrub', 'nodeep-scrub'):
            if flag not in flags:
                ceph('osd', 'set', flag)
                changed_flags.append(flag)
        ceph('config', 'set', 'mon', 'mon_allow_pool_size_one', 'true')
        config_changed = True
        assert ceph('config', 'get', 'mon', 'mon_allow_pool_size_one') == 'true'
        cluster = rados.Rados(conffile='/etc/ceph/ceph.conf', rados_id='admin')
        cluster.connect()
        names = [f'obj-{i:08d}' for i in range(args.objects)]
        payloads = [data_for(r, args.objects) for r in range(args.rounds + 1)]
        save(directory / 'parameters.json', {**state['parameters'], 'object_bytes': 4096,
                                             'write_offset': 0, 'write_api': 'aio_write',
                                             'replica_size': 1, 'compression_mode': 'none',
                                             'counter_scope': 'all 12 OSDs',
                                             'data_generator': 'SHAKE256(snapWrite-v1:round:index)',
                                             'group_order': 'serial groups; odd repetition baseline first, even snapshot first'})
        for repetition in range(1, args.repetitions + 1):
            modes = ('baseline', 'snapshot') if repetition % 2 else ('snapshot', 'baseline')
            for mode in modes:
                prefix = f'rep{repetition:02d}-{mode}'
                pool = f'sw-{stamp.lower()}-{args.label}-r{repetition}-{mode}'
                assert pool not in cluster.list_pools(), f'pool already exists: {pool}'
                print(f'{prefix}: create pool {pool}', flush=True)
                ceph('osd', 'pool', 'create', pool, args.pg_num, args.pg_num,
                     '--autoscale-mode=off')
                assert cluster.wait_for_latest_osdmap() == 0
                pool_id = cluster.pool_lookup(pool)
                state['created_pools'].append({'name': pool, 'id': pool_id})
                save(directory / 'results.json', state)
                ceph('osd', 'pool', 'set', pool, 'size', '1', '--yes-i-really-mean-it')
                ceph('osd', 'pool', 'set', pool, 'min_size', '1')
                ceph('osd', 'pool', 'set', pool, 'compression_mode', 'none')
                ceph('osd', 'pool', 'application', 'enable', pool, 'rados')
                wait_clean()
                assert cluster.wait_for_latest_osdmap() == 0
                group = {'repetition': repetition, 'mode': mode, 'pool': pool,
                         'pool_id': pool_id, 'rounds': []}
                state['groups'].append(group)
                with cluster.open_ioctx(pool) as ioctx:
                    group['initial_load_seconds'] = write_round(ioctx, names, payloads[0], args.concurrency)
                    group['initial_readback_objects'] = verify(ioctx, names, payloads[0])
                    assert sum(1 for _ in ioctx.list_objects()) == args.objects
                    wait_clean()
                    group['pool_config'] = next(p for p in ceph('osd', 'pool', 'ls', 'detail', as_json=True)
                                                if p['pool_name'] == pool)
                    snapshot_id = None
                    if mode == 'snapshot':
                        before = probe('dump')
                        save(raw / f'{prefix}-snap-create-before.json', before)
                        ioctx.create_snap('s0')
                        assert cluster.wait_for_latest_osdmap() == 0
                        snapshot_id = ioctx.lookup_snap('s0').snap_id
                        time.sleep(1)
                        after = probe('dump')
                        save(raw / f'{prefix}-snap-create-after.json', after)
                        group['snapshot_create'] = delta(before, after)
                        group['snapshot_id'] = snapshot_id
                    group['idle_before'] = idle_window(raw, f'{prefix}-idle-before', args.idle_seconds)
                    save(raw / f'{prefix}-reset.json', probe('reset'))
                    before = probe('dump')
                    save(raw / f'{prefix}-round00.json', before)
                    for round_id in range(1, args.rounds + 1):
                        duration = write_round(ioctx, names, payloads[round_id], args.concurrency)
                        after = probe('dump')
                        save(raw / f'{prefix}-round{round_id:02d}.json', after)
                        measured = delta(before, after)
                        result = {'repetition': repetition, 'mode': mode, 'round': round_id,
                                  'client_ops': args.objects, 'client_bytes': args.objects * 4096,
                                  'workload_seconds': duration,
                                  'metadata_bytes_per_client_byte': measured['sum']['submitted_bytes'] / (args.objects * 4096),
                                  'delta': measured}
                        group['rounds'].append(result)
                        state['round_results'].append(result)
                        save(directory / 'results.json', state)
                        print(f'{prefix} round {round_id:02d}: bytes={measured["sum"]["submitted_bytes"]:,} '
                              f'ops={measured["sum"]["submitted_ops"]:,} '
                              f'deferred={measured["sum"]["issued_deferred_write_bytes"]:,} '
                              f'time={duration:.3f}s', flush=True)
                        before = after
                    group['head_readback_objects'] = verify(ioctx, names, payloads[-1])
                    if snapshot_id is not None:
                        with cluster.open_ioctx(pool) as snapctx:
                            snapctx.set_read(snapshot_id)
                            group['snapshot_readback_objects'] = verify(snapctx, names, payloads[0])
                    group['idle_after'] = idle_window(raw, f'{prefix}-idle-after', args.idle_seconds)
                    deadline = time.monotonic() + 90
                    while True:
                        stats = ioctx.get_stats()
                        expected_clones = args.objects if mode == 'snapshot' else 0
                        if stats['num_object_clones'] == expected_clones:
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f'clone count mismatch: {stats}')
                        time.sleep(1)
                    group['final_pool_stats'] = stats
                    group['sample_object_snapsets'] = subprocess.run(
                        [str(LAB / 'rados.sh'), '-p', pool, 'listsnaps', names[0]],
                        capture_output=True, text=True, timeout=30).stdout
                    save(directory / 'results.json', state)
                wait_clean()
        state['state'] = 'completed'
    except BaseException:
        state['state'] = 'failed'
        state['error'] = traceback.format_exc()
        raise
    finally:
        if cluster is not None:
            cluster.shutdown()
        # Delete only pools created by this run, verifying their exact pool IDs.
        for pool in reversed(state['created_pools']):
            try:
                existing = {p['pool_name']: p['pool_id'] for p in
                            ceph('osd', 'pool', 'ls', 'detail', as_json=True)}
                if pool['name'] in existing:
                    assert existing[pool['name']] == pool['id']
                    ceph('osd', 'pool', 'delete', pool['name'], pool['name'],
                         '--yes-i-really-really-mean-it')
                state['cleanup'][pool['name']] = 'deleted'
            except Exception:
                state['cleanup'][pool['name']] = traceback.format_exc()
        if config_changed:
            if config_original:
                ceph('config', 'set', 'mon', 'mon_allow_pool_size_one', config_original[0]['value'])
            else:
                ceph('config', 'rm', 'mon', 'mon_allow_pool_size_one')
        for flag in changed_flags:
            ceph('osd', 'unset', flag)
        state['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        state['final_status'] = wait_clean()
        state['final_pools'] = ceph('osd', 'pool', 'ls', as_json=True)
        save(directory / 'results.json', state)
        print(f'{state["state"]}: {directory}', flush=True)


if __name__ == '__main__':
    main()
