"""
InstaGrid — Профиль-менеджер (Camoufox + BrowserForge).

Управление браузерными профилями:
- Создание: папка + JSON fingerprint (BrowserForge), генерируется один раз навсегда
- Запуск: Camoufox persistent_context + proxy + fingerprint + GeoIP timezone
- WebRTC заблокирован, язык en-US
- Retry при ошибке прокси: до 3 попыток, экспоненциальная задержка
- Geometry: пресеты по ОС (windows_large_1680x1050 и т.д.)
- KNOWN-GOOD: camoufox==0.4.11, playwright==1.53.0

Параметры из browser_launcher.py SparkGrid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from browserforge.fingerprints import (
    FingerprintGenerator, Screen, Fingerprint,
    ScreenFingerprint, NavigatorFingerprint, VideoCard,
)
from camoufox import AsyncNewBrowser
from camoufox.addons import DefaultAddons
from playwright.async_api import BrowserContext, Page

from backend.config import PROFILES_DIR as _CONFIG_PROFILES_DIR

logger = logging.getLogger("instagrid.profile_manager")


# ─── Утилита: dict → объект с атрибутами ─────────────────────────────────────

class _Namespace:
    """dict с доступом через точку: ns.navigator.userAgent"""
    def __init__(self, d: dict):
        for k, v in d.items():
            if isinstance(v, dict):
                setattr(self, k, _Namespace(v))
            elif isinstance(v, list):
                setattr(self, k, [_Namespace(i) if isinstance(i, dict) else i for i in v])
            else:
                setattr(self, k, v)

    def __repr__(self):
        return f"_Namespace({self.__dict__})"


def _dict_to_namespace(d: dict) -> _Namespace:
    """Конвертирует dict в объект с атрибутами (legacy, не для Camoufox)."""
    return _Namespace(d)


def _build_dataclass(cls, data: dict):
    """Собирает dataclass из dict, подставляя None для отсутствующих полей."""
    import dataclasses
    if not isinstance(data, dict):
        return None
    kwargs = {}
    for f in dataclasses.fields(cls):
        kwargs[f.name] = data.get(f.name)
    return cls(**kwargs)


def fingerprint_from_dict(d: dict) -> Fingerprint:
    """
    Реконструирует настоящий browserforge.Fingerprint из сохранённого JSON.

    КРИТИЧНО: без этого Camoufox генерирует НОВЫЙ случайный fingerprint
    при каждом запуске. Тот же аккаунт → разный Canvas/WebGL/шрифты/UA
    каждую сессию = мгновенный флаг для Instagram.
    """
    screen = _build_dataclass(ScreenFingerprint, d.get("screen") or {})
    navigator = _build_dataclass(NavigatorFingerprint, d.get("navigator") or {})

    video_card = None
    vc = d.get("videoCard")
    if isinstance(vc, dict):
        video_card = _build_dataclass(VideoCard, vc)

    return Fingerprint(
        screen=screen,
        navigator=navigator,
        headers=d.get("headers") or {},
        videoCodecs=d.get("videoCodecs") or {},
        audioCodecs=d.get("audioCodecs") or {},
        pluginsData=d.get("pluginsData") or {},
        battery=d.get("battery"),
        videoCard=video_card,
        multimediaDevices=d.get("multimediaDevices") or [],
        fonts=d.get("fonts") or [],
        mockWebRTC=d.get("mockWebRTC"),
        slim=d.get("slim"),
    )

# ─── Константы ────────────────────────────────────────────────────────────────

PROFILES_DIR = _CONFIG_PROFILES_DIR
FINGERPRINT_FILE = "fingerprint.json"
META_FILE = "meta.json"

# Язык — всегда en-US (из SparkGrid)
DEFAULT_LOCALE = "en-US"
DEFAULT_LANGUAGES = ["en-US", "en"]

# Retry при ошибке прокси
MAX_PROXY_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # секунды, экспоненциальная задержка


# ─── Geometry пресеты ─────────────────────────────────────────────────────────

class OsPlatform(Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


@dataclass(frozen=True)
class ScreenGeometry:
    """Полная геометрия окна: screen ≠ outer ≠ viewport (из SparkGrid)."""
    # Экран монитора
    screen_width: int
    screen_height: int
    avail_width: int       # screen - taskbar
    avail_height: int
    # Окно браузера (рамки + тулбар)
    outer_width: int
    outer_height: int
    # Видимая область контента
    viewport_width: int
    viewport_height: int
    # Позиция окна на экране
    position_x: int
    position_y: int
    label: str

    # Backward compat
    @property
    def width(self) -> int:
        return self.viewport_width

    @property
    def height(self) -> int:
        return self.viewport_height


def _windows_geometry(screen_w: int, screen_h: int, label: str) -> ScreenGeometry:
    """Генерирует реалистичную Windows геометрию из размера экрана (порт SparkGrid)."""
    avail_w = screen_w
    avail_h = max(700, screen_h - 40)  # taskbar ~40px
    outer_w = max(1180, min(avail_w - 56, round(avail_w * 0.967)))
    outer_h = max(780, min(avail_h - 24, round(avail_h * 0.965)))
    viewport_w = outer_w - 16    # window chrome
    viewport_h = outer_h - 88   # toolbar + tabs
    pos_x = max(12, (screen_w - outer_w) // 2)
    pos_y = 18
    return ScreenGeometry(
        screen_width=screen_w, screen_height=screen_h,
        avail_width=avail_w, avail_height=avail_h,
        outer_width=outer_w, outer_height=outer_h,
        viewport_width=viewport_w, viewport_height=viewport_h,
        position_x=pos_x, position_y=pos_y,
        label=label,
    )


# Пресеты привязаны к ОС (из SparkGrid browser_launcher.py)
GEOMETRY_PRESETS: dict[OsPlatform, list[ScreenGeometry]] = {
    OsPlatform.WINDOWS: [
        _windows_geometry(1366, 768, "windows_1366x768"),
        _windows_geometry(1440, 900, "windows_1440x900"),
        _windows_geometry(1536, 864, "windows_1536x864"),
        _windows_geometry(1600, 900, "windows_1600x900"),
        _windows_geometry(1680, 1050, "windows_1680x1050"),
        _windows_geometry(1920, 1080, "windows_1920x1080"),
    ],
    OsPlatform.MACOS: [
        ScreenGeometry(
            screen_width=1512, screen_height=982,
            avail_width=1512, avail_height=944,
            outer_width=1424, outer_height=896,
            viewport_width=1408, viewport_height=804,
            position_x=44, position_y=28,
            label="macos_retina_1512x982",
        ),
        ScreenGeometry(
            screen_width=1440, screen_height=900,
            avail_width=1440, avail_height=860,
            outer_width=1360, outer_height=824,
            viewport_width=1344, viewport_height=736,
            position_x=40, position_y=18,
            label="macos_1440x900",
        ),
    ],
    OsPlatform.LINUX: [
        _windows_geometry(1366, 768, "linux_1366x768"),
        _windows_geometry(1920, 1080, "linux_1920x1080"),
    ],
}


# ─── GeoIP ────────────────────────────────────────────────────────────────────

class GeoIPResolver:
    """
    Определяет timezone по IP прокси через MaxMind GeoIP2.
    Если база не установлена — fallback на America/New_York.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._reader = None
        self._db_path = db_path or "GeoLite2-City.mmdb"
        try:
            import geoip2.database
            p = Path(self._db_path)
            if p.exists():
                self._reader = geoip2.database.Reader(str(p))
                logger.info("GeoIP2 database loaded: %s", p)
            else:
                logger.warning("GeoIP2 database not found at %s, using fallback timezone", p)
        except ImportError:
            logger.warning("geoip2 not installed, using fallback timezone")

    def get_timezone(self, ip: str) -> str:
        """Возвращает IANA timezone для IP. Fallback: America/New_York."""
        if not self._reader:
            return "America/New_York"
        try:
            resp = self._reader.city(ip)
            tz = resp.location.time_zone
            if tz:
                return tz
        except Exception as e:
            logger.warning("GeoIP lookup failed for %s: %s", ip, e)
        return "America/New_York"

    def close(self) -> None:
        if self._reader:
            self._reader.close()
            self._reader = None


