"""InstaGrid — API постинга рилсов."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.content_manager import ContentManager
from backend.services.profile_manager import ProfileManager
from backend.services.posting import PostingController, PostResult

router = APIRouter(prefix="/api/posting", tags=["posting"])

# Синглтоны — инициализируются при первом вызове
_pm: ProfileManager | None = None
_controller: PostingController | None = None


def _get_controller() -> PostingController:
    global _pm, _controller
    if _controller is None:
        _pm = ProfileManager()
        cm = ContentManager()
        _controller = PostingController(
            profile_manager=_pm,
            content_manager=cm,
        )
    return _controller


# ─── Модели ───────────────────────────────────────────────────────────────────

class ManualPostRequest(BaseModel):
    account_ids: list[int]
    reels_count: int | None = None


class AutoPostRequest(BaseModel):
    niche_id: int | None = None
    account_ids: list[int] | None = None
    reels_count: int | None = None
    loop_forever: bool = False


# ─── Хранилище результатов ────────────────────────────────────────────────────

_last_results: list[dict] = []
_auto_task: asyncio.Task | None = None


# ─── Эндпоинты ────────────────────────────────────────────────────────────────

@router.post("/manual")
async def manual_post(data: ManualPostRequest):
    """Ручной постинг: выбранные аккаунты, N рилсов."""
    global _last_results
    controller = _get_controller()

    if controller.is_running:
        raise HTTPException(409, "Posting already running")

    results = await controller.manual_post(
        account_ids=data.account_ids,
        reels_count=data.reels_count,
    )
    _last_results = [_result_to_dict(r) for r in results]
    return {"completed": len(results), "results": _last_results}


@router.post("/auto/start")
async def auto_post_start(data: AutoPostRequest):
    """Запуск автопостинга в фоне."""
    global _auto_task, _last_results
    controller = _get_controller()

    if _auto_task and not _auto_task.done():
        raise HTTPException(409, "Auto posting already running")

    _last_results = []

    async def _on_progress(result: PostResult):
        _last_results.append(_result_to_dict(result))

    _auto_task = asyncio.create_task(controller.auto_post(
        niche_id=data.niche_id,
        account_ids=data.account_ids,
        reels_count=data.reels_count,
        loop_forever=data.loop_forever,
        on_progress=_on_progress,
    ))

    return {"status": "started"}


@router.post("/auto/stop")
async def auto_post_stop():
    """Остановка автопостинга."""
    controller = _get_controller()
    await controller.stop()
    return {"status": "stopping"}


@router.get("/status")
async def posting_status():
    """Текущий статус постинга."""
    controller = _get_controller()
    return {
        "is_running": controller.is_running,
        "progress": controller.progress,
        "results": _last_results,
    }


@router.get("/results")
async def posting_results():
    """Последние результаты постинга."""
    return _last_results


def _result_to_dict(r: PostResult) -> dict:
    return {
        "account_id": r.account_id,
        "username": r.username,
        "status": r.status.value,
        "reels_posted": r.reels_posted,
        "reels_target": r.reels_target,
        "message": r.message,
        "duration_sec": round(r.duration_sec, 1),
    }
