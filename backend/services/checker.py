"""
InstaGrid — Чекер.

Отдельный пул аккаунтов-парсеров для мониторинга целевых аккаунтов:
- Парсит каждые 50-90 минут (рандомизация)
- Собирает: просмотры, лайки, комменты, подписчики по каждому рилсу
- Ротация чекеров, рандомизация порядка проверки
- Подтверждение бана: минимум 2-3 проверки с разных чекеров
- Если чекер-аккаунт сам забанен — помечается, берётся следующий
- Порог живых чекеров: если осталось < N — алерт, остановка чекинга
- Панель статистики: просмотры, подписчики, лайки, комменты
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from playwright.async_api import Page, BrowserContext

from backend.database import execute, execute_many, query, query_one, get_db
from backend.services.human import HumanInteractor
from backend.services.profile_manager import ProfileManager

logger = logging.getLogger("instagrid.checker")

# ─── Константы ────────────────────────────────────────────────────────────────

CHECK_INTERVAL_MIN = 50 * 60       # 50 мин (сек)
CHECK_INTERVAL_MAX = 90 * 60       # 90 мин
MIN_ALIVE_CHECKERS = 3             # порог алерта
BAN_CONFIRM_COUNT = 3              # проверок с разных чекеров для подтверждения бана
PAGE_LOAD_TIMEOUT = 30_000         # мс


# ─── Миграция ─────────────────────────────────────────────────────────────────

CHECKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS checker_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL,
    password        TEXT    NOT NULL,
    totp_secret     TEXT,
    proxy_host      TEXT,
    proxy_port      INTEGER,
    proxy_username  TEXT,
    proxy_password  TEXT,
    status          TEXT    NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'banned', 'cooldown')),
    last_used_at    REAL,
    ban_count       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS reel_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    reel_url        TEXT    NOT NULL,
    reel_shortcode  TEXT,
    views           INTEGER NOT NULL DEFAULT 0,
    likes           INTEGER NOT NULL DEFAULT 0,
    comments        INTEGER NOT NULL DEFAULT 0,
    checked_at      REAL    NOT NULL DEFAULT (unixepoch('now')),
    checker_id      INTEGER REFERENCES checker_accounts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    followers       INTEGER NOT NULL DEFAULT 0,
    following       INTEGER NOT NULL DEFAULT 0,
    posts_count     INTEGER NOT NULL DEFAULT 0,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    checked_at      REAL    NOT NULL DEFAULT (unixepoch('now')),
    checker_id      INTEGER REFERENCES checker_accounts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ban_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    checker_id      INTEGER NOT NULL REFERENCES checker_accounts(id) ON DELETE CASCADE,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    checked_at      REAL    NOT NULL DEFAULT (unixepoch('now')),
    UNIQUE(account_id, checker_id)
);

CREATE INDEX IF NOT EXISTS idx_reel_stats_account    ON reel_stats(account_id);
CREATE INDEX IF NOT EXISTS idx_reel_stats_checked    ON reel_stats(checked_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_account     ON account_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_checked     ON account_snapshots(checked_at);
CREATE INDEX IF NOT EXISTS idx_ban_votes_account     ON ban_votes(account_id);
CREATE INDEX IF NOT EXISTS idx_checker_status        ON checker_accounts(status);
"""


def init_checker_tables():
    with get_db() as conn:
        conn.executescript(CHECKER_SCHEMA)
    logger.info("Checker tables initialized")


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def _run_sync(fn, *args):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, functools.partial(fn, *args))


# ─── Результаты ──────────────────────────────────────────────────────────────

@dataclass
class ReelData:
    url: str
    shortcode: str
    views: int = 0
    likes: int = 0
    comments: int = 0


@dataclass
class ProfileData:
    username: str
    followers: int = 0
    following: int = 0
    posts_count: int = 0
    is_private: bool = False
    is_banned: bool = False
    reels: list[ReelData] = field(default_factory=list)


@dataclass
class CheckResult:
    account_id: int
    target_username: str
    checker_username: str
    success: bool
    profile: ProfileData | None = None
    message: str = ""


# ─── Парсер профиля ──────────────────────────────────────────────────────────

