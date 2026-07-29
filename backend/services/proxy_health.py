"""
InstaGrid — Предполётная проверка статических прокси.

Зачем
-----
Раньше мёртвый прокси обнаруживался только после полного запуска браузера:
поднять Camoufox, дождаться NS_ERROR_ABORT, закрыть — 30-60 секунд впустую,
и так три раза. Пул при этом не чистился: нерабочий адрес возвращался
обратно и снова попадал следующему аккаунту.

Как работает
------------
Перед тем как отдать прокси профилю, делаем короткий HTTP-запрос через него
с таймаутом 5 секунд.

  Ответил   → счётчик неудач сбрасывается, прокси уходит в работу
  Молчит    → счётчик +1, прокси возвращается в пул
  3 неудачи → прокси удаляется навсегда

Считаем именно неудачи подряд: одиночный сбой сети не должен убивать
рабочий адрес. Успешная проверка обнуляет счётчик.

Проверяем ТОЛЬКО при первичной выдаче прокси аккаунту. Если прокси уже
закреплён за аккаунтом и работал — лишний запрос не делаем: это и трафик,
и ещё один след в логах провайдера.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.database import execute, query_one, run_sync

logger = logging.getLogger("instagrid.proxy_health")

# Таймаут проверки
CHECK_TIMEOUT = 5.0

# Сколько неудач подряд до удаления
MAX_FAILS = 3

# Куда стучимся. Лёгкие эндпоинты, отдают несколько байт.
CHECK_URLS = (
    "https://api.ipify.org?format=json",
    "https://ipinfo.io/ip",
)


def _proxy_url(proxy: dict[str, Any]) -> str:
    """Собирает URL прокси для httpx из данных БД."""
    protocol = proxy.get("protocol") or "http"
    host = proxy["host"]
    port = proxy["port"]
    user = proxy.get("username")
    pwd = proxy.get("password")
    if user and pwd:
        return f"{protocol}://{user}:{pwd}@{host}:{port}"
    return f"{protocol}://{host}:{port}"


async def is_proxy_alive(proxy: dict[str, Any]) -> tuple[bool, str]:
    """
    Проверяет отзывчивость прокси.

    Returns:
        (жив, описание) — при успехе в описании выходной IP.
    """
    url = _proxy_url(proxy)
    last_err = "no response"

    for check_url in CHECK_URLS:
        try:
            async with httpx.AsyncClient(
                proxy=url, timeout=CHECK_TIMEOUT, verify=True,
            ) as client:
                r = await client.get(check_url)
                if r.status_code == 200:
                    text = r.text.strip()
                    ip = text
                    if text.startswith("{"):
                        try:
                            ip = r.json().get("ip", text)
                        except Exception:
                            pass
                    return True, str(ip)[:45]
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = type(e).__name__

    return False, last_err


async def verify_before_bind(proxy_id: int, proxy: dict[str, Any]) -> bool:
    """
    Проверяет прокси перед выдачей аккаунту и ведёт счётчик неудач.

    Returns:
        True  — прокси жив, можно работать
        False — прокси не ответил (возвращён в пул или удалён)
    """
    server = f"{proxy['host']}:{proxy['port']}"
    alive, info = await is_proxy_alive(proxy)

    if alive:
        # Обнуляем счётчик — серия неудач прервана
        await run_sync(
            execute,
            "UPDATE static_proxies SET fail_count = 0 WHERE id = ?",
            (proxy_id,),
        )
        logger.info("Proxy %s alive (exit IP %s)", server, info)
        return True

    # Не ответил — увеличиваем счётчик
    row = await run_sync(
        query_one,
        "SELECT fail_count FROM static_proxies WHERE id = ?",
        (proxy_id,),
    )
    fails = int((row or {}).get("fail_count") or 0) + 1

    if fails >= MAX_FAILS:
        await run_sync(
            execute, "DELETE FROM static_proxies WHERE id = ?", (proxy_id,),
        )
        logger.warning(
            "Proxy %s DELETED after %d consecutive failures (last: %s)",
            server, fails, info,
        )
    else:
        await run_sync(
            execute,
            "UPDATE static_proxies "
            "SET fail_count = ?, status = 'available', account_id = NULL, "
            "    used_at = unixepoch('now') "
            "WHERE id = ?",
            (fails, proxy_id),
        )
        logger.warning(
            "Proxy %s unreachable (%s), failure %d/%d — returned to pool",
            server, info, fails, MAX_FAILS,
        )

    return False


async def acquire_working_proxy(
    pool_id: int,
    get_next_proxy,
    max_tries: int = 8,
) -> dict[str, Any] | None:
    """
    Берёт из пула прокси, который реально отвечает.

    Перебирает адреса, пока не найдёт живой либо не упрётся в max_tries.
    Нерабочие по пути либо возвращаются в пул со счётчиком, либо удаляются.

    Returns:
        dict прокси (с ключом "id") или None, если живых не нашлось.
    """
    for attempt in range(1, max_tries + 1):
        proxy_data = await get_next_proxy(pool_id)
        if not proxy_data:
            logger.error("Pool %s: no available proxies left", pool_id)
            return None

        proxy_id = proxy_data.get("id")

        # Для проверки нужны host/port отдельно — восстанавливаем из server
        server = str(proxy_data.get("server", ""))
        bare = server.split("://")[-1]
        host, _, port_s = bare.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            logger.warning("Proxy %s: cannot parse port, skipping", server)
            continue

        protocol = server.split("://")[0] if "://" in server else "http"

        check_data = {
            "host": host,
            "port": port,
            "username": proxy_data.get("username") or None,
            "password": proxy_data.get("password") or None,
            "protocol": protocol,
        }

        if await verify_before_bind(proxy_id, check_data):
            return proxy_data

        logger.info("Trying next proxy (%d/%d)...", attempt, max_tries)

    logger.error(
        "Pool %s: no working proxy found after %d tries", pool_id, max_tries,
    )
    return None
