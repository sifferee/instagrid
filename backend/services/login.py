"""
InstaGrid — Логин-модуль.

Логин в Instagram через Camoufox + HumanInteractor:
- Ввод логина/пароля с burst-typing, тайпо
- 2FA через pyotp (TOTP из сохранённого секрета)
- Прокликивание попапов: «Save login info», «Turn on notifications», ADS consent
- После успешного логина: настройки → отключение приватного профиля
- Ошибка логина → статический прокси удаляется, берётся следующий из пула
- 2-3 неудачи подряд → аккаунт невалидный
- Параллельный пул воркеров, атомарный захват задач
- Hard timeout: 15 мин на весь цикл аккаунта

Перезагрузка при лаге прокси:
- 30 сек нет ответа → клик адресная строка → Enter
- Прокси мёртв → берёт другой
- 3 провала → cooldown, следующий
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

import pyotp
from playwright.async_api import BrowserContext, Page, TimeoutError as PwTimeout

from backend.services.human import HumanInteractor
from backend.services.profile_manager import ProfileManager
from backend.services.dialog_gate import (
    inspect_dialog, continue_after_dialog, dismiss_known_dialog,
    verify_authenticated, set_account_public,
    NO_BLOCKER, HANDLED_REEVALUATE, TERMINAL_MANUAL,
)

logger = logging.getLogger("instagrid.login")


# ─── Константы ────────────────────────────────────────────────────────────────

INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"
INSTAGRAM_SETTINGS_URL = "https://www.instagram.com/accounts/edit/"
INSTAGRAM_PRIVACY_URL = "https://www.instagram.com/accounts/privacy_and_security/"

# Таймауты
PAGE_LOAD_TIMEOUT = 30_000          # мс — ожидание загрузки страницы
ELEMENT_WAIT_TIMEOUT = 30_000       # мс — ожидание элемента (через прокси дольше)
HARD_TIMEOUT = 15 * 60             # сек — 15 мин на весь цикл аккаунта
PROXY_LAG_TIMEOUT = 30              # сек — нет ответа = лаг прокси
PROXY_LAG_RETRY_PAUSE = 150        # сек — пауза если прокси жив, но IG не грузит

# Retry
MAX_LOGIN_ATTEMPTS = 3              # неудач подряд → аккаунт невалидный
MAX_PROXY_RETRIES = 3               # попытки с разными прокси
MAX_PAGE_RELOAD_RETRIES = 3         # попытки перезагрузки при лаге

# Статусы аккаунтов — должны совпадать с CHECK constraint в database.py
# ('new', 'logged_in', 'cooldown', 'dead')
class AccountStatus(str, Enum):
    NEW = "new"
    LOGGED_IN = "logged_in"
    COOLDOWN = "cooldown"          # challenge, временные ошибки
    DEAD = "dead"                  # невалидные credentials, бан


# ─── Селекторы Instagram ──────────────────────────────────────────────────────

class Selectors:
    """CSS-селекторы элементов Instagram (web, desktop view)."""

    # Логин-форма (Instagram меняет name: username→email, password→pass)
    USERNAME_INPUT = 'input[name="username"], input[name="email"], input[type="text"][autocomplete="username"]'
    PASSWORD_INPUT = 'input[name="password"], input[name="pass"], input[type="password"]'
    LOGIN_BUTTON = '[type="submit"]'

    # 2FA
    SECURITY_CODE_INPUT = 'input[name="verificationCode"], input[name="approvals_code"], input[name="code"], input[aria-label="Code"], input[placeholder="Code"]'
    CONFIRM_2FA_BUTTON = '//button[contains(text(), "Continue") or contains(text(), "Confirm") or contains(text(), "Submit")]'

    # Попапы после логина
    SAVE_INFO_NOT_NOW = '//button[contains(text(), "Not Now") or contains(text(), "Not now")]'
    SAVE_INFO_BUTTON = '//button[contains(text(), "Save Info") or contains(text(), "Save info")]'
    NOTIFICATIONS_NOT_NOW = '//button[contains(text(), "Not Now") or contains(text(), "Not now")]'
    TURN_ON_NOTIFICATIONS = '//button[contains(text(), "Turn On") or contains(text(), "Turn on")]'

    # ADS consent (cookie/GDPR popup)
    ADS_ACCEPT_ALL = '//button[contains(text(), "Allow all cookies") or contains(text(), "Allow essential and optional cookies")]'
    ADS_ESSENTIAL_ONLY = '//button[contains(text(), "Decline optional cookies") or contains(text(), "Allow essential cookies only")]'

    # Ошибки логина
    ERROR_MESSAGE = '#slfErrorAlert, [data-testid="login-error-message"], [role="alert"]'
    SUSPICIOUS_LOGIN = 'text="Suspicious Login Attempt"'
    CHALLENGE_REQUIRED = 'text="challenge_required"'

    # Подтверждение залогинен
    HOME_FEED = 'svg[aria-label="Home"], a[href="/"]'
    PROFILE_ICON = 'svg[aria-label="Profile"], img[data-testid="user-avatar"]'
    NAV_BAR = 'nav[role="navigation"]'

    # Настройки приватности
    # Приватность
    PRIVATE_ACCOUNT_TOGGLE = 'input[type="checkbox"], [role="switch"], [role="checkbox"]'
    PRIVATE_ACCOUNT_LABEL = '//span[contains(text(), "Private account") or contains(text(), "Private Account")] | //label[contains(text(), "Private account")]'


# ─── Результат логина ─────────────────────────────────────────────────────────

@dataclass
class LoginResult:
    """Результат попытки логина."""
    success: bool
    account_id: int | None = None
    username: str = ""
    status: AccountStatus = AccountStatus.NEW
    message: str = ""
    proxy_used: str = ""
    attempts: int = 0
    duration_sec: float = 0.0


# ─── Основной класс логина ────────────────────────────────────────────────────

class InstagramLogin:
    """
    Выполняет логин одного аккаунта в Instagram.

    Жизненный цикл:
    1. launch_profile() → Camoufox с прокси
    2. navigate_to_login() → открываем IG
    3. handle_cookies_popup() → убираем GDPR
    4. enter_credentials() → логин/пароль с HumanInteractor
    5. handle_2fa() → если требуется
    6. handle_post_login_popups() → «Save info», «Notifications»
    7. verify_logged_in() → проверяем что залогинены
    8. disable_private_profile() → настройки → снять галку
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        db: Any = None,  # DatabaseSession — передаём для обновления статусов
    ) -> None:
        self.pm = profile_manager
        self.db = db

    async def login_account(
        self,
        account: dict[str, Any],
        proxy: dict[str, str] | None = None,
    ) -> LoginResult:
        """
        Полный цикл логина одного аккаунта.

        Args:
            account: {"id": int, "username": str, "password": str, "totp_secret": str, ...}
            proxy: {"server": "host:port", "username": "...", "password": "..."} или None

        Returns:
            LoginResult
        """
        username = account["username"]
        start_time = time.time()
        result = LoginResult(
            success=False,
            account_id=account.get("id"),
            username=username,
            proxy_used=proxy["server"] if proxy else "none",
        )

        context: BrowserContext | None = None
        page: Page | None = None

        try:
            # Hard timeout на весь цикл
            async with asyncio.timeout(HARD_TIMEOUT):
                # 1. Запуск профиля
                logger.info("[%s] Launching profile with proxy %s", username, result.proxy_used)

                if not self.pm.profile_exists(username):
                    self.pm.create_profile(username)

                context, page = await self.pm.launch_profile(
                    profile_id=username,
                    proxy=proxy,
                    headless=False,
                )

                human = HumanInteractor(page, username)
                logger.info("[%s] Profile launched, behavior=%s", username, human.profile.value)

                # Автосборщик селекторов
                from backend.services.selector_collector import SelectorCollector
                collector = SelectorCollector(page, username)
                await collector.start()

                # 2. Навигация на страницу логина
                await self._navigate_to_login(page, human)

                # 3. Cookie/GDPR popup
                await self._handle_cookies_popup(page, human)

                # 4. Ввод credentials
                await self._enter_credentials(page, human, account)

                # 5. Ожидание результата + 2FA
                login_ok = await self._wait_for_login_result(page, human, account)

                if not login_ok:
                    result.status = AccountStatus.DEAD
                    result.message = "Login failed — invalid credentials or challenge"
                    result.attempts = 1
                    return result

                # 6. Попапы после логина
                await self._handle_post_login_popups(page, human)

                # 7. Проверка что залогинены
                verified = await self._verify_logged_in(page)
                if not verified:
                    result.status = AccountStatus.COOLDOWN
                    result.message = "Login seemed OK but couldn't verify feed"
                    return result

                # 8. Отключение приватного профиля
                await self._disable_private_profile(page, human)

                result.success = True
                result.status = AccountStatus.LOGGED_IN
                result.message = "Login successful"
                logger.info("[%s] Login successful", username)

        except TimeoutError:
            result.status = AccountStatus.COOLDOWN
            result.message = f"Hard timeout ({HARD_TIMEOUT}s) exceeded"
            logger.error("[%s] Hard timeout exceeded", username)

        except Exception as e:
            result.status = AccountStatus.COOLDOWN
            result.message = f"Unexpected error: {e}"
            logger.exception("[%s] Login error", username)

        finally:
            result.duration_sec = time.time() - start_time
            result.attempts = 1

            # Всегда закрываем браузер — профиль (куки) уже сохранён на диске
            if context:
                try:
                    await self.pm.close_profile(username)
                    logger.info("[%s] Browser closed", username)
                except Exception as e:
                    logger.debug("[%s] Browser close error: %s", username, e)

        return result

    # ── Навигация ─────────────────────────────────────────────────────────

    async def _navigate_to_login(self, page: Page, human: HumanInteractor) -> None:
        """Открывает страницу логина с обработкой лага прокси."""
        for attempt in range(1, MAX_PAGE_RELOAD_RETRIES + 1):
            try:
                await page.goto(
                    INSTAGRAM_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT,
                )
                # Ждём поле username
                await page.wait_for_selector(
                    Selectors.USERNAME_INPUT,
                    timeout=ELEMENT_WAIT_TIMEOUT,
                )
                await human.random_pause(1.0, 2.5)
                return

            except PwTimeout:
                logger.warning(
                    "[%s] Page load timeout attempt %d/%d",
                    human.username, attempt, MAX_PAGE_RELOAD_RETRIES,
                )
                if attempt < MAX_PAGE_RELOAD_RETRIES:
                    # 30 сек нет ответа → кликаем адресную строку → Enter
                    await self._reload_via_address_bar(page, human)
                    await asyncio.sleep(2.0)
                else:
                    raise RuntimeError("Instagram login page not loading after retries")

    async def _reload_via_address_bar(self, page: Page, human: HumanInteractor) -> None:
        """
        Перезагрузка страницы.

        Раньше здесь был page.keyboard.press("Control+l") + Enter — но
        page.keyboard шлёт события В СТРАНИЦУ, а не в хром браузера.
        Адресная строка не фокусировалась, зато Instagram получал
        посторонний Ctrl+L keydown на своей странице. Чистый минус.
        """
        await human.random_pause(0.8, 2.0)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        except Exception as e:
            logger.debug("[%s] Reload failed: %s", human.username, e)
        await human.random_pause(1.5, 3.5)

    # ── Cookie/GDPR попап ─────────────────────────────────────────────────

    async def _handle_cookies_popup(self, page: Page, human: HumanInteractor) -> None:
        """Обрабатывает GDPR/cookie-попап через dialog_gate (JS-based, надёжнее CSS)."""
        dialog = await inspect_dialog(page)
        if dialog["present"] and dialog["category"] == "cookie_consent":
            await human.random_pause(0.5, 1.5)
            result = await continue_after_dialog(
                page, wait_seconds=5.0, cookie_action="decline_optional_cookies"
            )
            if result.get("dismissed"):
                logger.info("[%s] Dismissed cookie popup via dialog_gate", human.username)
            else:
                logger.warning("[%s] Cookie popup not dismissed: %s", human.username, result.get("outcome"))
            await human.random_pause(1.0, 2.0)
        else:
            # Fallback на CSS-селекторы если dialog_gate не нашёл
            try:
                btn = await page.wait_for_selector(
                    Selectors.ADS_ESSENTIAL_ONLY, timeout=3000,
                )
                if btn:
                    await human.random_pause(0.5, 1.5)
                    await human.click_element(btn)
                    logger.info("[%s] Dismissed cookie popup (CSS fallback)", human.username)
                    await human.random_pause(1.0, 2.0)
            except PwTimeout:
                logger.debug("[%s] No cookie popup found", human.username)

    # ── Ввод credentials ──────────────────────────────────────────────────

    async def _enter_credentials(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> None:
        """Вводит логин и пароль с имитацией человека."""
        username = account["username"]
        password = account["password"]

        # Клик по полю username → ввод
        logger.info("[%s] Entering username", username)
        await human.clear_and_type(Selectors.USERNAME_INPUT, username)

        # Пауза "переводит взгляд" на поле пароля
        await human.random_pause(0.4, 1.2)

        # Tab или клик по полю password → ввод
        logger.info("[%s] Entering password", username)
        await human.clear_and_type(Selectors.PASSWORD_INPUT, password)

        # Пауза перед нажатием Login
        await human.random_pause(0.6, 1.8)

        # Нажимаем Enter — самый надёжный способ отправить форму (как человек)
        await page.keyboard.press("Enter")
        logger.info("[%s] Pressed Enter to submit login", username)

    # ── Ожидание результата логина + 2FA ──────────────────────────────────

    async def _wait_for_login_result(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> bool:
        """
        Ждёт результата после нажатия Login.
        Стратегия: ждём пока URL изменится с login page, потом определяем что произошло.
        """
        login_url = page.url

        # Ждём пока URL изменится (redirect после логина)
        for _ in range(30):  # макс 30 сек
            await asyncio.sleep(random.uniform(0.8, 1.4))
            current_url = page.url
            if current_url != login_url:
                break

        await human.random_pause(1.0, 2.0)
        current_url = page.url
        logger.info("[%s] Post-login URL: %s", human.username, current_url[:100])

        # 2FA
        if "two_step_verification" in current_url or "two_factor" in current_url:
            logger.info("[%s] 2FA page detected", human.username)
            return await self._handle_2fa(page, human, account)

        # Challenge
        if "challenge" in current_url:
            logger.warning("[%s] Challenge required", human.username)
            return False

        # Suspicious
        if "suspicious" in current_url.lower():
            logger.warning("[%s] Suspicious login", human.username)
            return False

        # Уже на главной (успех)
        if "/accounts/" not in current_url and "login" not in current_url:
            logger.info("[%s] Appears logged in (URL: %s)", human.username, current_url[:80])
            return True

        # Всё ещё на login — проверяем ошибки на странице
        error = await page.query_selector(Selectors.ERROR_MESSAGE)
        if error:
            error_text = await error.inner_text()
            logger.warning("[%s] Login error: %s", human.username, error_text.strip())
            return False

        # Проверяем 2FA по селектору (fallback)
        twofa_input = await page.query_selector(Selectors.SECURITY_CODE_INPUT)
        if twofa_input:
            return await self._handle_2fa(page, human, account)

        logger.warning("[%s] No result detected (URL: %s)", human.username, current_url[:100])
        return False

    async def _handle_2fa(
        self,
        page: Page,
        human: HumanInteractor,
        account: dict[str, Any],
    ) -> bool:
        """Обрабатывает 2FA: генерирует TOTP-код и вводит."""
        totp_secret = account.get("totp_secret", "")
        if not totp_secret:
            logger.error("[%s] 2FA required but no TOTP secret saved", human.username)
            return False

        # Ждём загрузку 2FA страницы
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await human.random_pause(2.0, 4.0)

        # Генерируем TOTP-код
        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        logger.info("[%s] 2FA required, entering TOTP code: %s", human.username, code)

        # Пауза — "достаёт телефон, смотрит код"
        await human.random_pause(2.0, 5.0)

        # На 2FA странице поле: input[type="text"] без name/placeholder
        # Используем простой селектор — на этой странице только один text input
        twofa_selector = 'input[type="text"][autocomplete="off"]'
        try:
            await page.wait_for_selector(twofa_selector, timeout=15_000)
            await human.clear_and_type(twofa_selector, code)
        except Exception as e:
            logger.warning("[%s] 2FA input not found: %s, trying fallback", human.username, e)
            # Fallback: первый видимый text input
            try:
                await human.clear_and_type('input[type="text"]', code)
            except Exception as e2:
                logger.error("[%s] 2FA input fallback failed: %s", human.username, e2)
                return False

        await human.random_pause(0.5, 1.2)

        # Нажимаем Enter (кнопка Continue тоже скрытая, как и Log In)
        await page.keyboard.press("Enter")
        logger.info("[%s] Pressed Enter to confirm 2FA", human.username)

        await human.random_pause(3.0, 6.0)

        # Ждём redirect после 2FA
        for _ in range(20):
            await asyncio.sleep(random.uniform(0.8, 1.4))
            current_url = page.url
            # Успех — ушли со страницы 2FA
            if "two_step" not in current_url and "two_factor" not in current_url and "challenge" not in current_url:
                logger.info("[%s] 2FA passed, URL: %s", human.username, current_url[:80])
                return True
            # Challenge после 2FA
            if "challenge" in current_url:
                logger.warning("[%s] Challenge after 2FA", human.username)
                return False

        # Проверяем ошибки на странице
        error = await page.query_selector(Selectors.ERROR_MESSAGE)
        if error:
            logger.warning("[%s] 2FA code rejected", human.username)
            return False

        logger.warning("[%s] Still on 2FA page after 20s", human.username)
        return False

    # ── Попапы после логина ───────────────────────────────────────────────

    async def _handle_post_login_popups(self, page: Page, human: HumanInteractor) -> None:
        """
        Обрабатывает все попапы после логина через dialog_gate.
        JS-based стейт-машина: классифицирует диалог, кликает семантически,
        проверяет исчез ли, не кликает один и тот же дважды.
        """
        max_rounds = 5  # максимум попапов подряд
        for round_num in range(max_rounds):
            await human.random_pause(0.5, 1.5)

            result = await continue_after_dialog(
                page, allow_safe_close=True, wait_seconds=4.0,
            )
            outcome = result.get("outcome", "")

            if outcome == NO_BLOCKER:
                logger.debug("[%s] No blocking dialog (round %d)", human.username, round_num + 1)
                break

            if outcome == HANDLED_REEVALUATE:
                logger.info(
                    "[%s] Dismissed dialog: %s (round %d)",
                    human.username, result.get("clicked_category", "?"), round_num + 1,
                )
                continue  # проверяем следующий попап

            if outcome == TERMINAL_MANUAL:
                state = result.get("state", "")
                logger.warning(
                    "[%s] Terminal dialog state: %s (round %d)",
                    human.username, state, round_num + 1,
                )
                break

            # TRANSITIONING_RETRY или UNKNOWN_BLOCKER
            logger.debug("[%s] Dialog outcome %s (round %d)", human.username, outcome, round_num + 1)
            break

        await human.random_pause(0.5, 1.0)

    # ── Проверка залогиненности ───────────────────────────────────────────

    async def _verify_logged_in(self, page: Page) -> bool:
        """
        API-based проверка аутентификации (порт из SparkGrid).

        Множественная корроборация:
        1. API endpoint /api/v1/accounts/current_user/ — 100% надёжен
        2. UI: auth_nav + account_menu + app_shell (≥2 из 3)
        3. Session cookies: sessionid + csrftoken + ds_user_id
        4. Отсутствие login form

        Confirmed = API OK || (≥2 UI + cookie && no login form)
        """
        # Ждём загрузку страницы
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        auth = await verify_authenticated(page)
        if auth["confirmed"]:
            logger.info(
                "[verify] Auth confirmed: %s (evidence: %s)",
                auth["reason"], auth["evidence"],
            )
            return True

        # Retry через 3 секунды (Instagram SPA может ещё грузиться)
        await asyncio.sleep(3.0)
        auth = await verify_authenticated(page)
        if auth["confirmed"]:
            logger.info("[verify] Auth confirmed on retry: %s", auth["reason"])
            return True

        logger.warning(
            "[verify] Auth NOT confirmed: %s (evidence: %s)",
            auth["reason"], auth["evidence"],
        )
        return False

    # ── Отключение приватного профиля ─────────────────────────────────────

    async def _disable_private_profile(self, page: Page, human: HumanInteractor) -> None:
        """
        Отключает приватный профиль через Instagram API (не UI-селекторы).
        Порт из SparkGrid account_privacy.py.

        API-based подход не ломается при смене дизайна.
        """
        try:
            logger.info("[%s] Checking privacy via API", human.username)
            result = await set_account_public(page)
            if result:
                logger.info("[%s] Profile confirmed public via API", human.username)
            else:
                logger.warning("[%s] API privacy toggle failed, trying UI fallback", human.username)
                await self._disable_private_profile_ui_fallback(page, human)
        except Exception as e:
            logger.warning("[%s] Privacy check error: %s, trying UI fallback", human.username, e)
            await self._disable_private_profile_ui_fallback(page, human)

    async def _disable_private_profile_ui_fallback(
        self, page: Page, human: HumanInteractor,
    ) -> None:
        """UI fallback для отключения приватности (если API не работает)."""
        try:
            await page.goto(
                INSTAGRAM_PRIVACY_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT,
            )
            await human.random_pause(1.5, 3.0)

            label = await page.query_selector(Selectors.PRIVATE_ACCOUNT_LABEL)
            if not label:
                await page.goto(
                    "https://www.instagram.com/accounts/who_can_see_your_content/",
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT,
                )
                await human.random_pause(1.5, 3.0)
                label = await page.query_selector(Selectors.PRIVATE_ACCOUNT_LABEL)

            if not label:
                logger.warning("[%s] Private account toggle not found (UI fallback)", human.username)
                return

            toggle = await page.query_selector(Selectors.PRIVATE_ACCOUNT_TOGGLE)
            if toggle:
                is_checked = await toggle.is_checked()
                if is_checked:
                    await human.click_element(toggle)
                    await human.random_pause(1.0, 2.0)
                    try:
                        confirm = await page.wait_for_selector(
                            '//button[contains(text(), "Switch to Public") or contains(text(), "Turn Off")]',
                            timeout=5000,
                        )
                        if confirm:
                            await human.click_element(confirm)
                            logger.info("[%s] Disabled private profile (UI fallback)", human.username)
                    except PwTimeout:
                        logger.info("[%s] Private toggle clicked (UI fallback)", human.username)
                else:
                    logger.info("[%s] Profile already public (UI fallback)", human.username)
        except Exception as e:
            logger.warning("[%s] UI privacy fallback failed: %s", human.username, e)

        # Возвращаемся на главную
        try:
            await page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=PAGE_LOAD_TIMEOUT,
            )
        except Exception:
            pass
        await human.random_pause(1.0, 2.0)


# ─── Оркестратор с ротацией прокси ────────────────────────────────────────────

class LoginOrchestrator:
    """
    Логин одного аккаунта с ротацией прокси при ошибках.

    Логика:
    - Попытка 1 с текущим прокси
    - Неудача → удаляем прокси из пула, берём следующий, повторяем
    - 2-3 неудачи подряд → аккаунт невалидный (пароль неверный)
    - Challenge → cooldown 24ч
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        get_next_proxy: Callable[[int], Coroutine[Any, Any, dict[str, str] | None]],
        delete_proxy: Callable[[int], Coroutine[Any, Any, None]],
        update_account_status: Callable[[int, str, str | None], Coroutine[Any, Any, None]],
        bind_proxy_to_account: Callable[[int, int], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Args:
            profile_manager: ProfileManager instance
            get_next_proxy: async fn(pool_id) → {"id": int, "server": ..., ...} или None
            delete_proxy: async fn(proxy_id) → удаляет прокси навсегда
            update_account_status: async fn(account_id, status, message) → обновляет статус
            bind_proxy_to_account: async fn(account_id, proxy_id) → привязывает прокси
        """
        self.pm = profile_manager
        self._get_next_proxy = get_next_proxy
        self._delete_proxy = delete_proxy
        self._update_status = update_account_status
        self._bind_proxy = bind_proxy_to_account
        self._login = InstagramLogin(profile_manager)

    async def login_with_rotation(
        self,
        account: dict[str, Any],
        proxy_pool_id: int,
    ) -> LoginResult:
        """
        Логин с ротацией прокси при неудачах.

        - Успех → прокси привязывается к аккаунту
        - Неудача → прокси удаляется, берётся следующий
        - MAX_LOGIN_ATTEMPTS неудач → аккаунт невалидный
        """
        last_result = LoginResult(success=False, username=account["username"])
        consecutive_failures = 0

        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            # Получаем прокси
            proxy_data = await self._get_next_proxy(proxy_pool_id)
            if not proxy_data:
                last_result.message = "No available proxies in pool"
                last_result.status = AccountStatus.COOLDOWN
                await self._update_status(
                    account["id"],
                    AccountStatus.COOLDOWN.value,
                    "No proxies available",
                )
                return last_result

            proxy_id = proxy_data.pop("id")
            proxy_for_launch = {
                "server": proxy_data["server"],
                "username": proxy_data.get("username", ""),
                "password": proxy_data.get("password", ""),
            }

            logger.info(
                "[%s] Login attempt %d/%d with proxy %s",
                account["username"], attempt, MAX_LOGIN_ATTEMPTS,
                proxy_for_launch["server"],
            )

            # Попытка логина
            result = await self._login.login_account(account, proxy_for_launch)
            result.attempts = attempt

            if result.success:
                # Привязываем прокси к аккаунту
                await self._bind_proxy(account["id"], proxy_id)
                await self._update_status(
                    account["id"],
                    AccountStatus.LOGGED_IN.value,
                    None,
                )
                logger.info(
                    "[%s] Login OK on attempt %d, proxy %s bound",
                    account["username"], attempt, proxy_for_launch["server"],
                )
                return result

            # Неудача
            consecutive_failures += 1
            last_result = result

            if "challenge" in result.message.lower():
                # Challenge → cooldown 24ч, прокси НЕ удаляем (не его вина)
                await self._update_status(
                    account["id"],
                    AccountStatus.COOLDOWN.value,
                    "Challenge required — cooldown 24h",
                )
                logger.warning("[%s] Challenge → cooldown", account["username"])
                return result

            # Удаляем прокси ТОЛЬКО если Instagram реально ответил (неверный пароль).
            # НЕ удаляем при внутренних ошибках (crash, timeout, наш баг).
            proxy_kill_reasons = [
                "incorrect password", "wrong password", "invalid credentials",
                "the password you entered is incorrect",
                "doesn't belong to an account", "user not found",
            ]
            is_proxy_fault = any(r in result.message.lower() for r in proxy_kill_reasons)

            if is_proxy_fault:
                await self._delete_proxy(proxy_id)
                logger.info(
                    "[%s] Proxy %s DELETED — Instagram rejected: %s",
                    account["username"], proxy_for_launch["server"], result.message,
                )
            else:
                # Внутренняя ошибка — возвращаем прокси в пул
                from backend.database import execute, run_sync
                await run_sync(
                    execute,
                    "UPDATE static_proxies SET status = 'available', account_id = NULL WHERE id = ?",
                    (proxy_id,),
                )
                logger.warning(
                    "[%s] Login failed, proxy %s returned to pool (internal error): %s",
                    account["username"], proxy_for_launch["server"], result.message,
                )

            # Закрываем профиль перед следующей попыткой
            await self.pm.close_profile(account["username"])

            # Пауза между попытками
            if attempt < MAX_LOGIN_ATTEMPTS:
                pause = 5.0 + attempt * 3.0
                await asyncio.sleep(pause)

        # Все попытки исчерпаны → аккаунт невалидный (dead)
        await self._update_status(
            account["id"],
            AccountStatus.DEAD.value,
            f"Failed {MAX_LOGIN_ATTEMPTS} attempts — likely invalid credentials",
        )
        logger.error(
            "[%s] All %d login attempts failed — marked dead",
            account["username"], MAX_LOGIN_ATTEMPTS,
        )
        last_result.status = AccountStatus.DEAD
        return last_result


# ─── Пул воркеров ─────────────────────────────────────────────────────────────

class LoginWorkerPool:
    """
    Параллельный пул воркеров для логина.

    - N воркеров (по умолчанию 5)
    - Атомарный захват задач: каждый воркер берёт следующий аккаунт из очереди
    - По порядку от первого до последнего
    - Пропуск уже отработанных
    - Перезапуск Camoufox каждые 20-30 аккаунтов (утечки памяти)
    """

    def __init__(
        self,
        orchestrator: LoginOrchestrator,
        max_workers: int = 5,
        restart_every: int = 25,  # перезапуск каждые N аккаунтов
    ) -> None:
        self.orchestrator = orchestrator
        self.max_workers = max_workers
        self.restart_every = restart_every

        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._results: list[LoginResult] = []
        self._results_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._active_workers: int = 0
        self._processed_count: int = 0
        self._total_count: int = 0

    async def run(
        self,
        accounts: list[dict[str, Any]],
        proxy_pool_id: int,
        on_progress: Callable[[LoginResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[LoginResult]:
        """
        Запускает логин для списка аккаунтов.

        Args:
            accounts: список аккаунтов [{id, username, password, totp_secret, ...}]
            proxy_pool_id: ID пула прокси
            on_progress: callback при каждом завершённом аккаунте

        Returns:
            Список LoginResult для каждого аккаунта
        """
        self._results = []
        self._stop_event.clear()
        self._processed_count = 0
        self._total_count = len(accounts)

        # Заполняем очередь
        for acc in accounts:
            await self._queue.put(acc)

        # Запускаем воркеров
        workers = [
            asyncio.create_task(
                self._worker(i, proxy_pool_id, on_progress)
            )
            for i in range(min(self.max_workers, len(accounts)))
        ]

        # Ждём завершения всех
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(
            "Login pool finished: %d/%d successful",
            sum(1 for r in self._results if r.success),
            self._total_count,
        )
        return self._results

    async def stop(self) -> None:
        """Останавливает пул (текущие задачи дорабатывают)."""
        self._stop_event.set()

    async def _worker(
        self,
        worker_id: int,
        proxy_pool_id: int,
        on_progress: Callable[[LoginResult], Coroutine[Any, Any, None]] | None,
    ) -> None:
        """Один воркер: берёт аккаунты из очереди и логинит."""
        processed_in_session = 0

        while not self._stop_event.is_set():
            try:
                account = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            logger.info(
                "[Worker %d] Processing %s (%d/%d)",
                worker_id, account["username"],
                self._processed_count + 1, self._total_count,
            )

            try:
                result = await self.orchestrator.login_with_rotation(
                    account, proxy_pool_id,
                )
            except Exception as e:
                result = LoginResult(
                    success=False,
                    account_id=account.get("id"),
                    username=account["username"],
                    status=AccountStatus.COOLDOWN,
                    message=f"Worker error: {e}",
                )
                logger.exception("[Worker %d] Error processing %s", worker_id, account["username"])

            async with self._results_lock:
                self._results.append(result)
                self._processed_count += 1

            if on_progress:
                try:
                    await on_progress(result)
                except Exception:
                    pass

            processed_in_session += 1

            # Перезапуск каждые N аккаунтов (утечки памяти Playwright/Camoufox)
            if processed_in_session >= self.restart_every:
                logger.info("[Worker %d] Restarting after %d accounts", worker_id, processed_in_session)
                processed_in_session = 0

            self._queue.task_done()

        logger.info("[Worker %d] Finished", worker_id)

    @property
    def progress(self) -> tuple[int, int]:
        """Возвращает (processed, total)."""
        return self._processed_count, self._total_count


# ─── DB-хелперы для LoginOrchestrator ─────────────────────────────────────────

from backend.database import execute, execute_atomic, query_one, query, run_sync


async def db_get_next_proxy(pool_id: int) -> dict[str, str] | None:
    """
    Атомарно берёт следующий свободный статический прокси из пула.
    Fix #2: BEGIN IMMEDIATE + UPDATE...RETURNING в одном запросе.
    Поддержка HTTP/HTTPS/SOCKS5 через поле protocol.
    """
    row = await run_sync(
        execute_atomic,
        """UPDATE static_proxies
           SET status = 'bound', used_at = unixepoch('now')
           WHERE id = (
               SELECT id FROM static_proxies
               WHERE pool_id = ? AND status = 'available' AND account_id IS NULL
               ORDER BY id ASC LIMIT 1
           )
           RETURNING id, host, port, username, password, protocol""",
        (pool_id,),
    )
    if not row:
        return None

    protocol = row.get("protocol", "http") or "http"
    server = f"{protocol}://{row['host']}:{row['port']}"

    return {
        "id": row["id"],
        "server": server,
        "username": row["username"] or "",
        "password": row["password"] or "",
    }


async def db_destroy_static_proxy(proxy_id: int) -> None:
    """Fix #16: настоящий DELETE статического прокси. Сгорел — удаляем навсегда."""
    await run_sync(
        execute,
        "DELETE FROM static_proxies WHERE id = ?",
        (proxy_id,),
    )


async def db_update_account_status(
    account_id: int, status: str, message: str | None = None,
) -> None:
    """Обновляет статус аккаунта + notes + timestamps."""
    now_field = "last_login_at" if status == "logged_in" else "updated_at"
    cooldown_sql = ""

    if status == "cooldown":
        cooldown_sql = ", cooldown_until = unixepoch('now') + 86400"

    if message:
        await run_sync(
            execute,
            f"""UPDATE accounts
                SET status = ?, notes = ?, {now_field} = unixepoch('now'),
                    updated_at = unixepoch('now'){cooldown_sql}
                WHERE id = ?""",
            (status, message, account_id),
        )
    else:
        await run_sync(
            execute,
            f"""UPDATE accounts
                SET status = ?, {now_field} = unixepoch('now'),
                    updated_at = unixepoch('now'){cooldown_sql}
                WHERE id = ?""",
            (status, account_id),
        )


async def db_bind_proxy_to_account(account_id: int, proxy_id: int) -> None:
    """Привязывает прокси к аккаунту после успешного логина."""
    await run_sync(
        execute,
        "UPDATE static_proxies SET account_id = ?, status = 'bound' WHERE id = ?",
        (account_id, proxy_id),
    )
    # Снимаем этот прокси с других аккаунтов (UNIQUE constraint)
    await run_sync(
        execute,
        "UPDATE accounts SET static_proxy_id = NULL WHERE static_proxy_id = ? AND id != ?",
        (proxy_id, account_id),
    )
    await run_sync(
        execute,
        "UPDATE accounts SET static_proxy_id = ? WHERE id = ?",
        (proxy_id, account_id),
    )


async def db_get_accounts_for_login(niche_id: int | None = None) -> list[dict]:
    """Возвращает список аккаунтов со статусом 'new' для логина."""
    if niche_id:
        return await run_sync(
            query,
            """SELECT id, username, password, totp_secret
               FROM accounts WHERE status = 'new' AND niche_id = ?
               ORDER BY id ASC""",
            (niche_id,),
        )
    return await run_sync(
        query,
        """SELECT id, username, password, totp_secret
           FROM accounts WHERE status = 'new'
           ORDER BY id ASC""",
        (),
    )


def create_orchestrator(profile_manager: ProfileManager) -> LoginOrchestrator:
    """Фабрика: создаёт LoginOrchestrator с DB-хелперами."""
    return LoginOrchestrator(
        profile_manager=profile_manager,
        get_next_proxy=db_get_next_proxy,
        delete_proxy=db_destroy_static_proxy,
        update_account_status=db_update_account_status,
        bind_proxy_to_account=db_bind_proxy_to_account,
    )
