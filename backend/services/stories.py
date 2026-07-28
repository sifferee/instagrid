"""
InstaGrid — Сторис через Instagram Web Private API.

Пайплайн:
1. Pillow: рисуем визуальный стикер (градиент, CTA, иконка) на JPEG 1080×1920
2. POST i.instagram.com/rupload_igphoto — загрузка изображения
3. POST www.instagram.com/api/v1/web/create/configure_to_story/ — публикация с story_link_stickers

Параметры из SparkGrid web_story_link.py:
- Куки: csrftoken, mid, ig_did, www-claim-v2
- Заголовки: x-csrftoken, x-ig-app-id (936619743392459), x-asbd-id (129477), x-mid, x-ig-device-id, x-ig-www-claim
- User-Agent из navigator.userAgent (НЕ мобильный)
- Позиция стикера: base x=0.5 y=0.82, рандомизация ±0.05, размер варьируется
- Автотриггер: рилс с 10к+ просмотров → сторис раз в сутки, разброс ±10-20мин
- Порог просмотров рандомизирован: 10000-12777
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import random
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import BrowserContext, Page

from backend.config import STORY_PHOTOS_DIR
from backend.database import execute, query, query_one, run_sync

logger = logging.getLogger("instagrid.stories")

# ─── Константы ────────────────────────────────────────────────────────────────

STORY_WIDTH = 1080
STORY_HEIGHT = 1920

# Instagram Web API
IG_UPLOAD_URL = "https://i.instagram.com/rupload_igphoto/"
IG_CONFIGURE_URL = "https://www.instagram.com/api/v1/web/create/configure_to_story/"

# Заголовки (фиксированные значения из SparkGrid)
IG_APP_ID = "936619743392459"
IG_ASBD_ID = "129477"

# Стикер: базовая позиция и рандомизация
STICKER_BASE_X = 0.5
STICKER_BASE_Y = 0.82
STICKER_JITTER = 0.05          # ±5% смещение
STICKER_BASE_WIDTH = 0.65      # базовая ширина стикера (доля от 1.0)
STICKER_BASE_HEIGHT = 0.065    # базовая высота
STICKER_SIZE_JITTER = 0.03     # ±3% вариация размера

# Автотриггер
VIEWS_THRESHOLD_MIN = 10000
VIEWS_THRESHOLD_MAX = 12777
STORY_COOLDOWN_HOURS = 24
STORY_JITTER_MINUTES = 20      # ±10-20 мин разброс

# Фото пул
PHOTOS_DIR = STORY_PHOTOS_DIR
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


# ─── Миграция: таблица сторис ─────────────────────────────────────────────────

STORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    image_path      TEXT    NOT NULL,
    link_url        TEXT,
    sticker_text    TEXT,
    upload_id       TEXT,
    media_id        TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'posted', 'failed')),
    trigger_reel_id INTEGER,
    posted_at       REAL,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS story_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id    INTEGER REFERENCES niches(id) ON DELETE SET NULL,
    filename    TEXT    NOT NULL,
    filepath    TEXT    NOT NULL,
    file_hash   TEXT    NOT NULL UNIQUE,
    used_count  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE INDEX IF NOT EXISTS idx_stories_account ON stories(account_id);
CREATE INDEX IF NOT EXISTS idx_stories_status  ON stories(status);
CREATE INDEX IF NOT EXISTS idx_story_photos_niche ON story_photos(niche_id);
"""


def init_stories_tables():
    from backend.database import get_db
    with get_db() as conn:
        conn.executescript(STORIES_SCHEMA)
    logger.info("Stories tables initialized")


# ─── Утилиты ─────────────────────────────────────────────────────────────────

# run_sync импортирован из backend.database (Fix #7)
_run_sync = run_sync


# ─── Извлечение кук/UA из BrowserContext ──────────────────────────────────────

