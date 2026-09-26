"""Shared time helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unix_to_iso(ts: int | float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expires_at_from_seconds(expires_in: int) -> str:
    seconds = int(expires_in or 0)
    if seconds <= 0:
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def is_expired(expires_at: str, *, skew_seconds: int = 120) -> bool:
    dt = parse_iso(expires_at)
    if dt is None:
        return False
    return datetime.now(timezone.utc) + timedelta(seconds=skew_seconds) >= dt
