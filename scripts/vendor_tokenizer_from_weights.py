"""Vendor a tokenizer out of a local HF model weight directory into models/.

离线 / air-gapped 场景: 模型权重目录 (例如 /mnt/esfs/DeepSeek-V3.1/) 本身已经
带了分词器相关的小文件 (tokenizer.json / tokenizer_config.json / chat_template.jinja
/ config.json ...). 本脚本把这些**小文件**抽出来放到 models/<name>_tokenizer/,
**绝不复制权重** (*.safetensors / *.bin / ...), 并从 config.json 推导 kv_meta.json
(KV cache 单 token 字节数, 供 GB/min cache 压力估算; 缺了它 hit-rate 仍能跑).

加进来后即可用 (无需改任何分析代码, 平台按 --tokenizer-path 选模型):
    --encoder hf_token --tokenizer-path models/<name>_tokenizer

用法:
    python scripts/vendor_tokenizer_from_weights.py \
        --src  /mnt/esfs/DeepSeek-V3.1 \
        --name deepseek_v31 \
        --verify

    # 只预览要复制哪些文件 + kv_meta 推导, 不落盘:
    python scripts/vendor_tokenizer_from_weights.py --src ... --name ... --dry-run

命名约定 (跟现有 glm5_tokenizer / qwen_v3_tokenizer 对齐, 全小写, 去掉点):
    Qwen-V3.5    → --name qwen_v35      → models/qwen_v35_tokenizer/
    DeepSeek-V4  → --name deepseek_v4   → models/deepseek_v4_tokenizer/
    DeepSeek-V3.1→ --name deepseek_v31  → models/deepseek_v31_tokenizer/
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

# 抽取的小文件白名单 (存在才复制; 不同模型子集不同).
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "chat_template.jinja",
    "chat_template.json",
    "tokenizer.model",   # sentencepiece (DeepSeek / Llama 系)
    "vocab.json",        # 老式 BPE (无 tokenizer.json 时的后备)
    "merges.txt",
    "added_tokens.json",
)

# 防御性: 绝不复制权重 / 大分片文件 (即使误列进白名单也跳过).
WEIGHT_EXTS = (
    ".safetensors", ".bin", ".gguf", ".pt", ".pth",
    ".h5", ".msgpack", ".ckpt", ".onnx",
)

# torch_dtype → 每元素字节数 (KV cache 默认与权重 dtype 同).
DTYPE_BYTES = {
    "bfloat16": 2, "float16": 2, "half": 2, "fp16": 2, "bf16": 2,
    "float32": 4, "float": 4, "fp32": 4,
    "float8_e4m3fn": 1, "float8_e5m2": 1, "fp8": 1,
}


def _resolve_text_config(config: dict) -> dict:
    """多模态模型 (如 DeepSeek-OCR) 把 LLM 配置塞在子字段里; 找到带 num_hidden_layers 的那层."""
    if "num_hidden_layers" in config:
        return config
    for key in ("text_config", "language_config", "llm_config", "thinker_config", "decoder"):
        sub = config.get(key)
        if isinstance(sub, dict) and "num_hidden_layers" in sub:
            return sub
    raise ValueError(
        "config.json 里找不到 num_hidden_layers (顶层及 text_config/language_config/"
        "llm_config 子字段都没有). 请人工核对该模型 config 结构后手写 kv_meta.json."
    )


def compute_kv_bytes_per_token(config: dict) -> dict:
    """从 HF config.json 推导 kv_meta 字段 (区分 MLA 与 GQA/MHA).

    返回的 dict 含完整推导链 (formula / num_layers / derivation / kv_bytes_per_token),
    与现有 models/*/kv_meta.json 字段对齐, 便于 audit. 不含 _comment/model/config_source
    (由 build_kv_meta 补).
    """
    cfg = _resolve_text_config(config)
    num_layers = int(cfg["num_hidden_layers"])

    dtype = (config.get("torch_dtype") or cfg.get("torch_dtype") or "bfloat16")
    dtype = str(dtype).replace("torch.", "")
    dtype_bytes = DTYPE_BYTES.get(dtype.lower())
    dtype_note = None
    if dtype_bytes is None:
        dtype_bytes = 2
        dtype_note = f"未知 torch_dtype={dtype!r}, 默认按 2 字节 (bf16); 请人工核对."
    elif dtype_bytes < 2:
        dtype_note = (
            f"torch_dtype={dtype} → {dtype_bytes} 字节. KV cache 通常仍用 bf16(2字节); "
            "若 vLLM 未开 fp8 kv-cache, 应把 dtype_bytes 改回 2."
        )

    # MLA (DeepSeek / GLM-5 系): KV 压成 latent, 每 token 每层 = kv_lora_rank + qk_rope_head_dim, 不乘 2.
    if cfg.get("kv_lora_rank"):
        kv_lora_rank = int(cfg["kv_lora_rank"])
        qk_rope = int(cfg["qk_rope_head_dim"])
        per_token = num_layers * (kv_lora_rank + qk_rope) * dtype_bytes
        meta = {
            "architecture": "MLA (Multi-head Latent Attention; KV 共享 latent)",
            "formula": "num_layers * (kv_lora_rank + qk_rope_head_dim) * dtype_bytes  (不乘 2)",
            "num_layers": num_layers,
            "kv_lora_rank": kv_lora_rank,
            "qk_rope_head_dim": qk_rope,
            "dtype": dtype,
            "dtype_bytes": dtype_bytes,
            "derivation": f"{num_layers} * ({kv_lora_rank} + {qk_rope}) * {dtype_bytes} = {per_token}",
            "kv_bytes_per_token": per_token,
        }
    else:
        # GQA / MHA: 2 (K+V) * num_layers * num_kv_heads * head_dim * dtype_bytes.
        num_kv_heads = int(cfg.get("num_key_value_heads") or cfg["num_attention_heads"])
        head_dim = cfg.get("head_dim")
        if head_dim is None:
            head_dim = int(cfg["hidden_size"]) // int(cfg["num_attention_heads"])
        head_dim = int(head_dim)
        per_token = 2 * num_layers * num_kv_heads * head_dim * dtype_bytes
        meta = {
            "architecture": "GQA" if num_kv_heads < int(cfg.get("num_attention_heads", num_kv_heads)) else "MHA",
            "formula": "2 (K+V) * num_layers * num_kv_heads * head_dim * dtype_bytes",
            "num_layers": num_layers,
            "num_kv_heads": num_kv_heads,
            "head_dim": head_dim,
            "dtype": dtype,
            "dtype_bytes": dtype_bytes,
            "derivation": f"2 * {num_layers} * {num_kv_heads} * {head_dim} * {dtype_bytes} = {per_token}",
            "kv_bytes_per_token": per_token,
        }
    if dtype_note:
        meta["dtype_note"] = dtype_note
    return meta


def build_kv_meta(config: dict, model_name: str, src: Path) -> dict:
    today = _dt.date.today().isoformat()
    meta = {
        "_comment": "KV cache 单 token 字节数. 用于 token-level pipeline 的 cache 压力 GB/min 估算.",
        "model": model_name,
        "config_source": f"{src}/config.json (read {today})",
    }
    meta.update(compute_kv_bytes_per_token(config))
    return meta


def _auto_map_modules(src: Path) -> list[str]:
    """trust_remote_code 模型在 tokenizer_config.json 的 auto_map 里引用 .py; 找出来一并复制."""
    cfg_path = src / "tokenizer_config.json"
    if not cfg_path.is_file():
        return []
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    auto_map = cfg.get("auto_map") or {}
    modules: set[str] = set()
    for val in auto_map.values():
        for ref in (val if isinstance(val, list) else [val]):
            if isinstance(ref, str) and "." in ref:
                modules.add(ref.rsplit(".", 1)[0])  # "tokenization_x.Foo" → "tokenization_x"
    out = []
    for mod in sorted(modules):
        py = f"{mod}.py"
        if (src / py).is_file():
            out.append(py)
    return out


def _plan_files(src: Path) -> list[str]:
    files = [f for f in TOKENIZER_FILES if (src / f).is_file()]
    files += _auto_map_modules(src)
    # 防御: 剔除任何权重扩展名
    files = [f for f in files if not f.lower().endswith(WEIGHT_EXTS)]
    return files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="把本地权重目录里的分词器抽到 models/<name>_tokenizer/ (不复制权重).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--src", required=True, help="源权重目录, 例如 /mnt/esfs/DeepSeek-V3.1")
    p.add_argument("--name", required=True,
                   help="分词器短名 (全小写去点), 例如 deepseek_v31 → models/deepseek_v31_tokenizer/")
    p.add_argument("--model-name", default=None,
                   help="kv_meta.json 里记录的模型全名 (默认用 --src 的目录名)")
    p.add_argument("--models-dir", default="models", help="目标根目录 (默认 models/)")
    p.add_argument("--dry-run", action="store_true", help="只打印计划, 不落盘")
    p.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p.add_argument("--no-kv-meta", action="store_true", help="不生成 kv_meta.json (跳过 GB/min)")
    p.add_argument("--verify", action="store_true",
                   help="落盘后用 transformers 加载并冒烟编码 (需 transformers; 报告 wrap_user 开销)")
    args = p.parse_args(argv)

    src = Path(args.src)
    if not src.is_dir():
        print(f"ERROR: --src 不是目录: {src}", file=sys.stderr)
        return 2
    dest = Path(args.models_dir) / f"{args.name}_tokenizer"
    model_name = args.model_name or src.name

    files = _plan_files(src)
    if not files:
        print(f"ERROR: {src} 里没找到任何分词器文件 ({', '.join(TOKENIZER_FILES)})", file=sys.stderr)
        return 2
    has_core = any(f in files for f in ("tokenizer.json", "tokenizer.model")) or (
        "vocab.json" in files and "merges.txt" in files
    )
    if not has_core:
        print("ERROR: 缺核心词表 (tokenizer.json / tokenizer.model / vocab.json+merges.txt)",
              file=sys.stderr)
        return 2

    config = None
    cfg_path = src / "config.json"
    if not args.no_kv_meta:
        if cfg_path.is_file():
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            print(f"WARN: {cfg_path} 不存在 → 跳过 kv_meta.json (GB/min 列将为空)", file=sys.stderr)

    print(f"源:   {src}")
    print(f"目标: {dest}")
    print(f"将复制 {len(files)} 个文件:")
    for f in files:
        size = (src / f).stat().st_size
        print(f"  - {f}  ({size:,} bytes)")

    kv_meta = None
    if config is not None:
        try:
            kv_meta = build_kv_meta(config, model_name, src)
            print("kv_meta.json 推导:")
            print(f"  architecture     = {kv_meta['architecture']}")
            print(f"  derivation       = {kv_meta['derivation']}")
            print(f"  kv_bytes_per_token = {kv_meta['kv_bytes_per_token']:,}")
            if kv_meta.get("dtype_note"):
                print(f"  ⚠ dtype_note     = {kv_meta['dtype_note']}")
        except (KeyError, ValueError) as e:
            print(f"WARN: kv_meta 推导失败 ({e}) → 不写 kv_meta.json, 请人工补", file=sys.stderr)
            kv_meta = None

    if args.dry_run:
        print("\n[dry-run] 未落盘. 去掉 --dry-run 实际执行.")
        return 0

    if dest.exists() and not args.force:
        print(f"ERROR: {dest} 已存在 (加 --force 覆盖)", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(src / f, dest / f)
    if kv_meta is not None:
        (dest / "kv_meta.json").write_text(
            json.dumps(kv_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("  + kv_meta.json (生成)")

    print(f"\n完成 → {dest}")
    print("下一步:")
    print(f"  1) (可选) --verify 冒烟测试; 2) git add {dest}/ && git commit;")
    print("  3) 在 models/README.md 的「Available tokenizers」表补一行;")
    print(f"  4) 用法: --encoder hf_token --tokenizer-path {dest}")

    if args.verify:
        print("\n--- verify ---")
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        try:
            from lib.hf_tokenizer import apply_template, load_tokenizer
            tok = load_tokenizer(str(dest))
            ids = tok.encode("你好,world", add_special_tokens=False)
            wrapped = apply_template(tok, "你好,world", "wrap_user")
            print(f"  load OK; vocab_size={tok.vocab_size}")
            print(f"  encode('你好,world') = {len(ids)} tokens")
            print(f"  wrap_user('你好,world') = {len(wrapped)} tokens "
                  f"(固定开销 {len(wrapped) - len(ids)} tokens)")
            print("  PASS")
        except Exception as e:  # noqa: BLE001  (冒烟测试: 任何异常都报告而非崩溃)
            print(f"  FAIL: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
