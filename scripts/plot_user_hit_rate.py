#!/usr/bin/env python3
"""每个 user 一张 grouped bar 图: 理想 KV cache 命中率 × 模型上下文窗口 × 天.

x 轴分若干大组 (模型上下文窗口, 如 GLM-5-32K / 64K / 128K), 每组内若干根
紧挨的柱子 (按 days 顺序, 左=第一天 右=第二天). 柱顶标 hit_rate%, 柱内标
请求条数 n=.... 每个 user 输出一张 PNG.

输入 JSON (默认 data/user_hit_rate.json)::

    {
      "users": [
        {
          "app": "EurekaX",
          "user_id": "S0...961",
          "days": ["5.13", "5.14"],
          "groups": [
            {"name": "GLM-5-32K",
             "days": [{"requests": 1374, "hit_rate": 68.27},
                      {"requests": 3722, "hit_rate": 66.48}]},
            ...
          ]
        }
      ]
    }

hit_rate 单位=百分比 (0-100). 之后补另外几组数据只需往 users[] 加对象.

Usage
-----
    .venv/bin/python3 scripts/plot_user_hit_rate.py \
        --data data/user_hit_rate.json --output-dir outputs/hit_rate

注意: 用 .venv (有 matplotlib), 不是 .venv_glm5. 本机无中文字体且 air-gapped,
故轴标签用英文以免渲染成方块; 组名/日期均为 ASCII 不受影响.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# vendor 的开源中文字体 (Noto Sans SC, SIL OFL); --lang zh 默认用它
DEFAULT_ZH_FONT = os.path.join(REPO_ROOT, "fonts", "NotoSansSC-VF.ttf")

# 第一天→第二天... 的配色 (够 4 天用)
DAY_COLORS = ["#4878cf", "#d65f5f", "#6acc65", "#ee854a"]

# 文案 (中/英). zh 需要已注册的中文字体, 否则汉字渲染成方块.
LABELS = {
    "en": {
        "ylabel": "Ideal KV cache hit rate (%)",
        "subtitle": "Ideal KV cache hit rate by model context window",
        "day": lambda i, d: f"Day {i} ({d})",
        "count": lambda n: f"n={n:,}",
    },
    "zh": {
        "ylabel": "理想 KV cache 命中率 (%)",
        "subtitle": "各模型上下文窗口的理想 KV cache 命中率",
        "day": lambda i, d: f"第{i}天 ({d})",
        "count": lambda n: f"{n:,} 条",
    },
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="每个 user 一张理想 KV cache 命中率 grouped bar 图",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--data", default="data/user_hit_rate.json",
                   help="输入 JSON (含 users[])")
    p.add_argument("--output-dir", default="outputs/hit_rate",
                   help="PNG 输出目录")
    p.add_argument("--lang", choices=["en", "zh"], default="en",
                   help="标签语言. zh 需配 --font 指向中文字体, 否则汉字成方块")
    p.add_argument("--font", default=None,
                   help="中文字体路径 (.ttf/.otf). 例: /mnt/c/Windows/Fonts/simhei.ttf")
    p.add_argument("--dpi", type=int, default=150)
    return p.parse_args()


def setup_font(font_path: str | None) -> None:
    """注册指定字体并设为默认 sans-serif (用于渲染中文)."""
    if not font_path:
        return
    if not os.path.isfile(font_path):
        raise SystemExit(f"字体文件不存在: {font_path}")
    import matplotlib
    from matplotlib import font_manager
    font_manager.fontManager.addfont(font_path)
    name = font_manager.FontProperties(fname=font_path).get_name()
    matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False  # 防负号缺字形
    print(f"已注册字体: {name}  ({font_path})")


def _slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s).strip("_")


def plot_user(user: dict, output_dir: str, dpi: int, lang: str = "en") -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    L = LABELS[lang]
    # zh 模式标题用中文原名 (app); en 模式用 app_en, 缺则用 ASCII 的 app, 再退回 user_id.
    app_cn = user.get("app", "")
    if lang == "zh":
        title_id = app_cn or user.get("app_en") or user.get("user_id", "")
    else:
        title_id = user.get("app_en") or (app_cn if app_cn.isascii() else "") \
            or user.get("user_id", "")
    uid = user.get("user_id", "")
    # 文件名始终用 ASCII (app_en/ascii app), 避免中文文件名
    fname_label = user.get("app_en") or (app_cn if app_cn.isascii() else "")
    day_labels = user["days"]
    groups = user["groups"]

    n_groups = len(groups)
    n_days = len(day_labels)
    x = np.arange(n_groups)
    # 每组内柱子总宽 0.8, 平分给各天
    total_w = 0.8
    bar_w = total_w / n_days

    fig, ax = plt.subplots(figsize=(max(7, 2.6 * n_groups), 5.5))

    max_val = max(g["days"][di]["hit_rate"]
                  for g in groups for di in range(n_days))
    ymax = min(100, max_val + 14)
    inside_y = ymax * 0.03           # 柱内 n= 标注的离底高度
    tall_enough = ymax * 0.13        # 柱子高于此才把 n= 放柱内 (白字)

    for di in range(n_days):
        vals = [g["days"][di]["hit_rate"] for g in groups]
        reqs = [g["days"][di]["requests"] for g in groups]
        # 该天的柱子整体在组中心两侧均匀排布
        offset = (di - (n_days - 1) / 2) * bar_w
        xpos = x + offset
        bars = ax.bar(xpos, vals, bar_w, label=L["day"](di + 1, day_labels[di]),
                      color=DAY_COLORS[di % len(DAY_COLORS)], edgecolor="white",
                      linewidth=0.5)
        for rect, v, n in zip(bars, vals, reqs):
            cx = rect.get_x() + rect.get_width() / 2
            ax.annotate(f"{v:.2f}%", (cx, v), textcoords="offset points",
                        xytext=(0, 3), ha="center", va="bottom",
                        fontsize=9, fontweight="bold")
            if v >= tall_enough:      # 高柱: 条数竖排放柱内, 白字
                ax.text(cx, inside_y, L["count"](n), ha="center", va="bottom",
                        rotation=90, fontsize=7.5, color="white")
            else:                     # 矮柱/0 柱: 条数放在 % 标签上方, 灰字
                ax.annotate(L["count"](n), (cx, v), textcoords="offset points",
                            xytext=(0, 15), ha="center", va="bottom",
                            fontsize=7, color="#555555")

    ax.set_xticks(x)
    ax.set_xticklabels([g["name"] for g in groups], fontsize=11)
    ax.set_ylabel(L["ylabel"], fontsize=11)
    ax.set_ylim(0, ymax)
    ax.set_title(f"{title_id}\n{L['subtitle']}", fontsize=12)
    ax.legend(title="", fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_axisbelow(True)

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    fname = f"{_slug(fname_label) or 'app'}_{_slug(uid)}.png"
    path = os.path.join(output_dir, fname)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> None:
    args = parse_args()
    font = args.font
    if args.lang == "zh" and not font:
        if os.path.isfile(DEFAULT_ZH_FONT):
            font = DEFAULT_ZH_FONT          # 用 vendor 的 Noto Sans SC
        else:
            print(f"⚠ --lang zh 但找不到 vendor 字体 {DEFAULT_ZH_FONT}, "
                  "汉字会渲染成方块. 用 --font 指定一个中文字体.")
    setup_font(font)
    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    users = data["users"] if isinstance(data, dict) else data
    if not users:
        print("输入里没有 user, 无事可做.")
        return
    for user in users:
        path = plot_user(user, args.output_dir, args.dpi, args.lang)
        print(f"图已保存: {path}  ({user.get('app','')} / {user.get('user_id','')})")
    print(f"共 {len(users)} 张图 → {args.output_dir}/")


if __name__ == "__main__":
    main()
