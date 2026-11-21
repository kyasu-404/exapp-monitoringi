"""FastAPI routes for the ExApp backend."""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select, update

from .algorithms import (
    ClassifiedFile,
    FileCandidate,
    effective_organization_aliases,
    extract_organization,
    normalize_filename,
    normalize_text,
    normalized_organization_file_stem,
    organization_sort_key,
)
from .db import get_session
from .errors import AppError
from .mail_queue import queue_batch
from .models import (
    BatchStatus,
    Campaign,
    CampaignOrganization,
    CampaignOrganizationStatus,
    CampaignStatus,
    CheckResult,
    FileClassification,
    FileRecord,
    MailItemStatus,
    MessageBatch,
    MessageItem,
    OperationAction,
    OperationBatch,
    OperationItem,
    OperationItemStatus,
    Organization,
    SettingsEntry,
    utcnow,
)
from .nextcloud import NextcloudClient, WebDAVEntry, configured_credentials
from .reports import MAILING_COLUMNS, csv_bytes, link_columns, link_rows, mailing_rows, missing_message, report_workbook, xlsx_bytes
from .scans import execute_scan_plan, list_scan_folder, scan_plan_items, shared_files
from .schemas import (
    BulkPathsIn,
    CampaignIn,
    CampaignMoveIn,
    CampaignPatch,
    OrganizationImportJsonIn,
    OrganizationIn,
    OrganizationPatch,
    ScanIn,
    SelectOrganizationsIn,
    SettingsIn,
    ShareOptionsIn,
)
from .security import campaign_folder_path, clean_path_segment, ensure_under_root, join_nc_path, path_parts
from .services import (
    apply_batch,
    aliases_or_type_defaults,
    apply_organization_import,
    audit,
    batch_payload,
    campaign_organization_upload_path,
    campaign_org_layout,
    campaign_participant_paths,
    campaign_share_targets,
    campaign_type_values,
    campaign_upload_mode,
    campaign_uses_type_folders,
    create_batch,
    invalidate_participant_plans,
    deadline_storage,
    missing_file_mail_values,
    missing_by_type,
    missing_organizations,
    nextcloud_overrides,
    organization_type_label,
    parse_organization_table,
    persist_scan_results,
    planned_folder_items,
    planned_rename_items,
    settings_map,
    reconcile_campaign_orgs,
    remember_participant_folder,
    validate_participant_folders,
    target_invalid_path,
    upload_link_mail_values,
    update_settings,
    validate_organization_rows,
)

SessionDep = Annotated[Session, Depends(get_session)]
CLEANUP_DUPLICATES_FOLDER = "Дубли"
CLEANUP_TRASH_FOLDER = "Мусор"
CLEANUP_FOLDERS = {CLEANUP_DUPLICATES_FOLDER, CLEANUP_TRASH_FOLDER}
ROOT_REQUIRED_PERMISSIONS = {"G", "C", "K"}
GROUP_SHARE_PERMISSIONS = 31
PUBLIC_FILE_REQUEST_PERMISSIONS = 4
PUBLIC_EDIT_PERMISSIONS = 15
UNSAFE_DOWNLOAD_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def _user(request: Request) -> str:
    # AppAPIAuthMiddleware writes this value after validating the request signature.
    user_id = request.scope.get("username")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=401, detail="Не удалось подтвердить пользователя Nextcloud.")
    return user_id


async def require_operator(session: SessionDep, request: Request) -> bool:
    """Return whether the authorized caller is a Nextcloud administrator."""
    user_id = _user(request)
    async with NextcloudClient(configured_credentials({"nc_user": user_id})) as client:
        groups = await client.current_user_groups()
    if "admin" in groups:
        return True
    operator_group = session.get(SettingsEntry, "operator_group")
    group_id = operator_group.value_json if operator_group else None
    if isinstance(group_id, str) and group_id and group_id in groups:
        return False
    raise HTTPException(status_code=403, detail="Для доступа нужна группа операторов приложения.")


IsAdmin = Annotated[bool, Depends(require_operator)]


def require_admin(is_admin: IsAdmin) -> None:
    if not is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора Nextcloud.")


router = APIRouter(prefix="/api", dependencies=[Depends(require_operator)])


def _nc_credentials(session: Session, request: Request, *, root_path: str | None = None):
    overrides = nextcloud_overrides(session, _user(request))
    if root_path:
        overrides["root_path"] = root_path
    return configured_credentials(overrides)


def _raise(exc: AppError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
    ) from exc


def _model_json(model) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _download_filename(prefix: str, campaign: Campaign, extension: str) -> str:
    title = UNSAFE_DOWNLOAD_FILENAME_CHARS.sub("_", str(campaign.title or "").strip()).strip(" .")
    title = title or campaign.id
    return f"{prefix}-{title}.{extension.lstrip('.')}"


def _download_disposition(prefix: str, campaign: Campaign, extension: str) -> str:
    filename = _download_filename(prefix, campaign, extension)
    ascii_fallback = f"{prefix}.{extension.lstrip('.')}"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _effective_org_folder_layout(upload_mode: str, org_folder_layout: str | None) -> str:
    if upload_mode != "organization":
        return "auto"
    return org_folder_layout or "auto"


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def _folder_structure_value(campaign: Campaign, organizations: list[Organization]) -> str:
    mode = campaign_upload_mode(campaign)
    if mode == "campaign_root":
        return "campaign_root"
    return "by_type" if campaign_uses_type_folders(campaign, organizations) else "flat"


def _campaign_operation_context(campaign: Campaign, organizations: list[Organization]) -> dict[str, Any]:
    type_values = campaign_type_values(campaign, organizations)
    return {
        "path": campaign.campaign_folder_path,
        "deadline": campaign.deadline,
        "upload_mode": campaign_upload_mode(campaign),
        "org_folder_layout": campaign_org_layout(campaign),
        "folder_structure": _folder_structure_value(campaign, organizations),
        "organization_types": type_values,
        "organization_type_labels": [organization_type_label(item) for item in type_values],
    }


def _operation_items_payload(session: Session, batch_id: str) -> list[dict[str, Any]]:
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch_id).order_by(OperationItem.created_at)).all()
    return [item.model_dump(mode="json") for item in items]


def _operation_batch_history_payload(session: Session, batch: OperationBatch) -> dict[str, Any]:
    return {
        "run_id": batch.id,
        "batch_id": batch.id,
        "source": "operation_batch",
        "operation_type": batch.operation_type,
        "status": _enum_value(batch.status),
        "created_by": batch.created_by,
        "created_at": _dt(batch.created_at),
        "started_at": _dt(batch.started_at),
        "finished_at": _dt(batch.finished_at),
        "summary": batch.summary_json or {},
        "items": _operation_items_payload(session, batch.id),
    }


def _message_items_payload(session: Session, batch: MessageBatch) -> list[dict[str, Any]]:
    items = session.exec(select(MessageItem).where(MessageItem.batch_id == batch.id).order_by(MessageItem.created_at)).all()
    org_ids = [item.organization_id for item in items if item.organization_id]
    org_by_id = {org.id: org for org in session.exec(select(Organization).where(Organization.id.in_(org_ids))).all()} if org_ids else {}
    result: list[dict[str, Any]] = []
    for item in items:
        payload = item.model_dump(mode="json")
        org = org_by_id.get(item.organization_id or "")
        if org is not None:
            payload["organization_code"] = org.code
            payload["organization_name"] = org.display_name
        result.append(payload)
    return result


def _message_batch_history_payload(session: Session, batch: MessageBatch) -> dict[str, Any]:
    items = _message_items_payload(session, batch)
    preview_item = next((item for item in items if item.get("body")), None)
    check_result = session.get(CheckResult, batch.check_result_id)
    check_summary = check_result.summary_json if check_result else {}
    summary = dict(batch.summary_json or {})
    for key in ("missing", "missing_organization_names", "missing_organization_codes"):
        if key not in summary and key in check_summary:
            summary[key] = check_summary[key]
    if check_result and "check_result_created_at" not in summary:
        summary["check_result_created_at"] = check_result.created_at.isoformat()
    summary["preview_text"] = (preview_item or {}).get("body") or summary.get("preview_text") or ""
    return {
        "run_id": batch.id,
        "batch_id": batch.id,
        "message_batch_id": batch.id,
        "source": "message_batch",
        "operation_type": "messages",
        "status": _enum_value(batch.status),
        "created_by": batch.created_by,
        "created_at": _dt(batch.created_at),
        "started_at": _dt(batch.queued_at),
        "queued_at": _dt(batch.queued_at),
        "finished_at": _dt(batch.finished_at),
        "check_result_id": batch.check_result_id,
        "summary": summary,
        "items": items,
    }


def _check_result_history_payload(session: Session, result: CheckResult) -> dict[str, Any]:
    cleanup_batch = session.get(OperationBatch, result.operation_batch_id) if result.operation_batch_id else None
    summary = result.summary_json or {}
    status = "completed_with_errors" if summary.get("operation_errors") or summary.get("skipped") else "completed"
    return {
        "run_id": result.id,
        "check_result_id": result.id,
        "batch_id": result.operation_batch_id,
        "source": "check_result",
        "operation_type": "scan",
        "status": status,
        "created_by": result.created_by,
        "created_at": _dt(result.created_at),
        "started_at": _dt(result.created_at),
        "finished_at": _dt(result.created_at),
        "summary": result.summary_json or {},
        "cleanup_batch": _operation_batch_history_payload(session, cleanup_batch) if cleanup_batch else None,
    }


def _log_entry_from_run(run: dict[str, Any], tab: str) -> dict[str, Any]:
    return {
        "tab": tab,
        "run_id": run.get("run_id"),
        "operation_type": run.get("operation_type"),
        "status": run.get("status"),
        "created_by": run.get("created_by"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at") or run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "source": run.get("source"),
    }


def _public_share_options_for_scope(scope: str) -> dict[str, Any]:
    if scope == "organization":
        return {"public_upload": False, "permissions": PUBLIC_EDIT_PERMISSIONS, "file_request": False, "reason": "public_edit_link"}
    return {"public_upload": True, "permissions": PUBLIC_FILE_REQUEST_PERMISSIONS, "file_request": True, "reason": "public_upload_link"}


def _collaboration_group(settings: dict[str, Any]) -> str:
    return str(settings.get("collaboration_group") or "").strip()


def _delete_campaign_record(session: Session, campaign: Campaign, *, user_id: str | None = None, reason: str = "deleted") -> None:
    batches = session.exec(select(OperationBatch).where(OperationBatch.campaign_id == campaign.id)).all()
    batch_ids = [batch.id for batch in batches]
    if batch_ids:
        session.exec(delete(OperationItem).where(OperationItem.batch_id.in_(batch_ids)))
    session.exec(delete(OperationBatch).where(OperationBatch.campaign_id == campaign.id))
    session.exec(delete(FileRecord).where(FileRecord.campaign_id == campaign.id))
    session.exec(delete(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id))
    before = _model_json(campaign)
    session.delete(campaign)
    audit(
        session,
        user_id=user_id,
        action="delete_missing_folder",
        entity_type="campaign",
        entity_id=campaign.id,
        before=before,
        after={"reason": reason},
    )


async def _prune_missing_campaigns(session: Session, request: Request, campaigns: list[Campaign]) -> list[Campaign]:
    if all(campaign.folder_conflict for campaign in campaigns):
        return campaigns
    async with NextcloudClient(_nc_credentials(session, request)) as client:
        for campaign in campaigns:
            if campaign.folder_conflict:
                continue
            old_path = campaign.campaign_folder_path
            entry = await _campaign_folder_entry(client, old_path)
            if entry and entry.is_collection and (not campaign.folder_file_id or entry.file_id == campaign.folder_file_id):
                if entry.file_id and not campaign.folder_file_id:
                    _remember_campaign_folder_id(session, campaign, entry.file_id)
                    session.commit()
                continue
            moved_path = await _find_moved_campaign_folder(client, session, campaign)
            if moved_path:
                _rewrite_campaign_paths(session, campaign, old_path, moved_path)
                audit(
                    session, user_id=_user(request), action="relink_moved_folder", entity_type="campaign",
                    entity_id=campaign.id, before={"campaign_folder_path": old_path}, after={"campaign_folder_path": moved_path},
                )
                session.commit()
    return campaigns


def _folder_conflict(path: str) -> AppError:
    return AppError("CAMPAIGN_FOLDER_CONFLICT", "Папка уже используется другим мониторингом или была изменена.", 409, path)


def _root_overlaps_campaign(session: Session, path: str, *, exclude_id: str | None = None) -> bool:
    parents = [path, *(str(parent) for parent in PurePosixPath(path).parents)]
    query = select(Campaign.id).where(
        Campaign.campaign_folder_path.in_(parents) | Campaign.campaign_folder_path.startswith(path.rstrip("/") + "/", autoescape=True)
    )
    if exclude_id:
        query = query.where(Campaign.id != exclude_id)
    return session.exec(query.limit(1)).first() is not None


def _remember_campaign_folder_id(session: Session, campaign: Campaign, file_id: str) -> None:
    path = campaign.campaign_folder_path
    try:
        result = session.exec(update(Campaign).where(
            Campaign.id == campaign.id, Campaign.campaign_folder_path == path,
            Campaign.folder_file_id.is_(None) | (Campaign.folder_file_id == file_id),
        ).values(folder_file_id=file_id))
        if result.rowcount != 1:
            raise _folder_conflict(path)
    except IntegrityError as exc:
        session.rollback()
        raise _folder_conflict(path) from exc


def _rewrite_campaign_paths(session: Session, campaign: Campaign, old_folder: str, new_folder: str) -> None:
    if _root_overlaps_campaign(session, new_folder, exclude_id=campaign.id):
        raise _folder_conflict(new_folder)
    old_root = old_folder.rstrip("/")
    new_root = new_folder.rstrip("/")
    try:
        result = session.exec(update(Campaign).where(
            Campaign.id == campaign.id, Campaign.campaign_folder_path == old_folder, Campaign.folder_conflict == False,  # noqa: E712
        ).values(campaign_folder_path=new_folder))
        if result.rowcount != 1:
            raise _folder_conflict(old_folder)
    except IntegrityError as exc:
        session.rollback()
        raise _folder_conflict(new_folder) from exc
    parent = str(PurePosixPath(new_folder).parent)
    campaign.parent_nc_path = "/" if parent == "." else parent
    campaign.updated_at = utcnow()
    session.add(campaign)
    if old_root != new_root:
        session.exec(update(OperationBatch).where(
            OperationBatch.campaign_id == campaign.id, OperationBatch.status == BatchStatus.preview,
        ).values(status=BatchStatus.cancelled))
    for item in session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all():
        if item.folder_path == old_root or item.folder_path.startswith(old_root + "/"):
            item.folder_path = new_root + item.folder_path[len(old_root) :]
            item.updated_at = utcnow()
            session.add(item)
    for file_record in session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all():
        for attr in ("original_path", "current_path"):
            value = getattr(file_record, attr)
            if value == old_root or value.startswith(old_root + "/"):
                setattr(file_record, attr, new_root + value[len(old_root) :])
        file_record.updated_at = utcnow()
        session.add(file_record)


async def _campaign_folder_entry(client: NextcloudClient, path: str) -> WebDAVEntry | None:
    try:
        entries = await client.propfind(path, depth=0)
    except AppError as exc:
        if exc.code in {"WEBDAV_PATH_NOT_FOUND", "PATH_NOT_ALLOWED"}:
            return None
        raise
    return next((entry for entry in entries if entry.path == path), None)


async def _verify_campaign_root(session: Session, request: Request, campaign: Campaign) -> None:
    async with NextcloudClient(_nc_credentials(session, request)) as client:
        entry = await _campaign_folder_entry(client, campaign.campaign_folder_path)
    if not entry:
        raise AppError("CAMPAIGN_FOLDER_NOT_FOUND", "Папка мониторинга не найдена в Nextcloud.", 404)
    if not entry.is_collection or not entry.file_id or (campaign.folder_file_id and campaign.folder_file_id != entry.file_id):
        raise _folder_conflict(campaign.campaign_folder_path)
    if not campaign.folder_file_id:
        _remember_campaign_folder_id(session, campaign, entry.file_id)


def _candidate_roots_for_relink(settings: dict[str, Any], campaign: Campaign, client: NextcloudClient) -> list[str]:
    configured_root = settings.get("root_path") or client.credentials.root_path
    roots = [
        configured_root,
        campaign.parent_nc_path,
        "/Мониторинги",
        "/",
    ]
    allowed_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root:
            continue
        try:
            normalized = ensure_under_root(root, configured_root)
        except AppError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            allowed_roots.append(normalized)
    return allowed_roots


async def _find_moved_campaign_folder(client: NextcloudClient, session: Session, campaign: Campaign) -> str | None:
    if not campaign.folder_file_id:
        return None
    roots = _candidate_roots_for_relink(settings_map(session, include_secret=True), campaign, client)
    seen: set[str] = set()
    folders_to_scan = [(root, 0) for root in roots]
    while folders_to_scan:
        root, depth = folders_to_scan.pop(0)
        if root in seen or depth > 2:
            continue
        seen.add(root)
        try:
            entries = await client.propfind(root, depth=1)
        except AppError as exc:
            if exc.code in {"WEBDAV_PATH_NOT_FOUND", "PATH_NOT_ALLOWED"}:
                continue
            raise
        for entry in entries:
            if not entry.is_collection or entry.path == root:
                continue
            if entry.file_id == campaign.folder_file_id:
                if not _root_overlaps_campaign(session, entry.path, exclude_id=campaign.id):
                    return entry.path
            folders_to_scan.append((entry.path, depth + 1))
    return None


def _organization_create_data(payload: OrganizationIn) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    data["aliases"] = aliases_or_type_defaults(data["type"], data.get("aliases"))
    if data["type"] == "udod":
        if not data["aliases"]:
            raise HTTPException(
                status_code=400,
                detail={"code": "UDOD_ALIAS_REQUIRED", "message": "Для УДОД укажите хотя бы один алиас."},
            )
        data["code"] = data["aliases"][0]
        data["display_name"] = data["aliases"][0]
        data["prefix"] = None
    else:
        data["code"] = str(data.get("code") or "").strip()
        if not data["code"]:
            raise HTTPException(
                status_code=400,
                detail={"code": "ORGANIZATION_CODE_REQUIRED", "message": "Номер ОУ не должен быть пустым."},
            )
        data["display_name"] = (data.get("display_name") or data["code"]).strip()
    data["short_name"] = data.get("short_name") or data["display_name"]
    data["prefix"] = data.get("prefix") or None
    data["extra_emails"] = data.get("extra_emails") or []
    data["active"] = data.get("active", True)
    return data


def _organization_patch_data(payload: OrganizationPatch, organization: Organization) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True, mode="json")
    next_type = data.get("type", organization.type.value if hasattr(organization.type, "value") else organization.type)
    if next_type == "udod":
        aliases = aliases_or_type_defaults(next_type, data.get("aliases", organization.aliases))
        if not aliases:
            raise HTTPException(
                status_code=400,
                detail={"code": "UDOD_ALIAS_REQUIRED", "message": "Для УДОД укажите хотя бы один алиас."},
            )
        data["aliases"] = aliases
        data["code"] = aliases[0]
        data["display_name"] = aliases[0]
        data["short_name"] = aliases[0]
        data["prefix"] = None
    if "code" in data and data["code"] is not None:
        data["code"] = data["code"].strip()
    if data.get("display_name") is not None:
        data["display_name"] = data["display_name"].strip()
    elif "code" in data and data["code"]:
        data["display_name"] = data["code"]
    if data.get("display_name") == "":
        data["display_name"] = data.get("code") or organization.code
    if data.get("short_name") == "":
        data["short_name"] = data.get("display_name") or organization.display_name
    if next_type == "udod":
        pass
    elif "aliases" in data:
        data["aliases"] = aliases_or_type_defaults(data.get("type", organization.type), data.get("aliases"))
    elif "type" in data:
        data["aliases"] = aliases_or_type_defaults(data["type"], [])
    return data