# ─── Fingerprint ──────────────────────────────────────────────────────────────

def generate_fingerprint(
    os_platform: OsPlatform = OsPlatform.WINDOWS,
    screen_geometry: ScreenGeometry | None = None,
) -> dict[str, Any]:
    """
    Генерирует fingerprint через BrowserForge.
    Вызывается ОДИН раз при создании профиля, сохраняется навсегда.

    BrowserForge генерирует: UA, platform, screen, codecs, плагины — всё консистентно.
    Прокси НЕ влияет на fingerprint seed — можно менять прокси без смены identity.
    """
    if screen_geometry is None:
        presets = GEOMETRY_PRESETS.get(os_platform, GEOMETRY_PRESETS[OsPlatform.WINDOWS])
        screen_geometry = presets[0]  # default: первый пресет

    fg = FingerprintGenerator(
        browser="firefox",
        os=os_platform.value,
    )

    # Ограничиваем экран выбранным пресетом, чтобы screen в fingerprint
    # совпадал с geometry профиля. Иначе BrowserForge выдаст свой размер,
    # и метаданные профиля разойдутся с тем, что реально видит сайт.
    fingerprint = fg.generate(
        screen=Screen(
            min_width=screen_geometry.screen_width,
            max_width=screen_geometry.screen_width,
            min_height=screen_geometry.screen_height,
            max_height=screen_geometry.screen_height,
        ),
    )

    # BrowserForge возвращает Fingerprint-объект, конвертируем в dict для JSON
    def _to_dict(obj):
        if hasattr(obj, '__dict__'):
            return {k: _to_dict(v) for k, v in obj.__dict__.items() if not k.startswith('_')}
        elif isinstance(obj, list):
            return [_to_dict(i) for i in obj]
        return obj

    return _to_dict(fingerprint)


