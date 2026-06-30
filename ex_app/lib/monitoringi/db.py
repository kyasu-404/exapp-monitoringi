"""Database lifecycle helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from .config import SETTINGS
from .models import MailTemplate, SettingsEntry, utcnow

_engine = None


DEFAULT_MAIL_SUBJECT = 'Запрос "{campaign_title}" до {deadline}'
DEFAULT_MAIL_BODY = """Уважаемые коллеги!

Просим загрузить документы по запросу "{campaign_title}" от {campaign_date} до {deadline}.

Папка организации: {folder_path}
Ссылка для загрузки: {public_link}

Спасибо!"""


def get_engine():
    global _engine
    if _engine is None:
        SETTINGS.storage_dir.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if SETTINGS.database_url.startswith("sqlite") else {}
        _engine = create_engine(SETTINGS.database_url, echo=False, connect_args=connect_args)
    return _engine


def init_db() -> None:
    if SETTINGS.database_url.startswith("sqlite:///"):
        db_path = Path(SETTINGS.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())
    with Session(get_engine()) as session:
        default_template = session.exec(select(MailTemplate).where(MailTemplate.is_default == True)).first()  # noqa: E712
        if default_template is None:
            session.add(
                MailTemplate(
                    name="Стандартный шаблон",
                    subject_template=DEFAULT_MAIL_SUBJECT,
                    body_template=DEFAULT_MAIL_BODY,
                    is_default=True,
                )
            )
        for key, value in {
            "access_mode": "webdav_only",
            "root_path": SETTINGS.root_path,
            "duplicates_folder": SETTINGS.duplicates_folder,
            "invalid_folder": SETTINGS.invalid_folder,
            "archive_folder": SETTINGS.archive_folder,
            "default_extensions": list(SETTINGS.default_extensions),
            "public_upload_default": True,
            "share_permissions": 4,
            "max_file_size": None,
        }.items():
            existing = session.get(SettingsEntry, key)
            if existing is None:
                session.add(SettingsEntry(key=key, value_json=value, updated_at=utcnow()))
        session.commit()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session

