#!/usr/bin/env python3
"""Cache 压力图: 每分钟 token 涌入 avg/P50/P80/P90, 按模型上下文窗口分组.

读 cache_pressure_stats.py 产出的 JSON, 每个"天"(day) 出一张 grouped bar 图:
x = 模型上下文窗口大组 (GLM-5-32K / 64K / 128K), 每组 4 根紧挨柱子 = avg/P50/P80/P90,
y = 每分钟涌入 token 数. 用于判断要不要做 pooling (看高压 P90 与均值差距).

Usage (matplotlib venv)
-----------------------
    .venv/bin/python3 scripts/plot_cache_pressure.py \
      --stats outputs/cache_pressure/stats.json \
      --output-dir outputs/cache_pressure --lang zh

中文默认用 vendor 的 fonts/NotoSansSC-VF.ttf; 英文 (--lang en) 无需字体.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plot_user_hit_rate import DEFAULT_ZH_FONT, setup_font  # noqa: E402

# 4 个统计量配色: 均值=灰, P50→P90 蓝色渐深 (越深=压力峰值)
STAT_KEYS = ["avg", "p50", "p80", "p90"]
STAT_COLORS = {"avg": "#7f7f7f", "p50": "#9ecae1", "p80": "#4292c6", "p90": "#08519c"}

LABELS = {
    "en": {
        "ylabel": "Tokens per minute (influx)",
        "title": lambda day: (f"Cache pressure — {day}" if day and day != "all"
                              else "Cache pressure"),
        "subtitle": "Per-minute token influx (avg / P50 / P80 / P90)",
        "stat": {"avg": "avg", "p50": "P50", "p80": "P80", "p90": "P90"},
    },
    "zh": {
        "ylabel": "每分钟涌入 token 数",
        "title": lambda day: (f"Cache 压力 — {day}" if day and day != "all"
                              else "Cache 压力"),
        "subtitle": "每分钟 token 涌入分布 (均值 / P50 / P80 / P90)",
        "stat": {"avg": "均值", "p50": "P50", "p80": "P80", "p90": "P90"},
    },
}


def _slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", str(s)).strip("_")


def _fmt(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:.0f}"


def _model_label(row: dict) -> str:
    """x 轴大组标签: 去掉数据集名末尾的 -day → 'GLM-5-32K-0513' → 'GLM-5-32K'."""
    name = row.get("name", "")
    day = row.get("day")
    if day and name.endswith(f"-{day}"):
        return name[: -(len(day) + 1)]
    return row.get("size") or name


def _size_order(row: dict):
    m = re.search(r"(\d+)", row.get("size") or "")
    return int(m.group(1)) if m else 1 << 30


def pick_stats(row: dict, mode: str) -> dict:
    """mode=json → 用 stats 写入时选定的顶层值; span/active → 取对应子口径."""
    if mode in ("span", "active") and mode in row:
        return row[mode]
    return row


def plot_day(day_label, rows: list[dict], output_dir: str, lang: str, dpi: int,
             stat_mode: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    L = LABELS[lang]
    rows = sorted(rows, key=_size_order)
    groups = [_model_label(r) for r in rows]
    x = np.arange(len(groups))
    n_stat = len(STAT_KEYS)
    total_w = 0.8
    bar_w = total_w / n_stat

    fig, ax = plt.subplots(figsize=(max(7, 2.8 * len(groups)), 5.5))
    max_val = 0.0
    for si, key in enumerate(STAT_KEYS):
        vals = [float(pick_stats(r, stat_mode).get(key, 0.0)) for r in rows]
        max_val = max([max_val, *vals])
        offset = (si - (n_stat - 1) / 2) * bar_w
        bars = ax.bar(x + offset, vals, bar_w, label=L["stat"][key],
                      color=STAT_COLORS[key], edgecolor="white", linewidth=0.5)
        for rect, v in zip(bars, vals):
            ax.annotate(_fmt(v), (rect.get_x() + rect.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 2),
                        ha="center", va="bottom", fontsize=7.5)

    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=11)
    ax.set_ylabel(L["ylabel"], fontsize=11)
    ax.set_ylim(0, max_val * 1.18 if max_val > 0 else 1)
    ax.set_title(f"{L['title'](day_label)}\n{L['subtitle']}", fontsize=12)
    ax.legend(title="", fontsize=10, ncol=4)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"cache_pressure_{_slug(day_label)}.png")
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cache 压力 grouped bar 图 (每分钟 token 涌入 avg/P50/P80/P90)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--stats", required=True, help="cache_pressure_stats.py 产出的 JSON")
    p.add_argument("--output-dir", default="outputs/cache_pressure")
    p.add_argument("--lang", choices=["en", "zh"], default="en")
    p.add_argument("--font", default=None, help="中文字体路径 (默认用 vendor 的 Noto)")
    p.add_argument("--stat-mode", choices=["json", "span", "active"], default="json",
                   help="用哪种分钟口径: json=统计时选定的; span/active=改用对应子口径")
    p.add_argument("--dpi", type=int, default=150)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    font = args.font
    if args.lang == "zh" and not font and os.path.isfile(DEFAULT_ZH_FONT):
        font = DEFAULT_ZH_FONT
    if args.lang == "zh" and not (font and os.path.isfile(font)):
        print("⚠ --lang zh 但找不到中文字体, 汉字会成方块. 用 --font 指定.")
    setup_font(font)

    with open(args.stats, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("datasets", [])
    if not rows:
        raise SystemExit("JSON 里没有 datasets, 无可绘制.")

    # 按 day 分组, 每天一张图
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("day_label") or "all", []).append(r)

    for day_label, drows in groups.items():
        path = plot_day(day_label, drows, args.output_dir, args.lang, args.dpi, args.stat_mode)
        print(f"图已保存: {path}  (day={day_label}, {len(drows)} 个数据集)")
    print(f"共 {len(groups)} 张图 → {args.output_dir}/")


if __name__ == "__main__":
    main()
