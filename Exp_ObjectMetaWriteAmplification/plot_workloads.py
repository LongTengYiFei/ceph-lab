#!/usr/bin/env python3
"""Plot the saved Workload A/B measurements without accessing the cluster."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, PercentFormatter


HERE = Path(__file__).resolve().parent
GIB = 1024**3
DEFAULT_RUNS = [
    HERE / "results/20261111T094932Z-ycsb-a",
    HERE / "results/20261111T122359Z-ycsb-b",
]
WORKLOAD_COLORS = ["#376B9B", "#D8794A"]
PATH_COLORS = ["#7699BE", "#E5AD61", "#81B29A", "#D3D8DE"]
PATH_LABELS = ["后端 DB/日志", "缓存 DB/日志", "缓存对象数据", "计数残差"]


def configure_fonts(size):
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    for path in candidates:
        if path.exists():
            font_manager.fontManager.addfont(str(path))
            family = font_manager.FontProperties(fname=str(path)).get_name()
            break
    else:
        raise RuntimeError("需要安装 Noto Sans CJK 或文泉驿字体，以绘制中文标签。")
    plt.rcParams.update({
        "font.family": family,
        "font.size": size,
        "axes.titlesize": size,
        "axes.labelsize": size,
        "xtick.labelsize": size,
        "ytick.labelsize": size,
        "legend.fontsize": size,
        "figure.titlesize": size,
        "axes.unicode_minus": False,
        "pdf.fonttype": 3,
        "svg.fonttype": "none",
        "axes.titlepad": 18,
        "axes.labelpad": 10,
    })


def load_measurements(directories):
    rows = []
    for name, directory in zip(("A", "B"), directories):
        summary = json.loads((directory / "summary.json").read_text())
        result = json.loads((directory / "results.json").read_text())
        assert result["passed"] and result["cleanup_complete"], directory
        paths = summary["path_estimates"]
        total = summary["ssd_b_written_bytes"]
        client = summary["client"]["successful_write_bytes"]
        parts = [paths["backend"]["bluefs_write_bytes"],
                 paths["cache"]["bluefs_write_bytes"],
                 paths["cache"]["object_write_path_bytes"]]
        parts.append(total - sum(parts))
        assert all(value >= 0 for value in parts)
        assert sum(parts) == total
        rows.append({
            "workload": name,
            "client_write_bytes": client,
            "ssd_b_write_bytes": total,
            "write_amplification": total / client,
            "backend_bluefs_bytes": parts[0],
            "cache_bluefs_bytes": parts[1],
            "cache_object_path_bytes": parts[2],
            "residual_bytes": parts[3],
        })
    return rows


def style_axis(ax):
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#E1E6EB", linewidth=0.9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#A7B1BA")
    ax.tick_params(axis="both", length=0, pad=9)
    ax.set_xticks([0, 1], ["Workload A", "Workload B"])
    ax.set_xlim(-0.65, 1.65)


def draw_bars(ax, values, title, ylabel, decimals=3):
    style_axis(ax)
    ax.set_title(title, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, max(values) * 1.23)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=3))
    bars = ax.bar([0, 1], values, width=0.52, color=WORKLOAD_COLORS, zorder=3)
    suffix = "×" if ylabel == "写入比（倍）" else ""
    offset = ax.get_ylim()[1] * 0.025
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + offset,
                f"{value:.{decimals}f}{suffix}", ha="center", va="bottom",
                fontweight="bold")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-size", type=float, default=18)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--output-dir", type=Path, default=HERE / "figures")
    args = parser.parse_args()
    configure_fonts(args.font_size)
    rows = load_measurements(DEFAULT_RUNS)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(18, 12.5), facecolor="white")
    fig.subplots_adjust(left=0.085, right=0.98, top=0.86, bottom=0.20,
                        wspace=0.28, hspace=0.48)

    draw_bars(axes[0, 0], [row["client_write_bytes"] / GIB for row in rows],
              "（a）客户端成功更新载荷 D", "写入量（GiB）", decimals=1)
    draw_bars(axes[0, 1], [row["ssd_b_write_bytes"] / GIB for row in rows],
              "（b）六块 SSD B 整盘写入", "写入量（GiB）", decimals=1)
    draw_bars(axes[1, 0], [row["write_amplification"] for row in rows],
              "（c）写放大", "写入比（倍）", decimals=1)

    ax = axes[1, 1]
    style_axis(ax)
    ax.set_title("（d）SSD B 写入来源（内部计数估计）", fontweight="bold")
    ax.set_ylabel("占整盘写入量的比例")
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
    bottoms = [0.0, 0.0]
    fields = ["backend_bluefs_bytes", "cache_bluefs_bytes",
              "cache_object_path_bytes", "residual_bytes"]
    for field, color, label in zip(fields, PATH_COLORS, PATH_LABELS):
        values = [100 * row[field] / row["ssd_b_write_bytes"] for row in rows]
        ax.bar([0, 1], values, width=0.52, bottom=bottoms, color=color,
               label=label, zorder=3)
        for index, value in enumerate(values):
            if value >= 4:
                ax.text(index, bottoms[index] + value / 2, f"{value:.1f}%",
                        ha="center", va="center", fontweight="bold", color="#172B3A")
            bottoms[index] += value
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2,
              frameon=False, columnspacing=1.2, handlelength=1.2)

    # Keep a single explicit font size for titles, ticks, legends and annotations.
    for text in fig.findobj(match=matplotlib.text.Text):
        text.set_fontsize(args.font_size)
    stem = args.output_dir / "workload-a-b-overview"
    for extension in ("png", "pdf", "svg"):
        path = stem.with_suffix("." + extension)
        fig.savefig(path, dpi=args.dpi, facecolor="white",
                    bbox_inches="tight", pad_inches=0.12)
        print(path)
    with (args.output_dir / "workload-a-b-plot-data.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    plt.close(fig)


if __name__ == "__main__":
    main()
