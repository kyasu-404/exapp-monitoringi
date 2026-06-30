"""Pydantic request models."""

from __future__ import annotations

from datetime import date as Date
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from .models import OrganizationType


class OrganizationIn(BaseModel):
    type: OrganizationType = OrganizationType.other
    prefix: str | None = None
    code: str
    display_name: str
    short_name: str | None = None
    email: EmailStr | None = None
    extra_emails: list[EmailStr] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    active: bool = True
    sort_order: int = 0
    notes: str | None = None


class OrganizationPatch(BaseModel):
    type: OrganizationType | None = None
    prefix: str | None = None
    code: str | None = None
    display_name: str | None = None
    short_name: str | None = None
    email: EmailStr | None = None
    extra_emails: list[EmailStr] | None = None
    aliases: list[str] | None = None
    active: bool | None = None
    sort_order: int | None = None
    notes: str | None = None


class CampaignIn(BaseModel):
    title: str
    description: str | None = None
    date: Date
    deadline: Date | None = None
    parent_nc_path: str = "/Мониторинги"
    selected_org_types: list[OrganizationType] = Field(default_factory=list)
    allowed_extensions: list[str] = Field(default_factory=lambda: ["pdf", "docx", "xlsx"])
    allowed_mime_types: list[str] = Field(default_factory=list)


class CampaignPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    date: Date | None = None
    deadline: Date | None = None
    parent_nc_path: str | None = None
    selected_org_types: list[OrganizationType] | None = None
    allowed_extensions: list[str] | None = None
    allowed_mime_types: list[str] | None = None
    status: str | None = None


class SelectOrganizationsIn(BaseModel):
    organization_ids: list[str] | None = None
    organization_types: list[OrganizationType] | None = None


class ShareOptionsIn(BaseModel):
    public_upload: bool = True
    expire_date: Date | None = None
    password: str | None = None
    permissions: int = 4


class ScanIn(BaseModel):
    path: str | None = None
    allowed_extensions: list[str] | None = None


class BulkPathsIn(BaseModel):
    paths: list[str] = Field(default_factory=list)
    campaign_id: str | None = None
    batch_id: str | None = None


class SettingsIn(BaseModel):
    nc_base_url: str | None = None
    nc_user: str | None = None
    nc_app_password: str | None = None
    root_path: str | None = None
    duplicates_folder: str | None = None
    invalid_folder: str | None = None
    archive_folder: str | None = None
    default_extensions: list[str] | None = None
    public_upload_default: bool | None = None
    share_permissions: int | None = None
    max_file_size: int | None = None


class OperationPreview(BaseModel):
    batch_id: str
    operation_type: str
    status: str
    summary: dict[str, Any]
    items: list[dict[str, Any]]
