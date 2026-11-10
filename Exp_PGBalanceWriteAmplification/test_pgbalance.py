#!/usr/bin/env python3
"""Measure a test pool expanding from eight OSDs by one to four node3 OSDs."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import struct
import subprocess
import time
import traceback
from zoneinfo import ZoneInfo

EXPERIMENT = Path(__file__).resolve().parent
LAB = Path(os.environ.get('CEPH_LAB_DIR', EXPERIMENT.parent))
HOST_OSDS = {'node1': list(range(4)), 'node2': list(range(4, 8)), 'node3': list(range(8, 12))}
MIB = 1024**2
GIB = 1024**3
FIELDS = {
    'client_in_bytes': ('osd', 'op_in_bytes'),
    'client_write_bytes': ('osd', 'op_w_in_bytes'),
    'client_rw_bytes': ('osd', 'op_rw_in_bytes'),
    'replication_transaction_bytes': ('osd', 'subop_w_in_bytes'),
    'subop_push_in_bytes': ('osd', 'subop_push_in_bytes'),
    'push_out_bytes': ('osd', 'push_out_bytes'),
    'recovery_bytes': ('osd', 'recovery_bytes'),
    'store_write_big_bytes': ('bluestore', 'write_big_bytes'),
    'store_write_small_bytes': ('bluestore', 'write_small_bytes'),
    'stored_bytes': ('bluestore', 'stored'),
    'allocated_bytes': ('bluestore', 'allocated'),
    'rocksdb_submitted_bytes': ('rocksdb', 'submitted_bytes'),
}
GAUGES = {'stored_bytes', 'allocated_bytes'}


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def save_raw(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
        json.dump(value, stream, separators=(',', ':'))
    temporary.replace(path)


def ceph(*args, as_json=False):
    command = [str(LAB / 'ceph.sh'), *map(str, args)]
    if as_json:
        command += ['-f', 'json']
    result = subprocess.run(command, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'{args}: exit {result.returncode}: {result.stderr}')
    return json.loads(result.stdout) if as_json else result.stdout.strip()


def probe():
    source = (EXPERIMENT / 'osd_probe.py').read_text()

    def host_probe(item):
        host, ids = item
        remote = shlex.join(['sudo', '-n', '/usr/bin/python3', '-', *map(str, ids)])
        prefix = ['sudo', '-n', '-u', 'cyf'] if os.geteuid() == 0 else []
        result = subprocess.run([*prefix, 'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                 '-p', '23579', f'cyf@{host}', remote], input=source,
                                text=True, capture_output=True, timeout=40)
        if result.returncode:
            raise RuntimeError(f'{host} probe failed: {result.stderr}')
        return json.loads(result.stdout)

    start = time.time_ns()
    with ThreadPoolExecutor(max_workers=3) as executor:
        rows = list(executor.map(host_probe, HOST_OSDS.items()))
    osds = {key: value for row in rows for key, value in row.items()}
    assert set(osds) == {str(i) for i in range(12)}, osds.keys()
    return {'start_ns': start, 'end_ns': time.time_ns(), 'osds': osds}


def metrics(row):
    perf = row['perf']
    workers = {k: v for k, v in perf.items() if 'msgr_recv_bytes' in v}
    assert workers, 'Messenger receive counters not found'
    result = {name: perf[group][key] for name, (group, key) in FIELDS.items()}
    result['messenger_recv_bytes'] = sum(v['msgr_recv_bytes'] for v in workers.values())
    result['rdma_recv_bytes'] = sum(v['rx_bytes'] for k, v in perf.items()
                                    if k.startswith('AsyncMessenger::RDMAWorker-'))
    result['store_write_bytes'] = result['store_write_big_bytes'] + result['store_write_small_bytes']
    return result


def delta(before, after):
    rows = {}
    for osd_id, old in before['osds'].items():
        new = after['osds'][osd_id]
        assert (old['pid'], old['process_start_ticks']) == (new['pid'], new['process_start_ticks']), \
            f'osd.{osd_id} restarted'
        bm, am = metrics(old), metrics(new)
        values = {key: am[key] - bm[key] for key in bm}
        assert all(value >= 0 for key, value in values.items() if key not in GAUGES), values
        rows[osd_id] = values
    return {'seconds': ((after['start_ns'] + after['end_ns']) -
                        (before['start_ns'] + before['end_ns'])) / 2e9,
            'sum': {key: sum(row[key] for row in rows.values()) for key in next(iter(rows.values()))},
            'node3': {key: sum(rows[str(i)][key] for i in range(8, 12))
                      for key in next(iter(rows.values()))}, 'per_osd': rows}


def pg_rows():
    data = ceph('pg', 'dump', 'pgs', as_json=True)
    return data.get('pg_stats', data.get('pg_map', {}).get('pg_stats', []))


def pool_rows(pool_id):
    return [row for row in pg_rows() if row['pgid'].split('.')[0] == str(pool_id)]


def clean(status):
    osds = status['osdmap']
    pgs = status['pgmap']
    return (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds'] == 12 and
            sum(row['count'] for row in pgs.get('pgs_by_state', [])
                if set(row['state_name'].split('+')) <= {'active', 'clean', 'scrubbing', 'deep'}
                and {'active', 'clean'} <= set(row['state_name'].split('+'))) == pgs['num_pgs'])


def wait_clean(state, pool_id=None, require_node3=None, timeout=600):
    start = time.monotonic()
    last_print = -15
    while time.monotonic() - start < timeout:
        status = ceph('status', as_json=True)
        row = {'at_utc': datetime.now(timezone.utc).isoformat(), 'status': status}
        state.setdefault('health_timeline', []).append(row)
        if clean(status):
            if pool_id is None:
                return status
            pgs = pool_rows(pool_id)
            if pgs and all(len(p['acting']) == len(set(p['acting'])) == 3 and
                           p['up'] == p['acting'] for p in pgs):
                has_node3 = any(i >= 8 for p in pgs for i in p['acting'])
                if require_node3 is None or has_node3 == require_node3:
                    return status
        elapsed = time.monotonic() - start
        if elapsed - last_print >= 15:
            print(f'  waiting clean ({elapsed:.1f}s): {status["pgmap"]["pgs_by_state"]}', flush=True)
            last_print = elapsed
        time.sleep(1)
    raise RuntimeError(f'PGs did not become clean: {status}')


def settle(timeout=120):
    def signature(sample):
        return [(i, *(metrics(row)[key] for key in
                      ('push_out_bytes', 'stored_bytes', 'allocated_bytes', 'store_write_bytes')))
                for i, row in sorted(sample['osds'].items())]
    start = time.monotonic()
    old = probe()
    quiet = time.monotonic()
    while time.monotonic() - start < timeout:
        time.sleep(1)
        new = probe()
        if signature(new) != signature(old):
            quiet = time.monotonic()
        if time.monotonic() - quiet >= 3:
            return new
        old = new
    raise RuntimeError('data counters did not settle')


def idle(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(10, max(0, deadline - time.monotonic())))
        if deadline - time.monotonic() > 1:
            print(f'  idle control: {deadline - time.monotonic():.1f}s remaining', flush=True)


def payload(template, index):
    return struct.pack('>QQ', 1, index) + template[16:]


def write_objects(ioctx, count, template, concurrency):
    pending = deque()
    completed = 0
    start = time.monotonic()
    for index in range(count):
        data = payload(template, index)
        pending.append((ioctx.aio_write_full(f'obj-{index:08d}', data), data))
        if len(pending) >= concurrency:
            completion, keepalive = pending.popleft()
            completion.wait_for_complete()
            assert completion.get_return_value() == 0, completion.get_return_value()
            completed += 1
        if (index + 1) % 256 == 0:
            print(f'  load: {index + 1}/{count} objects', flush=True)
    while pending:
        completion, keepalive = pending.popleft()
        completion.wait_for_complete()
        assert completion.get_return_value() == 0, completion.get_return_value()
        completed += 1
    ioctx.aio_flush()
    assert completed == count
    return {'successful_objects': completed, 'successful_client_bytes': count * len(template),
            'io_seconds': time.monotonic() - start}


def verify(ioctx, count, template):
    started = time.monotonic()
    for index in range(count):
        name = f'obj-{index:08d}'
        assert ioctx.stat(name)[0] == len(template), name
        assert ioctx.read(name, len(template), 0) == payload(template, index), name
        if (index + 1) % 512 == 0:
            print(f'  verified {index + 1}/{count} complete objects', flush=True)
    assert sum(1 for _ in ioctx.list_objects()) == count
    return {'complete_objects_verified': count, 'bytes_verified': count * len(template),
            'seconds': time.monotonic() - started}


def wait_pool_stats(ioctx, count, client_bytes, timeout=120):
    """The librados aggregate can lag the detailed PG dump after fast writes."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        stats = ioctx.get_stats()
        if (stats['num_objects'] == count and stats['num_object_copies'] == 3 * count
                and stats['num_wr'] == count and stats['num_wr_kb'] == client_bytes // 1024
                and stats['num_object_clones'] == 0 and stats['num_objects_degraded'] == 0):
            return stats
        time.sleep(1)
    raise RuntimeError(f'librados pool statistics did not converge: {stats}')


