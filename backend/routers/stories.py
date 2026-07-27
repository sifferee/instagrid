"""InstaGrid — API сторис."""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from backend.services.stories import StoryController

router = APIRouter(prefix="/api/stories", tags=["stories"])
sc = StoryController()


# ─── Модели ───────────────────────────────────────────────────────────────────

class StoryPostRequest(BaseModel):
    account_id: int
    link_url: str = ""
    cta_text: str = "Learn More"
    photo_id: int | None = None  # если None — берём из пула


# ─── Фото-пул ─────────────────────────────────────────────────────────────────

@router.post("/photos/upload")
async def upload_story_photos(
    file: UploadFile = File(...),
    niche_id: int | None = Form(None),
):
    """Загрузка ZIP с фото для сторис."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only ZIP files accepted")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        result = await sc.photo_pool.upload_zip(tmp_path, niche_id)
        return result
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ─── Статистика ───────────────────────────────────────────────────────────────

@router.get("/stats")
async def story_stats(account_id: int | None = None):
    """Статистика сторис."""
    return await sc.get_story_stats(account_id)


@router.get("/trigger/check")
async def check_trigger(account_id: int, reel_views: int):
    """Проверить нужно ли постить сторис (автотриггер)."""
    should = await sc.auto_trigger.should_post_story(account_id, reel_views)
    threshold = sc.auto_trigger.get_threshold(account_id)
    return {
        "should_post": should,
        "threshold": threshold,
        "current_views": reel_views,
    }
