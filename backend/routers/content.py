"""InstaGrid — API контента (видео + описания)."""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from backend.services.content_manager import ContentManager

router = APIRouter(prefix="/api/content", tags=["content"])
cm = ContentManager()


# ─── Модели ───────────────────────────────────────────────────────────────────

class DescriptionImport(BaseModel):
    niche_id: int | None = None
    descriptions: list[str]


class DescriptionUpdate(BaseModel):
    text: str


class DescriptionGenerate(BaseModel):
    niche_id: int | None = None
    reference_text: str
    count: int = 50
    api_key: str = ""


class AssignVideo(BaseModel):
    video_id: int
    account_id: int


class AssignDescription(BaseModel):
    description_id: int
    account_id: int


# ─── Видео ────────────────────────────────────────────────────────────────────

@router.post("/videos/upload")
async def upload_video_zip(
    file: UploadFile = File(...),
    niche_id: int | None = Form(None),
):
    """Загрузка ZIP-архива с видео."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only ZIP files accepted")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        result = await cm.videos.upload_zip(tmp_path, niche_id)
        return result
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.get("/videos")
async def list_videos(niche_id: int | None = None, status: str | None = None):
    """Список видео."""
    return await cm.videos.list_videos(niche_id, status)


@router.post("/videos/distribute")
async def distribute_videos(niche_id: int | None = None):
    """Распределить свободные видео по аккаунтам."""
    return await cm.videos.distribute(niche_id)


@router.post("/videos/assign")
async def assign_video(data: AssignVideo):
    """Ручная привязка видео к аккаунту."""
    await cm.videos.assign_to_account(data.video_id, data.account_id)
    return {"ok": True}


@router.post("/videos/{video_id}/unassign")
async def unassign_video(video_id: int):
    """Снять видео с аккаунта."""
    video = await cm.videos.list_videos()
    for v in video:
        if v["id"] == video_id and v.get("account_id"):
            await cm.videos.unassign_from_account(v["account_id"])
            return {"ok": True}
    raise HTTPException(404, "Video not found or not assigned")


@router.delete("/videos/{video_id}")
async def delete_video(video_id: int):
    """Удалить видео."""
    await cm.videos.delete_video(video_id)
    return {"ok": True}


@router.get("/videos/stats")
async def video_stats(niche_id: int | None = None):
    """Статистика видео для UI-плашек."""
    return await cm.videos.get_stats(niche_id)


# ─── Описания ─────────────────────────────────────────────────────────────────

@router.post("/descriptions/import")
async def import_descriptions(data: DescriptionImport):
    """Импорт пула описаний."""
    count = await cm.descriptions.import_manual(data.descriptions, data.niche_id)
    return {"added": count}


@router.post("/descriptions/generate")
async def generate_descriptions(data: DescriptionGenerate):
    """Автогенерация описаний через Claude API."""
    if not data.api_key:
        # Пробуем из config.env
        from backend.config import CLAUDE_API_KEY
        data.api_key = CLAUDE_API_KEY
    if not data.api_key:
        raise HTTPException(400, "Claude API key required")

    descriptions = await cm.descriptions.generate_with_claude(
        reference_text=data.reference_text,
        count=data.count,
        niche_id=data.niche_id,
        api_key=data.api_key,
    )
    return {"generated": len(descriptions), "descriptions": descriptions}


@router.get("/descriptions")
async def list_descriptions(
    niche_id: int | None = None,
    status: str | None = None,
    source: str | None = None,
):
    """Список описаний."""
    return await cm.descriptions.list_descriptions(niche_id, status, source)


@router.post("/descriptions/distribute")
async def distribute_descriptions(niche_id: int | None = None):
    """Распределить описания по аккаунтам."""
    return await cm.descriptions.distribute(niche_id)


@router.post("/descriptions/assign")
async def assign_description(data: AssignDescription):
    """Ручная привязка описания к аккаунту."""
    await cm.descriptions.assign_to_account(data.description_id, data.account_id)
    return {"ok": True}


@router.put("/descriptions/{desc_id}")
async def update_description(desc_id: int, data: DescriptionUpdate):
    """Ручное редактирование описания."""
    await cm.descriptions.update_text(desc_id, data.text)
    return {"ok": True}


@router.delete("/descriptions/{desc_id}")
async def delete_description(desc_id: int):
    """Удалить описание."""
    await cm.descriptions.delete_description(desc_id)
    return {"ok": True}


# ─── Общее ────────────────────────────────────────────────────────────────────

@router.post("/distribute-all")
async def distribute_all(niche_id: int | None = None):
    """Распределить и видео, и описания."""
    return await cm.distribute_all(niche_id)


@router.get("/stats")
async def full_stats(niche_id: int | None = None):
    """Полная статистика контента."""
    return await cm.get_full_stats(niche_id)


@router.get("/account/{account_id}")
async def account_content(account_id: int):
    """Контент привязанный к конкретному аккаунту."""
    return await cm.get_posting_content(account_id)