def _ensure_organization_unique(
    session: Session,
    *,
    org_type: Any,
    code: str,
    exclude_id: str | None = None,
) -> None:
    existing = session.exec(select(Organization).where(Organization.type == org_type, Organization.code == code)).first()
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "DUPLICATE_ORGANIZATION", "message": "Запись с таким типом ОУ и номером уже есть."},
        )


def _parse_file_ids(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()}


@router.post("/files-actions/open")
async def files_action_open(_payload: dict[str, Any], _request: Request) -> dict[str, str]:
    return {"redirect_handler": "main"}


def _get_campaign(session: Session, campaign_id: str, *, allow_conflict: bool = False) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "CAMPAIGN_NOT_FOUND", "message": "Мониторинг не найден."})
    if campaign.folder_conflict and not allow_conflict:
        raise HTTPException(status_code=409, detail={
            "code": "CAMPAIGN_FOLDER_CONFLICT",
            "message": "У нескольких мониторингов общая папка. Создайте отдельный мониторинг для продолжения сбора.",
        })
    return campaign


def _get_batch(session: Session, batch_id: str) -> OperationBatch:
    batch = session.get(OperationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Пакет операций не найден."})
    return batch


def _organizations_selected_for_campaign(session: Session, campaign: Campaign) -> list[Organization]:
    query = select(Organization).where(Organization.active == True)  # noqa: E712
    if campaign.selected_org_types:
        query = query.where(Organization.type.in_(campaign.selected_org_types))
    return session.exec(query).all()


def _lock_participant_changes(session: Session, campaign_id: str) -> None:
    # A write lock also serializes membership changes and batch claims on SQLite.
    session.exec(update(Campaign).where(Campaign.id == campaign_id).values(id=Campaign.id))


def _start_participant_batch(session: Session, batch: OperationBatch) -> None:
    _lock_participant_changes(session, batch.campaign_id)
    session.refresh(batch)
    if batch.status != BatchStatus.preview:
        raise AppError("PREVIEW_STALE", "Предпросмотр устарел. Выполните новый предпросмотр.", 409)
    running = session.exec(select(OperationBatch.id).where(
        OperationBatch.campaign_id == batch.campaign_id,
        OperationBatch.operation_type.in_(["create_folders", "create_shares", "delete_shares"]),
        OperationBatch.status == BatchStatus.running,
    )).first()
    if running:
        raise AppError("PARTICIPANT_OPERATION_RUNNING", "Дождитесь окончания текущей операции мониторинга.", 409)
    batch.status = BatchStatus.running
    batch.started_at = utcnow()
    session.commit()


def _stored_participants(session: Session, campaign: Campaign) -> list[CampaignOrganization]:
    return session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()


def _participant_organizations(session: Session, participants: list[CampaignOrganization]) -> list[Organization]:
    ids = [item.organization_id for item in participants]
    if not ids:
        return []
    return session.exec(select(Organization).where(Organization.id.in_(ids))).all()


def _campaign_orgs(session: Session, campaign: Campaign) -> list[CampaignOrganization]:
    participants = _stored_participants(session, campaign)
    organizations = _participant_organizations(session, participants)
    if campaign.participant_selection_explicit:
        validate_participant_folders(campaign, participants, organizations, session=session)
        return participants
    known = {row.organization_id for row in participants}
    if all(org.id in known for org in _organizations_selected_for_campaign(session, campaign)):
        validate_participant_folders(campaign, participants, organizations, session=session)
        return participants

    # A concurrent request may have chosen an explicit list or added the same members.
    _lock_participant_changes(session, campaign.id)
    session.refresh(campaign)
    participants = _stored_participants(session, campaign)
    organizations = _participant_organizations(session, participants)
    known = {row.organization_id for row in participants}
    candidates = [] if campaign.participant_selection_explicit else _organizations_selected_for_campaign(session, campaign)
    added = [org for org in candidates if org.id not in known]
    organizations.extend(added)
    validate_participant_folders(campaign, participants, organizations, session=session)
    if added:
        invalidate_participant_plans(session, campaign)
        paths = campaign_participant_paths(campaign, organizations)
        for org in added:
            item = CampaignOrganization(campaign_id=campaign.id, organization_id=org.id, folder_path=paths[org.id])
            participants.append(item)
            session.add(item)
        session.commit()
    return participants


def _organizations_for_campaign(session: Session, campaign: Campaign) -> list[Organization]:
    return _participant_organizations(session, _campaign_orgs(session, campaign))


async def _webdav_folder_exists(client: NextcloudClient, path: str) -> bool:
    try:
        entries = await client.propfind(path, depth=0)
    except AppError as exc:
        if exc.code == "WEBDAV_PATH_NOT_FOUND":
            return False
        raise
    return bool(entries and entries[0].is_collection)


async def _root_access_status(
    client: NextcloudClient,
    root_path: str,
    group_id: str,
    *,
    ensure_share: bool,
) -> dict[str, Any]:
    entries = await client.propfind(root_path, depth=0)
    entry = entries[0] if entries else None
    if entry is None:
        raise AppError("ROOT_FOLDER_NOT_FOUND", "Корневая папка мониторингов не найдена в Nextcloud.", 400, root_path)
    if not entry.is_collection:
        raise AppError("ROOT_PATH_NOT_FOLDER", "Корневой путь мониторингов занят файлом.", 400, root_path)

    permissions = entry.permissions or ""
    missing_permissions = sorted(ROOT_REQUIRED_PERMISSIONS.difference(permissions))
    is_team_folder_root = entry.mount_type == "group" and entry.is_mount_root is True
    mode = "team_folder" if is_team_folder_root else "owner_folder"
    share_result: dict[str, Any] | None = None
    normalized_root = ensure_under_root(root_path, root_path)
    missing_group_share = False
    group_share_permissions: int | None = None
    if entry.mount_type == "shared":
        mode = "received_share"
    elif entry.mount_type == "group":
        mode = "team_folder" if is_team_folder_root else "team_folder_child"
    elif group_id:
        if normalized_root == "/":
            raise AppError("ROOT_SHARE_NOT_SUPPORTED", "Нельзя выдавать группе общий доступ на корень файлов пользователя. Укажите папку, например /Мониторинги.", 400)
        if ensure_share:
            share_result = await client.ensure_group_share(
                normalized_root,
                group_id,
                permissions=GROUP_SHARE_PERMISSIONS,
                label="Сбор файлов ОО",
                note="Доступ к корневой папке мониторингов",
            )
            mode = "group_share_fallback"
            group_share_permissions = GROUP_SHARE_PERMISSIONS
        else:
            for share in await client.shares_for_path(normalized_root):
                share_type = str(share.get("share_type") or share.get("shareType") or "")
                share_with = str(share.get("share_with") or share.get("shareWith") or "")
                if share_type == "1" and share_with == group_id:
                    group_share_permissions = int(share.get("permissions") or 0)
                    break
            if group_share_permissions is not None and (group_share_permissions & GROUP_SHARE_PERMISSIONS) == GROUP_SHARE_PERMISSIONS:
                mode = "group_share_fallback"
            else:
                missing_group_share = True
    elif entry.mount_type != "group":
        mode = "owner_folder"

    status = "ok"
    if missing_permissions or missing_group_share or mode in {"owner_folder", "received_share", "team_folder_child"}:
        status = "warning"
    return {
        "status": status,
        "path": entry.path,
        "mode": mode,
        "is_group_folder": is_team_folder_root,
        "is_team_folder_root": is_team_folder_root,
        "mount_type": entry.mount_type or "local",
        "is_mount_root": entry.is_mount_root,
        "owner_id": entry.owner_id,
        "permissions": permissions,
        "missing_permissions": missing_permissions,
        "missing_group_share": missing_group_share,
        "group_share_permissions": group_share_permissions,
        "collaboration_group": group_id or "",
        "share": share_result,
    }


def _folder_paths_to_verify(
    campaign: Campaign,
    campaign_orgs: list[CampaignOrganization],
    organizations: list[Organization],
) -> set[str]:
    paths = {campaign.campaign_folder_path}
    use_type_folder = campaign_uses_type_folders(campaign, organizations)
    if use_type_folder:
        for type_value in campaign_type_values(campaign, organizations):
            paths.add(join_nc_path(campaign.campaign_folder_path, organization_type_label(type_value)))
    org_by_id = {org.id: org for org in organizations}
    for campaign_org in campaign_orgs:
        paths.add(campaign_org.folder_path)
        org = org_by_id.get(campaign_org.organization_id)
        if org:
            paths.add(campaign_organization_upload_path(campaign, org, organizations))
    return paths


async def _existing_webdav_folders(
    session: Session,
    request: Request,
    paths: set[str],
) -> set[str]:
    existing: set[str] = set()
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            for path in sorted(paths, key=lambda item: (item.count("/"), item)):
                if await _webdav_folder_exists(client, path):
                    existing.add(path)
    except AppError as exc:
        _raise(exc)
    return existing


def _is_cleanup_path(campaign: Campaign, path: str) -> bool:
    campaign_parts = path_parts(campaign.campaign_folder_path)
    current_parts = path_parts(path)
    relative = current_parts[len(campaign_parts) :]
    return bool(relative and relative[0] in CLEANUP_FOLDERS)


def _cleanup_target_path(campaign: Campaign, folder: str, source_path: str) -> str:
    source = PurePosixPath(source_path)
    stamp = utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return join_nc_path(campaign.campaign_folder_path, folder, f"{source.stem}_{stamp}{source.suffix}")


async def _ensure_campaign_org_folders_exist(
    session: Session,
    request: Request,
    campaign: Campaign,
    campaign_orgs: list[CampaignOrganization],
) -> None:
    if not campaign_orgs:
        raise HTTPException(
            status_code=400,
            detail={"code": "CAMPAIGN_ORGANIZATIONS_REQUIRED", "message": "В мониторинге нет выбранных ОУ."},
        )
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            await client.propfind(campaign.campaign_folder_path, depth=0)
            missing = []
            for folder_path in sorted({item.folder_path for item in campaign_orgs}):
                if not await _webdav_folder_exists(client, folder_path):
                    missing.append(folder_path)
    except AppError as exc:
        if exc.code == "WEBDAV_PATH_NOT_FOUND":
            raise HTTPException(
                status_code=400,
                detail={"code": "CAMPAIGN_FOLDER_NOT_FOUND", "message": "Папка мониторинга не найдена в Nextcloud."},
            ) from exc
        _raise(exc)
    if missing:
        sample = ", ".join(PurePosixPath(path).name for path in missing[:5])
        tail = f" и ещё {len(missing) - 5}" if len(missing) > 5 else ""
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ORGANIZATION_FOLDERS_REQUIRED",
                "message": f"Сначала создайте папки ОУ внутри папки мониторинга. Отсутствуют: {sample}{tail}.",
                "detail": {"missing": missing},
            },
        )


