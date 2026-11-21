"""Database lifecycle helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import SETTINGS
from .models import MailTemplate, SettingsEntry, utcnow

_engine = None


DEFAULT_MAIL_SUBJECT = 'Запрос "{campaign_title}" до {deadline}'
LEGACY_DEFAULT_MAIL_BODY = """Уважаемые коллеги!

Просим загрузить документы по запросу "{campaign_title}" от {campaign_date} до {deadline}.

Папка организации: {folder_path}
Ссылка для загрузки: {public_link}

Спасибо!"""

DEFAULT_MAIL_BODY = """Уважаемые коллеги!

Просим загрузить документы по запросу "{campaign_title}" от {campaign_date} до {deadline}.

Раздел мониторинга: {organization_type_folder}
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


def _ensure_campaign_columns(session: Session) -> None:
    columns = {column["name"] for column in inspect(session.connection()).get_columns("campaign")}
    if "participant_selection_explicit" not in columns:
        session.exec(text("ALTER TABLE campaign ADD COLUMN participant_selection_explicit BOOLEAN DEFAULT FALSE NOT NULL"))
        session.exec(text(
            "UPDATE campaign SET participant_selection_explicit = TRUE WHERE EXISTS ("
            "SELECT 1 FROM auditlog WHERE auditlog.entity_type = 'campaign' "
            "AND auditlog.entity_id = campaign.id AND auditlog.action = 'select_organizations')"
        ))
    if "upload_mode" not in columns:
        session.exec(text("ALTER TABLE campaign ADD COLUMN upload_mode VARCHAR DEFAULT 'organization' NOT NULL"))
    if "org_folder_layout" not in columns:
        session.exec(text("ALTER TABLE campaign ADD COLUMN org_folder_layout VARCHAR DEFAULT 'auto' NOT NULL"))

    if "folder_file_id" not in columns:
        session.exec(text("ALTER TABLE campaign ADD COLUMN folder_file_id VARCHAR"))
    if "folder_conflict" not in columns:
        session.exec(text("ALTER TABLE campaign ADD COLUMN folder_conflict BOOLEAN DEFAULT false NOT NULL"))
    # Idempotent if an earlier startup stopped between ALTER and index creation.
    session.exec(text("""
        UPDATE campaign SET folder_conflict = true
        WHERE folder_conflict = false AND campaign_folder_path IN (
            SELECT campaign_folder_path FROM campaign GROUP BY campaign_folder_path HAVING count(*) > 1
        )
    """))
    session.exec(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_root_path
        ON campaign (campaign_folder_path) WHERE folder_conflict = false
    """))
    session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_root_file_id ON campaign (folder_file_id)"))


def init_db() -> None:
    if SETTINGS.database_url.startswith("sqlite:///"):
        db_path = Path(SETTINGS.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())
    with Session(get_engine()) as session:
        _ensure_campaign_columns(session)
        if session.get_bind().dialect.name == "postgresql":
            session.exec(text("ALTER TYPE campaignorganizationstatus ADD VALUE IF NOT EXISTS 'unverified'"))
            session.exec(text("ALTER TYPE mailitemstatus ADD VALUE IF NOT EXISTS 'unknown'"))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_type_code ON organization (type, code)"))
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
        elif default_template.body_template == LEGACY_DEFAULT_MAIL_BODY:
            default_template.body_template = DEFAULT_MAIL_BODY
            default_template.updated_at = utcnow()
            session.add(default_template)
        for key, value in {
            "access_mode": "webdav_only",
            "root_path": SETTINGS.root_path,
            "duplicates_folder": SETTINGS.duplicates_folder,
            "invalid_folder": SETTINGS.invalid_folder,
            "archive_folder": SETTINGS.archive_folder,
            "collaboration_group": "",
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