async def extract_session(context: BrowserContext, page: Page) -> dict[str, str]:
    """
    Достаёт куки и User-Agent из живого Playwright BrowserContext.

    Returns:
        {
            "csrftoken": "...",
            "mid": "...",
            "ig_did": "...",
            "www_claim": "...",
            "user_agent": "...",
            "sessionid": "...",
        }
    """
    cookies = await context.cookies("https://www.instagram.com")
    cookie_map = {c["name"]: c["value"] for c in cookies}

    # User-Agent из страницы (не мобильный!)
    user_agent = await page.evaluate("() => navigator.userAgent")

    return {
        "csrftoken": cookie_map.get("csrftoken", ""),
        "mid": cookie_map.get("mid", ""),
        "ig_did": cookie_map.get("ig_did", ""),
        "www_claim": cookie_map.get("x-ig-www-claim", "0"),
        "sessionid": cookie_map.get("sessionid", ""),
        "user_agent": user_agent,
    }


def build_cookie_header(session: dict[str, str]) -> str:
    """Собирает Cookie header из сессии."""
    parts = []
    for key in ("csrftoken", "mid", "ig_did", "sessionid"):
        val = session.get(key, "")
        if val:
            parts.append(f"{key}={val}")
    return "; ".join(parts)


def build_headers(session: dict[str, str]) -> dict[str, str]:
    """Собирает заголовки для Instagram Web API."""
    return {
        "user-agent": session["user_agent"],
        "x-csrftoken": session["csrftoken"],
        "x-ig-app-id": IG_APP_ID,
        "x-asbd-id": IG_ASBD_ID,
        "x-requested-with": "XMLHttpRequest",
        "x-mid": session["mid"],
        "x-ig-device-id": session["ig_did"],
        "x-ig-www-claim": session["www_claim"],
        "cookie": build_cookie_header(session),
        "referer": "https://www.instagram.com/",
        "origin": "https://www.instagram.com",
    }


# ─── Pillow: визуальный стикер ────────────────────────────────────────────────

