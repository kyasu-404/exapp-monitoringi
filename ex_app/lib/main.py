"""Monitoring Request Manager FastAPI entrypoint."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, run_app, set_handlers

from monitoringi.api import router as api_router
from monitoringi.db import init_db
from monitoringi.errors import AppError

BASE_DIR = Path(__file__).resolve().parent.parent


def _safe_asset(root: Path, path: str, media_type: str | None = None) -> FileResponse:
    base = root.resolve()
    target = (base / path).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=404)
    if not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "no-store"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    set_handlers(app, enabled_handler)
    yield


APP = FastAPI(title="Monitoring Request Manager", version="0.1.0", lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)
APP.include_router(api_router)


@APP.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
    )


@APP.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Monitoring Request Manager</title>
  <link rel="stylesheet" href="css/monitoringi.css">
</head>
<body>
  <div id="app"></div>
  <script src="js/monitoringi-main.js"></script>
</body>
</html>"""
        ,
        headers={"Cache-Control": "no-store"},
    )


@APP.get("/js/{path:path}")
async def js_asset(path: str) -> FileResponse:
    return _safe_asset(BASE_DIR / "js", path, "application/javascript")


@APP.get("/css/{path:path}")
async def css_asset(path: str) -> FileResponse:
    return _safe_asset(BASE_DIR / "css", path, "text/css")


@APP.get("/img/{path:path}")
async def img_asset(path: str) -> FileResponse:
    return _safe_asset(BASE_DIR / "img", path)


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    try:
        if enabled:
            nc.ui.top_menu.register("main", "Сбор файлов ОО")
            nc.ui.resources.set_script("top_menu", "main", "js/monitoringi-main")
            nc.ui.resources.set_style("top_menu", "main", "css/monitoringi")
            nc.ui.files_dropdown_menu.register_ex(
                "open_oo_file_collector",
                "Сбор файлов ОО",
                "api/files-actions/open",
                mime="httpd/unix-directory",
                permissions=31,
                order=80,
            )
            nc.log(LogLvl.WARNING, "Monitoring Request Manager enabled")
        else:
            nc.ui.top_menu.unregister("main")
            nc.ui.files_dropdown_menu.unregister("open_oo_file_collector")
            nc.log(LogLvl.WARNING, "Monitoring Request Manager disabled")
    except Exception as exc:
        return str(exc)
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level=os.getenv("MRM_LOG_LEVEL", "info"))
