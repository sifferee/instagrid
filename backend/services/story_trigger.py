"""
InstaGrid — Story Auto-Trigger Background Task.

Замыкает цепочку: checker → STORY_TRIGGER → story posting.

Логика:
1. Фоновый asyncio task стартует при запуске приложения
2. Каждые 5 минут проверяет accounts.notes на наличие 'STORY_TRIGGER:'
3. Если найден — открывает браузерный профиль аккаунта, постит сторис
4. Сбрасывает флаг после успешного постинга
5. Jitter ±20-30 мин перед постингом (чтобы не было очевидной корреляции)
6. Максимум 1 сторис в 24 часа на аккаунт (проверяется в StoryAutoTrigger)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from backend.database import execute, query, query_one, run_sync
from backend.services.profile_manager import ProfileManager
from backend.services.stories import StoryController

logger = logging.getLogger("instagrid.story_trigger")

# ─── Константы ────────────────────────────────────────────────────────────────

POLL_INTERVAL = 5 * 60          # 5 минут между проверками
JITTER_MIN = 10 * 60            # 10 мин минимальная задержка перед постингом
JITTER_MAX = 30 * 60            # 30 мин максимальная задержка
MAX_CONCURRENT = 2              # максимум параллельных постингов сторис


# ─── Background task ─────────────────────────────────────────────────────────

class StoryTriggerWorker:
    """
    Фоновый воркер: мониторит STORY_TRIGGER и постит сторис.

    Жизненный цикл:
    1. start() — запускает фоновый asyncio task
    2. Каждые POLL_INTERVAL проверяет БД
    3. Для каждого аккаунта с STORY_TRIGGER:
       - Задержка JITTER_MIN-JITTER_MAX (антидетект)
       - Открывает браузер с сохранённым профилем
       - Публикует сторис через StoryController
       - Сбрасывает флаг
    4. stop() — останавливает
    """

    def __init__(self, profile_manager: ProfileManager) -> None:
        self.pm = profile_manager
        self.controller = StoryController()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active_count = 0

    async def start(self) -> None:
        """Запускает фоновый task."""
        if self._task and not self._task.done():
            logger.warning("Story trigger worker already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Story trigger worker started (poll every %ds)", POLL_INTERVAL)

    async def stop(self) -> None:
        """Останавливает фоновый task."""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Story trigger worker stopped")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run_loop(self) -> None:
        """Основной цикл: poll → process → sleep."""
        while not self._stop_event.is_set():
            try:
                await self._process_triggers()
            except Exception as e:
                logger.exception("Story trigger loop error: %s", e)

            # Ждём POLL_INTERVAL или stop
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=POLL_INTERVAL,
                )
                break  # stop was called
            except TimeoutError:
                pass  # normal timeout, continue polling

    async def _process_triggers(self) -> None:
        """Находит аккаунты с STORY_TRIGGER и обрабатывает."""
        # Ищем аккаунты с флагом STORY_TRIGGER: в notes
        triggered = await run_sync(
            query,
            """SELECT id, username, niche_id, notes, static_proxy_id, mobile_pool_id
               FROM accounts
               WHERE status = 'logged_in'
                 AND notes LIKE 'STORY_TRIGGER:%'
               ORDER BY updated_at ASC""",
            (),
        )

        if not triggered:
            return

        logger.info("Found %d accounts with STORY_TRIGGER", len(triggered))

        # Обрабатываем по очереди (с ограничением параллелизма)
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        tasks = []
        for account in triggered:
            task = asyncio.create_task(
                self._process_one(account, sem)
            )
            tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_one(
        self,
        account: dict[str, Any],
        sem: asyncio.Semaphore,
    ) -> None:
        """Обрабатывает один STORY_TRIGGER."""
        async with sem:
            account_id = account["id"]
            username = account["username"]

            try:
                # Извлекаем views из notes: "STORY_TRIGGER:12345"
                notes = account.get("notes", "")
                views_str = notes.replace("STORY_TRIGGER:", "").strip()
                views = int(views_str) if views_str.isdigit() else 0

                logger.info(
                    "[%s] Processing story trigger (views=%d)",
                    username, views,
                )

                # Проверяем cooldown (24 часа)
                if not await self.controller.auto_trigger.should_post_story(account_id, views):
                    logger.info("[%s] Story cooldown active, skipping", username)
                    # Сбрасываем флаг — повторим при следующем чеке
                    await self._clear_trigger(account_id)
                    return

                # Jitter перед постингом (антидетект: 10-30 мин)
                jitter = random.uniform(JITTER_MIN, JITTER_MAX)
                logger.info("[%s] Story jitter: %.0f min", username, jitter / 60)
                await asyncio.sleep(jitter)

                # Проверяем не остановлены ли мы за время ожидания
                if self._stop_event.is_set():
                    return

                # Получаем прокси для аккаунта
                proxy = await self._get_account_proxy(account)

                # Открываем браузерный профиль
                if not self.pm.profile_exists(username):
                    logger.warning("[%s] No browser profile, skipping story", username)
                    await self._clear_trigger(account_id)
                    return

                context, page = await self.pm.launch_profile(
                    profile_id=username,
                    proxy=proxy,
                    headless=True,  # сторис через API, UI не нужен
                )

                try:
                    # Навигация на IG (для cookies)
                    await page.goto(
                        "https://www.instagram.com/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    await asyncio.sleep(random.uniform(2.5, 4.5))

                    # Постим сторис
                    result = await self.controller.post_story_auto(
                        account_id=account_id,
                        context=context,
                        page=page,
                        niche_id=account.get("niche_id"),
                        proxy=proxy,
                    )

                    if result.get("success"):
                        logger.info(
                            "[%s] Story posted successfully (media_id=%s)",
                            username, result.get("media_id"),
                        )
                    else:
                        logger.warning(
                            "[%s] Story posting failed: %s",
                            username, result.get("error"),
                        )

                finally:
                    await self.pm.close_profile(username)

                # Сбрасываем флаг
                await self._clear_trigger(account_id)

            except Exception as e:
                logger.exception("[%s] Story trigger processing failed: %s", username, e)
                # Сбрасываем флаг чтобы не зацикливаться
                await self._clear_trigger(account_id)

    async def _clear_trigger(self, account_id: int) -> None:
        """Сбрасывает STORY_TRIGGER из notes."""
        await run_sync(
            execute,
            """UPDATE accounts
               SET notes = NULL, updated_at = unixepoch('now')
               WHERE id = ? AND notes LIKE 'STORY_TRIGGER:%'""",
            (account_id,),
        )

    async def _get_account_proxy(self, account: dict) -> dict[str, str] | None:
        """Получает прокси для аккаунта (статический или мобильный)."""
        proxy_id = account.get("static_proxy_id")
        if proxy_id:
            row = await run_sync(
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

        pool_id = account.get("mobile_pool_id")
        if pool_id:
            pool = await run_sync(
                query_one,
                "SELECT proxy_host, proxy_port, proxy_username, proxy_password FROM proxy_pools WHERE id = ?",
                (pool_id,),
            )
            if pool and pool["proxy_host"]:
                return {
                    "server": f"{pool['proxy_host']}:{pool['proxy_port']}",
                    "username": pool["proxy_username"] or "",
                    "password": pool["proxy_password"] or "",
                }

        return None