def summarize(directory):
    state = json.loads((directory / 'results.json').read_text())
    assert state['state'] == 'completed', state['state']
    added_osds = state['parameters'].get('add_osds', 4)
    final_osds = 8 + added_osds
    final_idle = f'idle_{final_osds}'
    final_idle_end = f'{final_idle}_end'
    with gzip.open(directory / 'raw-counters.json.gz', 'rt') as stream:
        raw = json.load(stream)
    stages = {name: delta(raw[a], raw[b]) for name, a, b in [
        ('idle_8', 'idle_8_start', 'load_start'), ('load', 'load_start', 'load_end'),
        ('rebalance', 'load_end', 'rebalance_end'),
        (final_idle, 'rebalance_end', final_idle_end)]}
    d = state['load']['successful_client_bytes']
    before = {p['pgid']: p for p in state['pg_before']}
    after = {p['pgid']: p for p in state['pg_after']}
    movement = []
    for pgid, old in before.items():
        new = after[pgid]
        added = sorted(set(new['acting']) - set(old['acting']))
        removed = sorted(set(old['acting']) - set(new['acting']))
        movement.append({'pgid': pgid, 'objects': old['stat_sum']['num_objects'],
                         'logical_bytes': old['stat_sum']['num_bytes'],
                         'before': old['acting'], 'after': new['acting'],
                         'new_osds': added, 'removed_osds': removed,
                         'expected_new_replica_bytes': len(added) * old['stat_sum']['num_bytes'],
                         'expected_node3_bytes': sum(i >= 8 for i in added) * old['stat_sum']['num_bytes']})
    expected = sum(row['expected_new_replica_bytes'] for row in movement)
    expected_node3 = sum(row['expected_node3_bytes'] for row in movement)
    load, balance = stages['load']['sum'], stages['rebalance']['sum']
    idle_rate = stages[final_idle]['sum']['messenger_recv_bytes'] / stages[final_idle]['seconds']
    summary = {'client_bytes': d, 'replicas': 3, 'stages': stages,
               'initial_osds': 8, 'added_osds': added_osds, 'final_osds': final_osds,
               'added_osd_ids': state.get('added_osd_ids', list(range(8, final_osds))),
               'ideal_migration_bytes': 3 * d * added_osds / final_osds,
               'ideal_amplification': 1 + added_osds / final_osds,
               'final_idle_stage': final_idle,
               'load_receive_amplification': load['messenger_recv_bytes'] / (3 * d),
               'cumulative_receive_amplification': (load['messenger_recv_bytes'] +
                                                   balance['messenger_recv_bytes']) / (3 * d),
               'rebalance_receive_increment': balance['messenger_recv_bytes'] / (3 * d),
               'cumulative_to_load_receive_ratio': (load['messenger_recv_bytes'] +
                                                     balance['messenger_recv_bytes']) / load['messenger_recv_bytes'],
               'load_store_write_amplification': load['store_write_bytes'] / (3 * d),
               'cumulative_store_write_amplification': (load['store_write_bytes'] +
                                                       balance['store_write_bytes']) / (3 * d),
               'mapping_expected_new_replica_bytes': expected,
               'mapping_expected_node3_bytes': expected_node3,
               'mapping_expected_other_osds_bytes': expected - expected_node3,
               'push_out_minus_mapping_bytes': balance['push_out_bytes'] - expected,
               'mapping_implied_amplification': (3 * d + expected) / (3 * d),
               'changed_pg_count': sum(bool(p['new_osds']) for p in movement),
               'new_replica_count': sum(len(p['new_osds']) for p in movement),
               'final_idle_recv_bytes_per_second': idle_rate,
               'rebalance_to_idle_receive_ratio': balance['messenger_recv_bytes'] /
                   max(1, stages[final_idle]['sum']['messenger_recv_bytes']),
               'rebalance_idle_estimate_bytes': idle_rate * stages['rebalance']['seconds'],
               'verification': state['verification'], 'cleanup': state['cleanup']}
    save(directory / 'summary.json', summary)
    with (directory / 'per-osd.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['stage', 'osd', 'host', *load.keys()])
        writer.writeheader()
        for stage, data in stages.items():
            for osd_id, values in sorted(data['per_osd'].items(), key=lambda row: int(row[0])):
                writer.writerow({'stage': stage, 'osd': osd_id,
                                 'host': f'node{int(osd_id) // 4 + 1}', **values})
    with (directory / 'pg-movement.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(movement[0]))
        writer.writeheader()
        writer.writerows(movement)
    local_start = datetime.fromisoformat(state['started_at_utc']).astimezone(ZoneInfo('Asia/Shanghai'))
    lines = [f'# PG 均衡写放大：8 → {final_osds} OSD 实测（新增 {added_osds} 个）', '',
             f'运行时间（北京时间）：{local_start.isoformat(timespec="seconds")}。', '',
             f'客户端写入 {d:,} B（{d/GIB:.3f} GiB），'
             f'{state["parameters"]["objects"]} 个 {state["parameters"]["object_mib"]} MiB 对象；'
             f'三副本、{state["parameters"]["pg_num"]} PG、并发 {state["parameters"]["concurrency"]}。', '',
             '测试池全程使用独立的 osd 故障域规则；按池 flat 权重先将 node3 权重设为 0，'
             f'预先将未加入的 node3 叶子 OSD 权重设为 0，随后在一个 CRUSH 更新中将 node3 '
             f'桶权重恢复为所选 {added_osds} 个 OSD 的权重之和。'
             f'新增 OSD ID 为 {summary["added_osd_ids"]}。12 个 OSD 全程 up/in，'
             '既有 .mgr 池继续使用原 host 规则。未停止守护进程，也未更改副本数。', '',
             '关闭测试池压缩、PG 自动调整和 scrub；临时暂停自动 balancer。'
             '只有初始装载向测试池写数据，均衡和空闲窗口不提交客户端测试写入。', '',
             '**主指标**：全部 OSD Messenger worker 的 msgr_recv_bytes 增量之和 / '
             '(3 × 客户端成功提交字节数)。这些是消息接收字节，包含消息头、元数据、心跳等，'
             '所以是纯数据接收放大的近似，不能称为精确的 payload 或 SSD 物理写放大。'
             'RDMA rx_bytes 是另一层诊断计数，没有与 Messenger 字节相加。', '',
             '| 阶段 | 客户端测试写入 B | Messenger 接收 B | BlueStore 数据写入 B | push_out_bytes | 秒 |',
             '|---|---:|---:|---:|---:|---:|']
    for key, title in [('idle_8', '8 OSD 空闲'), ('load', '初始装载'),
                       ('rebalance', f'8→{final_osds} 均衡'),
                       (final_idle, f'{final_osds} OSD 匹配空闲')]:
        row = stages[key]
        values = row['sum']
        lines.append(f'| {title} | {d if key == "load" else 0:,} | '
                     f'{values["messenger_recv_bytes"]:,} | {values["store_write_bytes"]:,} | '
                     f'{values["push_out_bytes"]:,} | {row["seconds"]:.3f} |')
    lines += ['', f'- 初始装载接收放大：**{summary["load_receive_amplification"]:.6f}×**。',
              f'- 装载加均衡累计接收放大：**{summary["cumulative_receive_amplification"]:.6f}×**。',
              f'- 均衡新增放大：**{summary["rebalance_receive_increment"]:.6f}**，'
              f'即正常三副本数据量的 {summary["rebalance_receive_increment"]*100:.3f}%。',
              f'- 相对实测初始装载接收量，累计增幅：'
              f'**{(summary["cumulative_to_load_receive_ratio"]-1)*100:.3f}%**。',
              f'- BlueStore write_big_bytes + write_small_bytes 交叉核对：初始 '
              f'{summary["load_store_write_amplification"]:.6f}×，累计 '
              f'{summary["cumulative_store_write_amplification"]:.6f}×。'
              '这统计全部 OSD 数据写入路径处理的字节，包括内部对象写入，不能按池过滤；'
              '既不是接收计数，也不含完整设备内部写入。', '',
              f'均衡接收量是随后匹配空闲窗口的 '
              f'{summary["rebalance_to_idle_receive_ratio"]:.1f} 倍；按空闲接收速率折算的背景估计为 '
              f'{summary["rebalance_idle_estimate_bytes"]:,.0f} B。主结果保留原始增量，没有扣背景。', '',
              f'初始和最终映射中，{summary["changed_pg_count"]} 个 PG 的副本 OSD 集合发生变化，'
              f'共新增 {summary["new_replica_count"]} 个 PG 副本位置。按每 PG 的已存对象字节计算，'
              f'必要新增副本数据为 {expected:,} B，其中 node3 为 {expected_node3:,} B；'
              f'由实际映射推导的归一化值为 {summary["mapping_implied_amplification"]:.6f}×。'
              '它是映射和对象大小推导，需与实际数据路径计数分别看待。', '',
              f'细分迁移量：node3 接收 {expected_node3/GIB:.6f} GiB，'
              f'原有 8 个 OSD 之间另外迁移 {(expected-expected_node3)/GIB:.6f} GiB，'
              f'合计 {expected/GIB:.6f} GiB。实测 push_out_bytes 与该推导量之差为 '
              f'{summary["push_out_minus_mapping_bytes"]:,} B。'
              f'均匀分布理论值为 {summary["ideal_migration_bytes"]/GIB:.6f} GiB、'
              f'{summary["ideal_amplification"]:.6f}×；实际 PG 分布和旧 OSD 间迁移会使结果偏离该值。', '',
              f'node3 均衡窗口 Messenger 接收 {stages["rebalance"]["node3"]["messenger_recv_bytes"]:,} B，'
              f'BlueStore stored 增加 {stages["rebalance"]["node3"]["stored_bytes"]:,} B。', '',
              f'均衡后完整读回并逐字节核对全部 {state["verification"]["complete_objects_verified"]} 个对象，'
              f'合计 {state["verification"]["bytes_verified"]:,} B。验证和删除测试数据均在测量窗口外。', '',
              '初始和最终 pool 统计保留在 results.json；用于检查对象数、三副本数、'
              '客户端累计写入量及无 clone。逻辑对象字节使用详细 PG 的 stat_sum.num_bytes，'
              '不把 librados 池汇总中的 raw/allocated num_bytes 当作逻辑数据量。'
              '原始完整 perf 快照在 raw-counters.json.gz；'
              'per-osd.csv 可查看迁移来源和目标的分项，pg-movement.csv 可核对每 PG 映射。', '',
              '本次是单次原理验证，没有重复实验的置信区间。均匀分布估计'
              '不代替实际 PG 映射；三副本期间发生额外旧 OSD 间迁移时，实际值可以高于理论估计。', '',
              '清理记录：', '', '```json', json.dumps(state['cleanup'], ensure_ascii=False, indent=2), '```', '',
              f'结束状态：{state["final_status"]["health"]["status"]}；12 OSD up/in、全部 PG clean。'
              'MON_DISK_LOW 如仍存在，是运行前已有的 node1 系统盘告警。', '']
    (directory / 'report.md').write_text('\n'.join(lines))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects', type=int, default=4096)
    parser.add_argument('--object-mib', type=int, default=4)
    parser.add_argument('--pg-num', type=int, default=128)
    parser.add_argument('--concurrency', type=int, default=32)
    parser.add_argument('--idle-seconds', type=float, default=15)
    parser.add_argument('--add-osds', type=int, choices=range(1, 5), default=4,
                        help='Add OSDs 8 .. 8+N-1 to the test pool; default: all four node3 OSDs')
    parser.add_argument('--label', default='formal')
    parser.add_argument('--summarize', type=Path, help='Offline summary regeneration; no cluster access')
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(args.summarize), ensure_ascii=False, indent=2))
        return
    assert min(args.objects, args.object_mib, args.pg_num, args.concurrency, args.idle_seconds) > 0
    assert args.label.replace('-', '').replace('_', '').isalnum()
    import rados
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = EXPERIMENT / 'results' / f'{stamp}-{args.label}'
    directory.mkdir(parents=True, exist_ok=False)
    pool, rule = f'pgb-{stamp.lower()}-{args.label}', f'pgb-rule-{stamp.lower()}-{args.label}'
    parameters = vars(args).copy()
    parameters.pop('summarize')
    state = {'state': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
             'parameters': parameters, 'pool': pool, 'rule': rule, 'cleanup': {},
             'data_generator': 'SHAKE256(pgbalance-v1), with >QQ version=1/object-index header',
             'commands': []}
    raw = {}
    created_pool = created_rule = created_weights = changed_balancer = False
    cluster = None

    def checkpoint():
        save(directory / 'results.json', state)
        save_raw(directory / 'raw-counters.json.gz', raw)

    def mutate(*command):
        state['commands'].append({'at_utc': datetime.now(timezone.utc).isoformat(), 'args': command})
        checkpoint()
        print('  ceph ' + shlex.join(list(map(str, command))), flush=True)
        return ceph(*command)

    def terminate(signum, frame):
        raise RuntimeError(f'interrupted by signal {signum}; restoring experiment resources')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    checkpoint()
    print(f'RESULT_DIR={directory}', flush=True)
    try:
        state['initial_status'] = wait_clean(state)
        state['initial_osd_dump'] = ceph('osd', 'dump', as_json=True)
        state['initial_crush'] = ceph('osd', 'crush', 'dump', as_json=True)
        state['initial_pools'] = ceph('osd', 'pool', 'ls', 'detail', as_json=True)
        assert all(p['pool_name'] == '.mgr' for p in state['initial_pools']), 'unexpected user pool; aborting'
        flags = set(state['initial_osd_dump']['flags'].split(','))
        assert not flags & {'norecover', 'nobackfill', 'norebalance', 'pause', 'full'}, flags
        state['initial_balancer'] = ceph('balancer', 'status', as_json=True)
        state['versions'] = ceph('versions', as_json=True)
        state['osd_tree'] = ceph('osd', 'tree', as_json=True)
        assert all(o['weight'] == 1 for o in state['initial_osd_dump']['osds'])
        node3 = next(b for b in state['initial_crush']['buckets'] if b['name'] == 'node3')
        assert {item['id'] for item in node3['items']} == set(range(8, 12))
        state['node3_full_crush_weight'] = node3['weight'] / 65536
        state['added_osd_ids'] = list(range(8, 8 + args.add_osds))
        state['excluded_osd_ids'] = list(range(8 + args.add_osds, 12))
        state['node3_selected_crush_weight'] = sum(
            item['weight'] for item in node3['items'] if item['id'] in state['added_osd_ids']) / 65536
        raw['preflight'] = probe()
        for row in raw['preflight']['osds'].values():
            metrics(row)
        checkpoint()
        if state['initial_balancer']['active']:
            changed_balancer = True
            mutate('balancer', 'off')
        created_rule = True
        mutate('osd', 'crush', 'rule', 'create-replicated', rule, 'default', 'osd')
        created_pool = True
        mutate('osd', 'pool', 'create', pool, args.pg_num, args.pg_num, 'replicated',
               f'--rule={rule}', '--size=3', '--autoscale-mode=off')
        state['pool_id'] = next(p['pool_id'] for p in ceph('osd', 'pool', 'ls', 'detail', as_json=True)
                                if p['pool_name'] == pool)
        for key, value in [('min_size', '2'), ('compression_mode', 'none'),
                           ('noscrub', 'true'), ('nodeep-scrub', 'true')]:
            mutate('osd', 'pool', 'set', pool, key, value)
        mutate('osd', 'pool', 'application', 'enable', pool, 'rados')
        created_weights = True
        mutate('osd', 'crush', 'weight-set', 'create', pool, 'flat')
        # Configure the final leaf subset while the pool is still empty. The last
        # host-zero update below keeps ALL node3 devices excluded during the load.
        for osd_id in state['excluded_osd_ids']:
            mutate('osd', 'crush', 'weight-set', 'reweight', pool, f'osd.{osd_id}', '0')
        mutate('osd', 'crush', 'weight-set', 'reweight', pool, 'node3', '0')
        wait_clean(state, state['pool_id'], require_node3=False)
        state['crush_8'] = ceph('osd', 'crush', 'dump', as_json=True)
        state['pool_config'] = next(p for p in ceph('osd', 'pool', 'ls', 'detail', as_json=True)
                                    if p['pool_name'] == pool)
        assert state['pool_config']['size'] == 3
        assert state['pool_config']['pg_num'] == args.pg_num
        cluster = rados.Rados(conffile='/etc/ceph/ceph.conf', rados_id='admin')
        cluster.connect()
        cluster.wait_for_latest_osdmap()
        template = hashlib.shake_256(b'pgbalance-v1').digest(args.object_mib * MIB)
        state['template_sha256'] = hashlib.sha256(template).hexdigest()
        with cluster.open_ioctx(pool) as ioctx:
            raw['idle_8_start'] = settle()
            print(f'8-OSD idle control: {args.idle_seconds}s', flush=True)
            idle(args.idle_seconds)
            raw['load_start'] = probe()
            checkpoint()
            print(f'Initial load: {args.objects} x {args.object_mib} MiB; three replicas on OSD 0-7', flush=True)
            state['load'] = write_objects(ioctx, args.objects, template, args.concurrency)
            wait_clean(state, state['pool_id'], require_node3=False)
            deadline = time.monotonic() + 120
            while True:
                state['pg_before'] = pool_rows(state['pool_id'])
                if (sum(p['stat_sum']['num_bytes'] for p in state['pg_before']) ==
                        state['load']['successful_client_bytes'] and
                        sum(p['stat_sum']['num_objects'] for p in state['pg_before']) == args.objects):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('initial PG object statistics did not converge')
                time.sleep(1)
            state['pool_stats_before'] = wait_pool_stats(
                ioctx, args.objects, state['load']['successful_client_bytes'])
            raw['load_end'] = settle()
            checkpoint()
            print(f'Expanding test pool from 8 to {8 + args.add_osds} OSDs '
                  f'(adding {state["added_osd_ids"]}), with no client test writes', flush=True)
            mutate('osd', 'crush', 'weight-set', 'reweight', pool, 'node3',
                   state['node3_selected_crush_weight'])
            cluster.wait_for_latest_osdmap()
            wait_clean(state, state['pool_id'], require_node3=True)
            raw['rebalance_end'] = settle()
            state['pg_after'] = pool_rows(state['pool_id'])
            assert {i for p in state['pg_before'] for i in p['acting']} == set(range(8))
            assert {i for p in state['pg_after'] for i in p['acting']} == set(range(8 + args.add_osds))
            state['pool_stats_after'] = wait_pool_stats(
                ioctx, args.objects, state['load']['successful_client_bytes'])
            state[f'crush_{8 + args.add_osds}'] = ceph('osd', 'crush', 'dump', as_json=True)
            state['rebalance_delta'] = delta(raw['load_end'], raw['rebalance_end'])
            state['load_delta'] = delta(raw['load_start'], raw['load_end'])
            duration = state['rebalance_delta']['seconds']
            print(f'Rebalance complete: received {state["rebalance_delta"]["sum"]["messenger_recv_bytes"]:,} B '
                  f'in {duration:.3f}s; matched idle control next', flush=True)
            checkpoint()
            # Include the preceding diagnostic calls in this contiguous, explicitly recorded idle window.
            remaining = max(0, duration - (time.time_ns() - raw['rebalance_end']['end_ns']) / 1e9)
            idle(remaining)
            raw[f'idle_{8 + args.add_osds}_end'] = probe()
            # librados num_bytes reports allocated/raw pool space here, which can change
            # with metadata. Detailed PG num_bytes is the logical object-size total.
            for key in ('num_objects', 'num_object_copies', 'num_wr', 'num_wr_kb', 'num_object_clones'):
                assert state['pool_stats_before'][key] == state['pool_stats_after'][key], key
            assert sum(p['stat_sum']['num_bytes'] for p in state['pg_after']) == \
                state['load']['successful_client_bytes']
            assert state['pool_stats_after']['num_objects'] == args.objects
            assert state['pool_stats_after']['num_object_clones'] == 0
            assert sum(p['stat_sum']['num_object_copies'] for p in state['pg_after']) == args.objects * 3
            checkpoint()
            print('Full object content verification outside measurement windows', flush=True)
            state['verification'] = verify(ioctx, args.objects, template)
        state['state'] = 'completed'
    except BaseException:
        state['state'] = 'failed'
        state['error'] = traceback.format_exc()
        print(state['error'], flush=True)
    finally:
        if cluster is not None:
            cluster.shutdown()
        cleanup_errors = []
        for label, enabled, command in [
            ('pool', created_pool, ('osd', 'pool', 'delete', pool, pool, '--yes-i-really-really-mean-it')),
            ('weight_set', created_weights, None),
            ('rule', created_rule, ('osd', 'crush', 'rule', 'rm', rule)),
            ('balancer', changed_balancer, ('balancer', 'on'))]:
            if not enabled:
                continue
            try:
                if label == 'weight_set':
                    # Deleting the pool atomically removes its choose_args. Avoid
                    # restoring all four leaves before deletion, which would
                    # initiate an unwanted second rebalance in partial-add runs.
                    crush = ceph('osd', 'crush', 'dump', as_json=True)
                    assert str(state['pool_id']) not in crush.get('choose_args', {}), \
                        'test pool weight set remains after pool deletion'
                    state['cleanup'][label] = 'removed_with_pool'
                    continue
                if label == 'pool':
                    existing = {p['pool_name']: p['pool_id'] for p in
                                ceph('osd', 'pool', 'ls', 'detail', as_json=True)}
                    if pool not in existing:
                        state['cleanup'][label] = 'absent'
                        continue
                    if 'pool_id' in state:
                        assert existing[pool] == state['pool_id'], 'pool identity changed'
                mutate(*command)
                state['cleanup'][label] = 'restored' if label == 'balancer' else 'removed'
            except Exception:
                state['cleanup'][label] = traceback.format_exc()
                cleanup_errors.append(label)
        try:
            state['final_status'] = wait_clean(state)
            state['final_crush'] = ceph('osd', 'crush', 'dump', as_json=True)
            state['final_pools'] = ceph('osd', 'pool', 'ls', 'detail', as_json=True)
            state['final_balancer'] = ceph('balancer', 'status', as_json=True)
            state['final_osd_dump'] = ceph('osd', 'dump', as_json=True)
            if 'initial_crush' in state:
                assert state['final_crush'] == state['initial_crush'], 'CRUSH map contents not restored'
                assert state['final_osd_dump']['flags'] == state['initial_osd_dump']['flags']
                assert state['final_balancer']['active'] == state['initial_balancer']['active']
                assert [(p['pool_name'], p['pool_id'], p['size'], p['crush_rule']) for p in state['final_pools']] == \
                    [(p['pool_name'], p['pool_id'], p['size'], p['crush_rule']) for p in state['initial_pools']]
                state['cleanup']['original_cluster_settings'] = 'verified'
        except Exception:
            state['cleanup']['final_check'] = traceback.format_exc()
            cleanup_errors.append('final_check')
        if cleanup_errors:
            state['state'] = 'failed'
        state['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
        if state['state'] == 'completed':
            summary = summarize(directory)
            print(f'COMPLETED: load={summary["load_receive_amplification"]:.6f}x, '
                  f'load+balance={summary["cumulative_receive_amplification"]:.6f}x', flush=True)
        print(f'{state["state"]}: {directory}', flush=True)
        if os.geteuid() == 0 and 'SUDO_UID' in os.environ:
            for path in [EXPERIMENT / 'results', directory, *directory.rglob('*')]:
                os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
    if state['state'] != 'completed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