class StickerRenderer:
    """
    Рисует визуальный CTA-стикер на изображении сторис.

    Instagram получает только координаты невидимого кликабельного хитбокса.
    Визуальная часть — наша ответственность через Pillow.
    """

    # Цвета стикера
    BG_GRADIENT_START = (88, 86, 214)   # фиолетовый
    BG_GRADIENT_END = (59, 130, 246)    # синий
    TEXT_COLOR = (255, 255, 255)
    SHADOW_COLOR = (0, 0, 0, 80)

    def render(
        self,
        image: Image.Image,
        cta_text: str = "Learn More",
        link_icon: bool = True,
    ) -> Image.Image:
        """
        Рисует CTA-стикер на изображении.

        Args:
            image: PIL Image 1080×1920
            cta_text: текст на стикере
            link_icon: рисовать иконку ссылки

        Returns:
            Image с нарисованным стикером
        """
        img = image.copy().convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Размер и позиция стикера
        sticker_w = int(STORY_WIDTH * STICKER_BASE_WIDTH)
        sticker_h = int(STORY_HEIGHT * STICKER_BASE_HEIGHT)
        center_x = int(STORY_WIDTH * STICKER_BASE_X)
        center_y = int(STORY_HEIGHT * STICKER_BASE_Y)

        x1 = center_x - sticker_w // 2
        y1 = center_y - sticker_h // 2
        x2 = x1 + sticker_w
        y2 = y1 + sticker_h

        # Тень
        shadow_offset = 4
        draw.rounded_rectangle(
            [x1 + shadow_offset, y1 + shadow_offset, x2 + shadow_offset, y2 + shadow_offset],
            radius=sticker_h // 2,
            fill=self.SHADOW_COLOR,
        )

        # Градиентный фон стикера
        self._draw_gradient_rect(draw, x1, y1, x2, y2, sticker_h // 2)

        # Текст
        font_size = int(sticker_h * 0.45)
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

        # Иконка ссылки (простой символ ↗)
        display_text = f"↗ {cta_text}" if link_icon else cta_text

        bbox = draw.textbbox((0, 0), display_text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = center_x - tw // 2
        ty = center_y - th // 2

        draw.text((tx, ty), display_text, fill=self.TEXT_COLOR, font=font)

        # Composit
        result = Image.alpha_composite(img, overlay)
        return result.convert("RGB")

    def _draw_gradient_rect(
        self,
        draw: ImageDraw.Draw,
        x1: int, y1: int, x2: int, y2: int,
        radius: int,
    ) -> None:
        """Рисует прямоугольник с горизонтальным градиентом."""
        w = x2 - x1
        for i in range(w):
            ratio = i / max(w - 1, 1)
            r = int(self.BG_GRADIENT_START[0] + (self.BG_GRADIENT_END[0] - self.BG_GRADIENT_START[0]) * ratio)
            g = int(self.BG_GRADIENT_START[1] + (self.BG_GRADIENT_END[1] - self.BG_GRADIENT_START[1]) * ratio)
            b = int(self.BG_GRADIENT_START[2] + (self.BG_GRADIENT_END[2] - self.BG_GRADIENT_START[2]) * ratio)

            col_x = x1 + i
            # Рисуем вертикальную линию, но только внутри скруглённого rect
            # Упрощение: рисуем полный rect градиентом, потом overlay mask
            draw.line([(col_x, y1), (col_x, y2)], fill=(r, g, b, 220))

        # Скруглённая маска поверх
        mask = Image.new("L", draw.im.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=255)

        # Применяем маску к overlay — за пределами rect делаем прозрачным
        # Это работает потому что мы рисуем на RGBA overlay
        pixels = draw.im.load()
        mask_pixels = mask.load()
        for py in range(y1, min(y2 + 1, draw.im.size[1])):
            for px in range(x1, min(x2 + 1, draw.im.size[0])):
                if mask_pixels[px, py] == 0:
                    pixels[px, py] = (0, 0, 0, 0)


# ─── Подготовка изображения ──────────────────────────────────────────────────

def prepare_story_image(
    source_path: str | Path,
    link_url: str = "",
    cta_text: str = "Learn More",
) -> tuple[bytes, str]:
    """
    Подготавливает изображение для сторис:
    1. Ресайз/кроп до 1080×1920
    2. Рисует визуальный стикер (если есть ссылка)
    3. Сохраняет как JPEG в bytes

    Returns:
        (jpeg_bytes, upload_id)
    """
    img = Image.open(source_path).convert("RGB")

    # Ресайз с сохранением пропорций + кроп до 1080×1920
    target_ratio = STORY_WIDTH / STORY_HEIGHT
    img_ratio = img.width / img.height

    if img_ratio > target_ratio:
        # Шире чем нужно — кроп по ширине
        new_h = img.height
        new_w = int(new_h * target_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, new_h))
    else:
        # Выше чем нужно — кроп по высоте
        new_w = img.width
        new_h = int(new_w / target_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, new_w, top + new_h))

    img = img.resize((STORY_WIDTH, STORY_HEIGHT), Image.LANCZOS)

    # Визуальный стикер
    if link_url:
        renderer = StickerRenderer()
        img = renderer.render(img, cta_text=cta_text)

    # JPEG bytes
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    jpeg_bytes = buf.getvalue()

    upload_id = str(int(time.time() * 1000))

    return jpeg_bytes, upload_id


# ─── Рандомизация позиции стикера ─────────────────────────────────────────────

def randomize_sticker_position() -> dict[str, float]:
    """
    Рандомизирует позицию и размер кликабельного хитбокса.
    Base: x=0.5, y=0.82. Jitter: ±0.05 по X и Y. Размер тоже варьируется.
    """
    return {
        "x": STICKER_BASE_X + random.uniform(-STICKER_JITTER, STICKER_JITTER),
        "y": STICKER_BASE_Y + random.uniform(-STICKER_JITTER, STICKER_JITTER),
        "width": STICKER_BASE_WIDTH + random.uniform(-STICKER_SIZE_JITTER, STICKER_SIZE_JITTER),
        "height": STICKER_BASE_HEIGHT + random.uniform(-STICKER_SIZE_JITTER, STICKER_SIZE_JITTER),
        "rotation": random.uniform(-2.0, 2.0),  # лёгкий наклон ±2°
    }


# ─── Публикация сторис ───────────────────────────────────────────────────────

