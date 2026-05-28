"""Canonical SHA-256 chain block-keying for token-level traces — single source of truth.

raw prompt → token_ids → 固定大小 block 切分 → 链式 hash:
    K[0] = sha256(b"" + payload_0)
    K[i] = sha256(K[i-1] + payload_i)
其中 payload_i = ",".join(str(t) for t in token_ids[i*bs:(i+1)*bs]).encode("utf-8")。

此前 lib.prompt_encoder.HFTokenEncoder / convert_trace._sha256_chain / app_report._chain_bytes
各写一份, 现统一。返回 full 32-byte digest; 需要紧凑 hex 的调用方 (convert_trace CSV) 自行
`k.hex()[:N]` 截断 —— 链本身始终用 full digest, 截断只发生在输出边界。

注意: `sim.core.prefix_key` 是**另一套** key 体系 (输入是别人算好的未链化 hash_ids, 且要带
model_id 再链化), 不是这里。输入形态 → key 体系对照见 docs/terminology.md §6.5。
"""
from __future__ import annotations

import hashlib


def sha256_chain_tokens(token_ids: list[int], block_size: int) -> list[bytes]:
    """token_ids → 每个 block 一个链式 SHA-256 digest (full 32 bytes)。"""
    keys: list[bytes] = []
    prev = b""
    for i in range(0, len(token_ids), block_size):
        payload = ",".join(str(t) for t in token_ids[i:i + block_size]).encode("utf-8")
        h = hashlib.sha256()
        h.update(prev)
        h.update(payload)
        prev = h.digest()
        keys.append(prev)
    return keys
