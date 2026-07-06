"""Application services used by API routes."""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from typing import Any

from openpyxl import load_workbook
from sqlmodel import Session, delete, select

from .algorithms import FileCandidate, classify_files, default_organization_aliases, extension_of, organization_sort_key, plan_renames
from .config import SETTINGS
from .models import (
    AuditLog,
    BatchStatus,
    Campaign,
    CampaignOrgLayout,
    CampaignOrganization,
    CampaignOrganizationStatus,
    CampaignUploadMode,
    FileClassification,
    FileRecord,
    OperationAction,
    OperationBatch,
    OperationItem,
    OperationItemStatus,
    Organization,
    SettingsEntry,
    utcnow,
)
from .nextcloud import NextcloudClient, WebDAVEntry
from .reports import render_template
from .security import campaign_folder_path, ensure_under_root, join_nc_path, organization_folder_path

RUSSIAN_COLUMNS = {
    "Тип ОУ": "type",
    "Тип": "type",
    "Номер": "code",
    "Префикс": "prefix",
    "Код": "code",
    "Название": "display_name",
    "Email": "email",
    "Почта": "email",
    "Доп. почты": "extra_emails",
    "Алиасы": "aliases",
    "Активна": "active",
    "Примечание": "notes",
}

ORGANIZATION_TYPE_LABELS = {
    "school": "Школы",
    "kindergarten": "Сады",
    "odo": "ОДО",
    "udod": "УДОДы",
}
ORGANIZATION_TYPE_ORDER = ("school", "kindergarten", "odo", "udod", "other")

ORGANIZATION_TYPE_ALIASES = {
    "school": "school",
    "schools": "school",
    "школа": "school",
    "школы": "school",
    "сад": "kindergarten",
    "сады": "kindergarten",
    "детский сад": "kindergarten",
    "kindergarten": "kindergarten",
    "odo": "odo",
    "одо": "odo",
    "udod": "udod",
    "удод": "udod",
    "удоды": "udod",
}


def organization_type_aliases(value: Any) -> list[str]:
    return default_organization_aliases(normalize_organization_type(value))


def aliases_or_type_defaults(org_type: Any, aliases: list[Any] | None) -> list[str]:
    cleaned = [str(item).strip() for item in aliases or [] if str(item).strip()]
    return cleaned or organization_type_aliases(org_type)


def normalize_organization_type(value: Any) -> str:
    raw = value.value if hasattr(value, "value") else value
    key = str(raw or "school").strip()
    if "." in key:
        key = key.rsplit(".", 1)[-1]
    key = key.casefold()
    normalized = ORGANIZATION_TYPE_ALIASES.get(key, key or "school")
    if normalized in ORGANIZATION_TYPE_LABELS or normalized == "other":
        return normalized
    return "school"


def organization_type_label(value: Any) -> str:
    normalized = normalize_organization_type(value)
    return ORGANIZATION_TYPE_LABELS.get(normalized, str(value or ""))


def organization_type_value(organization: Organization) -> str:
    return organization.type.value if hasattr(organization.type, "value") else str(organization.type)


def campaign_type_values(campaign: Campaign, organizations: list[Organization] | None = None) -> list[str]:
    selected = [
        str(item.value if hasattr(item, "value") else item)
        for item in (campaign.selected_org_types or [])
        if str(item.value if hasattr(item, "value") else item)
    ]
    if selected:
        return list(dict.fromkeys(selected))
    values = {organization_type_value(org) for org in organizations or []}
    return sorted(values, key=lambda item: ORGANIZATION_TYPE_ORDER.index(item) if item in ORGANIZATION_TYPE_ORDER else len(ORGANIZATION_TYPE_ORDER))


def campaign_upload_mode(campaign: Campaign) -> str:
    value = getattr(campaign, "upload_mode", None)
    text = str(value.value if hasattr(value, "value") else value or CampaignUploadMode.organization.value)
    allowed = {item.value for item in CampaignUploadMode}
    return text if text in allowed else CampaignUploadMode.organization.value


def campaign_org_layout(campaign: Campaign) -> str:
    value = getattr(campaign, "org_folder_layout", None)
    text = str(value.value if hasattr(value, "value") else value or CampaignOrgLayout.auto.value)
    allowed = {item.value for item in CampaignOrgLayout}
    return text if text in allowed else CampaignOrgLayout.auto.value