class StoryPublisher:
    """
    Публикует сторис через Instagram Web Private API.

    Два шага:
    1. Upload: POST i.instagram.com/rupload_igphoto
    2. Configure: POST www.instagram.com/api/v1/web/create/configure_to_story/
    """

    def __init__(self, proxy: dict[str, str] | None = None) -> None:
        self.proxy = proxy

    async def publish(
        self,
        session: dict[str, str],
        image_bytes: bytes,
        upload_id: str,
        link_url: str = "",
        sticker_text: str = "",
    ) -> dict[str, Any]:
        """
        Публикует сторис.

        Args:
            session: куки/UA из extract_session()
            image_bytes: JPEG bytes
            upload_id: уникальный ID загрузки
            link_url: URL для кликабельного стикера
            sticker_text: текст на стикере (CTA)

        Returns:
            {"success": bool, "media_id": str, "error": str}
        """
        headers = build_headers(session)

        # Прокси для httpx
        proxy_url = None
        if self.proxy:
            user = self.proxy.get("username", "")
            pwd = self.proxy.get("password", "")
            server = self.proxy["server"]
            if user and pwd:
                proxy_url = f"http://{user}:{pwd}@{server}"
            else:
                proxy_url = f"http://{server}"

        async with httpx.AsyncClient(
            timeout=60,
            proxy=proxy_url,
            verify=False,
        ) as client:

            # ── Шаг 1: Upload ─────────────────────────────────────────────
            upload_name = f"{upload_id}_0_{random.randint(100000000, 999999999)}"
            upload_headers = {
                **headers,
                "content-type": "image/jpeg",
                "x-entity-name": upload_name,
                "x-entity-length": str(len(image_bytes)),
                "x-entity-type": "image/jpeg",
                "x-instagram-rupload-params": json.dumps({
                    "upload_id": upload_id,
                    "media_type": 1,  # photo
                }),
                "offset": "0",
            }

            upload_resp = await client.post(
                f"{IG_UPLOAD_URL}{upload_name}",
                headers=upload_headers,
                content=image_bytes,
            )

            if upload_resp.status_code != 200:
                return {
                    "success": False,
                    "media_id": "",
                    "error": f"Upload failed: {upload_resp.status_code} {upload_resp.text[:200]}",
                }

            upload_data = upload_resp.json()
            logger.info("Upload OK: %s", upload_data.get("status"))

            # ── Шаг 2: Configure to Story ─────────────────────────────────
            configure_data: dict[str, Any] = {
                "upload_id": upload_id,
                "source_type": "4",
            }

            # Кликабельный стикер-ссылка
            if link_url:
                pos = randomize_sticker_position()
                story_sticker = {
                    "story_link_stickers": json.dumps([{
                        "x": pos["x"],
                        "y": pos["y"],
                        "z": 0,
                        "width": pos["width"],
                        "height": pos["height"],
                        "rotation": pos["rotation"],
                        "is_sticker": True,
                        "link_type": "web_link",
                        "url": link_url,
                        "custom_cta": sticker_text or "Learn More",
                    }]),
                }
                configure_data.update(story_sticker)

            configure_resp = await client.post(
                IG_CONFIGURE_URL,
                headers={**headers, "content-type": "application/x-www-form-urlencoded"},
                data=configure_data,
            )

            if configure_resp.status_code != 200:
                return {
                    "success": False,
                    "media_id": "",
                    "error": f"Configure failed: {configure_resp.status_code} {configure_resp.text[:200]}",
                }

            result = configure_resp.json()
            media_id = result.get("media", {}).get("pk", "")

            if result.get("status") == "ok":
                logger.info("Story published: media_id=%s", media_id)
                return {"success": True, "media_id": str(media_id), "error": ""}
            else:
                return {
                    "success": False,
                    "media_id": "",
                    "error": f"Configure status: {result.get('status')}, {result.get('message', '')}",
                }


# ─── Управление фото-пулом ───────────────────────────────────────────────────

