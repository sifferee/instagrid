"""InstaGrid — FastAPI приложение."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.database import init_db
from backend.routers import niches, accounts, proxies

app = FastAPI(title="InstaGrid", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(niches.router)
app.include_router(accounts.router)
app.include_router(proxies.router)


@app.on_event("startup")
def on_startup():
    init_db()


# Serve React build
FRONTEND_BUILD = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_BUILD.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_BUILD / "assets")), name="assets")

    @app.get("/{path:path}")
    def serve_spa(path: str):
        file_path = FRONTEND_BUILD / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_BUILD / "index.html"))
