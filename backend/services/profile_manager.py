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

from browserforge.fingerprints import FingerprintGenerator, Screen
from camoufox import AsyncNewBrowser
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
    """Конвертирует dict в объект с атрибутами для Camoufox."""
    return _Namespace(d)

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
    width: int
    height: int
    label: str


# Пресеты привязаны к ОС (из browser_launcher.py)
GEOMETRY_PRESETS: dict[OsPlatform, list[ScreenGeometry]] = {
    OsPlatform.WINDOWS: [
        ScreenGeometry(1280, 720, "windows_hd_1280x720"),
        ScreenGeometry(1366, 768, "windows_small_1366x768"),
        ScreenGeometry(1280, 800, "windows_medium_1280x800"),
        ScreenGeometry(1360, 768, "windows_medium_1360x768"),
        ScreenGeometry(1440, 810, "windows_medium_1440x810"),
    ],
    OsPlatform.MACOS: [
        ScreenGeometry(1280, 800, "macos_default_1280x800"),
        ScreenGeometry(1366, 768, "macos_small_1366x768"),
        ScreenGeometry(1440, 810, "macos_medium_1440x810"),
    ],
    OsPlatform.LINUX: [
        ScreenGeometry(1366, 768, "linux_small_1366x768"),
        ScreenGeometry(1280, 720, "linux_hd_1280x720"),
        ScreenGeometry(1440, 810, "linux_medium_1440x810"),
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

    fingerprint = fg.generate()

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
            "screen_width": self.screen_geometry.width,
            "screen_height": self.screen_geometry.height,
            "screen_label": self.screen_geometry.label,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileInfo:
        os_plat = OsPlatform(d["os_platform"])
        geom = ScreenGeometry(d["screen_width"], d["screen_height"], d["screen_label"])
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

        # Загружаем fingerprint и конвертируем dict → объект с атрибутами (Camoufox требует .navigator и т.д.)
        fp_path = profile_dir / FINGERPRINT_FILE
        fp_dict = json.loads(fp_path.read_text(encoding="utf-8"))
        fingerprint = _dict_to_namespace(fp_dict)

        # Загружаем мету для geometry
        meta_path = profile_dir / META_FILE
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        screen_w = meta["screen_width"]
        screen_h = meta["screen_height"]

        # GeoIP timezone по прокси
        timezone_id = "America/New_York"
        if proxy:
            proxy_host = proxy["server"].split(":")[0]
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

    async def _do_launch(
        self,
        profile_dir: Path,
        fingerprint: dict,
        proxy: dict | None,
        timezone_id: str,
        screen_w: int,
        screen_h: int,
        headless: bool,
    ) -> BrowserContext:
        """Внутренний запуск Camoufox с persistent_context."""
        from playwright.async_api import async_playwright

        # user_data_dir — куки/кеш сохраняются между сессиями
        user_data_dir = str(profile_dir / "browser_data")

        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "persistent_context": True,
            "user_data_dir": user_data_dir,
            # Camoufox сам генерирует fingerprint через BrowserForge
            # Передаём только os для правильной генерации
            "os": "windows",
            # WebRTC всегда заблокирован
            "block_webrtc": True,
            # Язык всегда en-US
            "locale": DEFAULT_LOCALE,
            # Viewport = geometry пресет
            "viewport": {"width": screen_w, "height": screen_h},
            # Timezone по GeoIP
            "timezone_id": timezone_id,
            # Geolocation выключена
            "geolocation": None,
            "permissions": [],
        }

        if proxy:
            launch_kwargs["proxy"] = proxy

        # Camoufox AsyncNewBrowser требует playwright instance
        pw = await async_playwright().start()
        self._playwright = pw  # сохраняем чтобы закрыть потом
        context = await AsyncNewBrowser(pw, **launch_kwargs)

        return context

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
