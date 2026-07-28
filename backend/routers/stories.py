"""InstaGrid — API сторис + шаблоны (multi-photo)."""
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.database import query, query_one, execute, run_sync
from backend.services.stories import StoryController

router = APIRouter(prefix="/api/stories", tags=["stories"])
sc = StoryController()


# ─── Модели ───────────────────────────────────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    photo_ids: list[int]          # 1+ фото
    link_url: str
    cta_text: str = "Learn More"
    niche_ids: list[int] | None = None  # None = все ниши


class TemplateUpdate(BaseModel):
    name: str | None = None
    photo_ids: list[int] | None = None
    link_url: str | None = None
    cta_text: str | None = None
    is_active: bool | None = None
    niche_ids: list[int] | None = None


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
        return await sc.photo_pool.upload_zip(tmp_path, niche_id)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.get("/photos")
async def list_photos():
    """Список всех загруженных фото."""
    return await run_sync(
        query,
        """SELECT sp.*, n.name as niche_name
           FROM story_photos sp
           LEFT JOIN niches n ON n.id = sp.niche_id
           ORDER BY sp.id DESC""",
        (),
    )


@router.get("/photos/{photo_id}/preview")
async def photo_preview(photo_id: int):
    """Превью фото по ID — отдаёт сам файл."""
    photo = await run_sync(query_one, "SELECT filepath FROM story_photos WHERE id = ?", (photo_id,))
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    fp = Path(photo["filepath"])
    if not fp.exists():
        raise HTTPException(404, "Файл не найден на диске")
    return FileResponse(str(fp), media_type="image/jpeg")


# ─── Шаблоны сторис ──────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates():
    """Список всех шаблонов с фото и нишами."""
    templates = await run_sync(
        query, "SELECT * FROM story_templates ORDER BY id DESC", (),
    )
    for t in templates:
        t["photos"] = await run_sync(
            query,
            """SELECT sp.id, sp.filename, sp.filepath
               FROM story_template_photos stp
               JOIN story_photos sp ON sp.id = stp.photo_id
               WHERE stp.template_id = ?""",
            (t["id"],),
        )
        t["niches"] = await run_sync(
            query,
            """SELECT n.id, n.name
               FROM story_template_niches stn
               JOIN niches n ON n.id = stn.niche_id
               WHERE stn.template_id = ?""",
            (t["id"],),
        )
    return templates


@router.post("/templates", status_code=201)
async def create_template(body: TemplateCreate):
    """Создать шаблон с несколькими фото."""
    if not body.photo_ids:
        raise HTTPException(400, "Нужно выбрать хотя бы одно фото")

    tmpl_id = await run_sync(
        execute,
        "INSERT INTO story_templates (name, link_url, cta_text) VALUES (?, ?, ?)",
        (body.name.strip(), body.link_url.strip(), body.cta_text.strip()),
    )

    # Привязываем фото
    for pid in body.photo_ids:
        await run_sync(
            execute,
            "INSERT OR IGNORE INTO story_template_photos (template_id, photo_id) VALUES (?, ?)",
            (tmpl_id, pid),
        )

    # Привязка к нишам
    if body.niche_ids:
        for nid in body.niche_ids:
            await run_sync(
                execute,
                "INSERT OR IGNORE INTO story_template_niches (template_id, niche_id) VALUES (?, ?)",
                (tmpl_id, nid),
            )

    return await _get_template_full(tmpl_id)


@router.put("/templates/{template_id}")
async def update_template(template_id: int, body: TemplateUpdate):
    """Обновить шаблон."""
    existing = await run_sync(query_one, "SELECT id FROM story_templates WHERE id = ?", (template_id,))
    if not existing:
        raise HTTPException(404, "Шаблон не найден")

    if body.name is not None:
        await run_sync(execute, "UPDATE story_templates SET name = ? WHERE id = ?", (body.name.strip(), template_id))
    if body.link_url is not None:
        await run_sync(execute, "UPDATE story_templates SET link_url = ? WHERE id = ?", (body.link_url.strip(), template_id))
    if body.cta_text is not None:
        await run_sync(execute, "UPDATE story_templates SET cta_text = ? WHERE id = ?", (body.cta_text.strip(), template_id))
    if body.is_active is not None:
        await run_sync(execute, "UPDATE story_templates SET is_active = ? WHERE id = ?", (1 if body.is_active else 0, template_id))

    if body.photo_ids is not None:
        await run_sync(execute, "DELETE FROM story_template_photos WHERE template_id = ?", (template_id,))
        for pid in body.photo_ids:
            await run_sync(execute, "INSERT OR IGNORE INTO story_template_photos (template_id, photo_id) VALUES (?, ?)", (template_id, pid))

    if body.niche_ids is not None:
        await run_sync(execute, "DELETE FROM story_template_niches WHERE template_id = ?", (template_id,))
        for nid in body.niche_ids:
            await run_sync(execute, "INSERT OR IGNORE INTO story_template_niches (template_id, niche_id) VALUES (?, ?)", (template_id, nid))

    return await _get_template_full(template_id)


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int):
    existing = await run_sync(query_one, "SELECT id FROM story_templates WHERE id = ?", (template_id,))
    if not existing:
        raise HTTPException(404, "Шаблон не найден")
    await run_sync(execute, "DELETE FROM story_templates WHERE id = ?", (template_id,))
    return {"ok": True}


async def _get_template_full(template_id: int) -> dict:
    t = await run_sync(query_one, "SELECT * FROM story_templates WHERE id = ?", (template_id,))
    t["photos"] = await run_sync(
        query,
        """SELECT sp.id, sp.filename, sp.filepath
           FROM story_template_photos stp
           JOIN story_photos sp ON sp.id = stp.photo_id
           WHERE stp.template_id = ?""",
        (template_id,),
    )
    t["niches"] = await run_sync(
        query,
        """SELECT n.id, n.name FROM story_template_niches stn
           JOIN niches n ON n.id = stn.niche_id WHERE stn.template_id = ?""",
        (template_id,),
    )
    return t


# ─── Статистика ───────────────────────────────────────────────────────────────

@router.get("/stats")
async def story_stats(account_id: int | None = None):
    return await sc.get_story_stats(account_id)

@router.get("/trigger/check")
async def check_trigger(account_id: int, reel_views: int):
    should = await sc.auto_trigger.should_post_story(account_id, reel_views)
    threshold = sc.auto_trigger.get_threshold(account_id)
    return {"should_post": should, "threshold": threshold, "current_views": reel_views}
