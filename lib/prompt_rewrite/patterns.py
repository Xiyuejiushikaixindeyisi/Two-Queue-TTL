"""Regex patterns for dynamic-content detection in tool definitions.

Patterns are ported from the upstream demo (`detect_dynamic_tools.py`).
The only configurable axis is the list of Unix absolute-path root directories:
deployments differ on whether `/data /work /srv` etc. should be recognised, so
the list is overridable via JSON config (`configs/dynamic_patterns.json`).

YAML is intentionally avoided — it would be a new runtime dependency.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_UNIX_PATH_ROOTS: tuple[str, ...] = (
    "home", "Users", "usr", "var", "tmp", "opt", "etc",
    "mnt", "root", "deploy", "srv",
)

# json-schema.org URLs are JSON-Schema boilerplate; strip before scanning to
# avoid scoring `$schema: https://json-schema.org/...` as dynamic content.
_STATIC_URL_RE = re.compile(r"https?://json-schema\.org\S*")

_WIN_PATH_RE = re.compile(
    r"(?<![a-zA-Z])"   # not preceded by a letter (avoids mid-URL match)
    r"[A-Za-z]:[/\\]"  # drive letter + colon + separator
    r"[\w/\\.\- ]+"    # path body
)

_FILE_URI_RE = re.compile(r"file:///[\w/\\.\-: ]+")

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Date (and optional time): 2020-01-01 through 2039-12-31.
_DATE_RE = re.compile(
    r"\b20[2-3]\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"
)


def _build_unix_path_re(roots: Iterable[str]) -> re.Pattern[str]:
    """Compile Unix-path regex with the given root directory whitelist."""
    root_alt = "|".join(re.escape(r) for r in roots)
    return re.compile(
        rf"(?:(?<=\s)|(?<=\")|(?<=\')|\A)/(?:{root_alt})[\w/.\-]+"
    )


@dataclass(frozen=True)
class Patterns:
    """Compiled regex bundle used by detection and normalization."""

    static_url: re.Pattern[str]
    win_path: re.Pattern[str]
    unix_path: re.Pattern[str]
    file_uri: re.Pattern[str]
    uuid: re.Pattern[str]
    date: re.Pattern[str]

    def dynamic_labelled(self) -> list[tuple[str, re.Pattern[str]]]:
        """Return [(label, pattern)] in the order used by detection."""
        return [
            ("win_path", self.win_path),
            ("unix_path", self.unix_path),
            ("file_uri", self.file_uri),
            ("uuid", self.uuid),
            ("date", self.date),
        ]


def build_patterns(unix_path_roots: Iterable[str] | None = None) -> Patterns:
    roots = tuple(unix_path_roots) if unix_path_roots is not None else DEFAULT_UNIX_PATH_ROOTS
    return Patterns(
        static_url=_STATIC_URL_RE,
        win_path=_WIN_PATH_RE,
        unix_path=_build_unix_path_re(roots),
        file_uri=_FILE_URI_RE,
        uuid=_UUID_RE,
        date=_DATE_RE,
    )


DEFAULT_PATTERNS = build_patterns()


def load_patterns_config(path: str | Path) -> Patterns:
    """Load a JSON config overriding pattern parameters.

    Config schema (all keys optional):
        {
            "unix_path_roots": ["home", "Users", "data", "srv", ...]
        }
    """
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return build_patterns(unix_path_roots=cfg.get("unix_path_roots"))
