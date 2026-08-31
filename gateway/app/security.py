"""上游 URL 安全校验：scheme 仅 http/https；host 不得解析为环回/私有/保留地址。"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


def _is_forbidden_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_safe_upstream_url(
    url: str, resolver: Callable[[str], list[str]] | None = None
) -> None:
    """校验失败抛 ValueError；resolver(host)->IP 列表，默认 socket.getaddrinfo。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme 仅允许 http/https: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 缺少 host: {url!r}")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        if resolver is not None:
            ips = resolver(host)
        else:
            ips = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    if not ips:
        raise ValueError(f"无法解析 host: {host}")
    for ip in ips:
        if _is_forbidden_ip(ip):
            raise ValueError(f"禁止访问环回/私有/保留地址: {host} -> {ip}")
