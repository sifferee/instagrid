"""
InstaGrid — Персональное расписание аккаунтов.

Проблема, которую это решает
----------------------------
Было: пауза между сессиями глобальная. Батч отработал → все аккаунты спят
10-14 часов → все просыпаются вместе. То есть аккаунт №1 и аккаунт №300
раз за разом активны в одном и том же окне и вместе замолкают.

Сотни аккаунтов с разными IP, разными отпечатками и разными нишами, которые
оживают и затихают синхронно — это координированное поведение. Оно ловится
корреляцией по времени и не лечится ни прокси, ни fingerprint'ом: там нечего
подделывать, паттерн виден на уровне «когда пришли запросы».

Второе: не было привязки ко времени суток. Аккаунт с нью-йоркским прокси
и timezone America/New_York постил в 4:17 утра — стабильно, месяцами.
У живого человека есть сон.

Что делает этот модуль
----------------------
1. У каждого аккаунта своё окно активности (например 08:30–23:10), выведенное
   детерминированно из SHA256 username — стабильное между перезапусками.
2. Своё смещение внутри цикла, чтобы аккаунты расползлись по суткам.
3. Время считается в таймзоне прокси аккаунта, не сервера.
4. Выходные: раз в 5-9 дней аккаунт пропускает сутки — люди не постят
   каждый день без единого пропуска.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

logger = logging.getLogger("instagrid.scheduler")

# Базовый цикл между сессиями
CYCLE_MIN_HOURS = 10.0
CYCLE_MAX_HOURS = 14.0

# Разброс старта между аккаунтами, чтобы не шли пачкой
DESYNC_SPREAD_HOURS = 9.0

# Вероятность "выходного" — сутки без активности
OFF_DAY_EVERY_MIN = 5
OFF_DAY_EVERY_MAX = 9


def _seed_int(username: str, salt: str = "") -> int:
    """Детерминированное число из username — стабильно между перезапусками."""
    raw = f"{salt}:{username}".encode("utf-8", "ignore")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def active_window(username: str) -> tuple[float, float]:
    """
    Окно активности аккаунта в часах локального времени.

    Возвращает (начало, конец), например (8.5, 23.2) = 08:30–23:12.
    Ранние пташки, совы и обычные — распределение как у людей.
    """
    rnd = random.Random(_seed_int(username, "window"))

    kind = rnd.random()
    if kind < 0.20:
        # Ранняя пташка
        start = rnd.uniform(6.0, 8.0)
        end = rnd.uniform(20.5, 22.5)
    elif kind < 0.80:
        # Обычный режим
        start = rnd.uniform(8.0, 10.5)
        end = rnd.uniform(22.0, 23.9)
    else:
        # Сова — активна за полночь
        start = rnd.uniform(10.5, 13.0)
        end = rnd.uniform(24.5, 26.5)  # >24 = после полуночи

    return start, end


def desync_offset_hours(username: str) -> float:
    """
    Персональное смещение старта. Именно это расталкивает аккаунты
    по суткам, чтобы они не ходили одной волной.
    """
    rnd = random.Random(_seed_int(username, "desync"))
    return rnd.uniform(0.0, DESYNC_SPREAD_HOURS)


def is_off_day(username: str, day_number: int) -> bool:
    """
    Выходной — сутки без постинга. Человек не публикует каждый день
    подряд месяцами без единого пропуска.
    """
    rnd = random.Random(_seed_int(username, "offday"))
    period = rnd.randint(OFF_DAY_EVERY_MIN, OFF_DAY_EVERY_MAX)
    shift = rnd.randint(0, period - 1)
    return (day_number + shift) % period == 0


def _tz_offset_hours(timezone_id: str | None) -> float:
    """Смещение таймзоны в часах. Без zoneinfo — грубая таблица."""
    if not timezone_id:
        return -5.0  # America/New_York по умолчанию
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_id))
        off = now.utcoffset()
        return off.total_seconds() / 3600.0 if off else 0.0
    except Exception:
        table = {
            "America/New_York": -4.0, "America/Chicago": -5.0,
            "America/Denver": -6.0, "America/Los_Angeles": -7.0,
            "Europe/London": 1.0, "Europe/Berlin": 2.0,
            "Europe/Moscow": 3.0, "Asia/Tokyo": 9.0,
        }
        return table.get(timezone_id, -4.0)


def local_hour(ts: float, timezone_id: str | None) -> float:
    """Локальный час (0-24) для таймстемпа в таймзоне аккаунта."""
    off = _tz_offset_hours(timezone_id)
    local = datetime.fromtimestamp(ts, dt_timezone.utc) + timedelta(hours=off)
    return local.hour + local.minute / 60.0


def is_within_active_window(
    username: str,
    timezone_id: str | None = None,
    at: float | None = None,
) -> bool:
    """Попадает ли момент в окно активности аккаунта."""
    ts = at if at is not None else time.time()
    hour = local_hour(ts, timezone_id)
    start, end = active_window(username)

    if end <= 24.0:
        return start <= hour <= end
    # Окно переходит за полночь
    return hour >= start or hour <= (end - 24.0)


def next_session_at(
    username: str,
    timezone_id: str | None = None,
    last_session: float | None = None,
) -> float:
    """
    Когда аккаунту работать в следующий раз.

    Базовый цикл 10-14 часов + персональное смещение, затем результат
    подтягивается в окно активности. Если выпал выходной — переносим на сутки.
    """
    now = time.time()
    base = last_session if last_session else now

    rnd = random.Random(_seed_int(username, f"cycle:{int(base)}"))
    cycle = rnd.uniform(CYCLE_MIN_HOURS, CYCLE_MAX_HOURS) * 3600.0

    if last_session is None:
        # Первый запуск: разбрасываем аккаунты по суткам
        candidate = now + desync_offset_hours(username) * 3600.0
    else:
        candidate = base + cycle

    if candidate < now:
        candidate = now

    # Подтягиваем в окно активности
    start, end = active_window(username)
    for _ in range(4):
        hour = local_hour(candidate, timezone_id)
        inside = (start <= hour <= end) if end <= 24.0 else (hour >= start or hour <= end - 24.0)
        if inside:
            break
        # Не в окне — двигаем к ближайшему открытию + случайные минуты
        delta = (start - hour) % 24.0
        candidate += delta * 3600.0 + rnd.uniform(0, 55 * 60)

    # Выходной?
    day_number = int(candidate // 86400)
    if is_off_day(username, day_number):
        candidate += rnd.uniform(20.0, 28.0) * 3600.0
        logger.debug("[%s] Off-day, shifted to %s", username,
                     datetime.fromtimestamp(candidate).isoformat(timespec="minutes"))

    # Финальный джиттер ±25 минут
    candidate += rnd.uniform(-25 * 60, 25 * 60)

    return candidate


def describe(username: str, timezone_id: str | None = None) -> dict[str, Any]:
    """Человекочитаемое описание расписания — для UI и логов."""
    start, end = active_window(username)

    def fmt(h: float) -> str:
        h = h % 24.0
        return f"{int(h):02d}:{int((h % 1) * 60):02d}"

    return {
        "username": username,
        "active_from": fmt(start),
        "active_to": fmt(end),
        "desync_offset_h": round(desync_offset_hours(username), 2),
        "timezone": timezone_id or "America/New_York",
        "active_now": is_within_active_window(username, timezone_id),
    }
