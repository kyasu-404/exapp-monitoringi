"""Database models."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import Column
from sqlalchemy import JSON as SAJSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class OrganizationType(str, Enum):
    school = "school"
    kindergarten = "kindergarten"
    odo = "odo"
    udod = "udod"
    other = "other"


class CampaignStatus(str, Enum):
    draft = "draft"
    folders_created = "folders_created"
    links_created = "links_created"
    active = "active"
    closed = "closed"
    archived = "archived"


class CampaignOrganizationStatus(str, Enum):
    pending = "pending"
    folder_created = "folder_created"
    link_created = "link_created"
    sent_manually = "sent_manually"
    uploaded = "uploaded"
    accepted = "accepted"
    rejected = "rejected"
    missing = "missing"


class FileClassification(str, Enum):
    valid = "valid"
    duplicate = "duplicate"
    trash = "trash"
    unknown = "unknown"
    invalid_extension = "invalid_extension"
    invalid_name = "invalid_name"


class BatchStatus(str, Enum):
    preview = "preview"
    running = "running"
    completed = "completed"
    completed_with_errors = "completed_with_errors"
    failed = "failed"
    cancelled = "cancelled"


class OperationAction(str, Enum):
    rename = "rename"
    move = "move"
    copy = "copy"
    delete = "delete"
    create_folder = "create_folder"
    create_share = "create_share"
    update_share = "update_share"
    classify = "classify"
    generate_report = "generate_report"


class OperationItemStatus(str, Enum):
    planned = "planned"
    skipped = "skipped"
    success = "success"
    failed = "failed"


class Organization(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    type: OrganizationType = Field(index=True)
    prefix: str | None = Field(default=None, index=True)
    code: str = Field(index=True)
    display_name: str = Field(index=True)
    short_name: str | None = None
    email: str | None = Field(default=None, index=True)
    extra_emails: list[str] = Field(default_factory=list, sa_column=Column(SAJSON))
    aliases: list[str] = Field(default_factory=list, sa_column=Column(SAJSON))
    active: bool = Field(default=True, index=True)
    sort_order: int = 0
    notes: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(index=True)
    description: str | None = None
    date: date
    deadline: date | None = None
    parent_nc_path: str
    campaign_folder_path: str
    selected_org_types: list[str] = Field(default_factory=list, sa_column=Column(SAJSON))
    allowed_extensions: list[str] = Field(default_factory=list, sa_column=Column(SAJSON))
    allowed_mime_types: list[str] = Field(default_factory=list, sa_column=Column(SAJSON))
    status: CampaignStatus = Field(default=CampaignStatus.draft, index=True)
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CampaignOrganization(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    campaign_id: str = Field(index=True, foreign_key="campaign.id")
    organization_id: str = Field(index=True, foreign_key="organization.id")
    folder_path: str
    share_id: str | None = None
    public_link: str | None = None
    mail_subject: str | None = None
    mail_body: str | None = None
    status: CampaignOrganizationStatus = Field(default=CampaignOrganizationStatus.pending, index=True)
    last_file_detected_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class FileRecord(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    campaign_id: str = Field(index=True, foreign_key="campaign.id")
    organization_id: str | None = Field(default=None, index=True, foreign_key="organization.id")
    original_path: str
    current_path: str
    original_name: str
    current_name: str
    extension: str
    mime_type: str | None = None
    size: int | None = None
    mtime: datetime | None = None
    checksum: str | None = None
    classification: FileClassification = Field(index=True)
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OperationBatch(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    campaign_id: str | None = Field(default=None, index=True, foreign_key="campaign.id")
    operation_type: str = Field(index=True)
    status: BatchStatus = Field(default=BatchStatus.preview, index=True)
    created_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(SAJSON))


class OperationItem(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(index=True, foreign_key="operationbatch.id")
    action: OperationAction = Field(index=True)
    source_path: str | None = None
    target_path: str | None = None
    status: OperationItemStatus = Field(default=OperationItemStatus.planned, index=True)
    reason: str | None = None
    error: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(SAJSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MailTemplate(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True)
    subject_template: str
    body_template: str
    is_default: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    user_id: str | None = Field(default=None, index=True)
    action: str = Field(index=True)
    entity_type: str = Field(index=True)
    entity_id: str | None = Field(default=None, index=True)
    before_json: dict[str, Any] | None = Field(default=None, sa_column=Column(SAJSON))
    after_json: dict[str, Any] | None = Field(default=None, sa_column=Column(SAJSON))
    result: str = Field(default="success", index=True)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SettingsEntry(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value_json: Any = Field(default=None, sa_column=Column(SAJSON))
    updated_at: datetime = Field(default_factory=utcnow)