# ─── Модель профиля ──────────────────────────────────────────────────────────

@dataclass
class ProfileInfo:
    """Метаданные браузерного профиля."""
    profile_id: str           # = account username
    profile_dir: Path         # путь к папке профиля
    os_platform: OsPlatform
    screen_geometry: ScreenGeometry
    created_at: float = field(default_factory=time.time)
    last_used_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_dir": str(self.profile_dir),
            "os_platform": self.os_platform.value,
            "screen_width": self.screen_geometry.viewport_width,
            "screen_height": self.screen_geometry.viewport_height,
            "screen_label": self.screen_geometry.label,
            "screen_geometry": {
                "screen_width": self.screen_geometry.screen_width,
                "screen_height": self.screen_geometry.screen_height,
                "avail_width": self.screen_geometry.avail_width,
                "avail_height": self.screen_geometry.avail_height,
                "outer_width": self.screen_geometry.outer_width,
                "outer_height": self.screen_geometry.outer_height,
                "viewport_width": self.screen_geometry.viewport_width,
                "viewport_height": self.screen_geometry.viewport_height,
                "position_x": self.screen_geometry.position_x,
                "position_y": self.screen_geometry.position_y,
            },
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileInfo:
        os_plat = OsPlatform(d["os_platform"])
        sg = d.get("screen_geometry")
        if sg and isinstance(sg, dict):
            geom = ScreenGeometry(
                screen_width=sg.get("screen_width", d.get("screen_width", 1366)),
                screen_height=sg.get("screen_height", d.get("screen_height", 768)),
                avail_width=sg.get("avail_width", sg.get("screen_width", 1366)),
                avail_height=sg.get("avail_height", sg.get("screen_height", 728)),
                outer_width=sg.get("outer_width", d.get("screen_width", 1366) - 16),
                outer_height=sg.get("outer_height", d.get("screen_height", 768) - 88),
                viewport_width=sg.get("viewport_width", d.get("screen_width", 1350)),
                viewport_height=sg.get("viewport_height", d.get("screen_height", 680)),
                position_x=sg.get("position_x", 28),
                position_y=sg.get("position_y", 18),
                label=d.get("screen_label", "migrated"),
            )
        else:
            # Backward compat: old format with just screen_width/screen_height
            w = d.get("screen_width", 1366)
            h = d.get("screen_height", 768)
            geom = _windows_geometry(w, h, d.get("screen_label", f"migrated_{w}x{h}"))
        return cls(
            profile_id=d["profile_id"],
            profile_dir=Path(d["profile_dir"]),
            os_platform=os_plat,
            screen_geometry=geom,
            created_at=d.get("created_at", 0),
            last_used_at=d.get("last_used_at"),
        )


