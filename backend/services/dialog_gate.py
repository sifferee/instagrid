"""
InstaGrid — Dialog Gate: JS-based классификация и обработка попапов Instagram.

Порт из SparkGrid instagram_dialog_gate.py:
- JS-код внутри page.evaluate() находит видимый диалог с наивысшим z-index
- Классифицирует по тексту: cookie_consent, save_login, notification,
  policy_notice, checkpoint, restriction, suspended, operation_*
- Стейт-машина: fingerprint попапа, не кликает один и тот же дважды
- Семантический клик: ищет кнопку ВНУТРИ видимого диалога по тексту
- Никакой текст попапа не возвращается наружу — только категория + hash
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("instagrid.dialog_gate")

# ─── Outcomes ────────────────────────────────────────────────────────────────

NO_BLOCKER = "NO_BLOCKER"
HANDLED_REEVALUATE = "HANDLED_REEVALUATE"
TRANSITIONING_RETRY = "TRANSITIONING_RETRY"
UNKNOWN_BLOCKER = "UNKNOWN_BLOCKER"
TERMINAL_MANUAL = "TERMINAL_MANUAL"


# ─── JS: инспекция диалогов ──────────────────────────────────────────────────

_INSPECT_JS = """() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!(r.width > 8 && r.height > 8 && s.display !== 'none' &&
      s.visibility !== 'hidden' && s.opacity !== '0' && r.bottom > 0 &&
      r.right > 0 && r.top < innerHeight && r.left < innerWidth)) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' || style.visibility === 'hidden' ||
          Number.parseFloat(style.opacity || '1') <= 0.01) return false;
    }
    return true;
  };
  const score = (el, i) => {
    const z = Number.parseInt(getComputedStyle(el).zIndex, 10);
    return (Number.isFinite(z) ? z : 0) * 100000 + i;
  };
  const dialogs = [...document.querySelectorAll("[role='dialog'],[aria-modal='true']")]
    .filter(visible).map((el, i) => ({el, score: score(el, i)}))
    .sort((a, b) => b.score - a.score);
  const top = dialogs[0] && dialogs[0].el;
  if (!top) return {category:'', present:false};
  const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const text = normalize(top.innerText || top.textContent);
  const labels = [...top.querySelectorAll('button,[role="button"],[aria-label]')]
    .filter(visible).map(el => normalize(el.getAttribute('aria-label') || el.innerText || el.textContent));
  const progress = !!top.querySelector(
    "[role='progressbar'],svg[aria-label='Loading...'],[aria-busy='true']");
  const signature = [
    normalize(top.getAttribute('role')),
    normalize(top.getAttribute('aria-label')),
    text,
    labels.join('|'),
    progress ? 'progress' : '',
  ].join('|');
  let fingerprint = 2166136261;
  for (let i = 0; i < signature.length; i++) {
    fingerprint ^= signature.charCodeAt(i);
    fingerprint = Math.imul(fingerprint, 16777619);
  }
  const result = (category) => ({
    category, present:true, progress,
    fingerprint:(fingerprint >>> 0).toString(16),
  });
  const composerAction = labels.some(label =>
    /^(next|share|post|publish|continue|done|edit)$/.test(label));
  const composerMedia = !!top.querySelector(
    "video,canvas,input[type='file'],[contenteditable='true'],textarea");
  if (composerAction && (composerMedia ||
      /crop|cover photo|trim|write a caption|create new post|edit/.test(text)))
    return result('operation_composer');
  if (progress || /\\b(sharing|posting|publishing|processing|uploading|preparing|checking)\\b/.test(text))
    return result('operation_processing');
  if (/\\b(shared successfully|reel shared|your reel (?:was|has been) shared)\\b/.test(text))
    return result('operation_success');
  const cookieHeading = text.includes('allow the use of cookies from instagram on this browser?');
  if (cookieHeading && labels.includes('allow all cookies') && labels.includes('decline optional cookies'))
    return result('cookie_consent');
  if (text.includes('save your login info') && labels.includes('not now'))
    return result('save_login');
  if (text.includes('what happened') && text.includes('we removed your post') && text.includes('see why'))
    return result('policy_notice');
  if (text.includes('turn on notifications') && text.includes('not now'))
    return result('notification');
  if (/(challenge|checkpoint|confirm it'?s you|help us confirm|verification)/.test(text))
    return result('checkpoint');
  if (/(try again later|we restrict|restricted|suspicious)/.test(text))
    return result('restriction');
  if (/(suspended|disabled)/.test(text)) return result('suspended');
  return result('unknown_dialog');
}"""


# ─── JS: document observation (DOM liveness, auth state) ─────────────────────

_DOCUMENT_OBSERVE_JS = """() => {
  const visible = (el) => {
    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>8 && r.height>8 && s.display!=='none' &&
      s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>0.01 &&
      r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
  };
  const normalized = String((document.body && document.body.innerText) || '')
    .replace(/\\s+/g, ' ').trim();
  const busy = [...document.querySelectorAll(
    "[aria-busy='true'],[role='progressbar'],svg[aria-label='Loading...']"
  )].some(visible);
  const password = [...document.querySelectorAll(
    "input[type='password'],input[name='password'],input[autocomplete='current-password']"
  )].some(visible);
  const authenticated = [...document.querySelectorAll(
    "a[href*='/direct/inbox'],a[href*='/accounts/edit'],svg[aria-label='Home'],svg[aria-label='New post']"
  )].some(visible);
  const signature = [
    location.href, document.readyState, normalized.length,
    document.body ? document.body.childElementCount : 0,
    document.querySelectorAll("[role='dialog'],[aria-modal='true']").length,
    password ? 'login' : '', authenticated ? 'authenticated' : '',
  ].join('|');
  let hash=2166136261;
  for(let i=0;i<signature.length;i++){hash^=signature.charCodeAt(i);hash=Math.imul(hash,16777619);}
  return {
    loading: document.readyState !== 'complete' || busy || normalized.length === 0,
    authenticated_ui: authenticated,
    login_ui: password,
    document_fingerprint: (hash>>>0).toString(16),
  };
}"""


# ─── JS: семантический клик по кнопке в диалоге ─────────────────────────────

def _make_semantic_action_js(wanted_text: str) -> str:
    """Генерирует JS для клика по кнопке с нужным текстом внутри top-level dialog."""
    return f"""() => {{
      const visible = (el) => {{ const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        return r.width>8 && r.height>8 && s.display!=='none' && s.visibility!=='hidden' &&
          s.opacity!=='0' && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth; }};
      const dialogs=[...document.querySelectorAll("[role='dialog'],[aria-modal='true']")].filter(visible)
        .map((el,i)=>{{const z=Number.parseInt(getComputedStyle(el).zIndex,10);return {{el,score:(Number.isFinite(z)?z:0)*100000+i}};}})
        .sort((a,b)=>b.score-a.score);
      const top=dialogs[0] && dialogs[0].el; if(!top) return false;
      const label=(el)=>String(el.getAttribute('aria-label')||el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase();
      const wanted={wanted_text!r};
      const target=[...top.querySelectorAll('button,[role="button"],[aria-label]')].find(el=>visible(el) && label(el)===wanted);
      if(!target) return false;
      target.click(); return true;
    }}"""


def _make_page_click_js(wanted_text: str) -> str:
    """
    Генерирует JS для клика по видимой кнопке ГДЕ УГОДНО на странице
    (не только внутри диалога). Нужно для полностраничных экранов вроде
    "Continue as <username>" — это не [role='dialog'], а обычный контент
    страницы, обычный _semantic_action его не найдёт.
    """
    return f"""() => {{
      const visible = (el) => {{ const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        return r.width>8 && r.height>8 && s.display!=='none' && s.visibility!=='hidden' &&
          s.opacity!=='0' && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth; }};
      const label=(el)=>String(el.getAttribute('aria-label')||el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase();
      const wanted={wanted_text!r};
      const candidates=[...document.querySelectorAll('button,[role="button"],a,[aria-label]')];
      const target=candidates.find(el=>visible(el) && label(el)===wanted);
      if(!target) return false;
      target.click(); return true;
    }}"""


async def click_button_by_text(page, text: str) -> bool:
    """Кликает первую видимую кнопку/ссылку с точным текстом ГДЕ УГОДНО на странице."""
    try:
        return bool(await page.evaluate(_make_page_click_js(text.strip().lower())))
    except Exception:
        return False


# ─── Диагностика неизвестного состояния страницы ────────────────────────────

async def diagnose_unknown_state(page, username: str, tag: str) -> None:
    """
    Снимает скриншот + текст страницы, когда встретилось что-то незнакомое
    (unknown_dialog, не удалось восстановить сессию и т.п.).

    Сохраняет в logs/screenshots/ — это и есть тот самый "сбор данных на будущее":
    периодически просматривай эту папку, и по накопленным примерам можно будет
    дописать новую категорию прямо в _INSPECT_JS выше.

    Портировано из login.py::_diagnose_page (там уже проверено на практике),
    здесь — как отдельная переиспользуемая функция для posting.py и других мест.
    """
    from pathlib import Path as _P
    import time as _t

    try:
        shots = _P("logs") / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        stamp = _t.strftime("%Y%m%d_%H%M%S")
        shot = shots / f"{stamp}_{username}_{tag}.png"
        await page.screenshot(path=str(shot), full_page=False)
        logger.warning("[%s] Unknown state screenshot: %s", username, shot)
    except Exception as e:
        logger.debug("[%s] Screenshot failed: %s", username, e)

    try:
        info = await page.evaluate("""() => {
            const vis = (el) => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.display !== 'none' &&
                       s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
            };
            const clean = (t) => String(t || '').replace(/\\s+/g, ' ').trim();
            const buttons = [...document.querySelectorAll('button,[role="button"],a')]
                .filter(vis).map(e => clean(e.getAttribute('aria-label') || e.innerText)).filter(t => t.length > 0);
            return {
                title: clean(document.title),
                url: location.href,
                bodyStart: clean(document.body ? document.body.innerText : '').slice(0, 400),
                buttons: [...new Set(buttons)].slice(0, 15),
            };
        }""")
        logger.warning("[%s] -- ДИАГНОСТИКА НЕИЗВЕСТНОГО СОСТОЯНИЯ (%s) --", username, tag)
        logger.warning("[%s]   url     : %s", username, str(info.get("url", ""))[:120])
        logger.warning("[%s]   title   : %s", username, str(info.get("title", ""))[:90])
        logger.warning("[%s]   buttons : %s", username, info.get("buttons", []))
        logger.warning("[%s]   body    : %s", username, str(info.get("bodyStart", ""))[:300])
    except Exception as e:
        logger.debug("[%s] Page diagnostics failed: %s", username, e)


# ─── Public API ──────────────────────────────────────────────────────────────

async def inspect_dialog(page) -> dict[str, Any]:
    """Инспектирует текущий верхний диалог. Возвращает category + fingerprint."""
    try:
        result = await page.evaluate(_INSPECT_JS)
        if isinstance(result, dict):
            return {
                "category": str(result.get("category") or ""),
                "present": bool(result.get("present")),
                "progress": bool(result.get("progress")),
                "fingerprint": str(result.get("fingerprint") or ""),
            }
    except Exception:
        pass
    return {"category": "", "present": False, "progress": False, "fingerprint": ""}


async def _document_observation(page) -> dict[str, Any]:
    """DOM/auth state без утечки текста."""
    try:
        result = await page.evaluate(_DOCUMENT_OBSERVE_JS)
        if isinstance(result, dict):
            return {
                "loading": bool(result.get("loading")),
                "authenticated_ui": bool(result.get("authenticated_ui")),
                "login_ui": bool(result.get("login_ui")),
                "document_fingerprint": str(result.get("document_fingerprint") or ""),
            }
    except Exception:
        pass
    return {"loading": False, "authenticated_ui": False,
            "login_ui": False, "document_fingerprint": ""}


async def _observe(page) -> dict[str, Any]:
    """Полное наблюдение: dialog + document state."""
    observed = await inspect_dialog(page)
    doc = await _document_observation(page)
    observed.update(doc)
    try:
        observed["url"] = str(getattr(page, "url", "") or "")
    except Exception:
        observed["url"] = ""
    return observed


async def _semantic_action(page, action: str) -> bool:
    """Кликает named action внутри topmost dialog. Не использует координаты."""
    wanted = {
        "allow_all_cookies": "allow all cookies",
        "decline_optional_cookies": "decline optional cookies",
        "not_now": "not now",
        "close": "close",
    }.get(action)
    if not wanted:
        return False
    try:
        return bool(await page.evaluate(_make_semantic_action_js(wanted)))
    except Exception:
        return False


async def dismiss_known_dialog(page, category: str) -> bool:
    """Один безопасный клик для известного диалога."""
    action = {
        "cookie_consent": "decline_optional_cookies",
        "save_login": "not_now",
        "notification": "not_now",
        "policy_notice": "close",
    }.get(str(category or ""))
    return bool(action and await _semantic_action(page, action))


# ─── Стейт-машина: continue_after_dialog ────────────────────────────────────

async def continue_after_dialog(
    page,
    *,
    allow_safe_close: bool = False,
    wait_seconds: float = 4.0,
    cookie_action: str = "decline_optional_cookies",
) -> dict[str, Any]:
    """
    Основная стейт-машина обработки блокирующих диалогов.

    KNOWN_DIALOG → один клик → HANDLED_REEVALUATE → fresh DOM reads →
    bounded transition observation → next actual state.

    Unknown — terminal только после 3 идентичных non-loading observations.
    """
    deadline = time.time() + max(0.0, float(wait_seconds))
    observed = await _observe(page)

    # Ждём пока диалог появится (или timeout)
    while not observed["present"] and time.time() < deadline:
        await asyncio.sleep(0.2)
        observed = await _observe(page)

    handled = False
    clicked_at = 0.0
    clicked_action = ""
    clicked_category = ""
    post_click_read_pending = False
    clicked_fingerprints: set[tuple[str, str]] = set()
    fresh_reads = 0
    stable_reads = 0
    settled_reads = 0
    last_identity: tuple[str, str, str, str] | None = None
    last_document = ""

    while True:
        fresh_reads += 1
        category = str(observed.get("category") or "")
        present = bool(observed.get("present"))
        loading = bool(observed.get("loading") or observed.get("progress"))

        if not present:
            if not handled:
                return {
                    "outcome": NO_BLOCKER, "state": "", "present": False,
                    "dismissed": False, "fresh_reads": fresh_reads,
                }
            document = str(observed.get("document_fingerprint") or "")
            if loading:
                settled_reads = 0
            elif document and document == last_document:
                settled_reads += 1
            else:
                settled_reads = 1
            last_document = document

            if loading and time.time() >= deadline:
                return {
                    "outcome": TRANSITIONING_RETRY, "state": "",
                    "present": False, "dismissed": True,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }
            if settled_reads >= 2 or (not loading and time.time() >= deadline):
                return {
                    "outcome": HANDLED_REEVALUATE, "state": "",
                    "present": False, "dismissed": True,
                    "fresh_reads": fresh_reads, "stable_reads": settled_reads,
                    "clicked_at": clicked_at, "clicked_action": clicked_action,
                    "clicked_category": clicked_category,
                }

        elif category in {"operation_composer", "operation_success"}:
            return {
                "outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                "state": "", "present": True, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": 1,
            }

        elif category == "operation_processing" or loading:
            stable_reads = settled_reads = 0
            last_identity = None
            if time.time() >= deadline:
                return {
                    "outcome": TRANSITIONING_RETRY, "state": "",
                    "present": present, "dismissed": handled,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }

        elif category in {"policy_notice", "cookie_consent", "notification", "save_login"}:
            action = {
                "policy_notice": "close",
                "cookie_consent": cookie_action,
                "notification": "not_now",
                "save_login": "not_now",
            }[category]
            allowed = category != "policy_notice" or allow_safe_close
            fingerprint = str(observed.get("fingerprint") or category)
            click_key = (category, fingerprint)

            if not allowed or click_key in clicked_fingerprints:
                if time.time() >= deadline or not allowed:
                    return {
                        "outcome": TERMINAL_MANUAL,
                        "state": "blocking_dialog_not_dismissed",
                        "present": True, "dismissed": False,
                        "fresh_reads": fresh_reads, "stable_reads": stable_reads,
                    }
            else:
                clicked_fingerprints.add(click_key)
                if not await _semantic_action(page, action):
                    return {
                        "outcome": TERMINAL_MANUAL,
                        "state": "blocking_dialog_not_dismissed",
                        "present": True, "dismissed": False,
                        "fresh_reads": fresh_reads, "stable_reads": 0,
                    }
                handled = True
                clicked_at = time.time()
                clicked_action = action
                clicked_category = category
                deadline = max(deadline, clicked_at + 0.05)
                post_click_read_pending = True
                stable_reads = settled_reads = 0
                last_identity = None
                last_document = ""

        elif category in {"checkpoint", "restriction", "suspended"}:
            return {
                "outcome": TERMINAL_MANUAL,
                "state": {
                    "checkpoint": "checkpoint",
                    "restriction": "restricted",
                    "suspended": "suspended",
                }[category],
                "present": True, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": 1,
            }

        else:
            # Unknown dialog — authenticated UI wins over stray dialog node
            if observed.get("authenticated_ui"):
                return {
                    "outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                    "state": "", "present": True, "dismissed": handled,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }
            identity = (
                category,
                str(observed.get("fingerprint") or category),
                str(observed.get("document_fingerprint") or ""),
                str(observed.get("url") or ""),
            )
            if identity == last_identity:
                stable_reads += 1
            else:
                stable_reads = 1
                last_identity = identity
            if stable_reads >= 3:
                return {
                    "outcome": UNKNOWN_BLOCKER, "state": "unknown_dialog",
                    "present": True, "dismissed": handled,
                    "fresh_reads": fresh_reads, "stable_reads": stable_reads,
                    "fingerprint": identity[1],
                }

        if time.time() >= deadline and not post_click_read_pending:
            return {
                "outcome": TRANSITIONING_RETRY, "state": "",
                "present": present, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": stable_reads,
            }
        await asyncio.sleep(0.2)
        observed = await _observe(page)
        post_click_read_pending = False


# ─── Auth-aware dialog resolution ────────────────────────────────────────────

async def resolve_dialog_gate(
    page,
    *,
    allow_safe_close: bool = False,
    wait_seconds: float = 4.0,
) -> dict[str, Any]:
    """Backward-compatible alias."""
    return await continue_after_dialog(
        page,
        allow_safe_close=allow_safe_close,
        wait_seconds=wait_seconds,
    )


# ─── Auth verification: API endpoint + cookie + UI corroboration ─────────────

_AUTH_SIGNALS_JS = """() => {
  const visible = (el) => {
    if (!el) return false;
    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>8 && r.height>8 && s.display!=='none' &&
      s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>0.01 &&
      r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
  };
  const anyVisible = (selector) =>
    [...document.querySelectorAll(selector)].some(visible);
  const loginForm = anyVisible(
    "input[type='password'],input[name='password'],input[autocomplete='current-password']"
  );
  const authNav = anyVisible(
    "svg[aria-label='Home' i],svg[aria-label='New post' i]," +
    "a[href*='/direct/inbox'],nav a[href*='/direct/']"
  );
  const accountMenu = anyVisible(
    "nav img[alt*='profile picture' i]," +
    "a[href*='/accounts/edit']," +
    "[aria-label='Profile' i],[aria-label*='profile picture' i]"
  );
  const appShell = !!document.querySelector('main') &&
    anyVisible("nav,[role='navigation'],a[href='/']");
  return {login_form: loginForm, auth_nav: authNav,
          account_menu: accountMenu, app_shell: appShell};
}"""


_CURRENT_USER_ENDPOINT_JS = """async () => {
  try {
    const response = await fetch('/api/v1/accounts/current_user/', {
      credentials: 'include',
      headers: {
        'X-IG-App-ID': '936619743392459',
        'X-Requested-With': 'XMLHttpRequest'
      }
    });
    if (response.status !== 200) return false;
    const data = await response.json();
    const user = data && (data.user || data);
    return !!(user && (user.pk || user.id) && user.username);
  } catch (_) { return false; }
}"""


async def verify_authenticated(page) -> dict[str, Any]:
    """
    Подтверждает аутентификацию через множественную корроборацию:

    1. API endpoint: /api/v1/accounts/current_user/ — самый надёжный сигнал
    2. UI: auth_nav + account_menu + app_shell (≥2 из 3)
    3. Session cookies: sessionid + csrftoken + ds_user_id
    4. Отсутствие login form

    Confirmed = API OK || (≥2 UI + cookie && no login form)
    """
    # UI signals
    try:
        ui = await page.evaluate(_AUTH_SIGNALS_JS)
    except Exception:
        ui = {}

    login_form = bool(ui.get("login_form"))
    auth_nav = bool(ui.get("auth_nav"))
    account_menu = bool(ui.get("account_menu"))
    app_shell = bool(ui.get("app_shell"))

    # Session cookies
    try:
        cookies = await page.context.cookies("https://www.instagram.com/")
        cookie_names = {
            str(c.get("name") or "").lower()
            for c in cookies
            if str(c.get("value") or "").strip()
        }
    except Exception:
        cookie_names = set()

    session_cookie = "sessionid" in cookie_names
    full_cookie_set = {"sessionid", "csrftoken", "ds_user_id"}.issubset(cookie_names)

    # API endpoint — самый сильный сигнал
    try:
        endpoint_ok = bool(await page.evaluate(_CURRENT_USER_ENDPOINT_JS))
    except Exception:
        endpoint_ok = False

    # Корроборация
    ui_count = sum([auth_nav, account_menu, app_shell])
    corroborated = bool(
        not login_form
        and (ui_count >= 3 or (ui_count >= 2 and (session_cookie or full_cookie_set)))
    )
    confirmed = bool(not login_form and (endpoint_ok or corroborated))

    evidence = {
        "current_user_endpoint": endpoint_ok,
        "auth_nav": auth_nav,
        "account_menu": account_menu,
        "app_shell": app_shell,
        "session_cookie": session_cookie,
        "full_cookie_set": full_cookie_set,
        "no_login_form": not login_form,
        "ui_count": ui_count,
        "corroborated": corroborated,
    }

    if confirmed:
        reason = "API endpoint confirmed" if endpoint_ok else "corroborating UI+cookie signals"
    else:
        reason = "insufficient authentication evidence"

    logger.debug("Auth verification: confirmed=%s, reason=%s, evidence=%s", confirmed, reason, evidence)

    return {
        "confirmed": confirmed,
        "reason": reason,
        "evidence": evidence,
    }


# ─── Privacy check via API ───────────────────────────────────────────────────

_CHECK_PRIVACY_JS = """async () => {
  try {
    const r = await fetch('/api/v1/accounts/current_user/?edit=true', {
      credentials: 'include',
      headers: {
        'X-IG-App-ID': '936619743392459',
        'X-Requested-With': 'XMLHttpRequest'
      }
    });
    if (r.status !== 200) return {error: 'HTTP ' + r.status};
    const d = await r.json();
    const u = d && (d.user || d);
    if (!u) return {error: 'no user data'};
    return {
      is_private: !!u.is_private,
      username: u.username || '',
      is_professional: !!u.is_professional_account,
      ok: true,
    };
  } catch (e) { return {error: String(e)}; }
}"""

_TOGGLE_PRIVACY_JS = """async (set_private) => {
  try {
    const csrf = document.cookie.split(';')
      .map(c => c.trim())
      .find(c => c.startsWith('csrftoken='));
    const token = csrf ? csrf.split('=')[1] : '';
    const r = await fetch('/api/v1/accounts/set_private/', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': token,
        'X-IG-App-ID': '936619743392459',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: 'is_private=' + (set_private ? '1' : '0'),
    });
    return r.status === 200;
  } catch(_) { return false; }
}"""


async def check_account_privacy(page) -> dict[str, Any]:
    """Проверяет приватность через Instagram API (не UI-селекторы)."""
    try:
        result = await page.evaluate(_CHECK_PRIVACY_JS)
        if isinstance(result, dict):
            return result
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unknown"}


async def set_account_public(page) -> bool:
    """Переключает аккаунт в public через API."""
    privacy = await check_account_privacy(page)
    if privacy.get("error"):
        logger.warning("Cannot check privacy: %s", privacy["error"])
        return False

    if not privacy.get("is_private"):
        logger.info("Account already public")
        return True

    try:
        result = await page.evaluate(_TOGGLE_PRIVACY_JS, False)
        if result:
            logger.info("Account switched to public via API")
            return True
        logger.warning("API toggle returned false")
    except Exception as e:
        logger.warning("API privacy toggle failed: %s", e)

    return False