def _require_name_template(value: str | None) -> str:
    template = (value or "").strip()
    if not template:
        raise HTTPException(
            status_code=400,
            detail={"code": "NAME_TEMPLATE_REQUIRED", "message": "Укажите шаблон имени файла. Например: текст {алиас} {номер} текст."},
        )
    if "{номер}" not in template:
        raise HTTPException(
            status_code=400,
            detail={"code": "NAME_TEMPLATE_PLACEHOLDER_REQUIRED", "message": "В шаблоне должен быть номер ОУ: добавьте {номер}."},
        )
    if "{алиас}" not in template:
        raise HTTPException(
            status_code=400,
            detail={"code": "NAME_TEMPLATE_ALIAS_PLACEHOLDER_REQUIRED", "message": "В шаблоне должен быть алиас ОУ: добавьте {алиас}."},
        )
    return template


def _alias_from_current_name(organization: Organization, current_path: str) -> str:
    source_stem = normalize_text(PurePosixPath(current_path).stem)
    code = organization.code or ""
    for alias in effective_organization_aliases(organization):
        candidate = normalize_text(f"{alias} {code}")
        if candidate and (source_stem == candidate or source_stem.startswith(f"{candidate} ")):
            return alias
    alias_and_code = normalized_organization_file_stem(organization)
    return alias_and_code.removesuffix(code).strip() if code else alias_and_code


def _file_name_from_template(template: str, organization: Organization, current_path: str) -> str:
    source = PurePosixPath(current_path)
    alias = _alias_from_current_name(organization, current_path)
    stem = clean_path_segment(
        template.replace("{алиас}", alias).replace("{номер}", organization.code),
        fallback=organization.code,
    )
    return f"{stem}{source.suffix}"


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/organizations")
def list_organizations(
    session: SessionDep,
    type: str | None = None,  # noqa: A002 - public API field
    q: str | None = None,
    active: bool | None = True,
) -> list[dict[str, Any]]:
    organizations = session.exec(select(Organization)).all()
    if type:
        organizations = [org for org in organizations if org.type == type or getattr(org.type, "value", None) == type]
    if active is not None:
        organizations = [org for org in organizations if org.active is active]
    if q:
        query = q.casefold()
        organizations = [
            org
            for org in organizations
            if query in org.code.casefold()
            or query in org.display_name.casefold()
            or (org.email and query in org.email.casefold())
            or any(query in alias.casefold() for alias in (org.aliases or []))
        ]
    organizations.sort(key=lambda org: (str(org.type), org.sort_order, organization_sort_key(org)))
    return [_model_json(org) for org in organizations]


@router.post("/organizations")
def create_organization(payload: OrganizationIn, session: SessionDep, request: Request) -> dict[str, Any]:
    data = _organization_create_data(payload)
    _ensure_organization_unique(session, org_type=data["type"], code=data["code"])
    organization = Organization(**data)
    session.add(organization)
    audit(session, user_id=_user(request), action="create", entity_type="organization", entity_id=organization.id, after=data)
    session.commit()
    session.refresh(organization)
    return _model_json(organization)


@router.put("/organizations/{organization_id}")
def update_organization(
    organization_id: str,
    payload: OrganizationPatch,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "ORGANIZATION_NOT_FOUND", "message": "Организация не найдена."})
    before = _model_json(organization)
    data = _organization_patch_data(payload, organization)
    next_type = data.get("type", organization.type)
    next_code = data.get("code", organization.code)
    _ensure_organization_unique(session, org_type=next_type, code=next_code, exclude_id=organization.id)
    for key, value in data.items():
        setattr(organization, key, value)
    organization.updated_at = utcnow()
    session.add(organization)
    audit(session, user_id=_user(request), action="update", entity_type="organization", entity_id=organization.id, before=before, after=_model_json(organization))
    session.commit()
    session.refresh(organization)
    return _model_json(organization)


@router.delete("/organizations", dependencies=[Depends(require_admin)])
def deactivate_all_organizations(session: SessionDep, request: Request) -> dict[str, Any]:
    organizations = session.exec(select(Organization).where(Organization.active == True)).all()  # noqa: E712
    now = utcnow()
    for organization in organizations:
        organization.active = False
        organization.updated_at = now
        session.add(organization)
    summary = {"status": "deactivated", "count": len(organizations)}
    audit(session, user_id=_user(request), action="deactivate_all", entity_type="organization", after=summary)
    session.commit()
    return summary


@router.delete("/organizations/{organization_id}")
def deactivate_organization(organization_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "ORGANIZATION_NOT_FOUND", "message": "Организация не найдена."})
    before = _model_json(organization)
    organization.active = False
    organization.updated_at = utcnow()
    session.add(organization)
    audit(session, user_id=_user(request), action="deactivate", entity_type="organization", entity_id=organization.id, before=before, after=_model_json(organization))
    session.commit()
    return {"status": "deactivated", "id": organization.id}


def _import_organizations_content(
    session: Session,
    request: Request,
    *,
    filename: str,
    content: bytes,
    is_admin: bool,
    apply: bool,
    allow_update: bool,
) -> dict[str, Any]:
    rows = parse_organization_table(filename or "organizations.csv", content)
    validation = validate_organization_rows(session, rows)
    errors = [item for item in validation if item["errors"]]
    blocking_errors = [
        item
        for item in errors
        if any(error != "DUPLICATE_ORGANIZATION" for error in item["errors"]) or (not allow_update and "DUPLICATE_ORGANIZATION" in item["errors"])
    ]
    if not apply:
        return {"apply": False, "rows": validation, "errors_count": len(errors)}
    if blocking_errors:
        return {"apply": False, "rows": validation, "errors_count": len(errors), "message": "Импорт остановлен: в файле есть дубли или ошибки в обязательных полях."}
    if any("DUPLICATE_ORGANIZATION" in row["errors"] and row["data"]["active"] is False for row in validation):
        require_admin(is_admin)
    summary = apply_organization_import(session, rows)
    audit(session, user_id=_user(request), action="import", entity_type="organization", after=summary)
    session.commit()
    return {"apply": True, **summary}


@router.post("/organizations/import")
async def import_organizations(
    session: SessionDep,
    request: Request,
    is_admin: IsAdmin,
    file: UploadFile = File(...),
    apply: bool = Query(False),  # noqa: A002 - public API field
    allow_update: bool = Query(False),
) -> dict[str, Any]:
    content = await file.read()
    return _import_organizations_content(
        session,
        request,
        filename=file.filename or "organizations.csv",
        content=content,
        is_admin=is_admin,
        apply=apply,
        allow_update=allow_update,
    )


@router.post("/organizations/import-json")
def import_organizations_json(
    payload: OrganizationImportJsonIn,
    session: SessionDep,
    request: Request,
    is_admin: IsAdmin,
    apply: bool = Query(False),  # noqa: A002 - public API field
    allow_update: bool = Query(False),
) -> dict[str, Any]:
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_IMPORT_FILE", "message": "Не удалось прочитать файл импорта."},
        ) from exc
    return _import_organizations_content(
        session,
        request,
        filename=payload.filename,
        content=content,
        is_admin=is_admin,
        apply=apply,
        allow_update=allow_update,
    )


@router.get("/organizations/export.csv")
def export_organizations_csv(session: SessionDep) -> Response:
    organizations = session.exec(select(Organization).where(Organization.active == True)).all()  # noqa: E712
    organizations.sort(key=lambda org: (str(org.type), org.sort_order, organization_sort_key(org)))
    columns = ["Тип ОУ", "Номер", "Email", "Алиасы"]
    rows = [
        {
            "Тип ОУ": organization_type_label(org.type.value if hasattr(org.type, "value") else org.type),
            "Номер": org.code,
            "Email": org.email or "",
            "Алиасы": ", ".join(org.aliases or []),
        }
        for org in organizations
    ]
    return Response(
        csv_bytes(rows, columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="organizations.csv"'},
    )


@router.get("/organizations/export/xlsx")
@router.get("/organizations/export.xlsx")
def export_organizations_xlsx(session: SessionDep) -> Response:
    organizations = session.exec(select(Organization).where(Organization.active == True)).all()  # noqa: E712
    organizations.sort(key=lambda org: (str(org.type), org.sort_order, organization_sort_key(org)))
    columns = ["Тип ОУ", "Номер", "Email", "Алиасы"]
    data = [
        {
            "Тип ОУ": organization_type_label(org.type.value if hasattr(org.type, "value") else org.type),
            "Номер": org.code,
            "Email": org.email or "",
            "Алиасы": ", ".join(org.aliases or []),
        }
        for org in organizations
    ]
    return Response(
        xlsx_bytes(data, columns, sheet_name="Список ОУ"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="organizations.xlsx"'},
    )


@router.get("/organizations/{organization_id}")
def get_organization(organization_id: str, session: SessionDep) -> dict[str, Any]:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "ORGANIZATION_NOT_FOUND", "message": "Организация не найдена."})
    return _model_json(organization)


@router.post("/organizations/validate")
def validate_organizations(session: SessionDep) -> dict[str, Any]:
    organizations = session.exec(select(Organization)).all()
    rows = [org.model_dump(mode="json") for org in organizations]
    return {"rows": validate_organization_rows(session, rows)}


@router.get("/recent-uploads")
async def recent_uploads(session: SessionDep, request: Request, limit: int = Query(10, ge=1, le=50)) -> list[dict[str, Any]]:
    rows = session.exec(
        select(Campaign, CampaignOrganization, Organization)
        .join(CampaignOrganization, CampaignOrganization.campaign_id == Campaign.id)
        .join(Organization, CampaignOrganization.organization_id == Organization.id)
        .where(Campaign.status != CampaignStatus.archived, Campaign.folder_conflict == False,  # noqa: E712
               CampaignOrganization.public_link.is_not(None))
    ).all()
    folders: dict[str, list[tuple[Campaign, Organization]]] = {}
    for campaign, participant, organization in rows:
        folders.setdefault(participant.folder_path, []).append((campaign, organization))
    if not folders:
        return []

    uploads: list[dict[str, Any]] = []
    verified_roots: dict[str, bool] = {}
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            for folder_path, participants in folders.items():
                campaign = participants[0][0]
                if any(other.id != campaign.id for other, _ in participants):
                    # Legacy campaigns can point to the same directory. Ownership is ambiguous.
                    continue
                if campaign.folder_file_id:
                    if campaign.id not in verified_roots:
                        root = await _campaign_folder_entry(client, campaign.campaign_folder_path)
                        verified_roots[campaign.id] = bool(root and root.is_collection and root.file_id == campaign.folder_file_id)
                    if not verified_roots[campaign.id]:
                        continue
                organizations = {org.id: org for _, org in participants}
                individual = campaign_upload_mode(campaign) == "organization"
                try:
                    entries = await client.propfind(folder_path, depth=1)
                except AppError as exc:
                    if exc.code == "WEBDAV_PATH_NOT_FOUND":
                        continue
                    raise
                for entry in entries:
                    if entry.is_collection or entry.path == folder_path:
                        continue
                    organization = None
                    if individual and len(organizations) == 1:
                        organization = next(iter(organizations.values()))
                    elif not individual:
                        match = extract_organization(entry.name, organizations.values())
                        organization = organizations.get(match.organization_id)
                    uploads.append(
                        {
                            "campaign_id": campaign.id,
                            "campaign_title": campaign.title,
                            "organization_id": organization.id if organization else None,
                            "organization_name": organization.display_name if organization else "",
                            "folder_path": folder_path,
                            "path": entry.path,
                            "name": entry.name,
                            "size": entry.size,
                            "uploaded_at": entry.mtime.isoformat() if entry.mtime else None,
                        }
                    )
    except AppError as exc:
        _raise(exc)
    uploads.sort(key=lambda item: item.get("uploaded_at") or "", reverse=True)
    return uploads[:limit]


@router.get("/context-folder")
async def context_folder(file_ids: str, session: SessionDep, request: Request) -> dict[str, Any]:
    selected_ids = _parse_file_ids(file_ids)
    if not selected_ids:
        return {"is_monitoring": False}
    campaigns = session.exec(select(Campaign).where(Campaign.status != CampaignStatus.archived, Campaign.folder_conflict == False)).all()
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            for campaign in campaigns:
                try:
                    entries = await client.propfind(campaign.campaign_folder_path, depth=0)
                except AppError as exc:
                    if exc.code == "WEBDAV_PATH_NOT_FOUND":
                        continue
                    raise
                if entries and entries[0].file_id in selected_ids and (not campaign.folder_file_id or entries[0].file_id == campaign.folder_file_id):
                    return {"is_monitoring": True, "campaign": _model_json(campaign)}
    except AppError as exc:
        _raise(exc)
    return {"is_monitoring": False}


@router.get("/campaigns")
async def list_campaigns(session: SessionDep, request: Request, status: str | None = None) -> list[dict[str, Any]]:
    campaigns = session.exec(select(Campaign).where(Campaign.status != CampaignStatus.archived)).all()
    try:
        campaigns = await _prune_missing_campaigns(session, request, campaigns)
    except AppError as exc:
        _raise(exc)
    if status:
        campaigns = [campaign for campaign in campaigns if campaign.status == status or campaign.status.value == status]
    campaigns.sort(key=lambda campaign: (campaign.date, campaign.created_at), reverse=True)
    return [_model_json(campaign) for campaign in campaigns]


