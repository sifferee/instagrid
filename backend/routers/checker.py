"""InstaGrid — API чекера."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.checker import CheckerController, CheckResult, CheckerPool
from backend.services.profile_manager import ProfileManager

router = APIRouter(prefix="/api/checker", tags=["checker"])

_pm: ProfileManager | None = None
_controller: CheckerController | None = None
_auto_task: asyncio.Task | None = None
_last_results: list[dict] = []
_alerts: list[str] = []


def _get_controller() -> CheckerController:
    global _pm, _controller
    if _controller is None:
        _pm = ProfileManager()
        _controller = CheckerController(profile_manager=_pm)
    return _controller


# ─── Модели ───────────────────────────────────────────────────────────────────

class CheckerImport(BaseModel):
    data: str  # login:pass:2fa:host:port:user:pass, по строке


class ManualCheckRequest(BaseModel):
    account_ids: list[int]


class AutoCheckRequest(BaseModel):
    niche_id: int | None = None


# ─── Чекер-аккаунты ──────────────────────────────────────────────────────────

@router.post("/accounts/import")
async def import_checkers(data: CheckerImport):
    """Массовый импорт чекер-аккаунтов."""
    pool = CheckerPool()
    lines = [l.strip() for l in data.data.strip().split("\n") if l.strip()]
    count = await pool.import_checkers(lines)
    return {"added": count}


@router.get("/accounts")
async def list_checkers():
    """Список чекер-аккаунтов."""
    pool = CheckerPool()
    return await pool.list_checkers()


@router.get("/accounts/alive")
async def alive_count():
    """Количество живых чекеров."""
    pool = CheckerPool()
    count = await pool.get_alive_count()
    return {"alive": count, "min_required": 3}


# ─── Проверка ─────────────────────────────────────────────────────────────────

@router.post("/check")
async def manual_check(data: ManualCheckRequest):
    """Ручная проверка конкретных аккаунтов."""
    global _last_results
    controller = _get_controller()
    results = await controller.manual_check(data.account_ids)
    _last_results = [_result_to_dict(r) for r in results]
    return {"checked": len(results), "results": _last_results}


@router.post("/auto/start")
async def auto_check_start(data: AutoCheckRequest):
    """Запуск автоцикла чекера."""
    global _auto_task, _last_results, _alerts
    controller = _get_controller()

    if _auto_task and not _auto_task.done():
        raise HTTPException(409, "Checker already running")

    _last_results = []
    _alerts = []

    async def _on_result(r: CheckResult):
        _last_results.append(_result_to_dict(r))

    async def _on_alert(msg: str):
        _alerts.append(msg)

    _auto_task = asyncio.create_task(controller.auto_check(
        niche_id=data.niche_id,
        on_result=_on_result,
        on_alert=_on_alert,
    ))

    return {"status": "started"}


@router.post("/auto/stop")
async def auto_check_stop():
    """Остановка автоцикла."""
    controller = _get_controller()
    await controller.stop()
    return {"status": "stopping"}


@router.get("/status")
async def checker_status():
    """Текущий статус чекера."""
    is_running = _auto_task is not None and not _auto_task.done()
    return {
        "is_running": is_running,
        "results_count": len(_last_results),
        "alerts": _alerts,
    }


@router.get("/results")
async def checker_results():
    """Последние результаты."""
    return _last_results


# ─── Статистика ───────────────────────────────────────────────────────────────

@router.get("/stats/account/{account_id}")
async def account_stats(account_id: int):
    """Статистика аккаунта: просмотры, подписчики, рилсы."""
    controller = _get_controller()
    return await controller.get_account_stats(account_id)


@router.get("/stats/niche/{niche_id}")
async def niche_stats(niche_id: int):
    """Агрегированная статистика по нише."""
    controller = _get_controller()
    return await controller.get_niche_stats(niche_id)


def _result_to_dict(r: CheckResult) -> dict:
    profile = None
    if r.profile:
        profile = {
            "username": r.profile.username,
            "followers": r.profile.followers,
            "following": r.profile.following,
            "posts_count": r.profile.posts_count,
            "is_banned": r.profile.is_banned,
            "reels_count": len(r.profile.reels),
            "reels": [
                {
                    "shortcode": reel.shortcode,
                    "views": reel.views,
                    "likes": reel.likes,
                    "comments": reel.comments,
                }
                for reel in r.profile.reels[:6]
            ],
        }
    return {
        "account_id": r.account_id,
        "target_username": r.target_username,
        "checker_username": r.checker_username,
        "success": r.success,
        "profile": profile,
        "message": r.message,
    }
