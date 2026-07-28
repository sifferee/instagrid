"""
InstaGrid — Ротация мобильных прокси.

Мобильные прокси (lteboost, FlashProxy):
- Один прокси-эндпоинт на весь пул (host:port:user:pass), IP меняется через API-ссылку ротации
- Перед запуском профиля:
  1. Дёрнуть ссылку ротации → пауза 20 сек
  2. Проверить что IP сменился
  3. Проверить что IP не встречался в истории (за последние 30 дней)
  4. Если IP использовался — повторная ротация (до 5 попыток)
  5. Запуск профиля
- История IP: mobile_ip_history таблица
- TTL: если IP использовался >30 дней назад — считать чистым
- Реальные мобильные прокси — проверка на DC-IP НЕ нужна
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.database import execute, query, query_one, run_sync

logger = logging.getLogger("instagrid.mobile_proxy")

# ─── Константы ────────────────────────────────────────────────────────────────

ROTATION_PAUSE = 20              # сек — пауза после ротации перед проверкой IP
IP_HISTORY_TTL_DAYS = 30         # дней — старше этого = чистый IP
MAX_ROTATION_ATTEMPTS = 5        # попытки получить чистый IP
IP_CHECK_URL = "https://api.ipify.org?format=json"
IP_CHECK_TIMEOUT = 15            # сек


# ─── Утилиты ─────────────────────────────────────────────────────────────────

# run_sync импортирован из backend.database (Fix #7)
_run_sync = run_sync


# ─── Результат ротации ───────────────────────────────────────────────────────

@dataclass
class RotationResult:
    success: bool
    ip: str = ""
    attempts: int = 0
    pool_id: int = 0
    message: str = ""


# ─── Основной класс ──────────────────────────────────────────────────────────

class MobileProxyRotator:
    """
    Управляет ротацией мобильных прокси.

    Каждый мобильный пул — это один эндпоинт (host:port:user:pass)
    с API-ссылкой ротации (rotation_url). IP меняется при GET-запросе
    на rotation_url.

    Использование:
        rotator = MobileProxyRotator()

        # Получить чистый IP перед запуском профиля
        result = await rotator.rotate_and_verify(pool_id=1, account_id=42)
        if result.success:
            proxy = rotator.get_proxy_config(pool_id=1)
            # запускаем профиль с proxy
    """

    async def rotate_and_verify(
        self,
        pool_id: int,
        account_id: int,
    ) -> RotationResult:
        """
        Полный цикл ротации:
        1. Дёрнуть rotation_url
        2. Пауза 20 сек
        3. Проверить текущий IP через прокси
        4. Проверить что IP чистый (не в истории за 30 дней)
        5. Если грязный — повторить (до 5 раз)
        6. Записать IP в историю

        Returns:
            RotationResult с IP и статусом
        """
        pool = await self._get_pool(pool_id)
        if not pool:
            return RotationResult(success=False, pool_id=pool_id, message="Pool not found")

        rotation_url = pool.get("rotation_url")
        if not rotation_url:
            return RotationResult(success=False, pool_id=pool_id, message="No rotation_url configured")

        proxy_config = self._build_proxy_url(pool)
        if not proxy_config:
            return RotationResult(success=False, pool_id=pool_id, message="Invalid proxy config in pool")

        previous_ip = ""

        for attempt in range(1, MAX_ROTATION_ATTEMPTS + 1):
            logger.info(
                "[pool %d, account %d] Rotation attempt %d/%d",
                pool_id, account_id, attempt, MAX_ROTATION_ATTEMPTS,
            )

            # 1. Дёрнуть ссылку ротации
            rotation_ok = await self._trigger_rotation(rotation_url)
            if not rotation_ok:
                return RotationResult(
                    success=False, pool_id=pool_id, attempts=attempt,
                    message="Rotation URL request failed",
                )

            # 2. Пауза 20 сек — ждём смены IP на стороне провайдера
            logger.debug("[pool %d] Waiting %ds for IP change...", pool_id, ROTATION_PAUSE)
            await asyncio.sleep(ROTATION_PAUSE)

            # 3. Проверить текущий IP
            current_ip = await self._check_current_ip(proxy_config)
            if not current_ip:
                logger.warning("[pool %d] Could not determine current IP", pool_id)
                continue

            # IP не сменился?
            if current_ip == previous_ip:
                logger.warning(
                    "[pool %d] IP didn't change: %s, retrying...",
                    pool_id, current_ip,
                )
                continue

            previous_ip = current_ip

            # 4. Проверить что IP чистый
            is_clean = await self._is_ip_clean(current_ip, pool_id)
            if not is_clean:
                logger.info(
                    "[pool %d] IP %s was used recently, rotating again...",
                    pool_id, current_ip,
                )
                continue

            # 5. Чистый IP — записываем в историю
            await self._record_ip(pool_id, current_ip, account_id)

            logger.info(
                "[pool %d, account %d] Got clean IP: %s (attempt %d)",
                pool_id, account_id, current_ip, attempt,
            )

            return RotationResult(
                success=True,
                ip=current_ip,
                attempts=attempt,
                pool_id=pool_id,
            )

        # Все попытки исчерпаны
        return RotationResult(
            success=False,
            pool_id=pool_id,
            attempts=MAX_ROTATION_ATTEMPTS,
            message=f"Could not get clean IP after {MAX_ROTATION_ATTEMPTS} rotations",
        )

    def get_proxy_config(self, pool: dict[str, Any]) -> dict[str, str]:
        """
        Возвращает proxy dict для ProfileManager.launch_profile().

        Args:
            pool: строка из proxy_pools таблицы

        Returns:
            {"server": "host:port", "username": "...", "password": "..."}
        """
        return {
            "server": f"{pool['proxy_host']}:{pool['proxy_port']}",
            "username": pool.get("proxy_username") or "",
            "password": pool.get("proxy_password") or "",
        }

    async def get_pool(self, pool_id: int) -> dict | None:
        """Возвращает данные мобильного пула."""
        return await self._get_pool(pool_id)

    # ── Внутренние ────────────────────────────────────────────────────────

    async def _get_pool(self, pool_id: int) -> dict | None:
        """Получает мобильный пул из БД."""
        return await _run_sync(
            query_one,
            """SELECT * FROM proxy_pools
               WHERE id = ? AND pool_type = 'mobile'""",
            (pool_id,),
        )

    async def _trigger_rotation(self, rotation_url: str) -> bool:
        """GET-запрос на ссылку ротации провайдера."""
        try:
            async with httpx.AsyncClient(timeout=IP_CHECK_TIMEOUT) as client:
                resp = await client.get(rotation_url)
                logger.debug(
                    "Rotation URL response: %d %s",
                    resp.status_code, resp.text[:100],
                )
                # Большинство провайдеров отвечают 200 при успешной ротации
                return resp.status_code in (200, 201, 204)
        except Exception as e:
            logger.error("Rotation URL request failed: %s", e)
            return False

    async def _check_current_ip(self, proxy_url: str) -> str:
        """Проверяет текущий IP через прокси."""
        try:
            async with httpx.AsyncClient(
                timeout=IP_CHECK_TIMEOUT,
                proxy=proxy_url,
                verify=False,
            ) as client:
                resp = await client.get(IP_CHECK_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("ip", "")
        except Exception as e:
            logger.warning("IP check failed: %s", e)
        return ""

    async def _is_ip_clean(self, ip: str, pool_id: int) -> bool:
        """
        Проверяет что IP не использовался за последние 30 дней.
        TTL: если IP использовался >30 дней назад — считать чистым.
        """
        ttl_threshold = time.time() - (IP_HISTORY_TTL_DAYS * 86400)

        row = await _run_sync(
            query_one,
            """SELECT id FROM mobile_ip_history
               WHERE ip_address = ? AND pool_id = ? AND used_at > ?
               LIMIT 1""",
            (ip, pool_id, ttl_threshold),
        )
        return row is None

    async def _record_ip(self, pool_id: int, ip: str, account_id: int) -> None:
        """Записывает IP в историю."""
        await _run_sync(
            execute,
            """INSERT INTO mobile_ip_history (pool_id, ip_address, account_id, used_at)
               VALUES (?, ?, ?, unixepoch('now'))""",
            (pool_id, ip, account_id),
        )

    def _build_proxy_url(self, pool: dict[str, Any]) -> str:
        """Собирает proxy URL для httpx."""
        host = pool.get("proxy_host", "")
        port = pool.get("proxy_port", "")
        user = pool.get("proxy_username", "")
        pwd = pool.get("proxy_password", "")

        if not host or not port:
            return ""

        if user and pwd:
            return f"http://{user}:{pwd}@{host}:{port}"
        return f"http://{host}:{port}"

    # ── Утилиты ───────────────────────────────────────────────────────────

    async def get_ip_history(
        self,
        pool_id: int,
        limit: int = 50,
    ) -> list[dict]:
        """Возвращает последние N записей истории IP."""
        return await _run_sync(
            query,
            """SELECT h.*, a.username
               FROM mobile_ip_history h
               LEFT JOIN accounts a ON a.id = h.account_id
               WHERE h.pool_id = ?
               ORDER BY h.used_at DESC LIMIT ?""",
            (pool_id, limit),
        )

    async def cleanup_old_history(self, days: int = 60) -> int:
        """Удаляет записи старше N дней."""
        threshold = time.time() - (days * 86400)
        result = await _run_sync(
            execute,
            "DELETE FROM mobile_ip_history WHERE used_at < ?",
            (threshold,),
        )
        logger.info("Cleaned up IP history older than %d days", days)
        return result

    async def get_pool_stats(self, pool_id: int) -> dict[str, Any]:
        """Статистика мобильного пула."""
        total_ips = await _run_sync(
            query_one,
            "SELECT COUNT(DISTINCT ip_address) as cnt FROM mobile_ip_history WHERE pool_id = ?",
            (pool_id,),
        )
        ttl_threshold = time.time() - (IP_HISTORY_TTL_DAYS * 86400)
        recent_ips = await _run_sync(
            query_one,
            """SELECT COUNT(DISTINCT ip_address) as cnt
               FROM mobile_ip_history WHERE pool_id = ? AND used_at > ?""",
            (pool_id, ttl_threshold),
        )
        last_rotation = await _run_sync(
            query_one,
            "SELECT MAX(used_at) as ts FROM mobile_ip_history WHERE pool_id = ?",
            (pool_id,),
        )

        return {
            "pool_id": pool_id,
            "total_unique_ips": total_ips["cnt"],
            "recent_ips_30d": recent_ips["cnt"],
            "last_rotation_at": last_rotation["ts"] if last_rotation else None,
        }
