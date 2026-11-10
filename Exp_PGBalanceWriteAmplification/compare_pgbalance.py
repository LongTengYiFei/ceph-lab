#!/usr/bin/env python3
"""Compare completed independent 8 -> 8+N runs without accessing the cluster."""
import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

GIB = 1024**3


def compare(directories, output_dir):
    rows = []
    parameters = None
    seen = set()
    for directory in directories:
        directory = Path(directory).resolve()
        state = json.loads((directory / 'results.json').read_text())
        summary = json.loads((directory / 'summary.json').read_text())
        assert state['state'] == 'completed', directory
        p = state['parameters']
        shared = {k: p[k] for k in ('objects', 'object_mib', 'pg_num', 'concurrency')}
        assert parameters is None or parameters == shared, 'workload parameters differ'
        parameters = shared
        added = p.get('add_osds', 4)
        assert added not in seen, f'duplicate added OSD count: {added}'
        seen.add(added)
        d = summary['client_bytes']
        stages = summary['stages']
        b, r = stages['load']['sum'], stages['rebalance']['sum']
        actual_ids = sorted({i for pg in state['pg_after'] for i in pg['acting']})
        assert actual_ids == list(range(8 + added)), actual_ids
        assert {i for pg in state['pg_before'] for i in pg['acting']} == set(range(8))
        assert b['client_in_bytes'] == d and r['client_in_bytes'] == 0
        assert summary['verification']['bytes_verified'] == d
        assert state['cleanup']['original_cluster_settings'] == 'verified'
        assert r['push_out_bytes'] == summary['mapping_expected_new_replica_bytes']
        rows.append({
            'added_osds': added, 'final_osds': 8 + added, 'client_bytes': d,
            'ideal_migration_bytes': 3 * d * added / (8 + added),
            'ideal_amplification': 1 + added / (8 + added),
            'initial_receive_bytes': b['messenger_recv_bytes'],
            'rebalance_receive_bytes': r['messenger_recv_bytes'],
            'actual_data_migration_bytes': r['push_out_bytes'],
            'new_osds_data_bytes': summary['mapping_expected_node3_bytes'],
            'old_osds_data_bytes': (summary['mapping_expected_new_replica_bytes'] -
                                   summary['mapping_expected_node3_bytes']),
            'initial_receive_amplification': summary['load_receive_amplification'],
            'cumulative_receive_amplification': summary['cumulative_receive_amplification'],
            'cumulative_store_write_amplification': summary['cumulative_store_write_amplification'],
            'mapping_implied_amplification': summary['mapping_implied_amplification'],
            'rebalance_seconds': stages['rebalance']['seconds'],
            'rebalance_client_bytes': r['client_in_bytes'],
            'verified_objects': summary['verification']['complete_objects_verified'],
            'changed_pgs': summary['changed_pg_count'],
            'source': str(directory), 'started_at_utc': state['started_at_utc'],
        })
    rows.sort(key=lambda row: row['added_osds'])
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'comparison.json').write_text(
        json.dumps({'parameters': parameters, 'runs': rows}, indent=2, sort_keys=True) + '\n')
    with (output_dir / 'comparison.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = ['# PG 均衡：分别新增 1、2、3 个 OSD 的对比', '',
             '每种情况从相同的 node1/node2 八个 OSD 独立开始，重新创建测试池并装载数据；'
             '不是从 8→9→10→11 连续扩容。新增四个的结果如在表中，来自此前已完成的正式测试。', '',
             f'每组 {parameters["objects"]} 个 {parameters["object_mib"]} MiB 对象，'
             f'三副本、{parameters["pg_num"]} PG、并发 {parameters["concurrency"]}。'
             '关闭压缩、PG 自动调整及测试池 scrub，不创建快照。', '',
             '按池 flat 权重控制 node3 的参与子集，新增 N 个即选择 OSD 8 至 8+N-1；'
             '未加入的叶子权重保持 0。初始装载时 node3 桶权重为 0，装载完成后一次性'
             '恢复为所选叶子权重之和。全局 12 个 OSD 全程 up/in，既有 .mgr 池不受此池权重影响。', '',
             '| 新增 OSD | 参与 OSD | 理想迁移 GiB | 实测数据迁移 GiB | 新 OSD 获得 GiB | 旧 OSD 间迁移 GiB | 理想放大 | 累计消息接收放大 |',
             '|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f'| {r["added_osds"]} | 8→{r["final_osds"]} | '
                     f'{r["ideal_migration_bytes"]/GIB:.6f} | {r["actual_data_migration_bytes"]/GIB:.6f} | '
                     f'{r["new_osds_data_bytes"]/GIB:.6f} | {r["old_osds_data_bytes"]/GIB:.6f} | '
                     f'{r["ideal_amplification"]:.6f} | **{r["cumulative_receive_amplification"]:.6f}** |')
    lines += ['', '理想估算使用客户端数据量 D、初始三副本数据量 3D：', '',
              '```text', '理想迁移量 = 3D × N / (8 + N)',
              '理想累计放大 = 1 + N / (8 + N)', '```', '',
              '主指标为全部 OSD 的 Messenger 接收字节增量 / (3D)，包含协议头、事务元数据'
              '和控制消息，结果不扣空闲背景。这里的“实测数据迁移量”取 push_out_bytes，'
              '它是发送侧恢复数据计数；每组均与 PG 映射推导的必要新增副本字节完全相等。', '',
              '| 新增 OSD | 初始接收放大 | 累计数据写入路径放大 | PG 映射推导放大 | 均衡期间客户端写入 B | 完整校验对象数 |',
              '|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f'| {r["added_osds"]} | {r["initial_receive_amplification"]:.6f} | '
                     f'{r["cumulative_store_write_amplification"]:.6f} | '
                     f'{r["mapping_implied_amplification"]:.6f} | '
                     f'{r["rebalance_client_bytes"]} | {r["verified_objects"]} |')
    lines += ['', '数据写入路径放大使用 BlueStore write_big_bytes + write_small_bytes，'
              '包括 OSD 内部对象写入，不能按池过滤，是独立交叉核对项，不代表 SSD 内部写入。'
              '所有组均在均衡窗口之后读回校验全部对象，'
              '并完成测试池、权重集和规则清理，恢复原 CRUSH 内容、flags 和 balancer 状态。', '',
              '每种情况只有一次正式运行，没有重复实验置信区间。各组使用独立池，池 ID 不同'
              '会影响具体 PG 映射；实际 PG 分布及旧 OSD 之间的额外迁移都能使结果偏离理想值。', '',
              '各组原始数据与报告：', '']
    for r in rows:
        source = Path(r['source'])
        relative = Path(os.path.relpath(source, output_dir))
        local = datetime.fromisoformat(r['started_at_utc']).astimezone(ZoneInfo('Asia/Shanghai'))
        lines.append(f'- 新增 {r["added_osds"]} 个，{local.isoformat(timespec="seconds")}：'
                     f'[报告]({relative}/report.md)、[原始记录]({relative}/results.json)、'
                     f'[计数快照]({relative}/raw-counters.json.gz)。')
    lines += ['']
    (output_dir / 'comparison-report.md').write_text('\n'.join(lines))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', type=Path, nargs='+')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    rows = compare(args.directories, args.output_dir)
    for row in rows:
        print(f'+{row["added_osds"]}: {row["cumulative_receive_amplification"]:.6f}x; '
              f'{row["actual_data_migration_bytes"]/GIB:.6f} GiB migrated')


if __name__ == '__main__':
    main()
