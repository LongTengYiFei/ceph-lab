#!/usr/bin/env python3
"""Rebuild a readable YCSB A/B report from saved results; no cluster mutations."""
import argparse
import csv
from datetime import datetime
import gzip
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def metrics(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.startswith('[') and '], ' in line:
            parts = line.split(', ', 2)
            if len(parts) == 3:
                group, key, value = parts
                try:
                    result.setdefault(group[1:-1], {})[key] = float(value)
                except ValueError:
                    pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    result = json.loads((directory / 'results.json').read_text())
    summary = json.loads((directory / 'summary.json').read_text())
    args_used = result['parameters']
    workload = result.get('workload', args_used.get('workload', 'a')).upper()
    proportions = {'A': '50% READ / 50% UPDATE', 'B': '95% READ / 5% UPDATE'}[workload]
    metric = metrics(directory / 'formal.log')
    with gzip.open(directory / 'raw-counters.json.gz', 'rt') as stream:
        raw = json.load(stream)
    boundary = raw['boundaries']
    start, stop = boundary['formal-start'], boundary['tail-end']
    lv_rows = []
    for host, value in stop['hosts'].items():
        for device, item in value['disks'].items():
            old = start['hosts'][host]['disks'][device]
            for role, lv in item.get('lvs', {}).items():
                difference = lv['written_bytes'] - old['lvs'][role]['written_bytes']
                assert difference >= 0
                lv_rows.append({'host': host, 'physical_device': device, 'role': role,
                                'lv_device': lv['device'], 'written_bytes': difference})
    if lv_rows:
        lv_totals = {role: sum(row['written_bytes'] for row in lv_rows if row['role'] == role)
                     for role in ('backend_db', 'cache_data')}
        summary['lv_split'] = {**lv_totals,
                               'physical_minus_lv_bytes': summary['ssd_b_written_bytes'] - sum(lv_totals.values())}
        with (directory / 'per-lv.csv').open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(lv_rows[0]))
            writer.writeheader(); writer.writerows(lv_rows)
    path_totals = {}
    for role, ids in [('backend', {0, 1, 4, 5, 8, 9}), ('cache', {2, 3, 6, 7, 10, 11})]:
        values = {'bluefs_write_bytes': 0, 'object_write_path_bytes': 0,
                  'rocksdb_submitted_bytes': 0, 'deferred_write_bytes': 0}
        for host, value in stop['hosts'].items():
            for osd, item in value['osds'].items():
                if int(osd) not in ids:
                    continue
                old = start['hosts'][host]['osds'][osd]['perf']; new = item['perf']
                for name, section, keys in [
                        ('bluefs_write_bytes', 'bluefs', ('write_bytes',)),
                        ('object_write_path_bytes', 'bluestore', ('write_big_bytes', 'write_small_bytes')),
                        ('rocksdb_submitted_bytes', 'rocksdb', ('submitted_bytes',)),
                        ('deferred_write_bytes', 'bluestore', ('issued_deferred_write_bytes',))]:
                    values[name] += sum(new[section][k] - old[section][k] for k in keys)
        path_totals[role] = values
    summary['path_estimates'] = path_totals
    override_path = directory / 'promotion-overrides.json'
    overrides = json.loads(override_path.read_text()) if override_path.exists() else None
    if overrides:
        applied_ns = int(datetime.fromisoformat(overrides['applied_utc']).timestamp() * 1e9)
        assert applied_ns < start['started_ns'], 'promotion policy changed during formal interval'
        summary['promotion_overrides'] = overrides
    smart = []
    for host, value in stop['hosts'].items():
        for device, item in value['disks'].items():
            old = start['hosts'][host]['disks'][device]
            before_smart, after_smart = old.get('smart', {}), item.get('smart', {})
            row = {'host': host, 'device': device, 'serial': item['serial']}
            for field in ('data_units_written', 'media_errors', 'num_err_log_entries'):
                try:
                    row[field + '_delta'] = int(after_smart[field]) - int(before_smart[field])
                except (KeyError, ValueError, TypeError):
                    row[field + '_delta'] = None
            if row['data_units_written_delta'] is not None:
                row['smart_host_written_bytes'] = row['data_units_written_delta'] * 512000
            row['block_written_bytes'] = item['written_bytes'] - old['written_bytes']
            smart.append(row)
    (directory / 'smart-crosscheck.json').write_text(json.dumps(smart, indent=2) + '\n')
    if smart and all(row['data_units_written_delta'] is not None for row in smart):
        summary['smart_host_written_bytes'] = sum(row['smart_host_written_bytes'] for row in smart)
    available = [row for row in smart if row['data_units_written_delta'] is not None]
    summary['smart_crosscheck'] = {
        'available_disks': len(available), 'total_disks': len(smart),
        'smart_written_bytes': sum(row['smart_host_written_bytes'] for row in available),
        'block_written_bytes': sum(row['block_written_bytes'] for row in available),
    }
    for row in smart:
        if row['media_errors_delta'] is not None:
            assert row['media_errors_delta'] == 0, row
    read, update = summary['client']['read_success'], summary['client']['update_success']
    local = datetime.fromisoformat(result['started_utc']).astimezone(ZoneInfo('Asia/Shanghai'))
    lines = [
        f'# Workload {workload}：混合 SSD B 整盘写放大', '',
        f'开始时间：{local:%Y-%m-%d %H:%M:%S}（北京时间）。', '',
        f"运行通过：**{result['passed']}**；临时资源清理完成：**{result.get('cleanup_complete', False)}**。", '',
        '## 负载与测量口径', '',
        f'- 使用官方 YCSB **0.17.0 CoreWorkload** 的 Workload {workload}：目标 {proportions}，Zipfian 分布。',
        '- 采用本目录公开的自定义 RADOS binding：每条记录一个 4 KiB 字段，UPDATE 直接调用 `rados_write_full`。',
        '- 每个 4 KiB 记录内含 8 B 标识与 CRC32；每次读取校验大小和 CRC32，写入字节计数包含这 8 B。',
        '- 这不是默认的 10 字段/100 B 更新，也没有使用原 RADOS binding 的 delete+insert 更新。',
        f"- 数据集 {args_used['records']:,} 条、{args_used['records'] * 4096 / 1024**3:.6f} GiB；缓存目标 {args_used['cache_bytes'] / 1024**3:.6f} GiB，均为三副本前逻辑容量。",
        f"- 并发 {args_used['threads']}，目标总速率 {args_used['target_ops']:,} ops/s；预热 {args_used['warmup_seconds']} s，正式阶段 {args_used['seconds']} s。实际时长与速率以原始日志为准。",
        '- 两个临时池与既有环境共用相同六个后端、六个缓存 OSD，均为三副本、host 故障域、64 PG。',
        '- 初始化使用 IGNORE_CACHE|IGNORE_OVERLAY 直接装载后端；通过提升计数不增加核对，预热开始时测试缓存为空。',
        '- 预热、正式负载为独立 YCSB JVM；预热保留 Ceph 缓存，不保证正式 JVM 的 JIT 已预热。',
        '- 正式开始前保留预热产生的脏对象；尾部排空可能包含这些遗留回写，因此 W/D 是该窗口的写入代价，不是只归属于正式 UPDATE 的因果放大。',
        '- 正式分母 D 只累计返回成功的 UPDATE 的 4096 B 载荷；READ 字节、初始化和预热不计入 D。',
        '- 正式分子仅累加六块 SSD B 物理 NVMe 设备 stat 的写扇区差值 × 512；含两个 LV 的全部写入，排除 SSD A。',
        '- 主窗口为正式开始至回写尾部结束。尾部把临时缓存的 dirty 阈值和最小 flush 年龄改为 0，排空后等待三个连续低写入窗口；这些控制引发的元数据写入也在分子内。',
        '- 空闲对照另列，主值不扣后台。读回校验和测试池删除均在主窗口结束后进行。',
        '- 指标是块层主机写入代价，不是 NAND/FTL 内部 WAF；三副本归一化只是除以三，不强制基线等于 1。', '',
        '## 主结果', '', '| 指标 | 结果 |', '|---|---:|',
        f'| 成功 READ | {read:,} |',
        f'| 成功 UPDATE | {update:,} |',
        f'| 实际 READ / UPDATE | {read/(read+update):.4%} / {update/(read+update):.4%} |',
        f"| 成功写入 D | {summary['client']['successful_write_bytes']:,} B，{summary['client']['successful_write_bytes']/1024**3:.6f} GiB |",
        f"| 六块 SSD B 合计写入 W | {summary['ssd_b_written_bytes']:,} B，{summary['ssd_b_written_GiB']:.6f} GiB |",
        f"| W / D | **{summary['host_write_amplification']:.6f}** |",
        f"| W / (3D) | **{summary['three_replica_normalized']:.6f}** |",
        f"| 负载期间 SSD B 写入 | {summary['active_written_bytes']/1024**3:.6f} GiB |",
        f"| 回写尾部 SSD B 写入 | {summary['tail_written_bytes']/1024**3:.6f} GiB |",
        f"| 主窗口 | {summary['main_window_seconds']:.3f} s |",
        f"| 客户端失败 / CRC 错误 | {summary['client']['failures']} / {summary['client']['crc_failures']} |", '',
        '## 自然缓存活动', '',
        '以下增量只取正式负载窗口，不包含排空尾部和手动清理；来自缓存 OSD 的累计计数差值。', '',
        '| 计数器 | 正式窗口增量 |', '|---|---:|',
    ]
    if overrides:
        applied_local = datetime.fromisoformat(overrides['applied_utc']).astimezone(ZoneInfo('Asia/Shanghai'))
        before_load = applied_ns < boundary['load-start']['started_ns']
        when = '装载和预热开始前' if before_load else '预热期间'
        lines[lines.index('## 主结果'):lines.index('## 主结果')] = [
            '默认提升限速为每缓存 OSD 25 对象/s、5 MiB/s。为在短实验内形成容量压力，',
            f'于 {applied_local:%H:%M:%S}（北京时间，{when}）把六个缓存 OSD 的两个提升限速都设为 0，即取消限速。',
            '正式窗口开始前已全部生效，正式期间没有修改这些参数。原值和恢复记录见 promotion-overrides.json。',
            ('本轮预热和正式阶段提升参数固定；固定时长预热不等于已严格证明稳态。' if before_load
             else '本轮预热中调整过提升参数，不能据此声称已严格证明稳态。'), '',
        ]
    for key, value in summary['formal_cache_events'].items():
        lines.append(f'| {key} | {value:,} |')
    lines += ['', '## 六块 SSD B', '', '| 主机 | 物理盘 | 整盘写入 GiB |', '|---|---|---:|']
    for row in summary['per_disk']:
        lines.append(f"| {row['host']} | {row['device']} | {row['written_GiB']:.6f} |")
    cross = summary['smart_crosscheck']
    lines += ['', f"SMART 主机写入交叉核对覆盖 {cross['available_disks']}/{cross['total_disks']} 块盘；这部分 SMART 合计 {cross['smart_written_bytes']:,} B，块层合计 {cross['block_written_bytes']:,} B。"]
    missing = [row['host'] + ':' + row['device'] for row in smart if row['data_units_written_delta'] is None]
    if missing:
        lines += ['未取得 SMART 前后快照的设备：' + '、'.join(missing) + '；六块盘的块层主计数均完整。']
    if lv_rows:
        lines += ['', '## 两个 LV 的块层分账', '',
                  '| 区域 | LV 写入 GiB | 占整盘总量 |', '|---|---:|---:|']
        for role, label in [('backend_db', '后端 DB/WAL LV'), ('cache_data', '缓存 OSD LV（含自身 DB/WAL）')]:
            amount = summary['lv_split'][role]
            lines.append(f'| {label} | {amount/1024**3:.6f} | {amount/summary["ssd_b_written_bytes"]:.3%} |')
        lines += ['', f"物理盘总量减去两个 LV 之和的残差为 {summary['lv_split']['physical_minus_lv_bytes']:,} B。",
                  'LV 与物理设备属于不同的块层统计位置，采样时刻也略有差异；分账用于交叉核对，不再叠加到整盘主指标。',
                  '缓存 LV 的数值同时包含对象数据和缓存 OSD 自身的元数据，不能直接解释为热点数据量。']
    lines += ['', '## OSD 写入路径交叉核对', '',
              '| 路径计数 | GiB |', '|---|---:|',
              f"| 后端 OSD BlueFS DB/日志写入 | {path_totals['backend']['bluefs_write_bytes']/1024**3:.6f} |",
              f"| 缓存 OSD BlueFS DB/日志写入 | {path_totals['cache']['bluefs_write_bytes']/1024**3:.6f} |",
              f"| 缓存 OSD 对象数据写入路径 | {path_totals['cache']['object_write_path_bytes']/1024**3:.6f} |", '',
              '这些是 BlueFS/BlueStore 内部路径计数的近似归因，不是另一份物理盘总量。RocksDB submitted_bytes 是逻辑输入，不与 BlueFS 输出重复相加。']
    occupancy_path = directory / 'pool-samples.jsonl'
    if occupancy_path.exists():
        points = []
        for line in occupancy_path.read_text().splitlines():
            point = json.loads(line)
            time_ns = int(datetime.fromisoformat(point['time_utc']).timestamp() * 1e9)
            if point.get('cache') and start['started_ns'] <= time_ns <= boundary['formal-end']['finished_ns']:
                points.append({'time_utc': point['time_utc'], 'stored_bytes': point['cache']['stored'],
                               'objects': point['cache']['objects'], 'dirty': point['cache']['dirty']})
        if points:
            with (directory / 'cache-occupancy.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(points[0]))
                writer.writeheader(); writer.writerows(points)
            minimum = min(p['stored_bytes'] for p in points) / 1024**2
            maximum = max(p['stored_bytes'] for p in points) / 1024**2
            dirty_min = min(p['dirty'] for p in points)
            dirty_max = max(p['dirty'] for p in points)
            summary['cache_observed'] = {'samples': len(points),
                                         'stored_min_bytes': min(p['stored_bytes'] for p in points),
                                         'stored_max_bytes': max(p['stored_bytes'] for p in points),
                                         'dirty_min': dirty_min, 'dirty_max': dirty_max}
            lines += ['', f'正式窗口观测样本中，缓存池 stored 为 {minimum:.3f}–{maximum:.3f} MiB，dirty 为 {dirty_min:,}–{dirty_max:,}；样本见 cache-occupancy.csv。该范围不是严格硬容量限制。']
    stages = []
    for phase in ('load', 'warmup', 'formal'):
        a, b = boundary[phase + '-start'], boundary[phase + '-end']
        written = sum(d['written_bytes'] - a['hosts'][host]['disks'][dev]['written_bytes']
                      for host, item in b['hosts'].items() for dev, d in item['disks'].items())
        stages.append({'phase': phase, 'ssd_b_written_bytes': written,
                       'client_written_bytes': result['phases'][phase]['successful_write_bytes']})
    stages.append({'phase': 'tail', 'ssd_b_written_bytes': summary['tail_written_bytes'], 'client_written_bytes': 0})
    with (directory / 'stages.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(stages[0]))
        writer.writeheader(); writer.writerows(stages)
    lines += ['', '## 吞吐与延迟', '', '| 指标 | YCSB 日志值 |', '|---|---:|']
    for group, key in [('OVERALL', 'RunTime(ms)'), ('OVERALL', 'Throughput(ops/sec)'),
                        ('READ', 'AverageLatency(us)'), ('READ', '95thPercentileLatency(us)'),
                        ('READ', '99thPercentileLatency(us)'), ('UPDATE', 'AverageLatency(us)'),
                        ('UPDATE', '95thPercentileLatency(us)'), ('UPDATE', '99thPercentileLatency(us)')]:
        if key in metric.get(group, {}):
            lines.append(f'| {group} {key} | {metric[group][key]} |')
    idle = summary.get('idle_control')
    if idle:
        lines += ['', f"排空后的短空闲对照为 {idle['seconds']:.3f} s，六块 SSD B 写入 {idle['bytes']:,} B，平均 {idle['bytes_per_second']:.3f} B/s；不是与正式窗口等长的对照，未从主值扣除。"]
    validation = result.get('readback', {})
    lines += ['', '## 校验与范围', '',
              f"完整读回后端 {validation.get('records_checked', 0):,} 条记录，检查大小及内嵌 CRC32；绕过缓存和 overlay，数据量 {validation.get('bytes_checked',0)/1024**3:.6f} GiB。",
              '并发更新没有逐对象记录最终版本的客户端字节镜像，因此这里证明全量记录存在、大小与校验和正确，不声称逐对象与最后一次写入日志逐字节比对。', '',
              '标准 NVMe SMART Data Units Written 以 512,000 B 为单位，交叉核对保存在 smart-crosscheck.json；它仍是主机侧量，不是 NAND 写入量。',
              '本次只有一轮正式运行，没有重复实验置信区间。结果同时包含读引发的提升、写命中/未命中、回写合并以及后端元数据开销。', '',
              '逐边界和周期原始计数见 raw-counters.json.gz；逐 SSD 数据见 per-ssd.csv；成功字节计数和命令/恢复记录见 results.json。',
              '初始永久池的完整配置、全局 flags 和 balancer 状态在清理后逐项核对。', '']
    (directory / 'report.md').write_text('\n'.join(lines))
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(directory / 'report.md')


if __name__ == '__main__':
    main()