@router.post("/campaigns")
async def create_campaign(payload: CampaignIn, session: SessionDep, request: Request) -> dict[str, Any]:
    settings = settings_map(session, include_secret=True)
    root_path = settings.get("root_path") or payload.parent_nc_path
    group_id = _collaboration_group(settings)
    parent = ensure_under_root(payload.parent_nc_path, root_path)
    folder = campaign_folder_path(parent, payload.date, payload.title)
    upload_mode = payload.upload_mode.value
    campaign = Campaign(
        title=payload.title,
        description=payload.description,
        date=payload.date,
        deadline=deadline_storage(payload.deadline),
        parent_nc_path=parent,
        campaign_folder_path=folder,
        selected_org_types=[item.value for item in payload.selected_org_types],
        allowed_extensions=[item.lower().lstrip(".") for item in payload.allowed_extensions],
        allowed_mime_types=payload.allowed_mime_types,
        upload_mode=upload_mode,
        org_folder_layout=_effective_org_folder_layout(upload_mode, payload.org_folder_layout.value),
        created_by=_user(request),
    )
    campaign_participant_paths(campaign, _organizations_selected_for_campaign(session, campaign))
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            root_access = await _root_access_status(client, root_path, group_id, ensure_share=True)
            for candidate in (folder, f"{folder} ({campaign.id})"):
                if _root_overlaps_campaign(session, candidate):
                    continue
                try:
                    # Only a successful new collection may be adopted by this campaign.
                    await client.mkcol(candidate, exclusive=True)
                except AppError as exc:
                    if exc.code == "CAMPAIGN_TARGET_EXISTS":
                        continue
                    raise
                entry = await _campaign_folder_entry(client, candidate)
                if not entry or not entry.is_collection or not entry.file_id:
                    raise AppError("CAMPAIGN_FOLDER_ID_MISSING", "Не удалось определить ID новой папки в Nextcloud.", 502, candidate)
                if _root_overlaps_campaign(session, candidate):
                    raise _folder_conflict(candidate)
                campaign.campaign_folder_path = candidate
                campaign.folder_file_id = entry.file_id
                break
            else:
                raise _folder_conflict(folder)
    except AppError as exc:
        _raise(exc)
    session.add(campaign)
    audit(
        session,
        user_id=_user(request),
        action="create",
        entity_type="campaign",
        entity_id=campaign.id,
        after={**campaign.model_dump(mode="json"), "root_access": root_access},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _folder_conflict(folder) from exc
    session.refresh(campaign)
    return _model_json(campaign)


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id, allow_conflict=True)
    return _model_json(campaign)


def _campaign_move_target(campaign: Campaign, target_nc_path: str, root_path: str) -> str:
    target = ensure_under_root(target_nc_path, root_path)
    current_name = PurePosixPath(campaign.campaign_folder_path).name
    if PurePosixPath(target).name == current_name:
        return target
    return join_nc_path(target, current_name)


def _archive_folder_root(settings: dict[str, Any], root_path: str) -> str:
    archive_folder = str(settings.get("archive_folder") or "_Архив").strip()
    if archive_folder.startswith("/"):
        return ensure_under_root(archive_folder, root_path)
    return join_nc_path(root_path, archive_folder)


async def _relocate_campaign_root(
    session: Session, client: NextcloudClient, campaign: Campaign, new_folder: str, *, archive_root: str | None = None,
) -> None:
    old_folder = campaign.campaign_folder_path
    if _root_overlaps_campaign(session, new_folder, exclude_id=campaign.id):
        raise _folder_conflict(new_folder)
    if new_folder.startswith(old_folder + "/") or old_folder.startswith(new_folder + "/"):
        raise _folder_conflict(new_folder)
    old = await _campaign_folder_entry(client, old_folder)
    expected_id = campaign.folder_file_id
    if old:
        if not old.is_collection or not old.file_id or (expected_id and old.file_id != expected_id):
            raise _folder_conflict(old_folder)
        if not expected_id:
            _remember_campaign_folder_id(session, campaign, old.file_id)
        if new_folder == old_folder:
            return
    target = await _campaign_folder_entry(client, new_folder)
    if target:
        if old or not expected_id or not target.is_collection or target.file_id != expected_id:
            raise _folder_conflict(new_folder)
        return  # A MOVE already completed, possibly outside the app.
    if not old:
        raise AppError("CAMPAIGN_FOLDER_NOT_FOUND", "Папка мониторинга не найдена в Nextcloud.", 404, old_folder)
    if archive_root:
        await client.mkcol(archive_root)
    await client.move(old_folder, new_folder)


async def _move_or_relink_campaign_folder(
    session: Session, request: Request, campaign: Campaign, new_folder: str,
) -> None:
    old_folder = campaign.campaign_folder_path
    if new_folder == old_folder:
        return
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            await _relocate_campaign_root(session, client, campaign, new_folder)
    except AppError as exc:
        _raise(exc)
    _rewrite_campaign_paths(session, campaign, old_folder, new_folder)


async def _archive_campaign_to_folder(campaign: Campaign, session: Session, request: Request) -> dict[str, Any]:
    if campaign.folder_conflict:
        campaign.status = CampaignStatus.archived
        campaign.updated_at = utcnow()
        session.add(campaign)
        return {"archived_record_only": True, "archive_folder": None, "deleted_shares": 0}
    settings = settings_map(session, include_secret=True)
    root_path = settings.get("root_path") or campaign.parent_nc_path
    archive_root = _archive_folder_root(settings, root_path)
    new_folder = join_nc_path(archive_root, PurePosixPath(campaign.campaign_folder_path).name)
    old_folder = campaign.campaign_folder_path
    campaign_orgs = session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    if any(item.public_link and not item.share_id for item in campaign_orgs):
        raise AppError("SHARE_ID_MISSING", "Сначала удалите публичные ссылки в разделе операций.", 409)
    deleted_shares = 0
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            await _relocate_campaign_root(session, client, campaign, new_folder, archive_root=archive_root)
            for share_id in sorted({item.share_id for item in campaign_orgs if item.share_id}):
                await client.delete_public_share(share_id)
                deleted_shares += 1
    except AppError as exc:
        _raise(exc)
    _rewrite_campaign_paths(session, campaign, old_folder, new_folder)
    for item in campaign_orgs:
        item.share_id = None
        item.public_link = None
        item.mail_subject = None
        item.mail_body = None
        if item.status == CampaignOrganizationStatus.link_created:
            item.status = CampaignOrganizationStatus.folder_created
        item.updated_at = utcnow()
        session.add(item)
    campaign.status = CampaignStatus.archived
    campaign.updated_at = utcnow()
    session.add(campaign)
    return {"archive_folder": new_folder, "deleted_shares": deleted_shares}