class StoryPhotoPool:
    """Пул фотографий для сторис: загрузка ZIP, выдача следующей."""

    def __init__(self, base_dir: Path = PHOTOS_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def upload_zip(self, zip_path: str | Path, niche_id: int | None = None) -> dict[str, int]:
        """Распаковывает ZIP с фото для сторис."""
        zip_path = Path(zip_path)
        niche_dir = self.base_dir / (str(niche_id) if niche_id else "general")
        niche_dir.mkdir(parents=True, exist_ok=True)

        added = 0
        duplicates = 0

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith("/") or member.startswith("__MACOSX"):
                    continue

                ext = Path(member).suffix.lower()
                if ext not in ALLOWED_IMAGE_EXT:
                    continue

                filename = Path(member).name
                dest = niche_dir / filename
                counter = 1
                while dest.exists():
                    dest = niche_dir / f"{Path(member).stem}_{counter}{ext}"
                    counter += 1

                zf.extract(member, niche_dir)
                extracted = niche_dir / member
                if extracted != dest:
                    shutil.move(str(extracted), str(dest))

                fhash = hashlib.sha256(dest.read_bytes()).hexdigest()

                existing = await _run_sync(
                    query_one,
                    "SELECT id FROM story_photos WHERE file_hash = ?",
                    (fhash,),
                )
                if existing:
                    dest.unlink(missing_ok=True)
                    duplicates += 1
                    continue

                await _run_sync(
                    execute,
                    "INSERT INTO story_photos (niche_id, filename, filepath, file_hash) VALUES (?, ?, ?, ?)",
                    (niche_id, dest.name, str(dest), fhash),
                )
                added += 1

        logger.info("Story photos: added=%d, duplicates=%d", added, duplicates)
        return {"added": added, "duplicates": duplicates}

    async def get_next_photo(self, niche_id: int | None = None) -> dict | None:
        """Возвращает наименее использованное фото."""
        if niche_id:
            row = await _run_sync(
                query_one,
                """SELECT * FROM story_photos
                   WHERE niche_id = ? ORDER BY used_count ASC, id ASC LIMIT 1""",
                (niche_id,),
            )
        else:
            row = await _run_sync(
                query_one,
                "SELECT * FROM story_photos ORDER BY used_count ASC, id ASC LIMIT 1",
                (),
            )

        if row:
            await _run_sync(
                execute,
                "UPDATE story_photos SET used_count = used_count + 1 WHERE id = ?",
                (row["id"],),
            )
        return row


# ─── Автотриггер: сторис по просмотрам рилсов ────────────────────────────────

class StoryAutoTrigger:
    """
    Автоматически постит сторис когда рилс набирает 10к+ просмотров.

    Правила:
    - Порог рандомизирован: 10000-12777
    - Максимум 1 сторис в сутки на аккаунт
    - Разброс ±10-20 минут
    - Флаг «уже запощено» — без дублей
    """

    def __init__(self) -> None:
        # Рандомизированный порог на аккаунт
        self._thresholds: dict[int, int] = {}

    def get_threshold(self, account_id: int) -> int:
        """Возвращает порог просмотров для аккаунта (детерминированный)."""
        if account_id not in self._thresholds:
            self._thresholds[account_id] = random.randint(
                VIEWS_THRESHOLD_MIN, VIEWS_THRESHOLD_MAX,
            )
        return self._thresholds[account_id]

    async def should_post_story(
        self,
        account_id: int,
        reel_views: int,
    ) -> bool:
        """
        Проверяет нужно ли постить сторис.

        Условия:
        1. Просмотры >= порог
        2. Последняя сторис > 24ч назад (или не было)
        """
        threshold = self.get_threshold(account_id)

        if reel_views < threshold:
            return False

        # Проверяем последнюю сторис
        last_story = await _run_sync(
            query_one,
            """SELECT posted_at FROM stories
               WHERE account_id = ? AND status = 'posted'
               ORDER BY posted_at DESC LIMIT 1""",
            (account_id,),
        )

        if last_story and last_story["posted_at"]:
            hours_since = (time.time() - last_story["posted_at"]) / 3600
            if hours_since < STORY_COOLDOWN_HOURS:
                return False

        return True

    def get_jitter_seconds(self) -> float:
        """Возвращает случайную задержку ±10-20 минут."""
        return random.uniform(10 * 60, STORY_JITTER_MINUTES * 60)


# ─── Главный контроллер сторис ────────────────────────────────────────────────

class StoryController:
    """
    Фасад для работы со сторис.

    Ручной режим:
        await controller.post_story(account_id, context, page, image_path, link, cta)

    Автотриггер:
        if await controller.auto_trigger.should_post_story(account_id, views):
            await controller.post_story_auto(account_id, context, page, link, cta)
    """

    def __init__(self) -> None:
        self.publisher = StoryPublisher()
        self.photo_pool = StoryPhotoPool()
        self.auto_trigger = StoryAutoTrigger()

    def init(self) -> None:
        """Создаёт таблицы."""
        init_stories_tables()

    async def post_story(
        self,
        account_id: int,
        context: BrowserContext,
        page: Page,
        image_path: str | Path,
        link_url: str = "",
        cta_text: str = "Learn More",
        proxy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Публикует одну сторис для аккаунта.

        Returns:
            {"success": bool, "media_id": str, "error": str}
        """
        # Извлекаем сессию из BrowserContext
        session = await extract_session(context, page)

        if not session["csrftoken"] or not session["sessionid"]:
            return {"success": False, "media_id": "", "error": "Missing session cookies"}

        # Подготавливаем изображение
        image_bytes, upload_id = prepare_story_image(image_path, link_url, cta_text)

        # Публикуем
        publisher = StoryPublisher(proxy=proxy)
        result = await publisher.publish(
            session=session,
            image_bytes=image_bytes,
            upload_id=upload_id,
            link_url=link_url,
            sticker_text=cta_text,
        )

        # Сохраняем в БД
        status = "posted" if result["success"] else "failed"
        await _run_sync(
            execute,
            """INSERT INTO stories (account_id, image_path, link_url, sticker_text,
                                    upload_id, media_id, status, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id, str(image_path), link_url, cta_text,
                upload_id, result.get("media_id", ""),
                status,
                time.time() if result["success"] else None,
            ),
        )

        return result

    async def post_story_auto(
        self,
        account_id: int,
        context: BrowserContext,
        page: Page,
        link_url: str = "",
        cta_text: str = "Learn More",
        niche_id: int | None = None,
        proxy: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Автопостинг: берёт фото из пула, постит сторис.
        Используется автотриггером.
        """
        # Задержка для естественности
        jitter = self.auto_trigger.get_jitter_seconds()
        logger.info("[account %d] Story auto-trigger, delay %.0f min", account_id, jitter / 60)
        await asyncio.sleep(jitter)

        # Берём фото из пула
        photo = await self.photo_pool.get_next_photo(niche_id)
        if not photo:
            return {"success": False, "media_id": "", "error": "No photos in pool"}

        return await self.post_story(
            account_id=account_id,
            context=context,
            page=page,
            image_path=photo["filepath"],
            link_url=link_url,
            cta_text=cta_text,
            proxy=proxy,
        )

    async def get_story_stats(self, account_id: int | None = None) -> dict[str, Any]:
        """Статистика сторис."""
        if account_id:
            total = await _run_sync(query_one,
                "SELECT COUNT(*) as cnt FROM stories WHERE account_id = ?", (account_id,))
            posted = await _run_sync(query_one,
                "SELECT COUNT(*) as cnt FROM stories WHERE account_id = ? AND status = 'posted'", (account_id,))
            failed = await _run_sync(query_one,
                "SELECT COUNT(*) as cnt FROM stories WHERE account_id = ? AND status = 'failed'", (account_id,))
            last = await _run_sync(query_one,
                "SELECT posted_at FROM stories WHERE account_id = ? AND status = 'posted' ORDER BY posted_at DESC LIMIT 1",
                (account_id,))
        else:
            total = await _run_sync(query_one, "SELECT COUNT(*) as cnt FROM stories", ())
            posted = await _run_sync(query_one,
                "SELECT COUNT(*) as cnt FROM stories WHERE status = 'posted'", ())
            failed = await _run_sync(query_one,
                "SELECT COUNT(*) as cnt FROM stories WHERE status = 'failed'", ())
            last = await _run_sync(query_one,
                "SELECT posted_at FROM stories WHERE status = 'posted' ORDER BY posted_at DESC LIMIT 1", ())

        return {
            "total": total["cnt"],
            "posted": posted["cnt"],
            "failed": failed["cnt"],
            "last_posted_at": last["posted_at"] if last else None,
        }
