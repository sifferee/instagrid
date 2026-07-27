"""
InstaGrid — Контент-менеджер.

Управление видео и описаниями для постинга:
- Загрузка видео ZIP-архивом → распаковка → привязка по одному на аккаунт, без повторов
- UI-плашки: «Есть резервные видео» / «Не все аккаунты заполнены»
- Описания — два режима:
  - Ручной: пул уникальных описаний, распределяются по одному
  - Автогенерация: эталонное описание → Claude API → уникальные вариации
- Ручное редактирование видео/описания на конкретном аккаунте
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from backend.database import execute, execute_many, query, query_one, get_db

logger = logging.getLogger("instagrid.content")

# ─── Константы ────────────────────────────────────────────────────────────────

CONTENT_DIR = Path("content")
VIDEOS_DIR = CONTENT_DIR / "videos"
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# Claude API для автогенерации описаний
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"


# ─── Миграция: таблицы контента ───────────────────────────────────────────────

CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id    INTEGER REFERENCES niches(id) ON DELETE SET NULL,
    account_id  INTEGER UNIQUE REFERENCES accounts(id) ON DELETE SET NULL,
    filename    TEXT    NOT NULL,
    filepath    TEXT    NOT NULL,
    file_hash   TEXT    NOT NULL UNIQUE,
    file_size   INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'unassigned'
                        CHECK (status IN ('unassigned', 'assigned', 'posted', 'failed')),
    assigned_at REAL,
    posted_at   REAL,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS descriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id    INTEGER REFERENCES niches(id) ON DELETE SET NULL,
    account_id  INTEGER UNIQUE REFERENCES accounts(id) ON DELETE SET NULL,
    text        TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('manual', 'generated')),
    status      TEXT    NOT NULL DEFAULT 'unassigned'
                        CHECK (status IN ('unassigned', 'assigned', 'posted')),
    assigned_at REAL,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE INDEX IF NOT EXISTS idx_videos_niche     ON videos(niche_id);
CREATE INDEX IF NOT EXISTS idx_videos_account   ON videos(account_id);
CREATE INDEX IF NOT EXISTS idx_videos_status    ON videos(status);
CREATE INDEX IF NOT EXISTS idx_desc_niche       ON descriptions(niche_id);
CREATE INDEX IF NOT EXISTS idx_desc_account     ON descriptions(account_id);
CREATE INDEX IF NOT EXISTS idx_desc_status      ON descriptions(status);
"""


def init_content_tables():
    """Создаёт таблицы videos и descriptions если не существуют."""
    with get_db() as conn:
        conn.executescript(CONTENT_SCHEMA)
    logger.info("Content tables initialized")


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def _run_sync(fn, *args):
    """Запуск sync-функции в executor."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, functools.partial(fn, *args))


def _file_hash(filepath: Path) -> str:
    """SHA256 хеш файла (для дедупликации)."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Управление видео ────────────────────────────────────────────────────────

