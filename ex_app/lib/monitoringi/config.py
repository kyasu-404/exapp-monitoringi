"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_storage_dir() -> Path:
    return Path(
        os.getenv("APP_PERSISTENT_STORAGE")
        or os.getenv("MRM_STORAGE_DIR")
        or os.getenv("APP_DATA_DIR")
        or "/tmp/monitoring-request-manager"
    )


@dataclass(frozen=True)
class Settings:
    storage_dir: Path
    database_url: str
    nc_base_url: str | None
    nc_user: str | None
    nc_app_password: str | None
    app_secret: str | None
    app_id: str | None
    app_version: str | None
    aa_version: str | None
    root_path: str
    duplicates_folder: str
    invalid_folder: str
    archive_folder: str
    default_extensions: tuple[str, ...]
    max_webdav_parallelism: int
    request_timeout: float


def load_settings() -> Settings:
    storage_dir = _default_storage_dir()
    database_url = os.getenv("MRM_DATABASE_URL") or f"sqlite:///{storage_dir / 'monitoringi.sqlite3'}"
    extensions = os.getenv("MRM_DEFAULT_EXTENSIONS", "pdf,doc,docx,xls,xlsx,odt,ods,txt")
    return Settings(
        storage_dir=storage_dir,
        database_url=database_url,
        nc_base_url=os.getenv("MRM_NC_BASE_URL") or os.getenv("NEXTCLOUD_URL"),
        nc_user=os.getenv("MRM_NC_USER"),
        nc_app_password=os.getenv("MRM_NC_APP_PASSWORD"),
        app_secret=os.getenv("APP_SECRET"),
        app_id=os.getenv("APP_ID"),
        app_version=os.getenv("APP_VERSION"),
        aa_version=os.getenv("AA_VERSION"),
        root_path=os.getenv("MRM_ROOT_PATH", "/Мониторинги"),
        duplicates_folder=os.getenv("MRM_DUPLICATES_FOLDER", "_Дубли"),
        invalid_folder=os.getenv("MRM_INVALID_FOLDER", "_Неподходящие"),
        archive_folder=os.getenv("MRM_ARCHIVE_FOLDER", "_Архив"),
        default_extensions=tuple(ext.strip().lower() for ext in extensions.split(",") if ext.strip()),
        max_webdav_parallelism=max(1, int(os.getenv("MRM_WEBDAV_PARALLELISM", "4"))),
        request_timeout=float(os.getenv("MRM_REQUEST_TIMEOUT", "30")),
    )


SETTINGS = load_settings()
