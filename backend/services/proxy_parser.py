"""
InstaGrid — Универсальный парсер прокси.

Поддерживаемые форматы входных строк:
  login:password@hostname:port          ← основной формат InstaGrid
  hostname:port:login:password          ← альтернативный формат
  hostname:port                         ← без авторизации
  http://login:password@hostname:port   ← с явной схемой
  https://login:password@hostname:port
  socks5://login:password@hostname:port ← SOCKS5
  socks5://hostname:port                ← SOCKS5 без авторизации

Выход: унифицированный dict:
  {
      "host": "1.2.3.4",
      "port": 8080,
      "username": "user" | None,
      "password": "pass" | None,
      "protocol": "http" | "https" | "socks5",
      "raw": "original input string",
  }
"""

from __future__ import annotations

import re
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("instagrid.proxy_parser")


def parse_proxy(line: str) -> dict[str, Any] | None:
    """
    Парсит одну строку прокси в любом формате.

    Returns:
        dict с полями host, port, username, password, protocol, raw
        или None если не удалось распарсить
    """
    raw = line.strip()
    if not raw:
        return None

    # Убираем кавычки если есть
    raw = raw.strip("\"'")

    protocol = "http"  # default

    # ── Формат с явной схемой (http://, https://, socks5://) ─────────────
    scheme_match = re.match(r'^(https?|socks5)://', raw, re.IGNORECASE)
    if scheme_match:
        protocol = scheme_match.group(1).lower()
        return _parse_url_format(raw, protocol)

    # ── Формат login:password@hostname:port ──────────────────────────────
    if "@" in raw:
        return _parse_creds_at_host(raw, protocol)

    # ── Формат hostname:port:login:password ──────────────────────────────
    parts = raw.split(":")
    if len(parts) == 4:
        return _parse_host_port_creds(parts, protocol, raw)

    # ── Формат hostname:port (без авторизации) ───────────────────────────
    if len(parts) == 2:
        return _parse_host_port_only(parts, protocol, raw)

    logger.debug("Cannot parse proxy line: %s", raw[:60])
    return None


def _parse_url_format(raw: str, protocol: str) -> dict[str, Any] | None:
    """Парсит формат с URL-схемой: socks5://user:pass@host:port"""
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return None

        return {
            "host": host,
            "port": int(port),
            "username": parsed.username or None,
            "password": parsed.password or None,
            "protocol": protocol,
            "raw": raw,
        }
    except Exception:
        return None


def _parse_creds_at_host(raw: str, protocol: str) -> dict[str, Any] | None:
    """Парсит формат login:password@hostname:port"""
    try:
        creds, hostport = raw.rsplit("@", 1)
        if ":" not in creds or ":" not in hostport:
            return None

        username, password = creds.split(":", 1)
        host, port_str = hostport.rsplit(":", 1)

        return {
            "host": host.strip(),
            "port": int(port_str.strip()),
            "username": username.strip() or None,
            "password": password.strip() or None,
            "protocol": protocol,
            "raw": raw,
        }
    except (ValueError, TypeError):
        return None


def _parse_host_port_creds(
    parts: list[str], protocol: str, raw: str,
) -> dict[str, Any] | None:
    """Парсит формат hostname:port:login:password"""
    try:
        host = parts[0].strip()
        port = int(parts[1].strip())
        username = parts[2].strip() or None
        password = parts[3].strip() or None

        return {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "protocol": protocol,
            "raw": raw,
        }
    except (ValueError, TypeError):
        return None


def _parse_host_port_only(
    parts: list[str], protocol: str, raw: str,
) -> dict[str, Any] | None:
    """Парсит формат hostname:port (без авторизации)"""
    try:
        host = parts[0].strip()
        port = int(parts[1].strip())

        return {
            "host": host,
            "port": port,
            "username": None,
            "password": None,
            "protocol": protocol,
            "raw": raw,
        }
    except (ValueError, TypeError):
        return None


def parse_proxy_list(text: str) -> list[dict[str, Any]]:
    """Парсит многострочный список прокси. Пропускает пустые и нераспознанные."""
    results = []
    for line in text.strip().splitlines():
        parsed = parse_proxy(line)
        if parsed:
            results.append(parsed)
    return results


def proxy_to_playwright(parsed: dict[str, Any]) -> dict[str, str]:
    """
    Конвертирует распаршенный прокси в формат Playwright/Camoufox.

    Playwright proxy format:
        {"server": "http://host:port", "username": "...", "password": "..."}
    или для SOCKS5:
        {"server": "socks5://host:port", "username": "...", "password": "..."}
    """
    protocol = parsed.get("protocol", "http")
    host = parsed["host"]
    port = parsed["port"]

    server = f"{protocol}://{host}:{port}"

    result = {"server": server}
    if parsed.get("username"):
        result["username"] = parsed["username"]
    if parsed.get("password"):
        result["password"] = parsed["password"]

    return result


def proxy_to_httpx(parsed: dict[str, Any]) -> str | None:
    """
    Конвертирует в формат httpx proxy URL.
    httpx: "http://user:pass@host:port" или "socks5://user:pass@host:port"
    """
    protocol = parsed.get("protocol", "http")
    host = parsed["host"]
    port = parsed["port"]
    username = parsed.get("username")
    password = parsed.get("password")

    if username and password:
        return f"{protocol}://{username}:{password}@{host}:{port}"
    return f"{protocol}://{host}:{port}"