class VideoManager:
    """
    Загрузка, распаковка и распределение видео.

    Воркфлоу:
    1. upload_zip(path, niche_id) → распаковка → регистрация в БД
    2. distribute(niche_id) → привязка по одному на аккаунт, без повторов
    3. get_stats(niche_id) → сколько видео / аккаунтов, плашки для UI
    """

    def __init__(self, base_dir: Path = VIDEOS_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def upload_zip(
        self,
        zip_path: str | Path,
        niche_id: int | None = None,
    ) -> dict[str, int]:
        """
        Распаковывает ZIP с видео, регистрирует в БД.

        Returns:
            {"added": N, "duplicates": M, "skipped": K}
        """
        zip_path = Path(zip_path)
        if not zip_path.exists():
            raise FileNotFoundError(f"ZIP not found: {zip_path}")

        # Папка для ниши
        niche_dir = self.base_dir / (str(niche_id) if niche_id else "general")
        niche_dir.mkdir(parents=True, exist_ok=True)

        added = 0
        duplicates = 0
        skipped = 0

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                # Пропускаем папки и скрытые файлы
                if member.endswith("/") or member.startswith("__MACOSX"):
                    continue

                ext = Path(member).suffix.lower()
                if ext not in ALLOWED_VIDEO_EXT:
                    skipped += 1
                    continue

                # Извлекаем
                filename = Path(member).name
                dest = niche_dir / filename

                # Если файл с таким именем уже есть — добавляем суффикс
                counter = 1
                while dest.exists():
                    stem = Path(member).stem
                    dest = niche_dir / f"{stem}_{counter}{ext}"
                    counter += 1

                zf.extract(member, niche_dir)
                extracted = niche_dir / member

                # Перемещаем из вложенных папок в корень niche_dir
                if extracted != dest:
                    shutil.move(str(extracted), str(dest))

                # Хеш для дедупликации
                fhash = _file_hash(dest)

                # Проверяем дубль в БД
                existing = await _run_sync(
                    query_one,
                    "SELECT id FROM videos WHERE file_hash = ?",
                    (fhash,),
                )
                if existing:
                    dest.unlink(missing_ok=True)
                    duplicates += 1
                    continue

                # Регистрируем в БД
                file_size = dest.stat().st_size
                await _run_sync(
                    execute,
                    """INSERT INTO videos (niche_id, filename, filepath, file_hash, file_size)
                       VALUES (?, ?, ?, ?, ?)""",
                    (niche_id, dest.name, str(dest), fhash, file_size),
                )
                added += 1

        # Чистим пустые папки после распаковки
        for d in niche_dir.rglob("*"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

        logger.info(
            "ZIP upload: added=%d, duplicates=%d, skipped=%d",
            added, duplicates, skipped,
        )
        return {"added": added, "duplicates": duplicates, "skipped": skipped}

    async def upload_single(
        self,
        file_path: str | Path,
        niche_id: int | None = None,
    ) -> int | None:
        """Регистрирует одно видео. Возвращает video_id или None (дубль)."""
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Video not found: {file_path}")

        fhash = _file_hash(file_path)
        existing = await _run_sync(
            query_one,
            "SELECT id FROM videos WHERE file_hash = ?",
            (fhash,),
        )
        if existing:
            return None

        # Копируем в content dir
        niche_dir = self.base_dir / (str(niche_id) if niche_id else "general")
        niche_dir.mkdir(parents=True, exist_ok=True)
        dest = niche_dir / file_path.name
        shutil.copy2(file_path, dest)

        video_id = await _run_sync(
            execute,
            """INSERT INTO videos (niche_id, filename, filepath, file_hash, file_size)
               VALUES (?, ?, ?, ?, ?)""",
            (niche_id, dest.name, str(dest), fhash, dest.stat().st_size),
        )
        return video_id

    async def distribute(self, niche_id: int | None = None) -> dict[str, int]:
        """
        Привязывает свободные видео к аккаунтам без видео. По одному, без повторов.

        Returns:
            {"assigned": N, "unassigned_videos": M, "unfilled_accounts": K}
        """
        # Свободные видео
        if niche_id:
            free_videos = await _run_sync(
                query,
                """SELECT id FROM videos
                   WHERE status = 'unassigned' AND niche_id = ?
                   ORDER BY id ASC""",
                (niche_id,),
            )
            # Аккаунты без видео
            unfilled = await _run_sync(
                query,
                """SELECT a.id FROM accounts a
                   LEFT JOIN videos v ON v.account_id = a.id AND v.status IN ('assigned', 'posted')
                   WHERE a.niche_id = ? AND v.id IS NULL
                   ORDER BY a.id ASC""",
                (niche_id,),
            )
        else:
            free_videos = await _run_sync(
                query,
                "SELECT id FROM videos WHERE status = 'unassigned' ORDER BY id ASC",
                (),
            )
            unfilled = await _run_sync(
                query,
                """SELECT a.id FROM accounts a
                   LEFT JOIN videos v ON v.account_id = a.id AND v.status IN ('assigned', 'posted')
                   WHERE v.id IS NULL
                   ORDER BY a.id ASC""",
                (),
            )

        assigned = 0
        pairs = zip(free_videos, unfilled)

        for video, account in pairs:
            await _run_sync(
                execute,
                """UPDATE videos
                   SET account_id = ?, status = 'assigned', assigned_at = unixepoch('now')
                   WHERE id = ?""",
                (account["id"], video["id"]),
            )
            assigned += 1

        remaining_videos = len(free_videos) - assigned
        remaining_accounts = len(unfilled) - assigned

        logger.info(
            "Distribute: assigned=%d, spare_videos=%d, unfilled_accounts=%d",
            assigned, remaining_videos, remaining_accounts,
        )
        return {
            "assigned": assigned,
            "unassigned_videos": remaining_videos,
            "unfilled_accounts": remaining_accounts,
        }

    async def get_stats(self, niche_id: int | None = None) -> dict[str, Any]:
        """
        Статистика для UI-плашек.

        Returns:
            {
                "total_videos": N,
                "assigned": M,
                "unassigned": K,
                "posted": P,
                "total_accounts": A,
                "unfilled_accounts": U,
                "badge": "spare_videos" | "unfilled_accounts" | "balanced"
            }
        """
        niche_filter = "AND niche_id = ?" if niche_id else ""
        params: tuple = (niche_id,) if niche_id else ()

        total = await _run_sync(
            query_one,
            f"SELECT COUNT(*) as cnt FROM videos WHERE 1=1 {niche_filter}",
            params,
        )
        assigned = await _run_sync(
            query_one,
            f"SELECT COUNT(*) as cnt FROM videos WHERE status = 'assigned' {niche_filter}",
            params,
        )
        unassigned = await _run_sync(
            query_one,
            f"SELECT COUNT(*) as cnt FROM videos WHERE status = 'unassigned' {niche_filter}",
            params,
        )
        posted = await _run_sync(
            query_one,
            f"SELECT COUNT(*) as cnt FROM videos WHERE status = 'posted' {niche_filter}",
            params,
        )

        acc_filter = "AND a.niche_id = ?" if niche_id else ""
        total_accounts = await _run_sync(
            query_one,
            f"SELECT COUNT(*) as cnt FROM accounts a WHERE 1=1 {acc_filter}",
            params,
        )
        unfilled = await _run_sync(
            query_one,
            f"""SELECT COUNT(*) as cnt FROM accounts a
                LEFT JOIN videos v ON v.account_id = a.id AND v.status IN ('assigned', 'posted')
                WHERE v.id IS NULL {acc_filter}""",
            params,
        )

        uv = unassigned["cnt"]
        ua = unfilled["cnt"]
        if uv > 0 and ua == 0:
            badge = "spare_videos"
        elif ua > 0 and uv == 0:
            badge = "unfilled_accounts"
        elif uv > 0 and ua > 0:
            badge = "unfilled_accounts"
        else:
            badge = "balanced"

        return {
            "total_videos": total["cnt"],
            "assigned": assigned["cnt"],
            "unassigned": uv,
            "posted": posted["cnt"],
            "total_accounts": total_accounts["cnt"],
            "unfilled_accounts": ua,
            "badge": badge,
        }

    async def assign_to_account(self, video_id: int, account_id: int) -> None:
        """Ручная привязка видео к аккаунту."""
        # Снимаем старое видео с аккаунта если было
        await _run_sync(
            execute,
            """UPDATE videos SET account_id = NULL, status = 'unassigned', assigned_at = NULL
               WHERE account_id = ? AND status = 'assigned'""",
            (account_id,),
        )
        # Привязываем новое
        await _run_sync(
            execute,
            """UPDATE videos
               SET account_id = ?, status = 'assigned', assigned_at = unixepoch('now')
               WHERE id = ?""",
            (account_id, video_id),
        )

    async def unassign_from_account(self, account_id: int) -> None:
        """Снимает видео с аккаунта."""
        await _run_sync(
            execute,
            """UPDATE videos SET account_id = NULL, status = 'unassigned', assigned_at = NULL
               WHERE account_id = ? AND status = 'assigned'""",
            (account_id,),
        )

    async def get_video_for_account(self, account_id: int) -> dict | None:
        """Возвращает привязанное видео аккаунта."""
        return await _run_sync(
            query_one,
            "SELECT * FROM videos WHERE account_id = ? AND status = 'assigned'",
            (account_id,),
        )

    async def mark_posted(self, video_id: int) -> None:
        """Помечает видео как опубликованное."""
        await _run_sync(
            execute,
            "UPDATE videos SET status = 'posted', posted_at = unixepoch('now') WHERE id = ?",
            (video_id,),
        )

    async def delete_video(self, video_id: int) -> None:
        """Удаляет видео из БД и с диска."""
        row = await _run_sync(
            query_one,
            "SELECT filepath FROM videos WHERE id = ?",
            (video_id,),
        )
        if row:
            Path(row["filepath"]).unlink(missing_ok=True)
            await _run_sync(
                execute,
                "DELETE FROM videos WHERE id = ?",
                (video_id,),
            )

    async def list_videos(
        self,
        niche_id: int | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Список видео с фильтрами."""
        conditions = ["1=1"]
        params: list = []
        if niche_id:
            conditions.append("niche_id = ?")
            params.append(niche_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions)
        return await _run_sync(
            query,
            f"SELECT * FROM videos WHERE {where} ORDER BY id ASC",
            tuple(params),
        )


# ─── Управление описаниями ───────────────────────────────────────────────────

class DescriptionManager:
    """
    Два режима описаний:
    1. Ручной: пул уникальных описаний, распределяются по одному
    2. Автогенерация: эталонное описание → Claude API → уникальные вариации
    """

    async def import_manual(
        self,
        descriptions: list[str],
        niche_id: int | None = None,
    ) -> int:
        """
        Импорт пула описаний. Каждое описание уникально.

        Args:
            descriptions: список текстов описаний
            niche_id: привязка к нише

        Returns:
            количество добавленных
        """
        # Фильтруем пустые и дубли
        seen = set()
        unique = []
        for d in descriptions:
            text = d.strip()
            if text and text not in seen:
                seen.add(text)
                unique.append(text)

        params_list = [
            (niche_id, text, "manual")
            for text in unique
        ]

        added = await _run_sync(
            execute_many,
            """INSERT INTO descriptions (niche_id, text, source)
               VALUES (?, ?, ?)""",
            params_list,
        )

        logger.info("Imported %d manual descriptions", added)
        return added

    async def generate_with_claude(
        self,
        reference_text: str,
        count: int,
        niche_id: int | None = None,
        api_key: str = "",
    ) -> list[str]:
        """
        Генерирует уникальные вариации описания через Claude API.

        Args:
            reference_text: эталонное описание
            count: сколько вариаций сгенерировать
            niche_id: привязка к нише
            api_key: ключ Anthropic API

        Returns:
            список сгенерированных описаний
        """
        if not api_key:
            raise ValueError("Claude API key required for generation")

        import httpx

        prompt = (
            f"Generate exactly {count} unique Instagram post descriptions "
            f"based on this reference. Each description must be meaningfully different — "
            f"vary the wording, structure, emoji placement, hashtags, and tone. "
            f"Keep the same general topic and intent.\n\n"
            f"Reference description:\n{reference_text}\n\n"
            f"Output ONLY the descriptions, one per line, numbered 1. 2. 3. etc. "
            f"No other text."
        )

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Парсим ответ
        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block["text"]

        descriptions = []
        for line in raw_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Убираем нумерацию: "1. ", "2) ", etc.
            for prefix_len in range(1, 5):
                if len(line) > prefix_len and line[prefix_len - 1] in ".)" and line[:prefix_len - 1].isdigit():
                    line = line[prefix_len:].strip()
                    break
            if line:
                descriptions.append(line)

        # Сохраняем в БД
        params_list = [
            (niche_id, text, "generated")
            for text in descriptions
        ]
        if params_list:
            await _run_sync(
                execute_many,
                """INSERT INTO descriptions (niche_id, text, source)
                   VALUES (?, ?, ?)""",
                params_list,
            )

        logger.info("Generated %d descriptions via Claude API", len(descriptions))
        return descriptions

    async def distribute(self, niche_id: int | None = None) -> dict[str, int]:
        """
        Привязывает свободные описания к аккаунтам без описаний.

        Returns:
            {"assigned": N, "unassigned": M, "unfilled": K}
        """
        if niche_id:
            free = await _run_sync(
                query,
                """SELECT id FROM descriptions
                   WHERE status = 'unassigned' AND niche_id = ?
                   ORDER BY id ASC""",
                (niche_id,),
            )
            unfilled = await _run_sync(
                query,
                """SELECT a.id FROM accounts a
                   LEFT JOIN descriptions d ON d.account_id = a.id AND d.status IN ('assigned', 'posted')
                   WHERE a.niche_id = ? AND d.id IS NULL
                   ORDER BY a.id ASC""",
                (niche_id,),
            )
        else:
            free = await _run_sync(
                query,
                "SELECT id FROM descriptions WHERE status = 'unassigned' ORDER BY id ASC",
                (),
            )
            unfilled = await _run_sync(
                query,
                """SELECT a.id FROM accounts a
                   LEFT JOIN descriptions d ON d.account_id = a.id AND d.status IN ('assigned', 'posted')
                   WHERE d.id IS NULL
                   ORDER BY a.id ASC""",
                (),
            )

        assigned = 0
        for desc, account in zip(free, unfilled):
            await _run_sync(
                execute,
                """UPDATE descriptions
                   SET account_id = ?, status = 'assigned', assigned_at = unixepoch('now')
                   WHERE id = ?""",
                (account["id"], desc["id"]),
            )
            assigned += 1

        return {
            "assigned": assigned,
            "unassigned": len(free) - assigned,
            "unfilled": len(unfilled) - assigned,
        }

    async def assign_to_account(self, desc_id: int, account_id: int) -> None:
        """Ручная привязка описания к аккаунту."""
        await _run_sync(
            execute,
            """UPDATE descriptions SET account_id = NULL, status = 'unassigned', assigned_at = NULL
               WHERE account_id = ? AND status = 'assigned'""",
            (account_id,),
        )
        await _run_sync(
            execute,
            """UPDATE descriptions
               SET account_id = ?, status = 'assigned', assigned_at = unixepoch('now')
               WHERE id = ?""",
            (account_id, desc_id),
        )

    async def update_text(self, desc_id: int, new_text: str) -> None:
        """Ручное редактирование текста описания."""
        await _run_sync(
            execute,
            "UPDATE descriptions SET text = ? WHERE id = ?",
            (new_text.strip(), desc_id),
        )

    async def get_for_account(self, account_id: int) -> dict | None:
        """Возвращает привязанное описание аккаунта."""
        return await _run_sync(
            query_one,
            "SELECT * FROM descriptions WHERE account_id = ? AND status = 'assigned'",
            (account_id,),
        )

    async def delete_description(self, desc_id: int) -> None:
        """Удаляет описание."""
        await _run_sync(
            execute,
            "DELETE FROM descriptions WHERE id = ?",
            (desc_id,),
        )

    async def list_descriptions(
        self,
        niche_id: int | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> list[dict]:
        """Список описаний с фильтрами."""
        conditions = ["1=1"]
        params: list = []
        if niche_id:
            conditions.append("niche_id = ?")
            params.append(niche_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)

        where = " AND ".join(conditions)
        return await _run_sync(
            query,
            f"SELECT * FROM descriptions WHERE {where} ORDER BY id ASC",
            tuple(params),
        )


# ─── Единый ContentManager ───────────────────────────────────────────────────

class ContentManager:
    """
    Фасад: объединяет VideoManager + DescriptionManager.

    Использование:
        cm = ContentManager()
        cm.init()  # создаёт таблицы

        # Видео
        await cm.videos.upload_zip("path/to/videos.zip", niche_id=1)
        await cm.videos.distribute(niche_id=1)
        stats = await cm.videos.get_stats(niche_id=1)

        # Описания (ручные)
        await cm.descriptions.import_manual(["desc1", "desc2"], niche_id=1)
        await cm.descriptions.distribute(niche_id=1)

        # Описания (автогенерация)
        await cm.descriptions.generate_with_claude(
            reference_text="🔥 Check this out! ...",
            count=50,
            niche_id=1,
            api_key="sk-ant-...",
        )

        # Контент для постинга конкретного аккаунта
        content = await cm.get_posting_content(account_id=42)
        # → {"video": {...}, "description": {...}} или None
    """

    def __init__(self, videos_dir: str | Path = VIDEOS_DIR) -> None:
        self.videos = VideoManager(Path(videos_dir))
        self.descriptions = DescriptionManager()

    def init(self) -> None:
        """Создаёт таблицы контента в БД."""
        init_content_tables()

    async def get_posting_content(self, account_id: int) -> dict[str, Any] | None:
        """
        Возвращает готовый контент для постинга: видео + описание.
        Если чего-то нет — возвращает None.
        """
        video = await self.videos.get_video_for_account(account_id)
        description = await self.descriptions.get_for_account(account_id)

        if not video:
            return None

        return {
            "video": video,
            "description": description,  # может быть None — постинг без описания
        }

    async def distribute_all(self, niche_id: int | None = None) -> dict[str, Any]:
        """Распределяет и видео, и описания за один вызов."""
        v = await self.videos.distribute(niche_id)
        d = await self.descriptions.distribute(niche_id)
        return {"videos": v, "descriptions": d}

    async def get_full_stats(self, niche_id: int | None = None) -> dict[str, Any]:
        """Полная статистика контента для дашборда."""
        video_stats = await self.videos.get_stats(niche_id)

        if niche_id:
            desc_total = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions WHERE niche_id = ?",
                (niche_id,),
            )
            desc_assigned = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions WHERE status = 'assigned' AND niche_id = ?",
                (niche_id,),
            )
            desc_unassigned = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions WHERE status = 'unassigned' AND niche_id = ?",
                (niche_id,),
            )
        else:
            desc_total = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions",
                (),
            )
            desc_assigned = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions WHERE status = 'assigned'",
                (),
            )
            desc_unassigned = await _run_sync(
                query_one,
                "SELECT COUNT(*) as cnt FROM descriptions WHERE status = 'unassigned'",
                (),
            )

        return {
            "videos": video_stats,
            "descriptions": {
                "total": desc_total["cnt"],
                "assigned": desc_assigned["cnt"],
                "unassigned": desc_unassigned["cnt"],
            },
        }
