#!/usr/bin/env python3
"""Plot migration and write amplification as two separate figures for +1..+4 OSDs."""
import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
from matplotlib import font_manager
from matplotlib import pyplot as plt
from matplotlib.ticker import FormatStrFormatter, MultipleLocator

EXPERIMENT = Path(__file__).resolve().parent
GIB = 1024**3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=EXPERIMENT / 'comparison.csv')
    parser.add_argument('--output-dir', type=Path, default=EXPERIMENT / 'figures')
    parser.add_argument('--font-size', type=float, default=18)
    parser.add_argument('--formats', nargs='+', choices=['png', 'pdf', 'svg'],
                        default=['png', 'pdf', 'svg'])
    args = parser.parse_args()
    if args.font_size <= 0:
        parser.error('--font-size must be positive')
    with args.input.open(newline='') as stream:
        rows = sorted(csv.DictReader(stream), key=lambda row: int(row['added_osds']))
    counts = [int(row['added_osds']) for row in rows]
    if counts != [1, 2, 3, 4]:
        raise ValueError(f'Expected one result for each added OSD count 1..4; got {counts}')
    migration = [int(row['actual_data_migration_bytes']) / GIB for row in rows]
    amplification = [float(row['cumulative_receive_amplification']) for row in rows]

    installed = {font.name for font in font_manager.fontManager.ttflist}
    preferences = ['Noto Sans CJK SC', 'Source Han Sans SC', 'WenQuanYi Zen Hei',
                   'Microsoft YaHei', 'SimHei', 'Noto Sans CJK JP']
    font = next((name for name in preferences if name in installed), None)
    if font is None:
        raise RuntimeError('A Chinese font is required, for example Noto Sans CJK')
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': [font, 'DejaVu Sans'],
        'font.size': args.font_size,
        'axes.titlesize': args.font_size,
        'axes.labelsize': args.font_size,
        'xtick.labelsize': args.font_size,
        'ytick.labelsize': args.font_size,
        'legend.fontsize': args.font_size,
        'figure.titlesize': args.font_size,
        'axes.unicode_minus': False,
        # Type 3 supports the system CJK TTC font with Matplotlib 3.5; Type 42
        # subsetting requires a standalone face in this fontTools version.
        'pdf.fonttype': 3,
        'ps.fonttype': 3,
        'svg.fonttype': 'path',
    })
    args.output_dir.mkdir(parents=True, exist_ok=True)
    specifications = [
        ('pgbalance_migration', migration, '实际数据迁移量（GiB）',
         '新增 OSD 对数据迁移量的影响', '#2878B5', 0, 20, 4, '%.0f'),
        ('pgbalance_write_amplification', amplification, '写放大',
         '新增 OSD 对写放大的影响', '#D97B26', 1, 1.4, 0.2, '%.1f'),
    ]
    for filename, values, ylabel, title, color, lower, upper, tick_step, tick_format in specifications:
        fig, axis = plt.subplots(figsize=(8, 6))
        fig.subplots_adjust(left=0.16, right=0.96, bottom=0.16, top=0.85)
        bars = axis.bar(counts, values, width=0.55, color=color, zorder=3)
        axis.set_xlabel('新增 OSD 数量', labelpad=12)
        axis.set_ylabel(ylabel, color=color, labelpad=12)
        axis.set_title(title, pad=18)
        axis.set_xticks(counts)
        axis.set_xlim(0.45, 4.55)
        axis.set_ylim(lower, upper)
        axis.yaxis.set_major_locator(MultipleLocator(tick_step))
        axis.yaxis.set_major_formatter(FormatStrFormatter(tick_format))
        axis.tick_params(axis='y', colors=color)
        axis.spines['left'].set_color(color)
        axis.spines['top'].set_visible(False)
        axis.spines['right'].set_visible(False)
        axis.set_axisbelow(True)
        axis.grid(axis='y', color='#D8DEE5', linewidth=0.8, alpha=0.8)
        axis.bar_label(bars, labels=[f'{value:.2f}' for value in values],
                       padding=6, fontsize=args.font_size, color=color)
        for extension in args.formats:
            path = args.output_dir / f'{filename}.{extension}'
            fig.savefig(path, dpi=300, bbox_inches='tight', facecolor='white')
            print(f'Saved: {path}')
        plt.close(fig)
    print(f'Font: {font}; all text: {args.font_size:g} pt')
    for count, amount, ratio in zip(counts, migration, amplification):
        print(f'+{count} OSD: migration={amount:.6f} GiB, amplification={ratio:.6f}x')


if __name__ == '__main__':
    main()
