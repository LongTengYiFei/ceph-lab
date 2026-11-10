#!/usr/bin/env python3
"""Compare aligned large-object appends with periodic snapshots and no snapshots."""
import argparse
from collections import deque
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import time
import traceback

EXPERIMENT = Path(__file__).resolve().parent
MIB = 1024 * 1024


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def compressed(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
        json.dump(value, stream, separators=(',', ':'))
    temporary.replace(path)


def signature(sample):
    return tuple((osd, *(row['perf']['rocksdb'][k] for k in
                         ('submitted_txns', 'submitted_ops', 'submitted_bytes')),
                  row['perf']['bluestore']['issued_deferred_write_bytes'])
                 for osd, row in sorted(sample['osds'].items()))


def settle(quiet_seconds, minimum_seconds=0, timeout=90):
    started = time.monotonic()
    previous = probe('dump')
    quiet_since = time.monotonic()
    while time.monotonic() - started < timeout:
        time.sleep(0.25)
        current = probe('dump')
        if signature(current) != signature(previous):
            quiet_since = time.monotonic()
        previous = current
        if (time.monotonic() - quiet_since >= quiet_seconds and
                time.monotonic() - started >= minimum_seconds):
            return current
    raise RuntimeError('OSD logical submissions did not settle')


def workload(ioctx, names, template, epoch, concurrency):
    pending = deque()
    started = time.monotonic()
    count = 0
    for index, name in enumerate(names):
        payload = struct.pack('>QQ', epoch, index) + template[16:]
        completion = (ioctx.aio_write_full(name, payload) if epoch == 0
                      else ioctx.aio_append(name, payload))
        pending.append((completion, payload))
        if len(pending) >= concurrency:
            done, keepalive = pending.popleft()
            done.wait_for_complete()
            assert done.get_return_value() == 0
            count += 1
        if (index + 1) % 256 == 0:
            print(f'    submitted {index + 1}/{len(names)} objects', flush=True)
    while pending:
        done, keepalive = pending.popleft()
        done.wait_for_complete()
        assert done.get_return_value() == 0
        count += 1
    ioctx.aio_flush()
    assert count == len(names)
    return time.monotonic() - started


def verify_data(ioctx, names, templates, epoch, sample_count):
    expected_size = len(templates[0]) + epoch * len(templates[1])
    indexes = sorted({i * (len(names) - 1) // max(1, sample_count - 1)
                      for i in range(sample_count)})
    for index in indexes:
        name = names[index]
        assert ioctx.stat(name)[0] == expected_size, (name, epoch, 'size')
        offset = 0
        for version in range(epoch + 1):
            expected = struct.pack('>QQ', version, index) + templates[version][16:]
            actual = ioctx.read(name, len(expected), offset)
            assert actual == expected, (name, epoch, version, 'content')
            offset += len(expected)
    return {'sample_objects': len(indexes), 'epoch': epoch, 'bytes_per_object': expected_size,
            'sample_bytes_verified': len(indexes) * expected_size}


def summarize(directory, state):
    if state['state'] != 'completed':
        return
    groups = {g['mode']: g for g in state['groups']}
    assert set(groups) == {'snapshot', 'baseline'}
    with gzip.open(directory / 'raw-counters.json.gz', 'rt') as stream:
        raw = json.load(stream)
    reshards = {}
    for mode, group in groups.items():
        previous = raw[f'{mode}/start']
        for stage in group['stages']:
            current = raw[f'{mode}/{stage["kind"]}-{stage["round"]:02d}']
            reshards[mode, stage['kind'], stage['round']] = sum(
                current['osds'][i]['perf']['bluestore']['onode_reshard'] -
                previous['osds'][i]['perf']['bluestore']['onode_reshard']
                for i in current['osds'])
            previous = current
    fields = ['round', 'client_bytes_each_group', 'object_size_after',
              'baseline_submitted_bytes', 'snapshot_submitted_bytes', 'extra_submitted_bytes',
              'snapshot_to_baseline', 'baseline_deferred_bytes', 'snapshot_deferred_bytes',
              'baseline_ops', 'snapshot_ops', 'baseline_txns', 'snapshot_txns',
              'baseline_seconds', 'snapshot_seconds', 'baseline_onode_reshards',
              'snapshot_onode_reshards', 'baseline_allocated_bytes', 'snapshot_allocated_bytes']
    rows = []
    for epoch in range(1, state['parameters']['rounds'] + 1):
        b, s = (next(p for p in groups[m]['stages']
                     if p['kind'] == 'append' and p['round'] == epoch)
                for m in ('baseline', 'snapshot'))
        bv, sv = b['delta']['sum'], s['delta']['sum']
        rows.append(dict(zip(fields, [
            epoch, b['client_bytes'], b['object_size_after'], bv['submitted_bytes'],
            sv['submitted_bytes'], sv['submitted_bytes'] - bv['submitted_bytes'],
            sv['submitted_bytes'] / bv['submitted_bytes'],
            bv['issued_deferred_write_bytes'], sv['issued_deferred_write_bytes'],
            bv['submitted_ops'], sv['submitted_ops'], bv['submitted_txns'], sv['submitted_txns'],
            b['io_seconds'], s['io_seconds'], reshards['baseline', 'append', epoch],
            reshards['snapshot', 'append', epoch], bv['allocated'], sv['allocated']])))
    csv_path = directory / 'comparison.csv'
    csv_temporary = csv_path.with_suffix('.csv.tmp')
    with csv_temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    csv_temporary.replace(csv_path)
    summary = {'parameters': state['parameters'], 'totals': {}, 'append_rounds': rows,
               'verification': {m: g['verification'] for m, g in groups.items()},
               'initial_health': state['initial_status']['health'],
               'final_health': state['final_status']['health'], 'cleanup': state['cleanup']}
    for mode, group in groups.items():
        summary['totals'][mode] = {
            kind: {key: sum(p['delta']['sum'][key] for p in group['stages']
                            if kind == 'all' or p['kind'] == kind)
                   for key in ('submitted_bytes', 'submitted_ops', 'submitted_txns',
                               'issued_deferred_write_bytes')}
            for kind in ('initial', 'append', 'snapshot', 'idle', 'all')}
    summary['append_extra_bytes'] = (summary['totals']['snapshot']['append']['submitted_bytes'] -
                                     summary['totals']['baseline']['append']['submitted_bytes'])
    summary['append_extra_per_client_byte'] = summary['append_extra_bytes'] / (
        state['parameters']['objects'] * state['parameters']['append_bytes'] *
        state['parameters']['rounds'])
    summary['append_snapshot_to_baseline'] = (
        summary['totals']['snapshot']['append']['submitted_bytes'] /
        summary['totals']['baseline']['append']['submitted_bytes'])
    summary['all_snapshot_to_baseline'] = (
        summary['totals']['snapshot']['all']['submitted_bytes'] /
        summary['totals']['baseline']['all']['submitted_bytes'])
    save(directory / 'summary.json', summary)
    p = state['parameters']
    lines = ['# 大对象追加写与周期快照对照', '',
             f'{p["objects"]} 个对象，初始 {p["initial_bytes"] // MIB} MiB/对象，'
             f'每轮追加 {p["append_bytes"] // 1024} KiB，共 {p["rounds"]} 轮。'
             f'单副本、{p["pg_num"]} PG、并发 {p["concurrency"]}、关闭压缩。', '',
             f'快照组在初始装载后创建 S0，每轮追加后创建 S1–S{p["rounds"]}，'
             f'共 {p["rounds"] + 1} 个快照。无快照组重放相同数据和写入顺序；'
             '对应快照阶段执行时长近似匹配的空闲对照，实际窗口长度保存在原始记录。'
             '两组串行，测量前分别重置 RocksDB 计数器。', '',
             '本次是一组成对探索性实验，没有跨重复实验的置信区间。', '',
             '下表只比较追加阶段；MB=1,000,000 B。', '',
             '| 轮次 | 无快照提交 MB | 有快照提交 MB | 有/无 | deferred B（无/有） |',
             '|---|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f'| {row["round"]} | {row["baseline_submitted_bytes"]/1e6:.6f} | '
                     f'{row["snapshot_submitted_bytes"]/1e6:.6f} | '
                     f'{row["snapshot_to_baseline"]:.4f} | '
                     f'{row["baseline_deferred_bytes"]}/{row["snapshot_deferred_bytes"]} |')
    lines += ['', '分阶段累计提交字节数：', '',
              '| 阶段 | 无快照 | 有快照 |', '|---|---:|---:|']
    for kind, label in [('initial', '初始装载'), ('append', f'{p["rounds"]}轮追加'),
                        ('snapshot', '创建快照'), ('idle', '匹配的空闲窗口'), ('all', '完整流程')]:
        lines.append(f'| {label} | {summary["totals"]["baseline"][kind]["submitted_bytes"]:,} | '
                     f'{summary["totals"]["snapshot"][kind]["submitted_bytes"]:,} |')
    lines += ['', f'累计追加阶段的有/无快照比值为 {summary["append_snapshot_to_baseline"]:.5f}；'
              f'额外提交 {summary["append_extra_bytes"]:,} B，'
              f'相当于客户端追加字节的 {summary["append_extra_per_client_byte"]*100:.4f}%。', '']
    lines += ['', '统计口径是全部 OSD 成功提交的序列化 WriteBatch，包含批次头与编码开销，'
              '不包含 RocksDB 内部 WAL/SST/compaction 输出。deferred 字节是辅助诊断计数，'
              '没有直接从提交总量中扣除；若非零，该窗口不能据此宣称只有元数据。', '',
              'comparison.csv 还包含 onode_reshard 计数和实际新增分配字节。'
              '本次较高提交量轮次与 extent-map 重新分片同步，说明映射重写对逐轮波动有明显影响。'
              '追加阶段与创建快照阶段分开报告；后者可能包含 OSD 内部的 deferred 记录。', '',
              '每阶段起止计数器和收敛信息在 raw-counters.json.gz，'
              '逐阶段结果在 results.json，追加对照在 comparison.csv。'
              '完整流程计数不含建池、读回验证或删除测试池。', '',
              f'开始健康状态：{state["initial_status"]["health"]["status"]}；'
              f'结束：{state["final_status"]["health"]["status"]}。'
              '如有 MON_DISK_LOW，是运行前已有的告警。', '',
              '两组头对象均校验大小，抽样完整核对数据；快照组对所有快照进行抽样完整内容核对。'
              '测试池已删除，测试内容可按固定生成规则重建。', '']
    report_path = directory / 'report.md'
    report_temporary = report_path.with_suffix('.md.tmp')
    report_temporary.write_text('\n'.join(lines))
    report_temporary.replace(report_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects', type=int, default=1024)
    parser.add_argument('--initial-mib', type=int, default=16)
    parser.add_argument('--append-kib', type=int, default=512)
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--pg-num', type=int, default=32)
    parser.add_argument('--concurrency', type=int, default=32)
    parser.add_argument('--quiet-seconds', type=float, default=1)
    parser.add_argument('--sample-objects', type=int, default=3)
    parser.add_argument('--label', default='append')
    parser.add_argument('--summarize', type=Path,
                        help='Regenerate summaries from a completed run; no cluster operations')
    args = parser.parse_args()
    if args.summarize:
        saved = json.loads((args.summarize / 'results.json').read_text())
        assert saved['state'] == 'completed', saved['state']
        summarize(args.summarize, saved)
        print(f'Summaries regenerated: {args.summarize}')
        return
    # The offline summarizer needs only the standard library, not librados.
    import rados
    global ceph, delta, probe, wait_clean
    from test_snapwrite import ceph, delta, probe, wait_clean
    initial, append = args.initial_mib * MIB, args.append_kib * 1024
    assert min(args.objects, initial, append, args.rounds, args.concurrency, args.sample_objects) > 0
    assert initial % 4096 == append % 4096 == 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = EXPERIMENT / 'output' / f'{stamp}-{args.label}'
    directory.mkdir(parents=True, exist_ok=False)
    if os.geteuid() == 0 and 'SUDO_UID' in os.environ:
        for path in (directory.parent, directory):
            os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
    state = {'state': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
             'parameters': {**vars(args), 'initial_bytes': initial, 'append_bytes': append,
                            'size': 1, 'compression': 'none', 'snapshots': args.rounds + 1,
                            'data_generator': 'SHAKE256(append-v1:epoch) with >QQ epoch/object header'},
             'groups': [], 'created_pools': [], 'cleanup': {}}
    raw = {}
    save(directory / 'results.json', state)
    print(f'RESULT_DIR={directory}', flush=True)
    cluster = None
    changed_flags = []
    config_changed = False
    original_size_one = []
    try:
        state['initial_status'] = wait_clean()
        state['initial_pools'] = ceph('osd', 'pool', 'ls', as_json=True)
        state['versions'] = ceph('versions', as_json=True)
        assert len(state['versions']['osd']) == 1
        state['source_commit'] = re.search(r'\(([0-9a-f]{40})\)',
                                          next(iter(state['versions']['osd']))).group(1)
        probe('dump')
        original_size_one = [r for r in ceph('config', 'dump', as_json=True)
                             if r['name'] == 'mon_allow_pool_size_one' and r['section'] == 'mon']
        flags = ceph('osd', 'dump', as_json=True)['flags'].split(',')
        state['original_flags'] = flags
        state['original_size_one_config'] = original_size_one
        for flag in ('noscrub', 'nodeep-scrub'):
            if flag not in flags:
                ceph('osd', 'set', flag)
                changed_flags.append(flag)
        ceph('config', 'set', 'mon', 'mon_allow_pool_size_one', 'true')
        config_changed = True
        cluster = rados.Rados(conffile='/etc/ceph/ceph.conf', rados_id='admin')
        cluster.connect()
        names = [f'obj-{i:08d}' for i in range(args.objects)]
        templates = [hashlib.shake_256(f'append-v1:{epoch}'.encode()).digest(
            initial if epoch == 0 else append) for epoch in range(args.rounds + 1)]
        snapshot_windows = {}
        for mode in ('snapshot', 'baseline'):
            pool = f'swa-{stamp.lower()}-{args.label}-{mode}'
            assert pool not in cluster.list_pools()
            ceph('osd', 'pool', 'create', pool, args.pg_num, args.pg_num, '--autoscale-mode=off')
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
            settle(args.quiet_seconds, minimum_seconds=5)
            group = {'mode': mode, 'pool': pool, 'pool_id': pool_id, 'stages': [], 'snapshots': []}
            state['groups'].append(group)
            raw[f'{mode}/reset'] = probe('reset')
            previous = probe('dump')
            raw[f'{mode}/start'] = previous
            with cluster.open_ioctx(pool) as ioctx:
                def record(kind, epoch, operation, client_bytes=0, size_after=0):
                    nonlocal previous
                    started = time.monotonic()
                    io_seconds = operation()
                    after = settle(args.quiet_seconds)
                    elapsed = time.monotonic() - started
                    measured = delta(previous, after)
                    row = {'kind': kind, 'round': epoch, 'client_bytes': client_bytes,
                           'object_size_after': size_after, 'io_seconds': io_seconds,
                           'elapsed_seconds': elapsed, 'delta': measured}
                    group['stages'].append(row)
                    raw[f'{mode}/{kind}-{epoch:02d}'] = after
                    previous = after
                    save(directory / 'results.json', state)
                    compressed(directory / 'raw-counters.json.gz', raw)
                    print(f'{mode} {kind} {epoch:02d}: '
                          f'RocksDB={measured["sum"]["submitted_bytes"]:,} B '
                          f'ops={measured["sum"]["submitted_ops"]:,} '
                          f'deferred={measured["sum"]["issued_deferred_write_bytes"]:,} B '
                          f'elapsed={elapsed:.2f}s', flush=True)
                    return row

                print(f'{mode}: initial load {args.objects} x {args.initial_mib} MiB', flush=True)
                record('initial', 0,
                       lambda: workload(ioctx, names, templates[0], 0, args.concurrency),
                       args.objects * initial, initial)
                for epoch in range(args.rounds + 1):
                    if epoch:
                        print(f'{mode}: append round {epoch}', flush=True)
                        record('append', epoch,
                               lambda epoch=epoch: workload(ioctx, names, templates[epoch],
                                                           epoch, args.concurrency),
                               args.objects * append, initial + epoch * append)
                    if mode == 'snapshot':
                        def create_snapshot(epoch=epoch):
                            start = time.monotonic()
                            ioctx.create_snap(f's{epoch}')
                            assert cluster.wait_for_latest_osdmap() == 0
                            group['snapshots'].append({'name': f's{epoch}', 'epoch': epoch,
                                                      'id': ioctx.lookup_snap(f's{epoch}').snap_id})
                            return time.monotonic() - start
                        row = record('snapshot', epoch, create_snapshot)
                        snapshot_windows[epoch] = row['elapsed_seconds']
                    else:
                        record('idle', epoch,
                               lambda epoch=epoch: time.sleep(max(0, snapshot_windows[epoch] -
                                                                 args.quiet_seconds)) or 0)
                raw[f'{mode}/end'] = previous
                # All verification is outside the measured stage windows.
                expected_size = initial + args.rounds * append
                for name in names:
                    assert ioctx.stat(name)[0] == expected_size, name
                assert sum(1 for _ in ioctx.list_objects()) == args.objects
                group['verification'] = {
                    'all_head_sizes_checked': args.objects,
                    'head': verify_data(ioctx, names, templates, args.rounds, args.sample_objects),
                    'snapshots': []}
                for snapshot in group['snapshots']:
                    with cluster.open_ioctx(pool) as snapctx:
                        snapctx.set_read(snapshot['id'])
                        check = verify_data(snapctx, names, templates, snapshot['epoch'], args.sample_objects)
                        group['verification']['snapshots'].append({**snapshot, **check})
                deadline = time.monotonic() + 120
                expected_clones = args.objects * args.rounds if mode == 'snapshot' else 0
                while True:
                    stats = ioctx.get_stats()
                    if stats['num_object_clones'] == expected_clones:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f'clone count mismatch: {stats}')
                    time.sleep(1)
                group['final_pool_stats'] = stats
                group['pool_config'] = next(p for p in ceph('osd', 'pool', 'ls', 'detail', as_json=True)
                                            if p['pool_name'] == pool)
                save(directory / 'results.json', state)
            existing = {p['pool_name']: p['pool_id'] for p in
                        ceph('osd', 'pool', 'ls', 'detail', as_json=True)}
            assert existing[pool] == pool_id
            ceph('osd', 'pool', 'delete', pool, pool, '--yes-i-really-really-mean-it')
            state['cleanup'][pool] = 'deleted'
            wait_clean()
            settle(args.quiet_seconds, minimum_seconds=5)
            print(f'{mode}: verified and temporary pool removed', flush=True)
        state['state'] = 'completed'
    except BaseException:
        state['state'] = 'failed'
        state['error'] = traceback.format_exc()
        raise
    finally:
        if cluster is not None:
            cluster.shutdown()
        for pool in reversed(state['created_pools']):
            if state['cleanup'].get(pool['name']) == 'deleted':
                continue
            try:
                current = {p['pool_name']: p['pool_id'] for p in
                           ceph('osd', 'pool', 'ls', 'detail', as_json=True)}
                if pool['name'] in current:
                    assert current[pool['name']] == pool['id']
                    ceph('osd', 'pool', 'delete', pool['name'], pool['name'], '--yes-i-really-really-mean-it')
                state['cleanup'][pool['name']] = 'deleted'
            except Exception:
                state['cleanup'][pool['name']] = traceback.format_exc()
        if config_changed:
            if original_size_one:
                ceph('config', 'set', 'mon', 'mon_allow_pool_size_one', original_size_one[0]['value'])
            else:
                ceph('config', 'rm', 'mon', 'mon_allow_pool_size_one')
        for flag in changed_flags:
            ceph('osd', 'unset', flag)
        state['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        state['final_status'] = wait_clean()
        state['final_pools'] = ceph('osd', 'pool', 'ls', as_json=True)
        save(directory / 'results.json', state)
        compressed(directory / 'raw-counters.json.gz', raw)
        summarize(directory, state)
        print(f'{state["state"]}: {directory}', flush=True)


if __name__ == '__main__':
    main()
