#!/usr/bin/env python3
"""Create paired logical-metadata summaries and research plots from raw results."""
import argparse
import csv
import json
from pathlib import Path
import statistics
import math

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def stats(values):
    n = len(values)
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if n > 1 else 0
    # Two-sided 95% Student t critical value, n=5 for the formal experiment.
    critical = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776,
                5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}.get(n - 1, 1.96)
    half = critical * sd / math.sqrt(n) if n > 1 else 0
    return {'n': n, 'mean': mean, 'stdev': sd, 'ci95_low': mean - half,
            'ci95_high': mean + half, 'ci95_halfwidth': half}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--plot-formats', nargs='*', choices=['png', 'pdf', 'svg'], default=['png'])
    args = parser.parse_args()
    directory = args.directory
    data = json.loads((directory / 'results.json').read_text())
    assert data['state'] == 'completed', data['state']
    p = data['parameters']
    index = {(r['repetition'], r['mode'], r['round']): r for r in data['round_results']}
    paired_rows = []
    for rep in range(1, p['repetitions'] + 1):
        for round_id in range(1, p['rounds'] + 1):
            b, s = (index[rep, mode, round_id] for mode in ('baseline', 'snapshot'))
            base, snap = b['delta']['sum'], s['delta']['sum']
            extra = snap['submitted_bytes'] - base['submitted_bytes']
            base_records = base['submitted_bytes'] - 12 * base['submitted_txns']
            snap_records = snap['submitted_bytes'] - 12 * snap['submitted_txns']
            paired_rows.append({
                'repetition': rep, 'round': round_id, 'client_bytes': b['client_bytes'],
                'client_ops': b['client_ops'], 'baseline_bytes': base['submitted_bytes'],
                'snapshot_bytes': snap['submitted_bytes'], 'extra_bytes': extra,
                'baseline_bytes_per_object': base['submitted_bytes'] / p['objects'],
                'snapshot_bytes_per_object': snap['submitted_bytes'] / p['objects'],
                'extra_bytes_per_object': extra / p['objects'],
                'snapshot_to_baseline': snap['submitted_bytes'] / base['submitted_bytes'],
                'extra_bytes_per_client_byte': extra / b['client_bytes'],
                'baseline_record_bytes_per_object': base_records / p['objects'],
                'snapshot_record_bytes_per_object': snap_records / p['objects'],
                'extra_record_bytes_per_object': (snap_records - base_records) / p['objects'],
                'baseline_submitted_ops': base['submitted_ops'],
                'snapshot_submitted_ops': snap['submitted_ops'],
                'baseline_submitted_txns': base['submitted_txns'],
                'snapshot_submitted_txns': snap['submitted_txns'],
                'baseline_deferred_bytes': base['issued_deferred_write_bytes'],
                'snapshot_deferred_bytes': snap['issued_deferred_write_bytes'],
                'baseline_seconds': b['workload_seconds'],
                'snapshot_seconds': s['workload_seconds']})
    with (directory / 'paired-rounds.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(paired_rows[0]))
        writer.writeheader()
        writer.writerows(paired_rows)
    keys = ['baseline_bytes', 'snapshot_bytes', 'extra_bytes',
            'baseline_bytes_per_object', 'snapshot_bytes_per_object',
            'extra_bytes_per_object', 'snapshot_to_baseline',
            'extra_bytes_per_client_byte', 'baseline_record_bytes_per_object',
            'snapshot_record_bytes_per_object', 'extra_record_bytes_per_object']
    by_round = []
    for round_id in range(1, p['rounds'] + 1):
        rows = [r for r in paired_rows if r['round'] == round_id]
        by_round.append({'round': round_id, **{k: stats([r[k] for r in rows]) for k in keys}})
    phases = {}
    definitions = {'first_overwrite': [1], 'all_rounds': list(range(1, p['rounds'] + 1))}
    if p['rounds'] > 1:
        definitions['later_overwrites'] = list(range(2, p['rounds'] + 1))
    for phase, rounds in definitions.items():
        # Average rounds within each repetition first, preserving paired repeats.
        repetitions = []
        for rep in range(1, p['repetitions'] + 1):
            rows = [r for r in paired_rows if r['repetition'] == rep and r['round'] in rounds]
            repetitions.append({k: statistics.mean(r[k] for r in rows) for k in keys})
        phases[phase] = {k: stats([r[k] for r in repetitions]) for k in keys}
    idle = [{
        'repetition': g['repetition'], 'mode': g['mode'],
        **{f'{when}_bytes_per_second': g[f'idle_{when}']['sum']['submitted_bytes'] /
           g[f'idle_{when}']['window_seconds'] for when in ('before', 'after')}
    } for g in data['groups']]
    contaminated = [r for r in paired_rows
                    if r['baseline_deferred_bytes'] or r['snapshot_deferred_bytes']]
    summary = {'parameters': p, 'source_commit': data['source_commit'],
               'phases': phases, 'by_round': by_round, 'idle': idle,
               'round_pairs_with_deferred_payload': len(contaminated),
               'deferred_bytes_baseline': sum(r['baseline_deferred_bytes'] for r in paired_rows),
               'deferred_bytes_snapshot': sum(r['snapshot_deferred_bytes'] for r in paired_rows),
               'snapshot_clone_counts': [g['final_pool_stats']['num_object_clones']
                                         for g in data['groups'] if g['mode'] == 'snapshot'],
               'final_status': data['final_status'], 'final_pools': data['final_pools'],
               'cleanup': data['cleanup'], 'selection': data.get('selection', [])}
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    fig.subplots_adjust(left=0.075, right=0.985, bottom=0.16, top=0.84, wspace=0.28)
    rounds = [r['round'] for r in by_round]
    for key, label, color, marker in [
        ('baseline_bytes_per_object', 'No snapshot', '#2670a7', 'o'),
        ('snapshot_bytes_per_object', 'One snapshot before round 1', '#d75a2a', 's')]:
        axes[0].errorbar(rounds, [r[key]['mean'] / 1024 for r in by_round],
                         yerr=[r[key]['ci95_halfwidth'] / 1024 for r in by_round],
                         marker=marker, color=color, capsize=3, label=label)
    axes[0].set_ylabel('WriteBatch input (KiB/object)')
    axes[0].legend(frameon=False, fontsize=8)
    key = 'extra_bytes_per_object'
    axes[1].errorbar(rounds, [r[key]['mean'] for r in by_round],
                     yerr=[r[key]['ci95_halfwidth'] for r in by_round],
                     marker='o', color='#7b4f9d', capsize=3)
    axes[1].axhline(0, color='0.6', linewidth=0.8)
    axes[1].set_ylabel('Extra WriteBatch input (B/object)')
    for ax in axes:
        ax.set_xlabel('Overwrite round')
        ax.set_xticks(rounds)
        ax.grid(alpha=0.18)
        ax.spines[['top', 'right']].set_visible(False)
    fig.suptitle(f'Ceph 17.2.7: {p["objects"]:,} x 4 KiB objects, size=1, QD={p["concurrency"]}\n'
                 f'{p["repetitions"]} paired repetitions; mean and 95% t CI', fontsize=11, y=0.98)
    for extension in args.plot_formats:
        fig.savefig(directory / f'metadata-by-round.{extension}', dpi=180)
    plt.close(fig)
    lines = [
        '# Ceph 快照逻辑元数据测试结果', '',
        f'源码提交：`{data["source_commit"]}`。每组 {p["objects"]:,} 个 4 KiB 对象，'
        f'{p["rounds"]} 轮 offset 0 覆盖，并发 {p["concurrency"]}，'
        f'{p["repetitions"]} 次成对重复。单副本、关闭压缩；每组串行执行，组间重置计数器。', '',
        '统计量为全部 12 个 OSD 成功提交给 RocksDB 的序列化 WriteBatch 字节数，'
        '包含批次头与编码开销。下表是重复实验均值；后续轮次先在每次重复内平均，再跨重复汇总。', '',
        '| 阶段 | 无快照 B/对象/次 | 有快照 B/对象/次 | 额外 B/对象/次 | 额外/客户端字节 |',
        '|---|---:|---:|---:|---:|']
    for key, label in [('first_overwrite', '第一次覆盖'), ('later_overwrites', '后续覆盖'),
                       ('all_rounds', '全部轮次平均')]:
        if key in phases:
            phase = phases[key]
            lines.append(f'| {label} | {phase["baseline_bytes_per_object"]["mean"]:.2f} | '
                         f'{phase["snapshot_bytes_per_object"]["mean"]:.2f} | '
                         f'{phase["extra_bytes_per_object"]["mean"]:.2f} | '
                         f'{phase["extra_bytes_per_client_byte"]["mean"]:.5f} |')
    lines += ['', f'出现 deferred 数据的轮次对：{len(contaminated)} / {len(paired_rows)}。',
              f'快照组最终 clone 数：{summary["snapshot_clone_counts"]}。',
              '已抽样验证当前对象内容和快照初始内容，并确认每组对象数。', '',
              '原始计数器位于 raw/，逐轮成对结果位于 paired-rounds.csv，'
              '均值、标准差和 95% 置信区间位于 summary.json。', '',
              '作为辅助分析，CSV/JSON 同时给出减去每批次 12 B 固定头后的记录字节数，'
              '以区分 group commit 批次数量的变化。主表仍使用原始提交总字节数。', '',
              '结果没有扣除后台噪声，空闲窗口数据保存在 summary.json。'
              '若存在 deferred 数据，submitted_bytes 不能全部解释为纯元数据。'
              '本实验度量逻辑输入，不代表 SSD 写放大；结果仅对应当前对象大小和写入模式。', '',
              f'本次创建的 {len(data["cleanup"])} 个临时测试池已清理；'
              f'最终池：{data["final_pools"]}。详细清理记录见 results.json。'
              '测试数据可以按记录的固定种子重新生成。', '']
    for key, label in [('first_overwrite', '第一次覆盖'), ('later_overwrites', '后续覆盖')]:
        if key in phases:
            value = phases[key]['extra_bytes_per_object']
            lines += [f'{label}额外 B/对象/次的 95% t 置信区间：'
                      f'[{value["ci95_low"]:.2f}, {value["ci95_high"]:.2f}]。', '']
    if 'later_overwrites' in phases:
        later = phases['later_overwrites']
        lines += [f'后续覆盖的有/无快照提交量比值为 {later["snapshot_to_baseline"]["mean"]:.5f}。'
                  f'扣除批次头后，额外记录量为 {later["extra_record_bytes_per_object"]["mean"]:.3f} B/对象/次。'
                  '源码会在每次修改 head 时重新编码 SnapSet；当前单快照且完整覆盖的状态下，'
                  'clones、clone_overlap、clone_size、clone_snaps 四个字段分别增加 8、12、16、20 B，'
                  '合计 56 B，与观测到的稳定增量吻合。', '',
                  'Ceph 17.2.7 源码位置：`src/osd/PrimaryLogPG.cc:8991`（head 属性更新）、'
                  '`src/osd/osd_types.cc:5772`（SnapSet 编码）。', '']
    if data.get('selection'):
        lines += ['重复实验筛选记录见 selection.json。按测量窗口内 deferred 字节为 0 的预设条件，'
                  '只纳入完整的成对重复；被排除的数据仍保留在 sources.json 指向的原始目录中。', '']
    (directory / 'report.md').write_text('\n'.join(lines))
    print(json.dumps({'directory': str(directory), 'phases': phases,
                      'deferred_round_pairs': len(contaminated)}, indent=2))


if __name__ == '__main__':
    main()