def campaign_uses_organization_folders(campaign: Campaign) -> bool:
    return campaign_upload_mode(campaign) == CampaignUploadMode.organization.value


def campaign_uses_type_folders(campaign: Campaign, organizations: list[Organization] | None = None) -> bool:
    mode = campaign_upload_mode(campaign)
    if mode == CampaignUploadMode.campaign_root.value:
        return False
    if mode == CampaignUploadMode.org_type.value:
        return bool(campaign_type_values(campaign, organizations))
    layout = campaign_org_layout(campaign)
    if layout == CampaignOrgLayout.flat.value:
        return False
    if layout == CampaignOrgLayout.by_type.value:
        return bool(campaign_type_values(campaign, organizations))
    return len(campaign_type_values(campaign, organizations)) > 1


def current_user_id(headers: dict[str, str]) -> str | None:
    for key in ("x-nextcloud-user-id", "x-nextcloud-user", "remote-user"):
        value = headers.get(key) or headers.get(key.title())
        if value:
            return value
    return None


def audit(
    session: Session,
    *,
    user_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    result: str = "success",
    error: str | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_json=before,
            after_json=after,
            result=result,
            error=error,
        )
    )


def settings_map(session: Session, *, include_secret: bool = False) -> dict[str, Any]:
    result = {entry.key: entry.value_json for entry in session.exec(select(SettingsEntry)).all()}
    if "nc_app_password" in result and result["nc_app_password"] and not include_secret:
        result["nc_app_password"] = "********"
    return result


def update_settings(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    for key, value in values.items():
        if value is None:
            continue
        entry = session.get(SettingsEntry, key)
        if entry is None:
            session.add(SettingsEntry(key=key, value_json=value, updated_at=utcnow()))
        else:
            entry.value_json = value
            entry.updated_at = utcnow()
            session.add(entry)
    session.commit()
    return settings_map(session)


def nextcloud_overrides(session: Session, user_id: str | None = None) -> dict[str, Any]:
    values = settings_map(session, include_secret=True)
    use_legacy_credentials = not SETTINGS.app_secret
    return {
        "nc_base_url": values.get("nc_base_url"),
        "nc_user": user_id or (values.get("nc_user") if use_legacy_credentials else None),
        "nc_app_password": values.get("nc_app_password") if use_legacy_credentials else None,
        "root_path": values.get("root_path"),
    }


def parse_deadline(value: date | datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59))
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        return datetime.combine(datetime.fromisoformat(text).date(), time(23, 59))
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def deadline_storage(value: date | datetime | str | None) -> str | None:
    parsed = parse_deadline(value)
    return parsed.isoformat(timespec="minutes") if parsed else None


