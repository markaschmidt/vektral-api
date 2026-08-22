"""Slug helpers."""

from __future__ import annotations

import re
import secrets


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "item") -> str:
    s = (text or "").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or fallback


def unique_slug(text: str, *, suffix_bytes: int = 3) -> str:
    base = slugify(text)[:60]
    return f"{base}-{secrets.token_hex(suffix_bytes)}"
