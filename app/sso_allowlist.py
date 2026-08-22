"""VR SSO ``client_callback`` allowlist (lifted from domain/sso_vr_bridge.jac)."""

from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    port_suffix = ""
    if port and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        port_suffix = f":{port}"
    return f"{parsed.scheme}://{host}{port_suffix}"


def is_loopback_callback(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "") in _LOOPBACK_HOSTS


def is_vr_callback_allowed(url: str, allowlist: list[str]) -> bool:
    """Reject open redirects — callback origin must match the allowlist."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    # Loopback is always accepted for local VR (Vite :5173/:5174).
    if is_loopback_callback(url):
        return True

    target_origin = origin_of(url)
    normalized = url if url.endswith("/") else f"{url}/"
    for entry in allowlist:
        if not entry:
            continue
        candidate = entry if "://" in entry else f"http://{entry}"
        try:
            ep = urlparse(candidate)
        except Exception:  # noqa: BLE001
            continue
        if ep.scheme not in ("http", "https") or not ep.hostname:
            continue
        allowed_origin = origin_of(candidate)
        if target_origin == allowed_origin:
            return True
        entry_norm = entry if entry.endswith("/") else f"{entry}/"
        if normalized.startswith(entry_norm) or url.startswith(entry.rstrip("/")):
            return True
    return False