def deadline_text(value: date | datetime | str | None) -> str:
    parsed = parse_deadline(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else ""


def default_template_values(
    campaign: Campaign,
    organization: Organization,
    campaign_org: CampaignOrganization,
) -> dict[str, str | None]:
    org_type = organization_type_value(organization)
    org_type_label = organization_type_label(org_type)
    return {
        "campaign_title": campaign.title,
        "campaign_date": campaign.date.isoformat(),
        "deadline": deadline_text(campaign.deadline),
        "organization_name": organization.display_name,
        "organization_type": org_type,
        "organization_type_label": org_type_label,
        "organization_type_folder": org_type_label,
        "organization_code": organization.code,
        "public_link": campaign_org.public_link,
        "folder_path": campaign_org.folder_path,
    }


def parse_organization_table(filename: str, content: bytes) -> list[dict[str, Any]]:
    if filename.lower().endswith(".xlsx"):
        return _parse_xlsx(content)
    return _parse_csv(content)


def _normalize_header(header: str) -> str:
    stripped = header.strip()
    return RUSSIAN_COLUMNS.get(stripped, stripped)


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _parse_bool(value: Any) -> bool:
    if value is None or value == "":
        return True
    return str(value).strip().casefold() not in {"0", "false", "нет", "no", "inactive", "неактивна"}


def _parse_xlsx(content: bytes) -> list[dict[str, Any]]:
    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    headers = [_normalize_header(str(cell.value or "")) for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    rows: list[dict[str, Any]] = []
    for row in sheet.iter_rows(min_row=2):
        raw = {headers[index]: cell.value for index, cell in enumerate(row) if index < len(headers)}
        normalized = _normalize_import_row(raw)
        if normalized:
            rows.append(normalized)
    return rows


def _parse_csv(content: bytes) -> list[dict[str, Any]]:
    text: str | None = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("utf-8", errors="replace")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel()
        dialect.delimiter = ";" if ";" in sample else ","
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    rows = []
    for raw in reader:
        normalized = _normalize_import_row({_normalize_header(k or ""): v for k, v in raw.items()})
        if normalized:
            rows.append(normalized)
    return rows


def _normalize_import_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    code = str(raw.get("code") or "").strip()
    display_name = str(raw.get("display_name") or "").strip()
    if not code and not display_name:
        return None
    code = code or display_name
    display_name = display_name or code
    org_type = normalize_organization_type(raw.get("type"))
    aliases = _split_list(raw.get("aliases"))
    return {
        "type": org_type,
        "prefix": str(raw.get("prefix") or "").strip() or None,
        "code": code,
        "display_name": display_name,
        "short_name": str(raw.get("short_name") or "").strip() or None,
        "email": str(raw.get("email") or "").strip() or None,
        "extra_emails": _split_list(raw.get("extra_emails")),
        "aliases": aliases_or_type_defaults(org_type, aliases),
        "active": _parse_bool(raw.get("active")),
        "notes": str(raw.get("notes") or "").strip() or None,
    }


def validate_organization_rows(session: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_type_code: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    existing = session.exec(select(Organization)).all()
    existing_type_code = {(org.type.value, org.code): org.id for org in existing}
    for index, row in enumerate(rows, start=2):
        errors: list[str] = []
        type_code = (str(row.get("type")), str(row.get("code")))
        if not row.get("code"):
            errors.append("EMPTY_CODE")
        if type_code in seen_type_code:
            errors.append("DUPLICATE_ORGANIZATION_IN_FILE")
        if type_code in existing_type_code:
            errors.append("DUPLICATE_ORGANIZATION")
        seen_type_code.add(type_code)
        result.append({"row_number": index, "data": row, "errors": errors})
    return result


def apply_organization_import(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    for row in rows:
        existing = session.exec(
            select(Organization).where(Organization.type == row["type"], Organization.code == row["code"])
        ).first()
        if existing is None:
            session.add(Organization(**row))
            created += 1
        else:
            for key, value in row.items():
                setattr(existing, key, value)
            existing.updated_at = utcnow()
            session.add(existing)
            updated += 1
    session.commit()
    return {"created": created, "updated": updated}


def organization_folder_name(organization: Organization) -> str:
    return organization.code or organization.display_name


def campaign_type_folder_path(campaign: Campaign, type_value: Any) -> str:
    return join_nc_path(campaign.campaign_folder_path, organization_type_label(type_value))


def campaign_organization_folder_path(campaign: Campaign, organization: Organization, *, use_type_folder: bool) -> str:
    if use_type_folder:
        return join_nc_path(campaign_type_folder_path(campaign, organization_type_value(organization)), organization_folder_name(organization))
    return organization_folder_path(campaign.campaign_folder_path, organization_folder_name(organization))


def campaign_organization_upload_path(
    campaign: Campaign,
    organization: Organization,
    organizations: list[Organization] | None = None,
) -> str:
    mode = campaign_upload_mode(campaign)
    if mode == CampaignUploadMode.campaign_root.value:
        return campaign.campaign_folder_path
    if mode == CampaignUploadMode.org_type.value:
        return campaign_type_folder_path(campaign, organization_type_value(organization))
    return campaign_organization_folder_path(
        campaign,
        organization,
        use_type_folder=campaign_uses_type_folders(campaign, organizations),
    )


def _assign_campaign_org_path(
    item: CampaignOrganization,
    folder_path: str,
    *,
    reset_links_on_change: bool = True,
) -> None:
    if item.folder_path == folder_path:
        return
    item.folder_path = folder_path
    if reset_links_on_change:
        item.share_id = None
        item.public_link = None
        item.mail_subject = None
        item.mail_body = None
        if item.status != CampaignOrganizationStatus.pending:
            item.status = CampaignOrganizationStatus.pending
    item.updated_at = utcnow()


def create_campaign_orgs(session: Session, campaign: Campaign, organizations: list[Organization]) -> list[CampaignOrganization]:
    existing = {
        item.organization_id: item
        for item in session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    }
    result: list[CampaignOrganization] = []
    for org in organizations:
        folder_path = campaign_organization_upload_path(campaign, org, organizations)
        item = existing.get(org.id)
        if item is None:
            item = CampaignOrganization(campaign_id=campaign.id, organization_id=org.id, folder_path=folder_path)
        else:
            _assign_campaign_org_path(item, folder_path)
        session.add(item)
        result.append(item)
    session.commit()
    return result


def sync_campaign_orgs(session: Session, campaign: Campaign, organizations: list[Organization]) -> list[CampaignOrganization]:
    existing = {
        item.organization_id: item
        for item in session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    }
    changed = False
    for org in organizations:
        folder_path = campaign_organization_upload_path(campaign, org, organizations)
        if org.id in existing:
            before = existing[org.id].folder_path
            _assign_campaign_org_path(existing[org.id], folder_path)
            if existing[org.id].folder_path != before:
                session.add(existing[org.id])
                changed = True
            continue
        item = CampaignOrganization(
            campaign_id=campaign.id,
            organization_id=org.id,
            folder_path=folder_path,
        )
        session.add(item)
        existing[org.id] = item
        changed = True
    if changed:
        session.commit()
    return list(existing.values())


def create_batch(
    session: Session,
    *,
    operation_type: str,
    user_id: str | None,
    campaign_id: str | None = None,
    items: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> OperationBatch:
    batch = OperationBatch(
        campaign_id=campaign_id,
        operation_type=operation_type,
        created_by=user_id,
        summary_json=summary or {},
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    for item in items or []:
        session.add(
            OperationItem(
                batch_id=batch.id,
                action=item["action"],
                source_path=item.get("source_path"),
                target_path=item.get("target_path"),
                reason=item.get("reason"),
                metadata_json=item.get("metadata_json") or {},
            )
        )
    session.commit()
    session.refresh(batch)
    return batch


def batch_payload(session: Session, batch: OperationBatch) -> dict[str, Any]:
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
    return {
        "batch_id": batch.id,
        "operation_type": batch.operation_type,
        "status": batch.status.value if hasattr(batch.status, "value") else str(batch.status),
        "summary": batch.summary_json,
        "items": [item.model_dump(mode="json") for item in items],
    }


async def apply_batch(session: Session, batch: OperationBatch, client: NextcloudClient) -> OperationBatch:
    if batch.status != BatchStatus.preview:
        return batch
    batch.status = BatchStatus.running
    batch.started_at = utcnow()
    session.add(batch)
    session.commit()
    success = 0
    failed = 0
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
    for item in items:
        try:
            if item.action == OperationAction.create_folder:
                await client.mkcol(item.target_path or item.source_path or "")
            elif item.action in {OperationAction.rename, OperationAction.move}:
                await client.move(item.source_path or "", item.target_path or "")
            elif item.action == OperationAction.copy:
                await client.copy(item.source_path or "", item.target_path or "")
            elif item.action == OperationAction.delete:
                await client.delete(item.source_path or "")
            else:
                item.status = OperationItemStatus.skipped
                item.reason = item.reason or "not_applicable"
                session.add(item)
                continue
            item.status = OperationItemStatus.success
            item.error = None
            success += 1
        except Exception as exc:  # noqa: BLE001 - preserve per-item errors
            item.status = OperationItemStatus.failed
            item.error = str(exc)
            failed += 1
        item.updated_at = utcnow()
        session.add(item)
        session.commit()
    batch.finished_at = utcnow()
    batch.status = BatchStatus.completed if failed == 0 else BatchStatus.completed_with_errors
    batch.summary_json = {**(batch.summary_json or {}), "success": success, "failed": failed}
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def campaign_share_targets(
    campaign: Campaign,
    campaign_orgs: list[CampaignOrganization],
    organizations: list[Organization] | None = None,
) -> list[dict[str, Any]]:
    org_by_id = {org.id: org for org in organizations or []}
    mode = campaign_upload_mode(campaign)
    linked_items = [item for item in campaign_orgs if item.organization_id in org_by_id or not org_by_id]
    if mode == CampaignUploadMode.campaign_root.value:
        if not linked_items:
            return []
        return [
            {
                "scope": CampaignUploadMode.campaign_root.value,
                "target_path": campaign.campaign_folder_path,
                "label": "Корень мониторинга",
                "campaign_org_ids": [item.id for item in linked_items],
            }
        ]
    if mode == CampaignUploadMode.org_type.value:
        grouped: dict[str, list[str]] = {}
        for item in campaign_orgs:
            org = org_by_id.get(item.organization_id)
            if org is None:
                continue
            grouped.setdefault(organization_type_value(org), []).append(item.id)
        result: list[dict[str, Any]] = []
        for type_value in campaign_type_values(campaign, organizations):
            ids = grouped.get(type_value) or []
            if not ids:
                continue
            result.append(
                {
                    "scope": CampaignUploadMode.org_type.value,
                    "target_path": campaign_type_folder_path(campaign, type_value),
                    "label": organization_type_label(type_value),
                    "organization_type": type_value,
                    "organization_type_folder": organization_type_label(type_value),
                    "campaign_org_ids": ids,
                }
            )
        return result
    return [
        {
            "scope": CampaignUploadMode.organization.value,
            "target_path": item.folder_path,
            "label": item.folder_path,
            "campaign_org_ids": [item.id],
            "organization_id": item.organization_id,
        }
        for item in campaign_orgs
    ]


def planned_folder_items(
    campaign: Campaign,
    campaign_orgs: list[CampaignOrganization],
    organizations: list[Organization] | None = None,
    existing_folder_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    org_by_id = {org.id: org for org in organizations or []}
    items: list[dict[str, Any]] = []
    use_type_folder = campaign_uses_type_folders(campaign, organizations)
    if use_type_folder:
        for type_value in campaign_type_values(campaign, organizations):
            type_folder = organization_type_label(type_value)
            target_path = campaign_type_folder_path(campaign, type_value)
            if existing_folder_paths is not None and target_path in existing_folder_paths:
                continue
            items.append(
                {
                    "action": "create_folder",
                    "target_path": target_path,
                    "reason": "organization_type_folder",
                    "metadata_json": {"organization_type": type_value, "organization_type_folder": type_folder},
                }
            )
    if not campaign_uses_organization_folders(campaign):
        return items
    for item in campaign_orgs:
        org = org_by_id.get(item.organization_id)
        desired_path = campaign_organization_upload_path(campaign, org, organizations) if org else item.folder_path
        metadata = {"campaign_org_id": item.id, "organization_id": item.organization_id}
        if org:
            metadata.update(
                {
                    "organization_code": org.code,
                    "organization_name": org.display_name,
                    "organization_type": organization_type_value(org),
                    "organization_type_folder": organization_type_label(organization_type_value(org)),
                }
            )
        if item.folder_path != desired_path:
            if existing_folder_paths is not None:
                source_exists = item.folder_path in existing_folder_paths
                desired_exists = desired_path in existing_folder_paths
                if desired_exists or not source_exists:
                    items.append(
                        {
                            "action": "create_folder",
                            "target_path": desired_path,
                            "reason": "organization_folder",
                            "metadata_json": metadata,
                        }
                    )
                    continue
            elif item.status == CampaignOrganizationStatus.pending:
                items.append(
                    {
                        "action": "create_folder",
                        "target_path": desired_path,
                        "reason": "organization_folder",
                        "metadata_json": metadata,
                    }
                )
                continue
            items.append(
                {
                    "action": "move",
                    "source_path": item.folder_path,
                    "target_path": desired_path,
                    "reason": "organization_folder_rename",
                    "metadata_json": metadata,
                }
            )
            continue
        if existing_folder_paths is not None and desired_path not in existing_folder_paths:
            items.append(
                {
                    "action": "create_folder",
                    "target_path": desired_path,
                    "reason": "organization_folder",
                    "metadata_json": metadata,
                }
            )
            continue
        if item.status in {CampaignOrganizationStatus.folder_created, CampaignOrganizationStatus.link_created}:
            continue
        items.append(
            {
                "action": "create_folder",
                "target_path": item.folder_path,
                "reason": "organization_folder",
                "metadata_json": metadata,
            }
        )
    return items


def planned_rename_items(root_path: str, paths: list[str]) -> list[dict[str, Any]]:
    planned = []
    for item in plan_renames(paths):
        source = ensure_under_root(item["source_path"], root_path)
        target = ensure_under_root(item["target_path"], root_path)
        planned.append({**item, "source_path": source, "target_path": target})
    return planned


def entries_to_candidates(entries: list[WebDAVEntry], *, skip_root: str) -> list[FileCandidate]:
    root = ensure_under_root(skip_root, skip_root)
    candidates = []
    for entry in entries:
        if entry.is_collection or entry.path == root:
            continue
        candidates.append(
            FileCandidate(
                path=entry.path,
                name=entry.name,
                size=entry.size,
                mtime=entry.mtime,
                mime_type=entry.mime_type,
            )
        )
    return candidates


def persist_scan_results(
    session: Session,
    campaign: Campaign,
    classified,
    campaign_orgs: list[CampaignOrganization],
    current_paths: dict[str, str] | None = None,
) -> None:
    current_paths = current_paths or {}
    session.exec(delete(FileRecord).where(FileRecord.campaign_id == campaign.id))
    org_status = {item.organization_id: item for item in campaign_orgs}
    uploaded_org_ids: set[str] = set()
    rejected_org_ids: set[str] = set()
    for item in classified:
        current_path = current_paths.get(item.candidate.path, item.candidate.path)
        if item.organization_id and item.classification == "valid":
            uploaded_org_ids.add(item.organization_id)
        elif item.organization_id:
            rejected_org_ids.add(item.organization_id)
        session.add(
            FileRecord(
                campaign_id=campaign.id,
                organization_id=item.organization_id,
                original_path=item.candidate.path,
                current_path=current_path,
                original_name=item.candidate.name,
                current_name=PurePosixPath(current_path).name,
                extension=extension_of(item.normalized_name),
                mime_type=item.candidate.mime_type,
                size=item.candidate.size,
                mtime=item.candidate.mtime,
                classification=FileClassification(item.classification),
                reason=item.reason,
            )
        )
    for org_id, campaign_org in org_status.items():
        if org_id in uploaded_org_ids:
            campaign_org.status = CampaignOrganizationStatus.uploaded
            campaign_org.last_file_detected_at = utcnow()
        elif org_id in rejected_org_ids:
            campaign_org.status = CampaignOrganizationStatus.rejected
        else:
            campaign_org.status = CampaignOrganizationStatus.missing
        campaign_org.updated_at = utcnow()
        session.add(campaign_org)
    session.commit()


def missing_organizations(
    session: Session,
    campaign: Campaign,
) -> list[Organization]:
    campaign_orgs = session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    org_ids = [item.organization_id for item in campaign_orgs if item.status == CampaignOrganizationStatus.missing]
    if not org_ids:
        return []
    return session.exec(select(Organization).where(Organization.id.in_(org_ids))).all()


def missing_by_type(organizations: list[Organization]) -> dict[str, list[Organization]]:
    result: dict[str, list[Organization]] = {}
    for org in organizations:
        key = org.type.value if hasattr(org.type, "value") else str(org.type)
        result.setdefault(key, []).append(org)
    for items in result.values():
        items.sort(key=organization_sort_key)
    return result


def mail_values_for_campaign_org(
    campaign: Campaign,
    organization: Organization,
    campaign_org: CampaignOrganization,
    subject_template: str,
    body_template: str,
) -> tuple[str, str]:
    values = default_template_values(campaign, organization, campaign_org)
    return render_template(subject_template, values), render_template(body_template, values)


def target_invalid_path(root_folder: str, source_path: str, invalid_folder: str, reason: str) -> str:
    source = PurePosixPath(source_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_name = f"{source.stem}_{stamp}{source.suffix}"
    return join_nc_path(root_folder, invalid_folder, reason, target_name)
