"""Monitoring Request Manager FastAPI entrypoint."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, run_app, set_handlers

from monitoringi.api import router as api_router
from monitoringi.db import init_db
from monitoringi.errors import AppError

BASE_DIR = Path(__file__).resolve().parent.parent


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
    )


@APP.get("/js/{path:path}")
async def js_asset(path: str) -> FileResponse:
    return FileResponse(BASE_DIR / "js" / path, media_type="application/javascript")


@APP.get("/css/{path:path}")
async def css_asset(path: str) -> FileResponse:
    return FileResponse(BASE_DIR / "css" / path, media_type="text/css")


@APP.get("/img/{path:path}")
async def img_asset(path: str) -> FileResponse:
    return FileResponse(BASE_DIR / "img" / path)


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    try:
        if enabled:
            nc.log(LogLvl.WARNING, "Monitoring Request Manager enabled")
        else:
            nc.log(LogLvl.WARNING, "Monitoring Request Manager disabled")
    except Exception:
        return ""
    return ""


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level=os.getenv("MRM_LOG_LEVEL", "info"))

