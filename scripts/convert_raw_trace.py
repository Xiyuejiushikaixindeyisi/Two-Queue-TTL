#!/usr/bin/env python3
"""DEPRECATED — use `scripts/convert_trace.py --mode raw` instead.

This file is now a thin shim that forwards old-style invocations to
`scripts/convert_trace.py --mode raw --chat-mode wrap_user`, which fixes a
historical issue: the original `convert_raw_trace.py` tokenized prompts with
tiktoken (or UTF-8 bytes) and did NOT apply the model's chat template, so its
output never aligned with what vllm-ascend actually caches.

The new path uses the vendored GLM-5 tokenizer + chat_template wrapping +
SHA-256 chain hashing (same as `lib/prompt_encoder.HFTokenEncoder`), aligned
with vllm-ascend at 128 token / block.

Migration: pass `--mode raw` to `convert_trace.py` directly. The CLI flags
are otherwise unchanged. See `docs/stage3_prompt_rewrite_plan.md` D10 for the
decision rationale.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print(
    "[convert_raw_trace.py] DEPRECATED — forwarding to "
    "`convert_trace.py --mode raw --chat-mode wrap_user`. "
    "See docs/stage3_prompt_rewrite_plan.md (decision D10) for migration notes.",
    file=sys.stderr,
)

# Inject --mode raw before any user-supplied args, unless they already passed it.
if "--mode" not in sys.argv:
    sys.argv[1:1] = ["--mode", "raw"]

# Default chat-mode to wrap_user (matches GLM-V5 default in step1.6 plan).
if "--chat-mode" not in sys.argv:
    sys.argv[1:1] = ["--chat-mode", "wrap_user"]

from convert_trace import main  # noqa: E402

if __name__ == "__main__":
    main()