@router.put("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, payload: CampaignPatch, session: SessionDep, request: Request) -> dict[str, Any]:
    _lock_participant_changes(session, campaign_id)
    campaign = _get_campaign(session, campaign_id)
    before = _model_json(campaign)
    data = payload.model_dump(exclude_unset=True)
    old_folder = campaign.campaign_folder_path
    if data.get("parent_nc_path") or data.get("title"):
        parent_value = data.get("parent_nc_path") or campaign.parent_nc_path
        root_path = settings_map(session, include_secret=True).get("root_path") or parent_value
        data["parent_nc_path"] = ensure_under_root(parent_value, root_path)
        current_leaf = PurePosixPath(old_folder).name
        title_leaf = clean_path_segment(data.get("title") or campaign.title)
        # An unchanged title retains its allocated suffix or externally renamed folder.
        leaf = current_leaf if title_leaf == clean_path_segment(campaign.title) else title_leaf
        data["campaign_folder_path"] = join_nc_path(data["parent_nc_path"], leaf)
    if data.get("selected_org_types") is None:
        data.pop("selected_org_types", None)
    else:
        data["selected_org_types"] = [str(item.value if hasattr(item, "value") else item) for item in data["selected_org_types"]]
    for key in ("upload_mode", "org_folder_layout"):
        if key in data and data[key] is not None:
            data[key] = str(data[key].value if hasattr(data[key], "value") else data[key])
    if any(key in data for key in ("upload_mode", "org_folder_layout")):
        next_upload_mode = data.get("upload_mode", campaign.upload_mode)
        next_org_folder_layout = data.get("org_folder_layout", campaign.org_folder_layout)
        data["org_folder_layout"] = _effective_org_folder_layout(next_upload_mode, next_org_folder_layout)
    if "allowed_extensions" in data and data["allowed_extensions"] is not None:
        data["allowed_extensions"] = [str(item).lower().lstrip(".") for item in data["allowed_extensions"]]
    if "deadline" in data:
        data["deadline"] = deadline_storage(data["deadline"])
    new_folder = data.get("campaign_folder_path", old_folder)
    previous_mode = campaign_upload_mode(campaign)
    previous_layout = campaign_org_layout(campaign)
    previous_types = set(campaign.selected_org_types or [])
    for key, value in data.items():
        if key not in {"parent_nc_path", "campaign_folder_path"}:
            setattr(campaign, key, value)
    types_changed = previous_types != set(campaign.selected_org_types or [])
    if types_changed or previous_mode != campaign_upload_mode(campaign) or previous_layout != campaign_org_layout(campaign):
        existing = _stored_participants(session, campaign)
        if types_changed:
            campaign.participant_selection_explicit = False
        if campaign.participant_selection_explicit:
            organizations = _participant_organizations(session, existing)
        else:
            organizations = _organizations_selected_for_campaign(session, campaign)
        reconcile_campaign_orgs(session, campaign, organizations, existing, previous_mode=previous_mode)
    if new_folder != old_folder:
        invalidate_participant_plans(session, campaign)
        await _move_or_relink_campaign_folder(session, request, campaign, new_folder)
    campaign.updated_at = utcnow()
    session.add(campaign)
    audit(session, user_id=_user(request), action="update", entity_type="campaign", entity_id=campaign.id, before=before, after=_model_json(campaign))
    session.commit()
    session.refresh(campaign)
    return _model_json(campaign)


@router.post("/campaigns/{campaign_id}/move")
async def move_campaign(campaign_id: str, payload: CampaignMoveIn, session: SessionDep, request: Request) -> dict[str, Any]:
    _lock_participant_changes(session, campaign_id)
    campaign = _get_campaign(session, campaign_id)
    before = _model_json(campaign)
    root_path = settings_map(session, include_secret=True).get("root_path") or campaign.parent_nc_path
    new_folder = _campaign_move_target(campaign, payload.target_nc_path, root_path)
    ensure_under_root(new_folder, root_path)
    if new_folder != campaign.campaign_folder_path:
        invalidate_participant_plans(session, campaign)
    await _move_or_relink_campaign_folder(session, request, campaign, new_folder)
    audit(session, user_id=_user(request), action="move", entity_type="campaign", entity_id=campaign.id, before=before, after=_model_json(campaign))
    session.commit()
    session.refresh(campaign)
    return _model_json(campaign)


@router.post("/campaigns/{campaign_id}/archive")
async def archive_campaign_to_configured_folder(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    _lock_participant_changes(session, campaign_id)
    campaign = _get_campaign(session, campaign_id, allow_conflict=True)
    invalidate_participant_plans(session, campaign)
    before = _model_json(campaign)
    archive_result = await _archive_campaign_to_folder(campaign, session, request)
    after = _model_json(campaign)
    audit(
        session,
        user_id=_user(request),
        action="archive",
        entity_type="campaign",
        entity_id=campaign.id,
        before=before,
        after={**after, **archive_result},
    )
    session.commit()
    session.refresh(campaign)
    return {**_model_json(campaign), **archive_result}


@router.delete("/campaigns/{campaign_id}")
async def archive_campaign(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    _lock_participant_changes(session, campaign_id)
    campaign = _get_campaign(session, campaign_id, allow_conflict=True)
    invalidate_participant_plans(session, campaign)
    before = _model_json(campaign)
    archive_result = await _archive_campaign_to_folder(campaign, session, request)
    after = _model_json(campaign)
    audit(
        session,
        user_id=_user(request),
        action="archive",
        entity_type="campaign",
        entity_id=campaign.id,
        before=before,
        after={**after, **archive_result},
    )
    session.commit()
    return {"status": "archived", "id": campaign.id, **archive_result}


@router.post("/campaigns/{campaign_id}/select-organizations")
def select_campaign_organizations(
    campaign_id: str,
    payload: SelectOrganizationsIn,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    _lock_participant_changes(session, campaign_id)
    campaign = _get_campaign(session, campaign_id)
    if payload.organization_ids is not None:
        organizations = session.exec(select(Organization).where(Organization.id.in_(payload.organization_ids))).all()
        if len(organizations) != len(set(payload.organization_ids)):
            raise AppError("ORGANIZATION_NOT_FOUND", "Некоторые выбранные организации не найдены.", 404)
    elif payload.organization_types is not None:
        types = [item.value for item in payload.organization_types]
        organizations = session.exec(select(Organization).where(Organization.active == True, Organization.type.in_(types))).all()  # noqa: E712
    else:
        organizations = _organizations_selected_for_campaign(session, campaign)
    campaign_orgs = reconcile_campaign_orgs(
        session, campaign, organizations, _stored_participants(session, campaign), previous_mode=campaign_upload_mode(campaign)
    )
    campaign.participant_selection_explicit = True
    audit(session, user_id=_user(request), action="select_organizations", entity_type="campaign", entity_id=campaign.id, after={"count": len(campaign_orgs)})
    session.flush()
    result = {"campaign_id": campaign.id, "count": len(campaign_orgs), "items": [_model_json(item) for item in campaign_orgs]}
    session.commit()
    return result


@router.post("/campaigns/{campaign_id}/create-folders/preview")
async def create_folders_preview(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    await _verify_campaign_root(session, request, campaign)
    campaign_orgs = _campaign_orgs(session, campaign)
    organizations = _organizations_for_campaign(session, campaign)
    existing_folders = await _existing_webdav_folders(session, request, _folder_paths_to_verify(campaign, campaign_orgs, organizations))
    items = planned_folder_items(campaign, campaign_orgs, organizations, existing_folders)
    context = _campaign_operation_context(campaign, organizations)
    summary = {
        "folders_to_create": len([item for item in items if item["action"] == "create_folder"]),
        "folders_to_rename": len([item for item in items if item["action"] == "move"]),
        "organizations_without_email": len([org for org in organizations if not org.email]),
        **context,
    }
    batch = create_batch(session, operation_type="create_folders", campaign_id=campaign.id, user_id=_user(request), items=items, summary=summary)
    return batch_payload(session, batch)


@router.post("/campaigns/{campaign_id}/create-folders/apply")
async def create_folders_apply(
    campaign_id: str,
    session: SessionDep,
    request: Request,
    batch_id: str = Query(...),
) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    batch = _get_batch(session, batch_id)
    if batch.campaign_id != campaign.id or batch.operation_type != "create_folders":
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не подходит для этого мониторинга."})
    if batch.status in {BatchStatus.completed, BatchStatus.completed_with_errors}:
        return batch_payload(session, batch)
    if batch.status != BatchStatus.preview:
        raise AppError("PREVIEW_STALE", "Предпросмотр устарел. Выполните новый предпросмотр.", 409)
    await _verify_campaign_root(session, request, campaign)
    participants = _campaign_orgs(session, campaign)
    organizations = _participant_organizations(session, participants)
    existing = await _existing_webdav_folders(session, request, _folder_paths_to_verify(campaign, participants, organizations))
    fresh = planned_folder_items(campaign, participants, organizations, existing)
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
    expected = {(item["action"], item.get("source_path"), item["target_path"], item["metadata_json"].get("campaign_org_id")) for item in fresh}
    planned = {(item.action.value, item.source_path, item.target_path, item.metadata_json.get("campaign_org_id")) for item in items}
    if planned != expected:
        raise AppError("PREVIEW_STALE", "Папки или участники изменились. Выполните новый предпросмотр.", 409)
    _start_participant_batch(session, batch)
    success = 0
    failed = 0
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
            for item in items:
                try:
                    if item.action == OperationAction.create_folder:
                        await client.mkcol(item.target_path or "")
                    elif item.action == OperationAction.move:
                        source_path = item.source_path or ""
                        target_path = item.target_path or ""
                        target_exists = await _webdav_folder_exists(client, target_path)
                        source_exists = await _webdav_folder_exists(client, source_path)
                        if target_exists and not source_exists:
                            pass
                        elif target_exists and source_exists:
                            raise AppError("TARGET_FOLDER_EXISTS", "Целевая папка уже существует, автоматическое переименование остановлено.", 409, target_path)
                        elif source_exists:
                            await client.move(source_path, target_path)
                        else:
                            raise AppError("SOURCE_FOLDER_NOT_FOUND", "Исходная папка для переименования не найдена.", 404, source_path)
                    else:
                        item.status = OperationItemStatus.skipped
                        item.reason = item.reason or "not_applicable"
                        session.add(item)
                        session.commit()
                        continue
                    item.status = OperationItemStatus.success
                    item.error = None
                    success += 1
                except Exception as exc:  # noqa: BLE001 - keep per-item errors visible
                    item.status = OperationItemStatus.failed
                    item.error = str(exc)
                    failed += 1
                item.updated_at = utcnow()
                session.add(item)
                if item.status == OperationItemStatus.success and item.metadata_json.get("campaign_org_id"):
                    campaign_org = session.get(CampaignOrganization, item.metadata_json["campaign_org_id"])
                    if campaign_org is not None:
                        if item.target_path and item.target_path != campaign_org.folder_path:
                            remember_participant_folder(session, campaign, campaign_org)
                            campaign_org.folder_path = item.target_path
                        if campaign_org.status == CampaignOrganizationStatus.pending:
                            campaign_org.status = CampaignOrganizationStatus.folder_created
                        campaign_org.updated_at = utcnow()
                        session.add(campaign_org)
                session.commit()
    except Exception:
        batch.status = BatchStatus.failed
        batch.finished_at = utcnow()
        session.commit()
        raise
    batch.finished_at = utcnow()
    batch.status = BatchStatus.completed if failed == 0 else BatchStatus.completed_with_errors
    batch.summary_json = {**(batch.summary_json or {}), "success": success, "failed": failed}
    session.add(batch)
    session.commit()
    session.refresh(batch)
    successful_targets = {
        item.target_path
        for item in session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id, OperationItem.status == OperationItemStatus.success)).all()
    }
    campaign_orgs = _campaign_orgs(session, campaign)
    if not successful_targets and campaign_orgs:
        organizations = _organizations_for_campaign(session, campaign)
        successful_targets.update(await _existing_webdav_folders(session, request, _folder_paths_to_verify(campaign, campaign_orgs, organizations)))
    for item in campaign_orgs:
        if item.folder_path in successful_targets:
            if item.status == CampaignOrganizationStatus.pending:
                item.status = CampaignOrganizationStatus.folder_created
            item.updated_at = utcnow()
            session.add(item)
    if all(item.status in {CampaignOrganizationStatus.folder_created, CampaignOrganizationStatus.link_created} for item in campaign_orgs):
        campaign.status = CampaignStatus.folders_created
        campaign.updated_at = utcnow()
        session.add(campaign)
    audit(session, user_id=_user(request), action="apply_create_folders", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    return batch_payload(session, batch)


def _existing_participant_share(participants: list[CampaignOrganization]) -> CampaignOrganization | None:
    links = {(item.share_id, item.public_link) for item in participants if item.share_id or item.public_link}
    if len(links) > 1 or any(not share_id or not url for share_id, url in links):
        raise AppError("SHARE_RECORD_CONFLICT", "Сведения о ссылках папки расходятся. Удалите ссылки и создайте их заново.", 409)
    return next((item for item in participants if item.public_link), None)


@router.post("/campaigns/{campaign_id}/create-shares/preview")
async def create_shares_preview(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    await _verify_campaign_root(session, request, campaign)
    campaign_orgs = _campaign_orgs(session, campaign)
    organizations = _organizations_for_campaign(session, campaign)
    await _ensure_campaign_org_folders_exist(session, request, campaign, campaign_orgs)
    context = _campaign_operation_context(campaign, organizations)
    campaign_org_by_id = {item.id: item for item in campaign_orgs}
    targets = campaign_share_targets(campaign, campaign_orgs, organizations)
    items = []
    shares_to_create = 0
    shares_to_update = 0
    mailing_to_refresh = 0
    for target in targets:
        target_orgs = [campaign_org_by_id[item_id] for item_id in target["campaign_org_ids"] if item_id in campaign_org_by_id]
        existing_link = _existing_participant_share(target_orgs)
        share_options = _public_share_options_for_scope(target["scope"])
        needs_share_update = existing_link is not None and share_options["file_request"]
        needs_mail_refresh = [item for item in target_orgs if not item.public_link or not item.mail_subject or not item.mail_body]
        if existing_link is None:
            shares_to_create += 1
        elif needs_share_update:
            shares_to_update += 1
        mailing_to_refresh += len([item for item in target_orgs if item.public_link and (not item.mail_subject or not item.mail_body)])
        if existing_link is None or needs_share_update or needs_mail_refresh:
            items.append(
                {
                    "action": "create_share",
                    "target_path": target["target_path"],
                    "reason": share_options["reason"],
                    "metadata_json": {
                        "share_scope": target["scope"],
                        "public_upload": share_options["public_upload"],
                        "permissions": share_options["permissions"],
                        "file_request": share_options["file_request"],
                        "campaign_org_ids": target["campaign_org_ids"],
                        "organization_type": target.get("organization_type"),
                        "organization_type_folder": target.get("organization_type_folder"),
                    },
                }
            )
    summary = {
        "share_targets": len(targets),
        "shares_to_create": shares_to_create,
        "shares_to_update": shares_to_update,
        "mailing_to_refresh": mailing_to_refresh,
        "existing_links": len([item for item in campaign_orgs if item.public_link]),
        **context,
    }
    batch = create_batch(session, operation_type="create_shares", campaign_id=campaign.id, user_id=_user(request), items=items, summary=summary)
    return batch_payload(session, batch)


@router.post("/campaigns/{campaign_id}/create-shares/apply")
async def create_shares_apply(
    campaign_id: str,
    session: SessionDep,
    request: Request,
    options: ShareOptionsIn | None = None,
    batch_id: str = Query(...),
) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    batch = _get_batch(session, batch_id)
    if batch.campaign_id != campaign.id or batch.operation_type != "create_shares":
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не подходит для этого мониторинга."})
    if batch.status in {BatchStatus.completed, BatchStatus.completed_with_errors}:
        return batch_payload(session, batch)
    if batch.status != BatchStatus.preview:
        raise AppError("PREVIEW_STALE", "Предпросмотр устарел. Выполните новый предпросмотр.", 409)
    options = options or ShareOptionsIn()
    participants = _campaign_orgs(session, campaign)
    organizations = _participant_organizations(session, participants)
    org_by_id = {org.id: org for org in organizations}
    campaign_org_by_id = {item.id: item for item in participants}
    targets = {target["target_path"]: target for target in campaign_share_targets(campaign, participants, organizations)}
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
    for item in items:
        metadata = item.metadata_json or {}
        ids = metadata.get("campaign_org_ids") or [metadata.get("campaign_org_id")]
        target = targets.get(item.target_path)
        if (not target or target["scope"] != metadata.get("share_scope", "organization")
                or set(ids) != set(target["campaign_org_ids"])
                or any(campaign_org_by_id[org_id].folder_path != item.target_path for org_id in ids)):
            raise AppError("PREVIEW_STALE", "Папки или участники изменились. Выполните новый предпросмотр.", 409)
    await _verify_campaign_root(session, request, campaign)
    await _ensure_campaign_org_folders_exist(session, request, campaign, participants)
    _start_participant_batch(session, batch)
    success = 0
    failed = 0
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
            for item in items:
                metadata = item.metadata_json or {}
                target_ids = metadata.get("campaign_org_ids") or ([metadata.get("campaign_org_id")] if metadata.get("campaign_org_id") else [])
                target_orgs = [campaign_org_by_id[item_id] for item_id in target_ids if item_id in campaign_org_by_id]
                if not target_orgs:
                    item.status = OperationItemStatus.failed
                    item.error = "Campaign organizations not found"
                    failed += 1
                    session.add(item)
                    continue
                try:
                    share_options = _public_share_options_for_scope(str(metadata.get("share_scope") or "organization"))
                    public_upload = bool(metadata.get("public_upload", share_options["public_upload"]))
                    permissions = int(metadata.get("permissions") or share_options["permissions"])
                    file_request = bool(metadata.get("file_request", share_options["file_request"]))
                    existing_share = _existing_participant_share(target_orgs)
                    if existing_share:
                        share = {"share_id": existing_share.share_id, "public_link": existing_share.public_link}
                        if existing_share.share_id:
                            await client.update_share_permissions(
                                existing_share.share_id,
                                permissions,
                                public_upload=public_upload,
                                file_request=file_request,
                            )
                    else:
                        share = await client.create_public_share(
                            item.target_path or target_orgs[0].folder_path,
                            public_upload=public_upload,
                            expire_date=options.expire_date or campaign.deadline,
                            password=options.password,
                            permissions=permissions,
                            file_request=file_request,
                            label=campaign.title,
                            note=f"{item.target_path or target_orgs[0].folder_path} · {campaign.title}",
                        )
                    share_id = str(share.get("share_id") or "")
                    public_link = str(share.get("public_link") or "")
                    for campaign_org in target_orgs:
                        campaign_org.share_id = share_id
                        campaign_org.public_link = public_link
                        campaign_org.folder_path = item.target_path or campaign_org.folder_path
                        if campaign_org.status in {CampaignOrganizationStatus.pending, CampaignOrganizationStatus.folder_created}:
                            campaign_org.status = CampaignOrganizationStatus.link_created
                        org = org_by_id.get(campaign_org.organization_id)
                        if org:
                            subject, body = upload_link_mail_values(campaign, campaign_org)
                            campaign_org.mail_subject = subject
                            campaign_org.mail_body = body
                        campaign_org.updated_at = utcnow()
                        session.add(campaign_org)
                    item.status = OperationItemStatus.success
                    success += 1
                except Exception as exc:  # noqa: BLE001 - per-item error capture
                    item.status = OperationItemStatus.failed
                    item.error = str(exc)
                    failed += 1
                item.updated_at = utcnow()
                session.add(item)
                session.commit()
    except Exception:
        batch.status = BatchStatus.failed
        batch.finished_at = utcnow()
        session.commit()
        raise
    batch.finished_at = utcnow()
    batch.status = BatchStatus.completed if failed == 0 else BatchStatus.completed_with_errors
    batch.summary_json = {**batch.summary_json, "success": success, "failed": failed}
    campaign.status = CampaignStatus.links_created if failed == 0 else campaign.status
    campaign.updated_at = utcnow()
    session.add(batch)
    session.add(campaign)
    audit(session, user_id=_user(request), action="apply_create_shares", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    return batch_payload(session, batch)


@router.post("/campaigns/{campaign_id}/shares/delete-all")
async def delete_campaign_shares(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    await _verify_campaign_root(session, request, campaign)
    _lock_participant_changes(session, campaign_id)
    invalidate_participant_plans(session, campaign)
    campaign_orgs = _stored_participants(session, campaign)
    share_targets: dict[tuple, dict[str, Any]] = {}
    for participant in campaign_orgs:
        if not participant.share_id and not participant.public_link:
            continue
        # Some older records have only a URL. Resolve that specific link through OCS before deleting it.
        key = (participant.share_id,) if participant.share_id else (None, participant.folder_path, participant.public_link)
        target = share_targets.setdefault(key, {
            "share_id": participant.share_id,
            "public_link": participant.public_link,
            "target_path": participant.folder_path,
            "campaign_org_ids": [],
        })
        target["campaign_org_ids"].append(participant.id)
    items = [
        {
            "action": "delete",
            "source_path": data["share_id"],
            "target_path": data["public_link"] or data["target_path"],
            "reason": "public_share",
            "metadata_json": data,
        }
        for data in share_targets.values()
    ]
    batch = create_batch(
        session,
        operation_type="delete_shares",
        campaign_id=campaign.id,
        user_id=_user(request),
        items=items,
        summary={"planned": len(items), "share_targets": len(items), **_campaign_operation_context(campaign, _participant_organizations(session, campaign_orgs))},
    )
    _start_participant_batch(session, batch)
    success = 0
    failed = 0
    participants_by_id = {item.id: item for item in campaign_orgs}
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            for item in session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all():
                share_id = item.source_path or ""
                try:
                    if not share_id:
                        shares = await client.shares_for_path(item.metadata_json["target_path"])
                        matches = [share for share in shares if str(share.get("share_type")) == "3"
                                   and share.get("url") == item.metadata_json["public_link"] and share.get("id")]
                        if len(matches) != 1:
                            raise AppError("SHARE_ID_UNKNOWN", "Не удалось определить ссылку по её адресу. Проверьте общий доступ в Nextcloud.", 409)
                        share_id = str(matches[0]["id"])
                    await client.delete_public_share(share_id)
                    for participant_id in item.metadata_json["campaign_org_ids"]:
                        participant = participants_by_id[participant_id]
                        participant.share_id = None
                        participant.public_link = None
                        participant.mail_subject = None
                        participant.mail_body = None
                        if participant.status == CampaignOrganizationStatus.link_created:
                            participant.status = CampaignOrganizationStatus.folder_created
                        participant.updated_at = utcnow()
                    item.status = OperationItemStatus.success
                    item.error = None
                    success += 1
                except Exception as exc:  # noqa: BLE001 - per-share errors are shown in the UI
                    item.status = OperationItemStatus.failed
                    item.error = str(exc)
                    failed += 1
                item.updated_at = utcnow()
                session.add(item)
                session.commit()
    except Exception:
        batch.status = BatchStatus.failed
        batch.finished_at = utcnow()
        session.commit()
        raise
    if success and not failed and campaign.status == CampaignStatus.links_created:
        campaign.status = CampaignStatus.folders_created
        campaign.updated_at = utcnow()
        session.add(campaign)
    batch.finished_at = utcnow()
    batch.status = BatchStatus.completed if failed == 0 else BatchStatus.completed_with_errors
    batch.summary_json = {**(batch.summary_json or {}), "deleted": success, "success": success, "failed": failed}
    session.add(batch)
    audit(session, user_id=_user(request), action="delete_campaign_shares", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    session.refresh(batch)
    return batch_payload(session, batch)


@router.get("/campaigns/{campaign_id}/mailing.csv")
def campaign_mailing_csv(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    rows = mailing_rows(campaign, _organizations_for_campaign(session, campaign), _campaign_orgs(session, campaign))
    return Response(
        csv_bytes(rows, MAILING_COLUMNS),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-mailing.csv"'},
    )


@router.get("/campaigns/{campaign_id}/mailing.xlsx")
def campaign_mailing_xlsx(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    rows = mailing_rows(campaign, _organizations_for_campaign(session, campaign), _campaign_orgs(session, campaign))
    return Response(
        xlsx_bytes(rows, MAILING_COLUMNS, sheet_name="Рассылка"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-mailing.xlsx"'},
    )


@router.get("/campaigns/{campaign_id}/links.csv")
def campaign_links_csv(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    rows = link_rows(campaign, _organizations_for_campaign(session, campaign), _campaign_orgs(session, campaign))
    columns = link_columns(campaign)
    return Response(
        csv_bytes(rows, columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _download_disposition("links", campaign, "csv")},
    )


@router.get("/campaigns/{campaign_id}/links.xlsx")
def campaign_links_xlsx(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    rows = link_rows(campaign, _organizations_for_campaign(session, campaign), _campaign_orgs(session, campaign))
    columns = link_columns(campaign)
    return Response(
        xlsx_bytes(rows, columns, sheet_name="Ссылки"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _download_disposition("links", campaign, "xlsx")},
    )



def _check_summary(classified, missing: list[Organization], cleanup_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"total": len(classified), "missing": len(missing)}
    for item in classified:
        summary[item.classification] = summary.get(item.classification, 0) + 1
    summary["missing_organization_codes"] = [org.code for org in missing]
    summary["missing_organization_names"] = [org.display_name or org.code for org in missing]
    summary.update(cleanup_summary or {})
    return summary


def _missing_organizations_from_classified(organizations: list[Organization], classified, unverified_org_ids) -> list[Organization]:
    seen_org_ids = {item.organization_id for item in classified if item.organization_id}
    return sorted(
        [org for org in organizations if org.id not in seen_org_ids and org.id not in unverified_org_ids],
        key=organization_sort_key,
    )


def _planned_scan_file_operations(campaign: Campaign, classified) -> tuple[dict[str, dict[str, str | None]], dict[str, int]]:
    operations: dict[str, dict[str, str | None]] = {}
    summary = {
        "trash_moved": 0,
        "duplicates_moved": 0,
        "renamed": 0,
        "operation_errors": 0,
    }
    for item in classified:
        if _is_cleanup_path(campaign, item.candidate.path):
            continue
        if item.classification != "valid":
            folder = CLEANUP_DUPLICATES_FOLDER if item.classification == "duplicate" else CLEANUP_TRASH_FOLDER
            action = "move_duplicate" if item.classification == "duplicate" else "move_trash"
            operations[item.candidate.path] = {
                "action": action,
                "target_path": _cleanup_target_path(campaign, folder, item.candidate.path),
                "status": "planned",
                "error": None,
            }
            if action == "move_duplicate":
                summary["duplicates_moved"] += 1
            else:
                summary["trash_moved"] += 1
            continue
        parent = PurePosixPath(item.candidate.path).parent
        target_path = str(parent / item.normalized_name)
        if not target_path.startswith("/"):
            target_path = f"/{target_path}"
        if target_path == item.candidate.path:
            continue
        operations[item.candidate.path] = {
            "action": "rename_kept",
            "target_path": target_path,
            "status": "planned",
            "error": None,
        }
        summary["renamed"] += 1
    return operations, summary


def _latest_check_result(session: Session, campaign_id: str) -> CheckResult | None:
    results = session.exec(
        select(CheckResult).where(CheckResult.campaign_id == campaign_id).order_by(CheckResult.created_at.desc())
    ).all()
    return next((item for item in results if not (item.summary_json or {}).get("internal_message_reference")), None)


def _message_reference_check_result(session: Session, campaign: Campaign) -> CheckResult:
    check_result = _latest_check_result(session, campaign.id)
    if check_result is not None:
        return check_result
    internal = session.exec(
        select(CheckResult).where(CheckResult.campaign_id == campaign.id).order_by(CheckResult.created_at.desc())
    ).all()
    existing = next((item for item in internal if (item.summary_json or {}).get("internal_message_reference")), None)
    if existing is not None:
        return existing
    check_result = CheckResult(
        campaign_id=campaign.id,
        summary_json={"internal_message_reference": True},
    )
    session.add(check_result)
    session.commit()
    session.refresh(check_result)
    return check_result


def _message_items_for_missing(session: Session, campaign: Campaign, check_result: CheckResult) -> list[dict[str, Any]]:
    missing = missing_organizations(session, campaign)
    if not missing:
        return []
    campaign_orgs = _campaign_orgs(session, campaign)
    campaign_org_by_org_id = {item.organization_id: item for item in campaign_orgs}
    result: list[dict[str, Any]] = []
    for org in missing:
        campaign_org = campaign_org_by_org_id.get(org.id)
        if campaign_org is None:
            continue
        subject, body = missing_file_mail_values(campaign, campaign_org)
        result.append(
            {
                "campaign_id": campaign.id,
                "check_result_id": check_result.id,
                "organization_id": org.id,
                "organization_code": org.code,
                "organization_name": org.display_name,
                "to_email": org.email or "",
                "extra_emails": org.extra_emails or [],
                "subject": subject,
                "body": body,
                "status": "pending" if org.email else "failed",
                "last_error": None if org.email else "У организации не указан email.",
            }
        )
    return result


def _message_items_for_all_links(session: Session, campaign: Campaign, check_result: CheckResult) -> list[dict[str, Any]]:
    organizations = _organizations_for_campaign(session, campaign)
    campaign_org_by_org_id = {
        item.organization_id: item
        for item in _campaign_orgs(session, campaign)
    }
    result: list[dict[str, Any]] = []
    for org in organizations:
        campaign_org = campaign_org_by_org_id.get(org.id)
        if campaign_org is None:
            continue
        subject, body = upload_link_mail_values(campaign, campaign_org)
        error = None
        if not campaign_org.public_link:
            error = "Для организации не создана ссылка для загрузки."
        elif not org.email:
            error = "У организации не указан email."
        result.append(
            {
                "campaign_id": campaign.id,
                "check_result_id": check_result.id,
                "organization_id": org.id,
                "organization_code": org.code,
                "organization_name": org.display_name,
                "to_email": org.email or "",
                "extra_emails": org.extra_emails or [],
                "subject": subject,
                "body": body,
                "status": "pending" if not error else "failed",
                "last_error": error,
                "has_link": bool(campaign_org.public_link),
            }
        )
    return result


def _message_batch_payload(session: Session, batch: MessageBatch) -> dict[str, Any]:
    items = session.exec(select(MessageItem).where(MessageItem.batch_id == batch.id).order_by(MessageItem.created_at)).all()
    return {
        "batch_id": batch.id,
        "message_batch_id": batch.id,
        "campaign_id": batch.campaign_id,
        "check_result_id": batch.check_result_id,
        "operation_type": "messages",
        "status": batch.status.value if hasattr(batch.status, "value") else str(batch.status),
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "summary": batch.summary_json or {},
        "items": [item.model_dump(mode="json") for item in items],
    }


def _scan_root(session: Session, campaign: Campaign, payload: ScanIn | None) -> str:
    root = settings_map(session, include_secret=True).get("root_path") or campaign.parent_nc_path
    folder = ensure_under_root(campaign.campaign_folder_path, root)
    if payload and payload.path and ensure_under_root(payload.path, folder) != folder:
        raise AppError("FULL_CAMPAIGN_REQUIRED", "Для проверки всех участников выберите папку этого мониторинга.", 400)
    return folder


def _scan_context(session: Session, campaign: Campaign, request: Request, organizations: list[Organization]) -> str:
    last_check = _latest_check_result(session, campaign.id)
    connection = nextcloud_overrides(session, _user(request))
    context = {
        "path": campaign.campaign_folder_path,
        "folder_file_id": campaign.folder_file_id,
        "upload_mode": campaign_upload_mode(campaign),
        "layout": campaign_org_layout(campaign),
        "types": campaign.selected_org_types,
        "extensions": campaign.allowed_extensions,
        "check": last_check.id if last_check else None,
        "connection": [connection.get(key) for key in ("nc_user", "root_path")],
        "organizations": sorted((org.id, org.type, org.code, org.display_name, org.prefix, org.aliases, org.active) for org in organizations),
        "folders": sorted((row.organization_id, row.folder_path) for row in _campaign_orgs(session, campaign)),
    }
    return sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


async def _scan_organization_folders(
    session: Session,
    campaign: Campaign,
    request: Request,
    payload: ScanIn | None = None,
) -> tuple[list[ClassifiedFile], dict[str, Any]]:
    scan_path = _scan_root(session, campaign, payload)
    organizations = _organizations_for_campaign(session, campaign)
    org_by_id = {org.id: org for org in organizations}
    summary: dict[str, Any] = {
        "folder_presence_only": True,
        "scan_mode": "folder_presence",
        "renamed": 0,
        "duplicates_moved": 0,
        "trash_moved": 0,
        "operation_errors": 0,
        "folders_with_files": 0,
        "folders_without_files": 0,
        "unverified_organization_ids": [],
        "not_applicable_fields": ["renamed", "duplicates_moved", "trash_moved"],
    }
    classified: list[ClassifiedFile] = []
    seen_org_ids: set[str] = set()
    folder_errors: list[dict[str, str]] = []

    async with NextcloudClient(_nc_credentials(session, request)) as client:
        for campaign_org in _campaign_orgs(session, campaign):
            if campaign_org.organization_id in seen_org_ids:
                continue
            seen_org_ids.add(campaign_org.organization_id)
            organization = org_by_id.get(campaign_org.organization_id)
            if organization is None:
                continue
            folder_path = campaign_org.folder_path
            try:
                folder_path = ensure_under_root(folder_path, scan_path)
                entries = await list_scan_folder(client, folder_path)
            except AppError as exc:
                summary["operation_errors"] += 1
                summary["unverified_organization_ids"].append(organization.id)
                folder_errors.append({"folder_path": folder_path, "organization_id": organization.id, "organization_name": organization.display_name, "code": exc.code, "error": exc.message})
                continue
            files = [
                entry
                for entry in entries
                if not entry.is_collection and entry.path != folder_path and not _is_cleanup_path(campaign, entry.path)
            ]
            if not files:
                summary["folders_without_files"] += 1
                continue
            summary["folders_with_files"] += 1
            for entry in files:
                classified.append(
                    ClassifiedFile(
                        FileCandidate(
                            path=entry.path,
                            name=entry.name,
                            size=entry.size,
                            mtime=entry.mtime,
                            mime_type=entry.mime_type,
                        ),
                        entry.name,
                        "valid",
                        "not_applicable",
                        organization.id,
                    )
                )

    summary["unverified"] = len(folder_errors)
    if folder_errors:
        summary["folder_errors"] = folder_errors
    return classified, summary


def _scan_payload(
    session: Session,
    campaign: Campaign,
    classified,
    *,
    status: str,
    file_operations: dict[str, dict[str, str | None]] | None = None,
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    file_operations = file_operations or {}
    organizations = _organizations_for_campaign(session, campaign)
    org_by_id = {org.id: org for org in organizations}
    unverified = set((extra_summary or {}).get("unverified_organization_ids", []))
    missing = _missing_organizations_from_classified(organizations, classified, unverified)
    summary = _check_summary(classified, missing, extra_summary)
    return {
        "campaign_id": campaign.id,
        "operation_type": "scan",
        "status": status,
        "summary": summary,
        "files": [
            {
                "path": item.candidate.path,
                "original_name": item.candidate.name,
                "normalized_name": item.normalized_name,
                "classification": item.classification,
                "reason": item.reason,
                "organization_id": item.organization_id,
                "organization_code": org_by_id[item.organization_id].code if item.organization_id in org_by_id else None,
                "organization_name": org_by_id[item.organization_id].display_name if item.organization_id in org_by_id else None,
                "file_action": file_operations.get(item.candidate.path, {}).get("action"),
                "target_path": file_operations.get(item.candidate.path, {}).get("target_path"),
                "operation_status": file_operations.get(item.candidate.path, {}).get("status"),
                "operation_error": file_operations.get(item.candidate.path, {}).get("error"),
                "size": item.candidate.size,
                "mtime": item.candidate.mtime.isoformat() if item.candidate.mtime else None,
            }
            for item in classified
        ],
    }


@router.post("/campaigns/{campaign_id}/scan/preview")
async def scan_preview(campaign_id: str, session: SessionDep, request: Request, payload: ScanIn | None = None) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    batch_id = None
    try:
        path = _scan_root(session, campaign, payload)
        await _verify_campaign_root(session, request, campaign)
        if campaign_upload_mode(campaign) == "organization":
            classified, summary = await _scan_organization_folders(session, campaign, request, payload)
            operations = {}
        else:
            user = _user(request)
            organizations = _organizations_for_campaign(session, campaign)
            context = _scan_context(session, campaign, request, organizations)
            extensions = payload.allowed_extensions if payload and payload.allowed_extensions else campaign.allowed_extensions
            async with NextcloudClient(_nc_credentials(session, request)) as client:
                classified = await shared_files(client, campaign, organizations, extensions, CLEANUP_FOLDERS)
            operations, summary = _planned_scan_file_operations(campaign, classified)
            batch = create_batch(session, operation_type="scan_cleanup", user_id=user, campaign_id=campaign.id,
                                 items=scan_plan_items(classified, operations), summary={"context": context, "extensions": extensions})
            batch_id = batch.id
    except AppError as exc:
        _raise(exc)
    result = _scan_payload(session, campaign, classified, status="preview", file_operations=operations, extra_summary=summary)
    result["summary"]["path"] = path
    result["batch_id"] = batch_id
    return result


async def _apply_shared_scan(session, campaign, request, payload):
    user = _user(request)
    if not payload or not payload.batch_id:
        raise AppError("PREVIEW_REQUIRED", "Сначала проверьте файлы и просмотрите план обработки.", 409)
    batch = _get_batch(session, payload.batch_id)
    if batch.campaign_id != campaign.id or batch.created_by != user or batch.operation_type != "scan_cleanup":
        raise AppError("INVALID_SCAN_PREVIEW", "Предпросмотр не соответствует мониторингу или пользователю.", 409)
    plan = batch.summary_json
    organizations = _organizations_for_campaign(session, campaign)
    if (batch.status != BatchStatus.preview or plan.get("context") != _scan_context(session, campaign, request, organizations)
            or (payload.allowed_extensions is not None and payload.allowed_extensions != plan["extensions"])):
        raise AppError("STALE_SCAN_PREVIEW", "Предпросмотр устарел. Выполните проверку повторно.", 409)
    async with NextcloudClient(_nc_credentials(session, request)) as client:
        current = await shared_files(client, campaign, organizations, plan["extensions"], CLEANUP_FOLDERS)
        # Serialize apply for this campaign. Keep the claim, outcomes and saved check in one transaction.
        session.exec(update(Campaign).where(Campaign.id == campaign.id).values(id=campaign.id))
        session.expire_all()
        if _scan_context(session, campaign, request, _organizations_for_campaign(session, campaign)) != plan["context"]:
            raise AppError("STALE_SCAN_PREVIEW", "Мониторинг изменился. Повторите проверку.", 409)
        claimed = session.exec(update(OperationBatch).where(OperationBatch.id == batch.id, OperationBatch.status == BatchStatus.preview)
                               .values(status=BatchStatus.running, started_at=utcnow()))
        if claimed.rowcount != 1:
            raise AppError("STALE_SCAN_PREVIEW", "Этот предпросмотр уже обработан. Повторите проверку.", 409)
        items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id).order_by(OperationItem.created_at)).all()
        verified, paths, operations, summary, uncertain = await execute_scan_plan(client, campaign, items, current)
    verified_orgs = {item.organization_id for item in verified if item.classification == "valid"}
    summary["unverified_organization_ids"] = sorted(uncertain - verified_orgs)
    summary["unverified"] = len(summary["unverified_organization_ids"])
    batch.finished_at = utcnow()
    batch.status = BatchStatus.completed_with_errors if summary["operation_errors"] or summary["skipped"] else BatchStatus.completed
    batch.summary_json = {"planned": len(items), **summary}
    session.add(batch)
    return verified, paths, operations, summary, batch.id


@router.post("/campaigns/{campaign_id}/scan/apply")
async def scan_apply(campaign_id: str, session: SessionDep, request: Request, payload: ScanIn | None = None) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    try:
        path = _scan_root(session, campaign, payload)
        await _verify_campaign_root(session, request, campaign)
        if campaign_upload_mode(campaign) == "organization" and not (payload and payload.batch_id):
            classified, summary = await _scan_organization_folders(session, campaign, request, payload)
            paths, operations, batch_id = {}, {}, None
        else:
            classified, paths, operations, summary, batch_id = await _apply_shared_scan(session, campaign, request, payload)
    except AppError as exc:
        session.rollback()
        _raise(exc)
    campaign_orgs = _campaign_orgs(session, campaign)
    persist_scan_results(session, campaign, classified, campaign_orgs, paths, set(summary.get("unverified_organization_ids", [])))
    status = "completed_with_errors" if summary.get("operation_errors") or summary.get("skipped") else "completed"
    summary["path"] = path
    result = _scan_payload(session, campaign, classified, status=status, file_operations=operations, extra_summary=summary)
    check_result = CheckResult(campaign_id=campaign.id, operation_batch_id=batch_id, summary_json=result["summary"], created_by=_user(request))
    session.add(check_result)
    # Queueing a reviewed batch claims its preview status atomically as well.
    session.exec(update(MessageBatch).where(
        MessageBatch.campaign_id == campaign.id,
        MessageBatch.status == BatchStatus.preview,
        MessageBatch.queued_at.is_(None),
        func.coalesce(MessageBatch.summary_json["mailing_kind"].as_string(), "missing") == "missing",
    ).values(status=BatchStatus.cancelled, finished_at=utcnow()))
    audit(session, user_id=_user(request), action="scan_apply", entity_type="campaign", entity_id=campaign.id,
          after={"files": len(classified), "check_result_id": check_result.id, "cleanup_batch_id": batch_id})
    session.commit()
    result.update(batch_id=batch_id, check_result_id=check_result.id, check_result_created_at=check_result.created_at.isoformat())
    return result


@router.get("/campaigns/{campaign_id}/files")
def campaign_files(campaign_id: str, session: SessionDep) -> list[dict[str, Any]]:
    _get_campaign(session, campaign_id, allow_conflict=True)
    files = session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign_id)).all()
    return [_model_json(item) for item in files]


@router.get("/campaigns/{campaign_id}/missing")
def campaign_missing(campaign_id: str, session: SessionDep) -> list[dict[str, Any]]:
    campaign = _get_campaign(session, campaign_id)
    return [_model_json(org) for org in missing_organizations(session, campaign)]


@router.get("/campaigns/{campaign_id}/report.xlsx")
def campaign_report(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    files = session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all()
    missing = missing_organizations(session, campaign)
    check = _latest_check_result(session, campaign.id)
    return Response(
        report_workbook(campaign, files, missing, check.summary_json if check else None),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _download_disposition("report", campaign, "xlsx")},
    )


@router.get("/campaigns/{campaign_id}/message.txt")
def campaign_message(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    text = missing_message(campaign, missing_by_type(missing_organizations(session, campaign)))
    return Response(
        text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _download_disposition("message", campaign, "txt")},
    )



@router.get("/campaigns/{campaign_id}/operation-state")
def campaign_operation_state(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    campaign_orgs = _campaign_orgs(session, campaign)
    organizations = _organizations_for_campaign(session, campaign)
    files = session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all()
    check_result = _latest_check_result(session, campaign.id)
    message_batch = session.exec(
        select(MessageBatch)
        .where(MessageBatch.campaign_id == campaign.id)
        .order_by(MessageBatch.created_at.desc())
    ).first()
    folder_done = len([item for item in campaign_orgs if item.status in {CampaignOrganizationStatus.folder_created, CampaignOrganizationStatus.link_created, CampaignOrganizationStatus.uploaded, CampaignOrganizationStatus.missing, CampaignOrganizationStatus.rejected}])
    links_done = len([item for item in campaign_orgs if item.public_link])
    counts: dict[str, int] = {"total": len(files), "missing": len(missing_organizations(session, campaign))}
    for file_record in files:
        key = file_record.classification.value if hasattr(file_record.classification, "value") else str(file_record.classification)
        counts[key] = counts.get(key, 0) + 1
    return {
        "campaign_id": campaign.id,
        "organizations_total": len(organizations),
        "folders": {"done": folder_done, "total": len(campaign_orgs), "last_at": None},
        "shares": {"done": links_done, "total": len(campaign_orgs), "errors": 0},
        "check_result": check_result.model_dump(mode="json") if check_result else None,
        "check_summary": check_result.summary_json if check_result else counts,
        "message_batch": message_batch.model_dump(mode="json") if message_batch else None,
    }


@router.get("/campaigns/{campaign_id}/operation-history")
def campaign_operation_history(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id, allow_conflict=True)
    organizations = _organizations_selected_for_campaign(session, campaign) if campaign.folder_conflict else _organizations_for_campaign(session, campaign)
    batches = session.exec(
        select(OperationBatch)
        .where(OperationBatch.campaign_id == campaign.id,
               OperationBatch.operation_type.in_(["create_folders", "create_shares", "delete_shares", "rename"]))
        .order_by(OperationBatch.created_at.desc())
    ).all()
    checks = session.exec(
        select(CheckResult)
        .where(CheckResult.campaign_id == campaign.id)
        .order_by(CheckResult.created_at.desc())
    ).all()
    checks = [item for item in checks if not (item.summary_json or {}).get("internal_message_reference")]
    message_batches = session.exec(
        select(MessageBatch)
        .where(MessageBatch.campaign_id == campaign.id)
        .order_by(MessageBatch.created_at.desc())
    ).all()
    batch_runs = [_operation_batch_history_payload(session, batch) for batch in batches]
    check_runs = [_check_result_history_payload(session, item) for item in checks]
    message_runs = [_message_batch_history_payload(session, batch) for batch in message_batches]
    folders = [run for run in batch_runs if run["operation_type"] == "create_folders"]
    links = [run for run in batch_runs if run["operation_type"] in {"create_shares", "delete_shares"}]
    rename = [run for run in batch_runs if run["operation_type"] == "rename"]
    log = [
        *[_log_entry_from_run(run, "folders") for run in folders],
        *[_log_entry_from_run(run, "links") for run in links],
        *[_log_entry_from_run(run, "scan") for run in check_runs],
        *[_log_entry_from_run(run, "messages") for run in message_runs],
        *[_log_entry_from_run(run, "rename") for run in rename],
    ]
    log.sort(key=lambda item: item.get("started_at") or item.get("created_at") or "", reverse=True)
    return {
        "campaign_id": campaign.id,
        "context": _campaign_operation_context(campaign, organizations),
        "log": log,
        "folders": {"runs": folders},
        "links": {"runs": links},
        "scan": {"runs": check_runs},
        "messages": {"runs": message_runs},
        "rename": {"runs": rename},
    }


@router.post("/campaigns/{campaign_id}/messages/preview")
def messages_preview(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    check_result = _latest_check_result(session, campaign.id)
    if check_result is None:
        raise HTTPException(status_code=400, detail={"code": "CHECK_RESULT_REQUIRED", "message": "Сначала выполните и сохраните проверку файлов."})
    items = _message_items_for_missing(session, campaign, check_result)
    preview_item = next((item for item in items if item.get("body")), None)
    return {
        "operation_type": "messages",
        "status": "preview",
        "campaign_id": campaign.id,
        "check_result_id": check_result.id,
        "check_result_created_at": check_result.created_at.isoformat(),
        "summary": {
            "mailing_kind": "missing",
            "planned": len(items),
            "missing": len(items),
            "without_email": len([item for item in items if not item.get("to_email")]),
            "check_result_created_at": check_result.created_at.isoformat(),
            "preview_text": (preview_item or {}).get("body") or "",
        },
        "items": items,
    }


@router.post("/campaigns/{campaign_id}/messages/all-links/preview")
def all_links_messages_preview(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    check_result = _message_reference_check_result(session, campaign)
    items = _message_items_for_all_links(session, campaign, check_result)
    if not items or not any(item.get("has_link") for item in items):
        raise HTTPException(
            status_code=400,
            detail={"code": "PUBLIC_LINKS_REQUIRED", "message": "Сначала создайте ссылки для загрузки."},
        )
    preview_item = next((item for item in items if item.get("body")), None)
    return {
        "operation_type": "messages",
        "status": "preview",
        "campaign_id": campaign.id,
        "check_result_id": check_result.id,
        "summary": {
            "mailing_kind": "all_links",
            "planned": len(items),
            "pending": len([item for item in items if item.get("status") == "pending"]),
            "failed": len([item for item in items if item.get("status") != "pending"]),
            "without_email": len([item for item in items if not item.get("to_email")]),
            "without_link": len([item for item in items if not item.get("has_link")]),
            "preview_text": (preview_item or {}).get("body") or "",
        },
        "items": items,
    }


def _create_message_batch(
    session: Session,
    campaign: Campaign,
    check_result: CheckResult,
    user_id: str | None,
    *,
    mailing_kind: str = "missing",
) -> MessageBatch:
    planned = (
        _message_items_for_all_links(session, campaign, check_result)
        if mailing_kind == "all_links"
        else _message_items_for_missing(session, campaign, check_result)
    )
    without_email = len([item for item in planned if not item.get("to_email")])
    without_link = len([item for item in planned if not item.get("has_link", True)])
    preview_item = next((item for item in planned if item.get("body")), None)
    pending = len([item for item in planned if item.get("status") == "pending"])
    batch = MessageBatch(
        campaign_id=campaign.id,
        check_result_id=check_result.id,
        status=BatchStatus.preview,
        created_by=user_id,
        summary_json={
            "mailing_kind": mailing_kind,
            "planned": len(planned),
            "missing": len(planned) if mailing_kind == "missing" else 0,
            "pending": pending,
            "failed": len(planned) - pending,
            "without_email": without_email,
            "without_link": without_link,
            "check_result_created_at": check_result.created_at.isoformat(),
            "preview_text": (preview_item or {}).get("body") or "",
        },
    )
    session.add(batch)
    session.flush()
    for item in planned:
        session.add(
            MessageItem(
                batch_id=batch.id,
                campaign_id=campaign.id,
                check_result_id=check_result.id,
                organization_id=item.get("organization_id"),
                to_email=item.get("to_email") or "",
                extra_emails=item.get("extra_emails") or [],
                subject=item.get("subject") or "",
                body=item.get("body") or "",
                status=MailItemStatus.pending if item.get("status") == "pending" else MailItemStatus.failed,
                last_error=item.get("last_error"),
            )
        )
    audit(
        session,
        user_id=user_id,
        action="messages_prepare",
        entity_type="message_batch",
        entity_id=batch.id,
        after={"check_result_id": check_result.id, "count": len(planned), "mailing_kind": mailing_kind},
    )
    session.flush()
    return batch


def _start_or_finish_message_batch(batch: MessageBatch, session: Session, request: Request) -> dict[str, Any]:
    _lock_participant_changes(session, batch.campaign_id)
    campaign = _get_campaign(session, batch.campaign_id)
    if campaign.status == CampaignStatus.archived:
        raise AppError("CAMPAIGN_ARCHIVED", "Мониторинг в архиве. Отправка писем недоступна.", 409)
    session.refresh(batch)
    if batch.status == BatchStatus.preview and (batch.summary_json or {}).get("mailing_kind") != "all_links":
        latest = _latest_check_result(session, campaign.id)
        if latest is None or batch.check_result_id != latest.id:
            raise AppError("STALE_MESSAGE_BATCH", "Результат проверки обновился. Сформируйте напоминания заново.", 409)
    queue_batch(session, batch, _user(request))
    session.commit()
    return _message_batch_payload(session, batch)


@router.post("/campaigns/{campaign_id}/messages/prepare")
def messages_prepare(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    check_result = _latest_check_result(session, campaign.id)
    if check_result is None:
        raise HTTPException(status_code=400, detail={"code": "CHECK_RESULT_REQUIRED", "message": "Сначала выполните и сохраните проверку файлов."})
    batch = _create_message_batch(session, campaign, check_result, _user(request))
    session.commit()
    return _message_batch_payload(session, batch)


@router.post("/campaigns/{campaign_id}/messages/prepare-send")
async def messages_prepare_send(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    check_result = _latest_check_result(session, campaign.id)
    if check_result is None:
        raise HTTPException(status_code=400, detail={"code": "CHECK_RESULT_REQUIRED", "message": "Сначала выполните и сохраните проверку файлов."})
    batch = _create_message_batch(session, campaign, check_result, _user(request))
    return _start_or_finish_message_batch(batch, session, request)


@router.post("/campaigns/{campaign_id}/messages/all-links/prepare-send")
async def all_links_messages_prepare_send(
    campaign_id: str,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    check_result = _message_reference_check_result(session, campaign)
    planned = _message_items_for_all_links(session, campaign, check_result)
    if not planned or not any(item.get("has_link") for item in planned):
        raise HTTPException(
            status_code=400,
            detail={"code": "PUBLIC_LINKS_REQUIRED", "message": "Сначала создайте ссылки для загрузки."},
        )
    batch = _create_message_batch(
        session,
        campaign,
        check_result,
        _user(request),
        mailing_kind="all_links",
    )
    return _start_or_finish_message_batch(batch, session, request)


@router.post("/message-batches/{batch_id}/send")
async def messages_send(batch_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    batch = session.get(MessageBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_BATCH_NOT_FOUND", "message": "Пакет писем не найден."})
    return _start_or_finish_message_batch(batch, session, request)


@router.get("/message-batches/{batch_id}")
@router.post("/message-batches/{batch_id}")
def message_batch_get(batch_id: str, session: SessionDep) -> dict[str, Any]:
    batch = session.get(MessageBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_BATCH_NOT_FOUND", "message": "Пакет писем не найден."})
    return _message_batch_payload(session, batch)


@router.get("/message-batches/{batch_id}/xlsx")
def message_batch_xlsx(batch_id: str, session: SessionDep) -> Response:
    batch = session.get(MessageBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_BATCH_NOT_FOUND", "message": "Пакет писем не найден."})
    items = session.exec(select(MessageItem).where(MessageItem.batch_id == batch.id)).all()
    columns = ["Email", "Тема", "Текст", "Статус", "Ошибка"]
    rows = [
        {"Email": item.to_email, "Тема": item.subject, "Текст": item.body, "Статус": item.status.value if hasattr(item.status, "value") else str(item.status), "Ошибка": item.last_error or ""}
        for item in items
    ]
    return Response(
        xlsx_bytes(rows, columns, sheet_name="Письма"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="messages-{batch.id}.xlsx"'},
    )


@router.get("/message-batches/{batch_id}/txt")
def message_batch_txt(batch_id: str, session: SessionDep) -> Response:
    batch = session.get(MessageBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_BATCH_NOT_FOUND", "message": "Пакет писем не найден."})
    items = session.exec(select(MessageItem).where(MessageItem.batch_id == batch.id)).all()
    chunks = []
    for item in items:
        detail = f"\nКомментарий: {item.last_error}" if item.last_error else ""
        chunks.append(f"To: {item.to_email}\nSubject: {item.subject}\nStatus: {item.status.value}{detail}\n\n{item.body}\n")
    return Response(
        "\n---\n".join(chunks).encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="messages-{batch.id}.txt"'},
    )


@router.post("/bulk/rename/preview")
def bulk_rename_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    settings = settings_map(session, include_secret=True)
    root = settings.get("root_path") or "/"
    paths = payload.paths
    campaign = _get_campaign(session, payload.campaign_id) if payload.campaign_id else None
    if payload.campaign_id and not paths:
        template = _require_name_template(payload.name_template)
        files = session.exec(select(FileRecord).where(FileRecord.campaign_id == payload.campaign_id)).all()
        if not files:
            raise HTTPException(
                status_code=400,
                detail={"code": "SCAN_RESULTS_REQUIRED", "message": "Сначала выполните «Сохранить результаты проверки файлов»."},
            )
        org_ids = [file_record.organization_id for file_record in files if file_record.organization_id]
        org_by_id = {
            org.id: org
            for org in session.exec(select(Organization).where(Organization.id.in_(org_ids))).all()
        } if org_ids else {}
        items = []
        for file_record in files:
            organization = org_by_id.get(file_record.organization_id or "")
            if file_record.classification != FileClassification.valid or organization is None:
                continue
            new_name = _file_name_from_template(template, organization, file_record.current_path)
            parent = PurePosixPath(file_record.current_path).parent
            target_path = str(parent / new_name)
            if not target_path.startswith("/"):
                target_path = f"/{target_path}"
            try:
                target_path = ensure_under_root(target_path, root)
                source_path = ensure_under_root(file_record.current_path, root)
            except AppError as exc:
                _raise(exc)
            if target_path == source_path:
                continue
            items.append(
                {
                    "action": "rename",
                    "source_path": source_path,
                    "target_path": target_path,
                    "reason": "template_rename",
                    "metadata_json": {
                        "file_record_id": file_record.id,
                        "original_name": file_record.current_name,
                        "normalized_name": new_name,
                        "name_template": template,
                        "organization_id": organization.id,
                        "organization_code": organization.code,
                        "organization_name": organization.display_name,
                    },
                }
            )
        eligible = len([file_record for file_record in files if file_record.classification == FileClassification.valid and file_record.organization_id])
        summary = {
            "files_checked": len(files),
            "eligible": eligible,
            "planned": len(items),
            "already_ok": eligible - len(items),
            "skipped": len(files) - eligible,
            "name_template": template,
        }
        if campaign is not None:
            summary.update(_campaign_operation_context(campaign, _organizations_for_campaign(session, campaign)))
    else:
        try:
            items = planned_rename_items(root, paths)
        except AppError as exc:
            _raise(exc)
        summary = {"files_checked": len(paths), "planned": len(items), "already_ok": len(paths) - len(items), "name_template": payload.name_template or ""}
        if campaign is not None:
            summary.update(_campaign_operation_context(campaign, _organizations_for_campaign(session, campaign)))
    batch = create_batch(
        session,
        operation_type="rename",
        campaign_id=payload.campaign_id,
        user_id=_user(request),
        items=items,
        summary=summary,
    )
    return batch_payload(session, batch)


@router.post("/bulk/rename/apply")
async def bulk_rename_apply(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.batch_id:
        raise HTTPException(status_code=400, detail={"code": "PREVIEW_REQUIRED", "message": "Сначала выполните preview и передайте batch_id."})
    batch = _get_batch(session, payload.batch_id)
    if batch.campaign_id:
        campaign = _get_campaign(session, batch.campaign_id)
        if batch.status == BatchStatus.cancelled:
            raise AppError("STALE_PREVIEW", "Путь мониторинга изменился. Повторите предварительный просмотр.", 409)
        await _verify_campaign_root(session, request, campaign)
    if batch.operation_type != "rename":
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не является переименованием."})
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            batch = await apply_batch(session, batch, client)
    except AppError as exc:
        _raise(exc)
    for item in session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id, OperationItem.status == OperationItemStatus.success)).all():
        file_record_id = (item.metadata_json or {}).get("file_record_id")
        if not file_record_id:
            continue
        file_record = session.get(FileRecord, file_record_id)
        if file_record is None:
            continue
        file_record.current_path = item.target_path or file_record.current_path
        file_record.current_name = PurePosixPath(file_record.current_path).name
        file_record.updated_at = utcnow()
        session.add(file_record)
    audit(session, user_id=_user(request), action="bulk_rename_apply", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    return batch_payload(session, batch)


@router.post("/bulk/move/preview")
def bulk_move_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.campaign_id:
        raise HTTPException(status_code=400, detail={"code": "CAMPAIGN_REQUIRED", "message": "Для MVP перемещение планируется по результатам проверки мониторинга."})
    campaign = _get_campaign(session, payload.campaign_id)
    invalid_folder = CLEANUP_TRASH_FOLDER
    duplicates_folder = CLEANUP_DUPLICATES_FOLDER
    files = session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all()
    if not files:
        raise HTTPException(
            status_code=400,
            detail={"code": "SCAN_RESULTS_REQUIRED", "message": "Сначала выполните «Сохранить результаты проверки файлов»."},
        )
    items = []
    summary: dict[str, int] = {"files_checked": len(files), "planned": 0}
    for file_record in files:
        if file_record.classification == FileClassification.valid:
            continue
        reason = file_record.reason or file_record.classification.value
        folder = duplicates_folder if file_record.classification == FileClassification.duplicate else invalid_folder
        classification = file_record.classification.value if hasattr(file_record.classification, "value") else str(file_record.classification)
        summary[classification] = summary.get(classification, 0) + 1
        items.append(
            {
                "action": "move",
                "source_path": file_record.current_path,
                "target_path": target_invalid_path(campaign.campaign_folder_path, file_record.current_path, folder, reason),
                "reason": reason,
                "metadata_json": {"file_record_id": file_record.id, "classification": classification},
            }
        )
    summary["planned"] = len(items)
    batch = create_batch(
        session,
        operation_type="move",
        campaign_id=campaign.id,
        user_id=_user(request),
        items=items,
        summary=summary,
    )
    return batch_payload(session, batch)


@router.post("/bulk/move/apply")
async def bulk_move_apply(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.batch_id:
        raise HTTPException(status_code=400, detail={"code": "PREVIEW_REQUIRED", "message": "Сначала выполните preview и передайте batch_id."})
    batch = _get_batch(session, payload.batch_id)
    if batch.campaign_id:
        campaign = _get_campaign(session, batch.campaign_id)
        if batch.status == BatchStatus.cancelled:
            raise AppError("STALE_PREVIEW", "Путь мониторинга изменился. Повторите предварительный просмотр.", 409)
        await _verify_campaign_root(session, request, campaign)
    if batch.operation_type != "move":
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не является перемещением."})
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            batch = await apply_batch(session, batch, client)
    except AppError as exc:
        _raise(exc)
    for item in session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id, OperationItem.status == OperationItemStatus.success)).all():
        file_record_id = (item.metadata_json or {}).get("file_record_id")
        if not file_record_id:
            continue
        file_record = session.get(FileRecord, file_record_id)
        if file_record is None:
            continue
        file_record.current_path = item.target_path or file_record.current_path
        file_record.current_name = PurePosixPath(file_record.current_path).name
        file_record.updated_at = utcnow()
        session.add(file_record)
    audit(session, user_id=_user(request), action="bulk_move_apply", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    return batch_payload(session, batch)


@router.post("/bulk/create-folders/preview")
async def bulk_create_folders_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.campaign_id:
        raise HTTPException(status_code=400, detail={"code": "CAMPAIGN_REQUIRED", "message": "Укажите campaign_id."})
    return await create_folders_preview(payload.campaign_id, session, request)


@router.post("/bulk/create-folders/apply")
async def bulk_create_folders_apply(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.campaign_id or not payload.batch_id:
        raise HTTPException(status_code=400, detail={"code": "PREVIEW_REQUIRED", "message": "Укажите campaign_id и batch_id."})
    return await create_folders_apply(payload.campaign_id, session, request, payload.batch_id)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, session: SessionDep) -> dict[str, Any]:
    return batch_payload(session, _get_batch(session, batch_id))


@router.get("/batches/{batch_id}/items")
def get_batch_items(batch_id: str, session: SessionDep) -> list[dict[str, Any]]:
    _get_batch(session, batch_id)
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch_id)).all()
    return [_model_json(item) for item in items]


@router.post("/batches/{batch_id}/rollback")
def rollback_batch(batch_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    batch = _get_batch(session, batch_id)
    items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id, OperationItem.status == OperationItemStatus.success)).all()
    reverse_items = []
    for item in items:
        if item.action in {OperationAction.rename, OperationAction.move} and item.source_path and item.target_path:
            reverse_items.append(
                {
                    "action": "move",
                    "source_path": item.target_path,
                    "target_path": item.source_path,
                    "reason": f"rollback:{batch.id}",
                }
            )
    rollback = create_batch(
        session,
        operation_type="rollback",
        campaign_id=batch.campaign_id,
        user_id=_user(request),
        items=reverse_items,
        summary={"planned": len(reverse_items), "source_batch": batch.id},
    )
    return batch_payload(session, rollback)


@router.get("/access")
def access(session: SessionDep, is_admin: IsAdmin) -> dict[str, Any]:
    defaults = session.exec(select(SettingsEntry).where(SettingsEntry.key.in_(["root_path", "default_extensions"]))).all()
    return {"is_admin": is_admin, "defaults": {entry.key: entry.value_json for entry in defaults}}


@router.get("/settings", dependencies=[Depends(require_admin)])
def get_settings(session: SessionDep) -> dict[str, Any]:
    return settings_map(session)


@router.get("/settings/groups", dependencies=[Depends(require_admin)])
async def settings_groups(session: SessionDep, request: Request, search: str = "", limit: int = 200) -> dict[str, Any]:
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            groups = await client.list_groups(search=search, limit=limit)
    except AppError as exc:
        _raise(exc)
    return {"items": groups}


@router.put("/settings", dependencies=[Depends(require_admin)])
async def put_settings(payload: SettingsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    before = settings_map(session)
    values = payload.model_dump(exclude_unset=True)
    if values.get("root_path"):
        values["root_path"] = ensure_under_root(values["root_path"], values["root_path"])
    root_path = values.get("root_path") or before.get("root_path") or "/"
    if "collaboration_group" in values:
        values["collaboration_group"] = str(values.get("collaboration_group") or "").strip()
    group_id = str(values.get("collaboration_group") if "collaboration_group" in values else before.get("collaboration_group") or "").strip()
    if values.get("archive_folder") and str(values["archive_folder"]).startswith("/"):
        values["archive_folder"] = ensure_under_root(values["archive_folder"], root_path)
    try:
        async with NextcloudClient(_nc_credentials(session, request, root_path=root_path)) as client:
            root_access = await _root_access_status(client, root_path, group_id, ensure_share=True)
    except AppError as exc:
        _raise(exc)
    after = update_settings(session, values)
    audit(session, user_id=_user(request), action="update", entity_type="settings", before=before, after={**after, "root_access": root_access})
    session.commit()
    return {**after, "root_access": root_access}


@router.post("/settings/test-webdav", dependencies=[Depends(require_admin)])
async def test_webdav(session: SessionDep, request: Request, payload: SettingsIn | None = None) -> dict[str, Any]:
    try:
        settings = settings_map(session, include_secret=True)
        values = payload.model_dump(exclude_unset=True) if payload else {}
        root_path = values.get("root_path") or settings.get("root_path") or "/"
        root_path = ensure_under_root(root_path, root_path)
        group_id = str(values.get("collaboration_group") if "collaboration_group" in values else settings.get("collaboration_group") or "").strip()
        async with NextcloudClient(_nc_credentials(session, request, root_path=root_path)) as client:
            return await _root_access_status(client, root_path, group_id, ensure_share=False)
    except AppError as exc:
        _raise(exc)


@router.post("/settings/test-mail", dependencies=[Depends(require_admin)])
async def test_mail(session: SessionDep, request: Request) -> dict[str, Any]:
    user_id = _user(request)
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            email = await client.user_email(user_id or "")
            await client.send_service_mail(
                to_email=email,
                subject="Проверка почты: Сбор файлов ОО",
                body=(
                    "Это тестовое письмо приложения «Сбор файлов ОО».\n\n"
                    f"Проверку запустил пользователь Nextcloud: {user_id or 'не определён'}.\n"
                    f"Время запуска: {utcnow().isoformat(timespec='seconds')}.\n\n"
                    "Если вы получили это письмо, почтовый мост приложения работает."
                ),
            )
    except AppError as exc:
        _raise(exc)
    audit(session, user_id=user_id, action="test_mail", entity_type="settings", after={"email": email})
    session.commit()
    return {"status": "ok", "email": email}


@router.post("/settings/test-share-api", dependencies=[Depends(require_admin)])
async def test_share_api(session: SessionDep, request: Request) -> dict[str, Any]:
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            return await client.test_share_api()
    except AppError as exc:
        _raise(exc)
