# fonts/ — vendored 中文字体

为了让中文图表在**任何机器**(含 air-gapped 实验室)都能复现, 这里 vendor 了一个
开源中文字体, 不依赖运行时下载 (与 tokenizer 的 vendor 策略一致, 见 `models/README.md`).

| 文件 | 字体 | 版本 | 许可 |
|---|---|---|---|
| `NotoSansSC-VF.ttf` | Noto Sans SC (variable) | 2.04 | SIL OFL 1.1 |

- 版权: © 2014-2021 Adobe (http://www.adobe.com/), Reserved Font Name 'Source'
  (Noto Sans SC 由 Adobe Source Han Sans 构建).
- 完整许可: [`OFL.md`](OFL.md) (SIL Open Font License 1.1, 允许嵌入/再分发, 含公开仓库).

## 用法

`scripts/plot_user_hit_rate.py` 在 `--lang zh` 时默认用本字体, 无需 `--font`:

```bash
.venv/bin/python3 scripts/plot_user_hit_rate.py \
  --data data/user_hit_rate.json --output-dir outputs/hit_rate --lang zh
```

如需换字体 (如本机黑体), 显式传 `--font /path/to/font.ttf`.