# ─── Основной менеджер ───────────────────────────────────────────────────────

class ProfileManager:
    """
    Управляет Camoufox профилями:
    - create_profile()  → генерация fingerprint + папка
    - launch_profile()  → Camoufox persistent_context + proxy
    - close_profile()   → закрытие контекста, папка сохраняется
    - delete_profile()  → удаление папки
    - list_profiles()   → список всех профилей
    """

    def __init__(
        self,
        profiles_dir: str | Path = PROFILES_DIR,
        geoip_db_path: str | None = None,
    ) -> None:
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.geoip = GeoIPResolver(geoip_db_path)
        # Активные контексты: profile_id → BrowserContext
        self._active_contexts: dict[str, BrowserContext] = {}
        # Один playwright на процесс (см. _get_playwright)
        self._playwright = None

    # ── Создание профиля ──────────────────────────────────────────────────

    def create_profile(
        self,
        profile_id: str,
        os_platform: OsPlatform = OsPlatform.WINDOWS,
        screen_geometry: ScreenGeometry | None = None,
    ) -> ProfileInfo:
        """
        Создаёт новый профиль: папка + fingerprint.json + meta.json.
        Fingerprint генерируется один раз и сохраняется навсегда.
        """
        profile_dir = self.profiles_dir / profile_id
        if profile_dir.exists():
            logger.warning("Profile %s already exists, skipping creation", profile_id)
            return self.get_profile_info(profile_id)

        # Выбираем geometry
        if screen_geometry is None:
            presets = GEOMETRY_PRESETS.get(os_platform, GEOMETRY_PRESETS[OsPlatform.WINDOWS])
            # Случайный пресет для разнообразия
            import random
            screen_geometry = random.choice(presets)

        profile_dir.mkdir(parents=True, exist_ok=True)

        # Генерируем fingerprint (один раз навсегда)
        fp = generate_fingerprint(os_platform, screen_geometry)
        fp_path = profile_dir / FINGERPRINT_FILE
        fp_path.write_text(json.dumps(fp, indent=2, ensure_ascii=False), encoding="utf-8")

        # Сохраняем мету
        info = ProfileInfo(
            profile_id=profile_id,
            profile_dir=profile_dir,
            os_platform=os_platform,
            screen_geometry=screen_geometry,
        )
        meta_path = profile_dir / META_FILE
        meta_path.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")

        logger.info(
            "Created profile %s [%s, %s]",
            profile_id, os_platform.value, screen_geometry.label,
        )
        return info

    # ── Запуск профиля ────────────────────────────────────────────────────

    async def launch_profile(
        self,
        profile_id: str,
        proxy: dict[str, str] | None = None,
        headless: bool = False,
    ) -> tuple[BrowserContext, Page]:
        """
        Запускает Camoufox в persistent_context.

        Args:
            profile_id: ID профиля (username аккаунта)
            proxy: {"server": "host:port", "username": "...", "password": "..."}
            headless: запуск без окна

        Returns:
            (BrowserContext, Page) — контекст и первая страница

        Retry: до 3 попыток с экспоненциальной задержкой при ошибке прокси.
        """
        profile_dir = self.profiles_dir / profile_id
        if not profile_dir.exists():
            raise FileNotFoundError(f"Profile {profile_id} not found")

        # Загружаем СОХРАНЁННЫЙ fingerprint и реконструируем настоящий
        # browserforge.Fingerprint — Camoufox примет только его.
        fp_path = profile_dir / FINGERPRINT_FILE
        fp_dict = json.loads(fp_path.read_text(encoding="utf-8"))
        try:
            fingerprint = fingerprint_from_dict(fp_dict)
        except Exception as e:
            logger.error(
                "Profile %s: cannot rebuild fingerprint (%s). "
                "Camoufox would generate a RANDOM one — aborting to avoid identity drift.",
                profile_id, e,
            )
            raise RuntimeError(f"Corrupt fingerprint for {profile_id}: {e}") from e

        # Загружаем мету для geometry
        meta_path = profile_dir / META_FILE
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        info = ProfileInfo.from_dict(meta)
        geom = info.screen_geometry
        screen_w = geom.viewport_width
        screen_h = geom.viewport_height

        # GeoIP timezone по прокси
        timezone_id = "America/New_York"
        if proxy:
            server_str = proxy["server"]
            # Убираем схему для GeoIP lookup
            proxy_host = server_str.split("://")[-1].split(":")[0].split("@")[-1]
            timezone_id = self.geoip.get_timezone(proxy_host)

        # Формируем Camoufox proxy config (поддержка HTTP/HTTPS/SOCKS5)
        camoufox_proxy = None
        if proxy:
            server = proxy["server"]
            # Определяем протокол — если уже есть схема, оставляем как есть
            if not any(server.startswith(s) for s in ("http://", "https://", "socks5://")):
                server = f"http://{server}"
            camoufox_proxy = {
                "server": server,
                "username": proxy.get("username", ""),
                "password": proxy.get("password", ""),
            }

        # Retry с экспоненциальной задержкой
        last_error: Exception | None = None
        for attempt in range(1, MAX_PROXY_RETRIES + 1):
            try:
                context = await self._do_launch(
                    profile_dir=profile_dir,
                    fingerprint=fingerprint,
                    proxy=camoufox_proxy,
                    timezone_id=timezone_id,
                    screen_w=screen_w,
                    screen_h=screen_h,
                    headless=headless,
                )
                page = context.pages[0] if context.pages else await context.new_page()

                # ВАЖНО: никакого JS-инжекта геометрии.
                # Object.defineProperty(window,'outerWidth',{value:...}) заменяет
                # нативный accessor на data-property — это ловится одной строкой:
                #   Object.getOwnPropertyDescriptor(window,'outerWidth').get === undefined
                # У настоящего Firefox там getter. Всю геометрию (screen/outer/inner/
                # availWidth/screenX) Camoufox проставляет сам на уровне движка
                # из переданного fingerprint — нативно и неотличимо.

                self._active_contexts[profile_id] = context

                # Обновляем last_used_at
                self._update_last_used(profile_id)

                logger.info(
                    "Launched profile %s (attempt %d, tz=%s, proxy=%s)",
                    profile_id, attempt, timezone_id,
                    proxy["server"] if proxy else "none",
                )
                return context, page

            except Exception as e:
                last_error = e
                logger.warning(
                    "Launch attempt %d/%d failed for %s: %s",
                    attempt, MAX_PROXY_RETRIES, profile_id, e,
                )
                if attempt < MAX_PROXY_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"Failed to launch profile {profile_id} after {MAX_PROXY_RETRIES} attempts: {last_error}"
        )

    async def _get_playwright(self):
        """
        Один общий playwright-инстанс на весь процесс.

        Было: async_playwright().start() на КАЖДЫЙ запуск профиля, без .stop().
        Каждый вызов поднимает отдельный node-драйвер; ссылка перетиралась,
        старые процессы висели навсегда. На сотнях аккаунтов сервер съедало.
        """
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _do_launch(
        self,
        profile_dir: Path,
        fingerprint: Fingerprint,
        proxy: dict | None,
        timezone_id: str,
        screen_w: int,
        screen_h: int,
        headless: bool,
    ) -> BrowserContext:
        """Внутренний запуск Camoufox с persistent_context."""
        user_data_dir = str(profile_dir / "browser_data")

        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "persistent_context": True,
            "user_data_dir": user_data_dir,

            # ── ГЛАВНОЕ: постоянный fingerprint аккаунта ──
            # Без этого Camoufox генерирует новый случайный при каждом старте.
            # Отсюда же берутся screen/outer/inner/availWidth/screenX —
            # нативно, без JS-подмены.
            "fingerprint": fingerprint,

            # ── GeoIP: timezone/locale/geolocation/WebRTC по IP прокси ──
            # Camoufox сам определяет exit-IP через прокси и делает
            # согласованными timezone, язык, координаты и WebRTC-адрес.
            # Ручной timezone_id этого не даёт: Intl, Date.getTimezoneOffset
            # и геолокация расходятся между собой.
            "geoip": True,

            # WebRTC НЕ блокируем: у настоящего Firefox он есть, полное
            # отсутствие само по себе аномалия. При geoip=True Camoufox
            # подставляет в WebRTC IP прокси — утечки реального IP нет,
            # а стек выглядит живым.
            "block_webrtc": False,

            # Тёплый HTTP-кеш между сессиями. Без него постоянный профиль
            # каждый раз тянет всё заново и никогда не шлёт
            # If-None-Match/If-Modified-Since — для возвращающегося
            # пользователя это неестественно.
            "enable_cache": True,

            # page.evaluate() исполняется в изолированном мире:
            # наш dialog_gate/JS невидим для скриптов страницы.
            "main_world_eval": False,

            # Camoufox по умолчанию доставляет uBlock Origin.
            # Он режет телеметрию Instagram — их клиентский JS шлёт беконы,
            # которые просто не уходят. Отсутствие ожидаемых запросов
            # заметнее, чем сами запросы. Плюс наличие расширения
            # детектится по инжектируемым стилям.
            "exclude_addons": [DefaultAddons.UBO],
        }

        if proxy:
            launch_kwargs["proxy"] = proxy
        else:
            # Без прокси geoip=True полезет за реальным IP сервера
            launch_kwargs["geoip"] = False
            launch_kwargs["locale"] = DEFAULT_LOCALE
            launch_kwargs["timezone_id"] = timezone_id

        pw = await self._get_playwright()
        context = await AsyncNewBrowser(pw, **launch_kwargs)

        return context

    async def shutdown(self) -> None:
        """Закрывает все профили и общий playwright-инстанс."""
        await self.close_all()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("Playwright stop error: %s", e)
            self._playwright = None

    # ── Закрытие профиля ──────────────────────────────────────────────────

    async def close_profile(self, profile_id: str) -> None:
        """Закрывает браузерный контекст. Папка профиля сохраняется."""
        context = self._active_contexts.pop(profile_id, None)
        if context:
            try:
                await context.close()
                logger.info("Closed profile %s", profile_id)
            except Exception as e:
                logger.warning("Error closing profile %s: %s", profile_id, e)

    async def close_all(self) -> None:
        """Закрывает все активные профили."""
        profile_ids = list(self._active_contexts.keys())
        for pid in profile_ids:
            await self.close_profile(pid)

    # ── Удаление профиля ──────────────────────────────────────────────────

    def delete_profile(self, profile_id: str) -> None:
        """Удаляет папку профиля полностью."""
        if profile_id in self._active_contexts:
            raise RuntimeError(f"Cannot delete active profile {profile_id}, close it first")

        profile_dir = self.profiles_dir / profile_id
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
            logger.info("Deleted profile %s", profile_id)
        else:
            logger.warning("Profile %s not found for deletion", profile_id)

    # ── Информация о профилях ─────────────────────────────────────────────

    def get_profile_info(self, profile_id: str) -> ProfileInfo:
        """Возвращает метаданные профиля."""
        meta_path = self.profiles_dir / profile_id / META_FILE
        if not meta_path.exists():
            raise FileNotFoundError(f"Profile {profile_id} not found")
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return ProfileInfo.from_dict(data)

    def list_profiles(self) -> list[ProfileInfo]:
        """Список всех профилей."""
        result = []
        for d in sorted(self.profiles_dir.iterdir()):
            meta_path = d / META_FILE
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    result.append(ProfileInfo.from_dict(data))
                except Exception as e:
                    logger.warning("Failed to read profile %s: %s", d.name, e)
        return result

    def profile_exists(self, profile_id: str) -> bool:
        """Проверяет существование профиля."""
        return (self.profiles_dir / profile_id / META_FILE).exists()

    def is_active(self, profile_id: str) -> bool:
        """Проверяет, запущен ли профиль."""
        return profile_id in self._active_contexts

    def get_context(self, profile_id: str) -> BrowserContext | None:
        """Возвращает активный BrowserContext или None."""
        return self._active_contexts.get(profile_id)

    # ── Внутренние ────────────────────────────────────────────────────────

    def _update_last_used(self, profile_id: str) -> None:
        """Обновляет last_used_at в meta.json."""
        meta_path = self.profiles_dir / profile_id / META_FILE
        if not meta_path.exists():
            return
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["last_used_at"] = time.time()
            meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to update last_used for %s: %s", profile_id, e)

    def __del__(self) -> None:
        self.geoip.close()
