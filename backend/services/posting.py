"""
InstaGrid — Постинг рилсов.

Два режима:
1. Автоматический: полный цикл (отлёжка → прогрев → 3 рилса → пауза → повтор)
2. Ручной: выбираешь аккаунты, запускаешь постинг вручную

Воркфлоу сессии:
- Прогрев 20 сек – 3 мин: листает ленту, рилсы, 1-3 лайка, «читает» комменты
- Постит рилс мышкой как человек (HumanInteractor), burst-typing описание
- Ждёт подтверждения загрузки
- Прогрев → следующий рилс
- После N рилсов → закрытие профиля

Параметры из CLAUDE.md:
- Отлёжка после логина: 6-8 часов
- Рилсов за сессию: 3 (настраиваемо)
- Пауза между сессиями: 10-14 часов
- Hard timeout: 15 мин на цикл аккаунта
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from playwright.async_api import Page, TimeoutError as PwTimeout

from backend.database import execute, query, query_one, run_sync
from backend.services.human import HumanInteractor
from backend.services.profile_manager import ProfileManager
from backend.services.content_manager import ContentManager
from backend.services.mobile_proxy import MobileProxyRotator

logger = logging.getLogger("instagrid.posting")


# ─── Константы ────────────────────────────────────────────────────────────────

INSTAGRAM_URL = "https://www.instagram.com/"

# Таймауты — Fix #4: динамический расчёт
HARD_TIMEOUT_BASE = 5 * 60             # 5 мин базовое время (прогрев + открытие)
HARD_TIMEOUT_PER_REEL = 6 * 60         # 6 мин на каждый рилс (прогрев 3мин + загрузка 2мин + запас)
PAGE_LOAD_TIMEOUT = 30_000             # мс
ELEMENT_WAIT_TIMEOUT = 10_000          # мс
UPLOAD_CONFIRM_TIMEOUT = 120_000       # мс — ожидание загрузки видео на сервер

# Сессия постинга
DEFAULT_REELS_PER_SESSION = 3
REST_AFTER_LOGIN_MIN = 6 * 3600        # 6 часов (сек)
REST_AFTER_LOGIN_MAX = 8 * 3600        # 8 часов
PAUSE_BETWEEN_SESSIONS_MIN = 10 * 3600 # 10 часов
PAUSE_BETWEEN_SESSIONS_MAX = 14 * 3600 # 14 часов

# Прогрев между рилсами
WARMUP_MIN = 20                        # сек
WARMUP_MAX = 180                       # 3 мин


# ─── Селекторы Instagram (постинг) ───────────────────────────────────────────

class PostSelectors:
    """CSS/XPath селекторы для постинга рилсов."""

    # Кнопка «Создать» (+ в навбаре)
    CREATE_BUTTON = 'svg[aria-label="New post"], a[href="/create/select/"]'
    CREATE_MENU_POST = '//span[contains(text(), "Post")]'
    CREATE_MENU_REEL = '//span[contains(text(), "Reel")]'

    # Диалог загрузки
    FILE_INPUT = 'input[type="file"][accept*="video"]'
    FILE_INPUT_FALLBACK = 'input[type="file"]'

    # Шаги создания рилса
    NEXT_BUTTON = '//button[contains(text(), "Next")]'
    SHARE_BUTTON = '//button[contains(text(), "Share")]'

    # Поле описания
    CAPTION_INPUT = 'div[aria-label="Write a caption..."], div[role="textbox"]'

    # Подтверждение публикации
    SHARED_CONFIRMATION = '//span[contains(text(), "Your reel has been shared")]'
    POST_SHARED = '//span[contains(text(), "has been shared") or contains(text(), "Reel shared")]'
    SHARED_FALLBACK = 'img[data-testid="media-thumbnail"]'

    # Прогрев: элементы ленты
    FEED_POST = 'article[role="presentation"]'
    LIKE_BUTTON = 'svg[aria-label="Like"]'
    UNLIKE_BUTTON = 'svg[aria-label="Unlike"]'
    COMMENT_SECTION = 'svg[aria-label="Comment"]'
    REELS_TAB = 'svg[aria-label="Reels"]'


# ─── Результат постинга ──────────────────────────────────────────────────────

class PostStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    NO_CONTENT = "no_content"


@dataclass
class PostResult:
    account_id: int
    username: str
    status: PostStatus
    reels_posted: int = 0
    reels_target: int = 0
    message: str = ""
    duration_sec: float = 0.0


# ─── Утилиты БД ──────────────────────────────────────────────────────────────

# run_sync импортирован из backend.database (Fix #7)
_run_sync = run_sync


async def _update_account_action(account_id: int) -> None:
    """Обновляет last_action_at."""
    await _run_sync(
        execute,
        "UPDATE accounts SET last_action_at = unixepoch('now'), updated_at = unixepoch('now') WHERE id = ?",
        (account_id,),
    )


# ─── Прогрев между рилсами ───────────────────────────────────────────────────

class FeedWarmer:
    """
    Прогрев: листание ленты/рилсов, лайки, «чтение» комментов.
    Запускается между рилсами для имитации обычного поведения.
    """

    def __init__(self, page: Page, human: HumanInteractor) -> None:
        self.page = page
        self.human = human

    async def warmup(self, min_sec: float = WARMUP_MIN, max_sec: float = WARMUP_MAX) -> None:
        """
        Прогрев: 20 сек – 3 мин активности.
        - Скроллит ленту
        - 1-3 лайка
        - «Читает» комменты
        - Иногда заходит в рилсы
        """
        duration = random.uniform(min_sec, max_sec)
        end_time = time.time() + duration
        likes_given = 0
        max_likes = random.randint(1, 3)

        logger.info("[%s] Warmup %.0fs", self.human.username, duration)

        # Переходим на главную
        try:
            await self.page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        except PwTimeout:
            logger.warning("[%s] Feed load timeout during warmup", self.human.username)
            return

        await self.human.random_pause(1.0, 3.0)

        while time.time() < end_time:
            action = random.choices(
                ["scroll", "like", "dwell", "reels", "comments"],
                weights=[40, 20, 20, 10, 10],
                k=1,
            )[0]

            try:
                if action == "scroll":
                    await self._scroll_feed()
                elif action == "like" and likes_given < max_likes:
                    liked = await self._like_post()
                    if liked:
                        likes_given += 1
                elif action == "dwell":
                    await self.human.dwell()
                elif action == "reels":
                    await self._browse_reels()
                elif action == "comments":
                    await self._read_comments()
            except Exception as e:
                logger.debug("[%s] Warmup action '%s' failed: %s", self.human.username, action, e)

            await self.human.random_pause(1.0, 4.0)

        logger.info("[%s] Warmup done, %d likes", self.human.username, likes_given)

    async def _scroll_feed(self) -> None:
        """Скроллит ленту вниз."""
        delta = random.randint(200, 600)
        await self.human.scroll(delta)
        await asyncio.sleep(random.uniform(1.0, 3.0))

    async def _like_post(self) -> bool:
        """Лайкает первый нелайкнутый пост в видимой области."""
        btns = await self.page.query_selector_all(PostSelectors.LIKE_BUTTON)
        if not btns:
            return False
        btn = random.choice(btns[:3])  # из первых трёх видимых
        await self.human.click_element(btn)
        await self.human.random_pause(0.5, 1.5)
        return True

    async def _browse_reels(self) -> None:
        """Заходит в рилсы на несколько секунд."""
        reels_btn = await self.page.query_selector(PostSelectors.REELS_TAB)
        if reels_btn:
            await self.human.click_element(reels_btn)
            await asyncio.sleep(random.uniform(3.0, 8.0))
            # Скроллим пару рилсов
            for _ in range(random.randint(1, 3)):
                await self.human.scroll(random.randint(400, 800))
                await asyncio.sleep(random.uniform(2.0, 5.0))
            # Назад на главную
            await self.page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

    async def _read_comments(self) -> None:
        """Открывает комменты на посте и «читает»."""
        btns = await self.page.query_selector_all(PostSelectors.COMMENT_SECTION)
        if not btns:
            return
        btn = random.choice(btns[:2])
        await self.human.click_element(btn)
        await asyncio.sleep(random.uniform(2.0, 5.0))
        # Скроллим комменты
        await self.human.scroll(random.randint(100, 300))
        await asyncio.sleep(random.uniform(1.0, 3.0))
        # Закрываем (Escape или назад)
        await self.page.keyboard.press("Escape")


# ─── Постинг одного рилса ────────────────────────────────────────────────────

class ReelPoster:
    """Публикует один рилс через UI Instagram."""

    def __init__(self, page: Page, human: HumanInteractor) -> None:
        self.page = page
        self.human = human

    async def post_reel(self, video_path: str, caption: str = "") -> bool:
        """
        Загружает и публикует один рилс.

        Returns:
            True если опубликован, False если ошибка.
        """
        username = self.human.username
        logger.info("[%s] Posting reel: %s", username, video_path)

        try:
            # 1. Нажимаем «Создать»
            await self.human.click_selector(PostSelectors.CREATE_BUTTON)
            await self.human.random_pause(1.0, 2.0)

            # 2. Загружаем видеофайл через скрытый input
            file_input = await self.page.query_selector(PostSelectors.FILE_INPUT)
            if not file_input:
                file_input = await self.page.query_selector(PostSelectors.FILE_INPUT_FALLBACK)
            if not file_input:
                # Иногда нужно сначала выбрать "Reel" из меню
                reel_option = await self.page.query_selector(PostSelectors.CREATE_MENU_REEL)
                if reel_option:
                    await self.human.click_element(reel_option)
                    await self.human.random_pause(1.0, 2.0)
                file_input = await self.page.wait_for_selector(
                    f"{PostSelectors.FILE_INPUT}, {PostSelectors.FILE_INPUT_FALLBACK}",
                    timeout=ELEMENT_WAIT_TIMEOUT,
                )

            if not file_input:
                logger.error("[%s] File input not found", username)
                return False

            await file_input.set_input_files(video_path)
            logger.info("[%s] Video file uploaded to form", username)

            # 3. Ждём обработку + нажимаем Next
            await self.human.random_pause(3.0, 6.0)
            await self._click_next_buttons()

            # 4. Вводим описание
            if caption:
                await self._enter_caption(caption)

            # 5. Нажимаем Share
            await self._click_share()

            # 6. Ждём подтверждения публикации
            confirmed = await self._wait_confirmation()
            if confirmed:
                logger.info("[%s] Reel posted successfully", username)
            else:
                logger.warning("[%s] Reel share confirmation not detected", username)

            return confirmed

        except PwTimeout as e:
            logger.error("[%s] Timeout posting reel: %s", username, e)
            return False
        except Exception as e:
            logger.exception("[%s] Error posting reel", username)
            return False

    async def _click_next_buttons(self) -> None:
        """Прокликивает кнопки Next в визарде создания."""
        for _ in range(3):  # максимум 3 шага Next
            try:
                btn = await self.page.wait_for_selector(
                    PostSelectors.NEXT_BUTTON,
                    timeout=5000,
                )
                if btn:
                    await self.human.random_pause(0.8, 1.5)
                    await self.human.click_element(btn)
                    await self.human.random_pause(1.0, 2.5)
            except PwTimeout:
                break

    async def _enter_caption(self, caption: str) -> None:
        """Вводит описание с burst-typing."""
        try:
            caption_el = await self.page.wait_for_selector(
                PostSelectors.CAPTION_INPUT,
                timeout=ELEMENT_WAIT_TIMEOUT,
            )
            if caption_el:
                await self.human.click_element(caption_el)
                await self.human.random_pause(0.5, 1.0)
                await self.human.type_text(caption)
                await self.human.random_pause(0.5, 1.5)
        except PwTimeout:
            logger.warning("[%s] Caption input not found, posting without caption", self.human.username)

    async def _click_share(self) -> None:
        """Нажимает кнопку Share."""
        btn = await self.page.wait_for_selector(
            PostSelectors.SHARE_BUTTON,
            timeout=ELEMENT_WAIT_TIMEOUT,
        )
        if btn:
            await self.human.random_pause(0.5, 1.2)
            await self.human.click_element(btn)

    async def _wait_confirmation(self) -> bool:
        """Ждёт подтверждения что рилс опубликован."""
        try:
            await self.page.wait_for_selector(
                f"{PostSelectors.SHARED_CONFIRMATION}, {PostSelectors.POST_SHARED}",
                timeout=UPLOAD_CONFIRM_TIMEOUT,
            )
            return True
        except PwTimeout:
            # Fallback: проверяем URL или другие признаки
            url = self.page.url
            if "/reel/" in url or "/p/" in url:
                return True
            return False


# ─── Сессия постинга (N рилсов + прогрев) ────────────────────────────────────

class PostingSession:
    """
    Одна сессия постинга для одного аккаунта:
    прогрев → рилс → прогрев → рилс → ... → закрытие.
    """

    def __init__(
        self,
        page: Page,
        human: HumanInteractor,
        content_manager: ContentManager,
        account: dict[str, Any],
        reels_per_session: int = DEFAULT_REELS_PER_SESSION,
    ) -> None:
        self.page = page
        self.human = human
        self.cm = content_manager
        self.account = account
        self.reels_per_session = reels_per_session
        self.warmer = FeedWarmer(page, human)
        self.poster = ReelPoster(page, human)

    async def run(self) -> PostResult:
        """
        Запускает сессию постинга.

        Returns:
            PostResult с количеством опубликованных рилсов
        """
        account_id = self.account["id"]
        username = self.account["username"]
        start = time.time()

        result = PostResult(
            account_id=account_id,
            username=username,
            status=PostStatus.SUCCESS,
            reels_target=self.reels_per_session,
        )

        try:
            # Fix #4: динамический таймаут = base + per_reel × count
            session_timeout = HARD_TIMEOUT_BASE + HARD_TIMEOUT_PER_REEL * self.reels_per_session
            async with asyncio.timeout(session_timeout):
                for i in range(1, self.reels_per_session + 1):
                    logger.info("[%s] Reel %d/%d", username, i, self.reels_per_session)

                    # Прогрев перед каждым рилсом
                    await self.warmer.warmup()

                    # Получаем контент
                    content = await self.cm.get_posting_content(account_id)
                    if not content or not content.get("video"):
                        logger.warning("[%s] No content available for reel %d", username, i)
                        result.status = PostStatus.NO_CONTENT
                        result.message = f"No video for reel {i}"
                        break

                    video = content["video"]
                    caption = content["description"]["text"] if content.get("description") else ""

                    # Постим
                    ok = await self.poster.post_reel(video["filepath"], caption)

                    if ok:
                        result.reels_posted += 1
                        # Отмечаем видео как опубликованное
                        await self.cm.videos.mark_posted(video["id"])
                        await _update_account_action(account_id)
                    else:
                        logger.warning("[%s] Reel %d failed, skipping", username, i)
                        result.message += f"Reel {i} failed. "

                    # Пауза между рилсами (не после последнего)
                    if i < self.reels_per_session:
                        await self.human.random_pause(3.0, 8.0)

        except TimeoutError:
            result.status = PostStatus.TIMEOUT
            result.message = f"Hard timeout {session_timeout}s"
            logger.error("[%s] Session timeout", username)

        except Exception as e:
            result.status = PostStatus.FAILED
            result.message = str(e)
            logger.exception("[%s] Session error", username)

        result.duration_sec = time.time() - start

        if result.reels_posted == 0 and result.status == PostStatus.SUCCESS:
            result.status = PostStatus.FAILED

        logger.info(
            "[%s] Session done: %d/%d reels, %.0fs",
            username, result.reels_posted, result.reels_target, result.duration_sec,
        )
        return result


# ─── Контроллер постинга ─────────────────────────────────────────────────────

class PostingController:
    """
    Главный контроллер. Поддерживает два режима:

    1. manual_post(account_ids, ...) — ручной постинг выбранных аккаунтов
    2. auto_post(niche_id, ...) — полный автоцикл всех/выбранных аккаунтов

    В обоих режимах:
    - Параллельный пул воркеров
    - Отслеживание прогресса
    - Остановка по запросу
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        content_manager: ContentManager,
        max_workers: int = 5,
        reels_per_session: int = DEFAULT_REELS_PER_SESSION,
    ) -> None:
        self.pm = profile_manager
        self.cm = content_manager
        self.max_workers = max_workers
        self.reels_per_session = reels_per_session

        self._stop_event = asyncio.Event()
        self._results: list[PostResult] = []
        self._results_lock = asyncio.Lock()
        self._active_count: int = 0
        self._processed: int = 0
        self._total: int = 0

    # ── Ручной режим ──────────────────────────────────────────────────────

    async def manual_post(
        self,
        account_ids: list[int],
        reels_count: int | None = None,
        skip_warmup_first: bool = False,
        on_progress: Callable[[PostResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[PostResult]:
        """
        Ручной постинг: выбираешь аккаунты, запускаешь.
        Без отлёжки, без автоцикла — сразу прогрев + постинг.

        Args:
            account_ids: список ID аккаунтов для постинга
            reels_count: сколько рилсов (по умолчанию reels_per_session)
            skip_warmup_first: пропустить прогрев перед первым рилсом
            on_progress: callback при завершении каждого аккаунта
        """
        reels = reels_count or self.reels_per_session
        self._reset()
        self._total = len(account_ids)

        # Получаем данные аккаунтов
        accounts = []
        for aid in account_ids:
            acc = await _run_sync(
                query_one,
                "SELECT * FROM accounts WHERE id = ?",
                (aid,),
            )
            if acc and acc["status"] == "logged_in":
                accounts.append(acc)
            else:
                logger.warning("Account %d skipped (not logged_in or not found)", aid)

        if not accounts:
            return []

        self._total = len(accounts)

        # Очередь
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for acc in accounts:
            await queue.put(acc)

        # Воркеры
        workers = [
            asyncio.create_task(
                self._posting_worker(i, queue, reels, on_progress)
            )
            for i in range(min(self.max_workers, len(accounts)))
        ]

        await asyncio.gather(*workers, return_exceptions=True)
        return self._results

    # ── Автоматический режим ──────────────────────────────────────────────

    async def auto_post(
        self,
        niche_id: int | None = None,
        account_ids: list[int] | None = None,
        reels_count: int | None = None,
        rest_after_login: bool = True,
        loop_forever: bool = True,
        on_progress: Callable[[PostResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[PostResult]:
        """
        Автоматический цикл:
        1. Берёт залогиненные аккаунты (все из ниши или указанные)
        2. Проверяет отлёжку (6-8ч после логина)
        3. Сессия постинга (прогрев + N рилсов)
        4. Пауза 10-14ч
        5. Повтор (если loop_forever=True)

        Args:
            niche_id: постить все аккаунты из ниши
            account_ids: или конкретные аккаунты (приоритет над niche_id)
            reels_count: рилсов за сессию
            rest_after_login: ждать 6-8ч после логина
            loop_forever: зациклить (True) или одна итерация (False)
            on_progress: callback
        """
        reels = reels_count or self.reels_per_session
        self._reset()

        while not self._stop_event.is_set():
            # Получаем аккаунты
            accounts = await self._get_ready_accounts(
                niche_id=niche_id,
                account_ids=account_ids,
                check_rest=rest_after_login,
            )

            if not accounts:
                if loop_forever:
                    logger.info("No ready accounts, sleeping 30 min...")
                    await self._interruptible_sleep(1800)
                    continue
                else:
                    break

            self._total = len(accounts)
            self._processed = 0

            queue: asyncio.Queue[dict] = asyncio.Queue()
            for acc in accounts:
                await queue.put(acc)

            workers = [
                asyncio.create_task(
                    self._posting_worker(i, queue, reels, on_progress)
                )
                for i in range(min(self.max_workers, len(accounts)))
            ]

            await asyncio.gather(*workers, return_exceptions=True)

            if not loop_forever:
                break

            # Пауза между сессиями
            pause = random.uniform(PAUSE_BETWEEN_SESSIONS_MIN, PAUSE_BETWEEN_SESSIONS_MAX)
            logger.info("Session complete. Next in %.1f hours", pause / 3600)
            await self._interruptible_sleep(pause)

        return self._results

    # ── Управление ────────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Останавливает автоцикл. Текущие рилсы дорабатывают."""
        self._stop_event.set()
        logger.info("Stop requested, finishing active tasks...")

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set() and self._active_count > 0

    @property
    def progress(self) -> dict[str, int]:
        return {
            "processed": self._processed,
            "total": self._total,
            "active_workers": self._active_count,
            "total_reels_posted": sum(r.reels_posted for r in self._results),
        }

    @property
    def results(self) -> list[PostResult]:
        return list(self._results)

    # ── Внутренние ────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._results = []
        self._stop_event.clear()
        self._active_count = 0
        self._processed = 0
        self._total = 0

    async def _posting_worker(
        self,
        worker_id: int,
        queue: asyncio.Queue[dict],
        reels_count: int,
        on_progress: Callable[[PostResult], Coroutine[Any, Any, None]] | None,
    ) -> None:
        """Один воркер: берёт аккаунты из очереди и постит."""
        self._active_count += 1

        while not self._stop_event.is_set():
            try:
                account = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            username = account["username"]
            logger.info("[Worker %d] Starting %s", worker_id, username)

            result = await self._post_for_account(account, reels_count)

            async with self._results_lock:
                self._results.append(result)
                self._processed += 1

            if on_progress:
                try:
                    await on_progress(result)
                except Exception:
                    pass

            # Закрываем профиль после сессии
            try:
                await self.pm.close_profile(username)
            except Exception:
                pass

            queue.task_done()

        self._active_count -= 1

    async def _post_for_account(
        self,
        account: dict[str, Any],
        reels_count: int,
    ) -> PostResult:
        """Запускает профиль и проводит сессию постинга."""
        username = account["username"]

        try:
            # Собираем прокси
            proxy = await self._get_account_proxy(account)

            # Запуск профиля
            if not self.pm.profile_exists(username):
                return PostResult(
                    account_id=account["id"],
                    username=username,
                    status=PostStatus.FAILED,
                    message="Profile not found",
                )

            context, page = await self.pm.launch_profile(
                profile_id=username,
                proxy=proxy,
                headless=False,
            )

            human = HumanInteractor(page, username)

            # Сессия
            session = PostingSession(
                page=page,
                human=human,
                content_manager=self.cm,
                account=account,
                reels_per_session=reels_count,
            )
            return await session.run()

        except Exception as e:
            logger.exception("[%s] Failed to run posting session", username)
            return PostResult(
                account_id=account["id"],
                username=username,
                status=PostStatus.FAILED,
                message=str(e),
            )

    async def _get_account_proxy(self, account: dict) -> dict[str, str] | None:
        """Получает прокси аккаунта из БД. Fix #17: мобильный — с ротацией."""
        proxy_id = account.get("static_proxy_id")
        if proxy_id:
            row = await _run_sync(
                query_one,
                "SELECT host, port, username, password FROM static_proxies WHERE id = ?",
                (proxy_id,),
            )
            if row:
                return {
                    "server": f"{row['host']}:{row['port']}",
                    "username": row["username"] or "",
                    "password": row["password"] or "",
                }

        # Мобильный пул — ротация + проверка уникальности IP
        mobile_pool = account.get("mobile_pool_id")
        if mobile_pool:
            pool = await _run_sync(
                query_one,
                "SELECT id, proxy_host, proxy_port, proxy_username, proxy_password, rotation_url FROM proxy_pools WHERE id = ?",
                (mobile_pool,),
            )
            if pool and pool["proxy_host"]:
                # Fix #17: ротация мобильного прокси перед каждым профилем
                rotator = MobileProxyRotator()
                proxy_config = {
                    "server": f"{pool['proxy_host']}:{pool['proxy_port']}",
                    "username": pool["proxy_username"] or "",
                    "password": pool["proxy_password"] or "",
                }
                if pool["rotation_url"]:
                    try:
                        await rotator.rotate_and_verify(
                            pool_id=pool["id"],
                            account_id=account["id"],
                            rotation_url=pool["rotation_url"],
                            proxy_config=proxy_config,
                        )
                    except Exception as e:
                        logger.warning(
                            "[%s] Mobile proxy rotation failed: %s",
                            account["username"], e,
                        )
                return proxy_config

        return None

    async def _get_ready_accounts(
        self,
        niche_id: int | None = None,
        account_ids: list[int] | None = None,
        check_rest: bool = True,
    ) -> list[dict]:
        """
        Возвращает аккаунты готовые к постингу:
        - Статус logged_in
        - Если check_rest: прошло 6-8ч с логина
        - Не в cooldown
        """
        if account_ids:
            placeholders = ",".join("?" * len(account_ids))
            accounts = await _run_sync(
                query,
                f"SELECT * FROM accounts WHERE id IN ({placeholders}) AND status = 'logged_in'",
                tuple(account_ids),
            )
        elif niche_id:
            accounts = await _run_sync(
                query,
                "SELECT * FROM accounts WHERE niche_id = ? AND status = 'logged_in' ORDER BY id ASC",
                (niche_id,),
            )
        else:
            accounts = await _run_sync(
                query,
                "SELECT * FROM accounts WHERE status = 'logged_in' ORDER BY id ASC",
                (),
            )

        if not check_rest:
            return accounts

        # Фильтруем: прошла ли отлёжка
        now = time.time()
        rest_min = REST_AFTER_LOGIN_MIN
        ready = []
        for acc in accounts:
            login_at = acc.get("last_login_at")
            if not login_at:
                ready.append(acc)  # никогда не логинился через нас — допускаем
                continue
            if now - login_at >= rest_min:
                ready.append(acc)
            else:
                remaining = rest_min - (now - login_at)
                logger.debug(
                    "[%s] Still resting, %.1fh remaining",
                    acc["username"], remaining / 3600,
                )

        return ready

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Сон с возможностью прерывания через stop()."""
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=seconds,
            )
        except TimeoutError:
            pass
