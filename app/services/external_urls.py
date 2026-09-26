"""Validate external pane URLs (https only, no private/loopback, no credentials)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

MAX_URL_LEN = 1024


_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.google.com",
}


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_external_url(raw: str, *, resolve_dns: bool = True) -> str:
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_LEN:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_external_url", "message": "https URL required"},
        )
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_external_url", "message": "only https external URLs are allowed"},
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_external_url", "message": "credentials in URLs are forbidden"},
        )
    host = (parsed.hostname or "").strip().lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_external_url", "message": "private or loopback hosts are forbidden"},
        )
    try:
        ipaddress.ip_address(host)
        if _is_private_ip(host):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_external_url", "message": "private IP literals are forbidden"},
            )
    except ValueError:
        pass
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_external_url", "message": "DNS resolution failed"},
            ) from exc
        for info in infos:
            ip = info[4][0]
            if _is_private_ip(str(ip)):
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_external_url", "message": "URL resolves to a private address"},
                )
    return url