class ProfileParser:
    """
    Парсит публичный профиль Instagram: подписчики, рилсы, просмотры.
    Работает через залогиненный аккаунт-чекер.
    """

    def __init__(self, page: Page, human: HumanInteractor) -> None:
        self.page = page
        self.human = human

    async def parse_profile(self, target_username: str) -> ProfileData:
        """
        Заходит на профиль, собирает статистику.
        """
        profile = ProfileData(username=target_username)
        url = f"https://www.instagram.com/{target_username}/"

        try:
            resp = await self.page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

            await self.human.random_pause(2.0, 4.0)

            # Проверка: страница не существует (бан/удалён)
            page_text = await self.page.inner_text("body")

            if "Sorry, this page isn't available" in page_text:
                profile.is_banned = True
                return profile

            if "This Account is Private" in page_text:
                profile.is_private = True

            # Парсим мету из __initialData или из DOM
            profile = await self._parse_from_page(profile)

            # Парсим рилсы
            if not profile.is_private and not profile.is_banned:
                profile.reels = await self._parse_reels(target_username)

        except Exception as e:
            logger.warning("[%s] Failed to parse %s: %s", self.human.username, target_username, e)
            profile.is_banned = True  # на всякий случай — перепроверят другие чекеры

        return profile

    async def _parse_from_page(self, profile: ProfileData) -> ProfileData:
        """Парсит подписчиков, подписки, посты из DOM."""
        try:
            # Ищем секцию со счётчиками: posts, followers, following
            meta_elements = await self.page.query_selector_all('meta[property="og:description"]')
            for el in meta_elements:
                content = await el.get_attribute("content") or ""
                # "1,234 Followers, 567 Following, 89 Posts"
                profile = self._parse_meta_content(content, profile)
                break

            # Альтернатива: парсим из header секции
            if profile.followers == 0:
                header_spans = await self.page.query_selector_all("header section ul li span")
                values = []
                for span in header_spans:
                    text = await span.get_attribute("title") or await span.inner_text()
                    num = self._parse_number(text)
                    if num is not None:
                        values.append(num)

                if len(values) >= 3:
                    profile.posts_count = values[0]
                    profile.followers = values[1]
                    profile.following = values[2]

        except Exception as e:
            logger.debug("Parse from page failed: %s", e)

        return profile

    async def _parse_reels(self, username: str) -> list[ReelData]:
        """Парсит рилсы с вкладки Reels."""
        reels: list[ReelData] = []

        try:
            # Переходим на вкладку рилсов
            reels_tab = await self.page.query_selector(f'a[href="/{username}/reels/"]')
            if reels_tab:
                await self.human.click_element(reels_tab)
                await self.human.random_pause(2.0, 4.0)

            # Собираем ссылки на рилсы
            reel_links = await self.page.query_selector_all('a[href*="/reel/"]')

            for link in reel_links[:12]:  # максимум 12 последних
                href = await link.get_attribute("href") or ""
                shortcode = href.split("/reel/")[-1].rstrip("/") if "/reel/" in href else ""

                if not shortcode:
                    continue

                # Парсим просмотры из aria-label или из overlay
                views = 0
                try:
                    # Hover для overlay с просмотрами
                    box = await link.bounding_box()
                    if box:
                        await self.human.move_to(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        await asyncio.sleep(0.5)

                    # Ищем число просмотров в overlay
                    overlay_spans = await link.query_selector_all("span")
                    for span in overlay_spans:
                        text = await span.inner_text()
                        num = self._parse_number(text)
                        if num is not None and num > 0:
                            views = num
                            break
                except Exception:
                    pass

                reels.append(ReelData(
                    url=f"https://www.instagram.com/reel/{shortcode}/",
                    shortcode=shortcode,
                    views=views,
                ))

            # Для каждого рилса — открываем и парсим лайки/комменты
            for reel in reels[:6]:  # детально парсим только 6 последних
                detail = await self._parse_reel_detail(reel)
                reel.likes = detail.get("likes", 0)
                reel.comments = detail.get("comments", 0)
                if detail.get("views", 0) > reel.views:
                    reel.views = detail["views"]

        except Exception as e:
            logger.debug("Parse reels failed: %s", e)

        return reels

    async def _parse_reel_detail(self, reel: ReelData) -> dict[str, int]:
        """Открывает рилс и парсит лайки/комменты/просмотры."""
        result = {"views": 0, "likes": 0, "comments": 0}

        try:
            await self.page.goto(reel.url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            await self.human.random_pause(1.5, 3.0)

            body_text = await self.page.inner_text("body")

            # Ищем числа: "X views", "X likes", "X comments"
            import re
            views_match = re.search(r"([\d,]+)\s*(?:views|plays)", body_text, re.IGNORECASE)
            likes_match = re.search(r"([\d,]+)\s*likes?", body_text, re.IGNORECASE)
            comments_match = re.search(r"([\d,]+)\s*comments?", body_text, re.IGNORECASE)

            if views_match:
                result["views"] = int(views_match.group(1).replace(",", ""))
            if likes_match:
                result["likes"] = int(likes_match.group(1).replace(",", ""))
            if comments_match:
                result["comments"] = int(comments_match.group(1).replace(",", ""))

            # Имитируем обычное поведение
            await self.human.random_pause(0.5, 1.5)

        except Exception as e:
            logger.debug("Parse reel detail %s failed: %s", reel.shortcode, e)

        return result

    def _parse_meta_content(self, content: str, profile: ProfileData) -> ProfileData:
        """Парсит мета-описание профиля."""
        import re
        nums = re.findall(r"([\d,.]+[KMkm]?)\s+(Followers?|Following|Posts?)", content, re.IGNORECASE)
        for val_str, label in nums:
            num = self._parse_number(val_str)
            if num is None:
                continue
            label_lower = label.lower()
            if "follower" in label_lower:
                profile.followers = num
            elif "following" in label_lower:
                profile.following = num
            elif "post" in label_lower:
                profile.posts_count = num
        return profile

    @staticmethod
    def _parse_number(text: str) -> int | None:
        """Парсит числа вида '1,234', '12.5K', '1.2M'."""
        if not text:
            return None
        text = text.strip().replace(",", "")
        try:
            if text.upper().endswith("K"):
                return int(float(text[:-1]) * 1000)
            elif text.upper().endswith("M"):
                return int(float(text[:-1]) * 1000000)
            return int(float(text))
        except (ValueError, TypeError):
            return None


# ─── Управление чекер-аккаунтами ─────────────────────────────────────────────

class CheckerPool:
    """
    Пул чекер-аккаунтов: свой импорт, свои прокси.
    Ротация, пометка забаненных, порог живых.
    """

    async def import_checkers(self, lines: list[str]) -> int:
        """
        Массовый импорт чекеров.
        Формат: login:password[:2fa[:proxy_host:proxy_port:proxy_user:proxy_pass]]
        """
        added = 0
        for line in lines:
            parts = line.strip().split(":")
            if len(parts) < 2:
                continue

            username = parts[0]
            password = parts[1]
            totp = parts[2] if len(parts) > 2 else None
            proxy_host = parts[3] if len(parts) > 3 else None
            proxy_port = int(parts[4]) if len(parts) > 4 else None
            proxy_user = parts[5] if len(parts) > 5 else None
            proxy_pass = parts[6] if len(parts) > 6 else None

            await _run_sync(
                execute,
                """INSERT OR IGNORE INTO checker_accounts
                   (username, password, totp_secret, proxy_host, proxy_port, proxy_username, proxy_password)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (username, password, totp, proxy_host, proxy_port, proxy_user, proxy_pass),
            )
            added += 1

        logger.info("Imported %d checker accounts", added)
        return added

    async def get_next_checker(self, exclude_ids: set[int] | None = None) -> dict | None:
        """Берёт следующий активный чекер (ротация по last_used_at)."""
        exclude = exclude_ids or set()
        checkers = await _run_sync(
            query,
            """SELECT * FROM checker_accounts
               WHERE status = 'active'
               ORDER BY last_used_at ASC NULLS FIRST""",
            (),
        )

        for c in checkers:
            if c["id"] not in exclude:
                # Обновляем last_used_at
                await _run_sync(
                    execute,
                    "UPDATE checker_accounts SET last_used_at = unixepoch('now') WHERE id = ?",
                    (c["id"],),
                )
                return c
        return None

    async def mark_banned(self, checker_id: int) -> None:
        """Помечает чекер как забаненный."""
        await _run_sync(
            execute,
            "UPDATE checker_accounts SET status = 'banned', ban_count = ban_count + 1 WHERE id = ?",
            (checker_id,),
        )
        logger.warning("Checker %d marked as banned", checker_id)

    async def get_alive_count(self) -> int:
        """Количество живых чекеров."""
        row = await _run_sync(
            query_one,
            "SELECT COUNT(*) as cnt FROM checker_accounts WHERE status = 'active'",
            (),
        )
        return row["cnt"]

    async def list_checkers(self) -> list[dict]:
        return await _run_sync(query, "SELECT * FROM checker_accounts ORDER BY id", ())


# ─── Подтверждение бана ──────────────────────────────────────────────────────

class BanDetector:
    """
    Подтверждение бана: минимум 2-3 проверки с разных чекеров.
    Одна проверка = один голос. Большинство голосов = решение.
    """

    async def vote(self, account_id: int, checker_id: int, is_banned: bool) -> None:
        """Записывает голос чекера."""
        await _run_sync(
            execute,
            """INSERT INTO ban_votes (account_id, checker_id, is_banned, checked_at)
               VALUES (?, ?, ?, unixepoch('now'))
               ON CONFLICT(account_id, checker_id)
               DO UPDATE SET is_banned = ?, checked_at = unixepoch('now')""",
            (account_id, checker_id, int(is_banned), int(is_banned)),
        )

    async def is_confirmed_ban(self, account_id: int) -> bool:
        """
        Проверяет подтверждён ли бан.
        Нужно минимум BAN_CONFIRM_COUNT голосов «забанен» от разных чекеров.
        """
        row = await _run_sync(
            query_one,
            """SELECT COUNT(*) as cnt FROM ban_votes
               WHERE account_id = ? AND is_banned = 1""",
            (account_id,),
        )
        return row["cnt"] >= BAN_CONFIRM_COUNT

    async def get_votes(self, account_id: int) -> list[dict]:
        """Все голоса по аккаунту."""
        return await _run_sync(
            query,
            """SELECT bv.*, ca.username as checker_username
               FROM ban_votes bv
               JOIN checker_accounts ca ON ca.id = bv.checker_id
               WHERE bv.account_id = ?""",
            (account_id,),
        )

    async def clear_votes(self, account_id: int) -> None:
        """Сбрасывает голоса (если аккаунт ожил)."""
        await _run_sync(
            execute,
            "DELETE FROM ban_votes WHERE account_id = ?",
            (account_id,),
        )


# ─── Главный контроллер чекера ────────────────────────────────────────────────

class CheckerController:
    """
    Оркестрирует проверку целевых аккаунтов:
    - Берёт чекер из пула
    - Логинит (или использует существующий профиль)
    - Парсит целевые аккаунты
    - Записывает статистику
    - Голосует за бан
    - Ротирует чекеры

    Два режима:
    - manual_check(account_ids) — проверить конкретные
    - auto_check(niche_id) — автоцикл каждые 50-90 минут
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
    ) -> None:
        self.pm = profile_manager
        self.checker_pool = CheckerPool()
        self.ban_detector = BanDetector()
        self._stop_event = asyncio.Event()

    def init(self) -> None:
        init_checker_tables()

    # ── Ручная проверка ───────────────────────────────────────────────────

    async def manual_check(
        self,
        account_ids: list[int],
        on_result: Callable[[CheckResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[CheckResult]:
        """Проверить конкретные аккаунты прямо сейчас."""
        results: list[CheckResult] = []

        # Получаем чекер
        checker = await self.checker_pool.get_next_checker()
        if not checker:
            logger.error("No active checkers available")
            return results

        # Проверяем порог
        alive = await self.checker_pool.get_alive_count()
        if alive < MIN_ALIVE_CHECKERS:
            logger.warning("Only %d alive checkers (min %d), proceeding cautiously", alive, MIN_ALIVE_CHECKERS)

        context, page = None, None
        try:
            # Запускаем профиль чекера
            proxy = self._checker_proxy(checker)
            if not self.pm.profile_exists(checker["username"]):
                self.pm.create_profile(checker["username"])

            context, page = await self.pm.launch_profile(
                profile_id=checker["username"],
                proxy=proxy,
            )
            human = HumanInteractor(page, checker["username"])
            parser = ProfileParser(page, human)

            # Рандомизация порядка
            shuffled_ids = list(account_ids)
            random.shuffle(shuffled_ids)

            for account_id in shuffled_ids:
                target = await _run_sync(
                    query_one,
                    "SELECT id, username FROM accounts WHERE id = ?",
                    (account_id,),
                )
                if not target:
                    continue

                result = await self._check_one(
                    parser, checker, target, account_id,
                )
                results.append(result)

                if on_result:
                    try:
                        await on_result(result)
                    except Exception:
                        pass

                # Проверяем не забанен ли сам чекер
                if result.message == "checker_banned":
                    await self.checker_pool.mark_banned(checker["id"])
                    break

                # Пауза между проверками
                await asyncio.sleep(random.uniform(3.0, 8.0))

        except Exception as e:
            logger.exception("Manual check failed: %s", e)
        finally:
            if context:
                await self.pm.close_profile(checker["username"])

        return results

    # ── Автоцикл ──────────────────────────────────────────────────────────

    async def auto_check(
        self,
        niche_id: int | None = None,
        on_result: Callable[[CheckResult], Coroutine[Any, Any, None]] | None = None,
        on_alert: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """
        Автоцикл: проверяет все залогиненные аккаунты каждые 50-90 минут.
        Останавливается через stop().
        """
        self._stop_event.clear()

        while not self._stop_event.is_set():
            # Порог живых чекеров
            alive = await self.checker_pool.get_alive_count()
            if alive < MIN_ALIVE_CHECKERS:
                msg = f"ALERT: Only {alive} alive checkers (min {MIN_ALIVE_CHECKERS}), stopping"
                logger.error(msg)
                if on_alert:
                    await on_alert(msg)
                break

            # Целевые аккаунты
            if niche_id:
                targets = await _run_sync(
                    query,
                    "SELECT id FROM accounts WHERE niche_id = ? AND status = 'logged_in' ORDER BY id",
                    (niche_id,),
                )
            else:
                targets = await _run_sync(
                    query,
                    "SELECT id FROM accounts WHERE status = 'logged_in' ORDER BY id",
                    (),
                )

            if targets:
                target_ids = [t["id"] for t in targets]
                await self.manual_check(target_ids, on_result)

            # Пауза 50-90 мин (рандомизация)
            interval = random.uniform(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX)
            logger.info("Next check in %.0f minutes", interval / 60)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()

    # ── Проверка одного аккаунта ──────────────────────────────────────────

    async def _check_one(
        self,
        parser: ProfileParser,
        checker: dict,
        target: dict,
        account_id: int,
    ) -> CheckResult:
        """Парсит один целевой аккаунт, записывает данные."""
        target_username = target["username"]

        try:
            profile = await parser.parse_profile(target_username)

            # Проверка: чекер сам забанен?
            if profile.is_banned:
                # Может быть что целевой аккаунт забанен, а может чекер
                # Пробуем открыть известный существующий профиль для проверки
                test = await parser.parse_profile("instagram")
                if test.is_banned:
                    return CheckResult(
                        account_id=account_id,
                        target_username=target_username,
                        checker_username=checker["username"],
                        success=False,
                        message="checker_banned",
                    )

            # Голосуем за бан
            await self.ban_detector.vote(account_id, checker["id"], profile.is_banned)

            # Если бан подтверждён — помечаем аккаунт
            if profile.is_banned:
                confirmed = await self.ban_detector.is_confirmed_ban(account_id)
                if confirmed:
                    await _run_sync(
                        execute,
                        "UPDATE accounts SET status = 'dead', notes = 'Banned (confirmed by checkers)' WHERE id = ?",
                        (account_id,),
                    )
                    logger.warning("[%s] Ban CONFIRMED by %d checkers", target_username, BAN_CONFIRM_COUNT)

            # Сохраняем снапшот профиля
            await _run_sync(
                execute,
                """INSERT INTO account_snapshots
                   (account_id, followers, following, posts_count, is_banned, checker_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    account_id, profile.followers, profile.following,
                    profile.posts_count, int(profile.is_banned), checker["id"],
                ),
            )

            # Сохраняем статистику рилсов
            for reel in profile.reels:
                await _run_sync(
                    execute,
                    """INSERT INTO reel_stats
                       (account_id, reel_url, reel_shortcode, views, likes, comments, checker_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id, reel.url, reel.shortcode,
                        reel.views, reel.likes, reel.comments, checker["id"],
                    ),
                )

            return CheckResult(
                account_id=account_id,
                target_username=target_username,
                checker_username=checker["username"],
                success=True,
                profile=profile,
            )

        except Exception as e:
            logger.warning(
                "[%s] Check of %s failed: %s",
                checker["username"], target_username, e,
            )
            return CheckResult(
                account_id=account_id,
                target_username=target_username,
                checker_username=checker["username"],
                success=False,
                message=str(e),
            )

    def _checker_proxy(self, checker: dict) -> dict[str, str] | None:
        """Собирает proxy dict из чекер-аккаунта."""
        if checker.get("proxy_host") and checker.get("proxy_port"):
            return {
                "server": f"{checker['proxy_host']}:{checker['proxy_port']}",
                "username": checker.get("proxy_username") or "",
                "password": checker.get("proxy_password") or "",
            }
        return None

    # ── Статистика ────────────────────────────────────────────────────────

    async def get_account_stats(self, account_id: int) -> dict[str, Any]:
        """Полная статистика аккаунта для дашборда."""
        # Последний снапшот
        latest = await _run_sync(
            query_one,
            "SELECT * FROM account_snapshots WHERE account_id = ? ORDER BY checked_at DESC LIMIT 1",
            (account_id,),
        )

        # История подписчиков (последние 50 точек)
        followers_history = await _run_sync(
            query,
            """SELECT followers, checked_at FROM account_snapshots
               WHERE account_id = ? ORDER BY checked_at DESC LIMIT 50""",
            (account_id,),
        )

        # Последние рилсы
        reels = await _run_sync(
            query,
            """SELECT reel_shortcode, MAX(views) as views, MAX(likes) as likes,
                      MAX(comments) as comments, MAX(checked_at) as last_checked
               FROM reel_stats WHERE account_id = ?
               GROUP BY reel_shortcode ORDER BY last_checked DESC LIMIT 12""",
            (account_id,),
        )

        # Суммарные просмотры
        total_views = await _run_sync(
            query_one,
            """SELECT SUM(max_views) as total FROM (
                 SELECT MAX(views) as max_views FROM reel_stats
                 WHERE account_id = ? GROUP BY reel_shortcode
               )""",
            (account_id,),
        )

        return {
            "account_id": account_id,
            "latest_snapshot": latest,
            "followers_history": list(reversed(followers_history)),
            "reels": reels,
            "total_views": total_views["total"] if total_views else 0,
        }

    async def get_niche_stats(self, niche_id: int) -> dict[str, Any]:
        """Агрегированная статистика по нише."""
        accounts = await _run_sync(
            query,
            "SELECT id, username, status FROM accounts WHERE niche_id = ?",
            (niche_id,),
        )

        total_followers = 0
        total_views = 0
        alive_count = 0
        dead_count = 0

        for acc in accounts:
            if acc["status"] == "dead":
                dead_count += 1
                continue
            alive_count += 1

            snap = await _run_sync(
                query_one,
                "SELECT followers FROM account_snapshots WHERE account_id = ? ORDER BY checked_at DESC LIMIT 1",
                (acc["id"],),
            )
            if snap:
                total_followers += snap["followers"]

            views = await _run_sync(
                query_one,
                """SELECT SUM(max_views) as total FROM (
                     SELECT MAX(views) as max_views FROM reel_stats
                     WHERE account_id = ? GROUP BY reel_shortcode
                   )""",
                (acc["id"],),
            )
            if views and views["total"]:
                total_views += views["total"]

        return {
            "niche_id": niche_id,
            "total_accounts": len(accounts),
            "alive": alive_count,
            "dead": dead_count,
            "total_followers": total_followers,
            "total_views": total_views,
        }
