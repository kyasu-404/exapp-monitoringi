"""FastAPI routes for the ExApp backend."""

from __future__ import annotations

import base64
import binascii
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlmodel import Session, delete, select

from .algorithms import classify_files, normalize_filename, organization_sort_key
from .db import get_session
from .errors import AppError
from .models import (
    BatchStatus,
    Campaign,
    CampaignOrganization,
    CampaignOrganizationStatus,
    CampaignStatus,
    FileClassification,
    FileRecord,
    MailTemplate,
    OperationAction,
    OperationBatch,
    OperationItem,
    OperationItemStatus,
    Organization,
    SettingsEntry,
    utcnow,
)
from .nextcloud import NextcloudClient, configured_credentials
from .reports import LINK_COLUMNS, MAILING_COLUMNS, csv_bytes, link_rows, mailing_rows, missing_message, report_workbook, xlsx_bytes
from .schemas import (
    BulkPathsIn,
    CampaignIn,
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
    apply_organization_import,
    audit,
    batch_payload,
    create_batch,
    create_campaign_orgs,
    current_user_id,
    entries_to_candidates,
    mail_values_for_campaign_org,
    missing_by_type,
    missing_organizations,
    nextcloud_overrides,
    organization_type_label,
    parse_organization_table,
    persist_scan_results,
    planned_folder_items,
    planned_rename_items,
    settings_map,
    sync_campaign_orgs,
    target_invalid_path,
    update_settings,
    validate_organization_rows,
)

router = APIRouter(prefix="/api")


SessionDep = Annotated[Session, Depends(get_session)]
CLEANUP_DUPLICATES_FOLDER = "Дубли"
CLEANUP_TRASH_FOLDER = "Мусор"
CLEANUP_FOLDERS = {CLEANUP_DUPLICATES_FOLDER, CLEANUP_TRASH_FOLDER}


def _user(request: Request) -> str | None:
    return request.scope.get("username") or current_user_id(dict(request.headers))


def _nc_credentials(session: Session, request: Request):
    return configured_credentials(nextcloud_overrides(session, _user(request)))


def _raise(exc: AppError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
    ) from exc


def _model_json(model) -> dict[str, Any]:
    return model.model_dump(mode="json")


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
    if not campaigns:
        return []
    kept: list[Campaign] = []
    changed = False
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            for campaign in campaigns:
                try:
                    await client.propfind(campaign.campaign_folder_path, depth=0)
                    kept.append(campaign)
                except AppError as exc:
                    if exc.code != "WEBDAV_PATH_NOT_FOUND":
                        raise
                    _delete_campaign_record(session, campaign, user_id=_user(request), reason="webdav_path_not_found")
                    changed = True
    except AppError:
        raise
    if changed:
        session.commit()
    return kept


def _organization_create_data(payload: OrganizationIn) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    data["code"] = data["code"].strip()
    data["display_name"] = (data.get("display_name") or data["code"]).strip()
    data["short_name"] = data.get("short_name") or data["display_name"]
    data["prefix"] = data.get("prefix") or None
    data["extra_emails"] = data.get("extra_emails") or []
    data["aliases"] = data.get("aliases") or []
    data["active"] = data.get("active", True)
    return data


def _organization_patch_data(payload: OrganizationPatch, organization: Organization) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True, mode="json")
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


@router.post("/files-actions/open")
async def files_action_open(_payload: dict[str, Any], _request: Request) -> dict[str, str]:
    return {"redirect_handler": "main"}


def _get_campaign(session: Session, campaign_id: str) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "CAMPAIGN_NOT_FOUND", "message": "Мониторинг не найден."})
    return campaign


def _get_batch(session: Session, batch_id: str) -> OperationBatch:
    batch = session.get(OperationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Пакет операций не найден."})
    return batch


def _campaign_orgs(session: Session, campaign: Campaign) -> list[CampaignOrganization]:
    items = session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    if campaign.selected_org_types:
        organizations = session.exec(
            select(Organization).where(Organization.active == True, Organization.type.in_(campaign.selected_org_types))  # noqa: E712
        ).all()
        return sync_campaign_orgs(session, campaign, organizations) if items else create_campaign_orgs(session, campaign, organizations)
    if items:
        return items
    return []


def _organizations_for_campaign(session: Session, campaign: Campaign) -> list[Organization]:
    items = _campaign_orgs(session, campaign)
    ids = [item.organization_id for item in items]
    if not ids:
        return []
    return session.exec(select(Organization).where(Organization.id.in_(ids))).all()


def _default_mail_template(session: Session) -> MailTemplate:
    template = session.exec(select(MailTemplate).where(MailTemplate.is_default == True)).first()  # noqa: E712
    if template is None:
        raise HTTPException(status_code=500, detail={"code": "MAIL_TEMPLATE_MISSING", "message": "Шаблон письма не найден."})
    return template


async def _webdav_folder_exists(client: NextcloudClient, path: str) -> bool:
    try:
        entries = await client.propfind(path, depth=0)
    except AppError as exc:
        if exc.code == "WEBDAV_PATH_NOT_FOUND":
            return False
        raise
    return bool(entries and entries[0].is_collection)


def _is_cleanup_path(campaign: Campaign, path: str) -> bool:
    campaign_parts = path_parts(campaign.campaign_folder_path)
    current_parts = path_parts(path)
    relative = current_parts[len(campaign_parts) :]
    return bool(relative and relative[0] in CLEANUP_FOLDERS)


def _cleanup_target_path(campaign: Campaign, folder: str, source_path: str) -> str:
    source = PurePosixPath(source_path)
    stamp = utcnow().strftime("%Y%m%d_%H%M%S")
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
            entries = await client.propfind(campaign.campaign_folder_path, depth=1)
    except AppError as exc:
        if exc.code == "WEBDAV_PATH_NOT_FOUND":
            raise HTTPException(
                status_code=400,
                detail={"code": "CAMPAIGN_FOLDER_NOT_FOUND", "message": "Папка мониторинга не найдена в Nextcloud."},
            ) from exc
        _raise(exc)

    campaign_depth = len(path_parts(campaign.campaign_folder_path))
    available_folders = {
        entry.path.rstrip("/")
        for entry in entries
        if entry.is_collection
        and entry.path.rstrip("/") != campaign.campaign_folder_path.rstrip("/")
        and len(path_parts(entry.path)) == campaign_depth + 1
        and path_parts(entry.path)[-1] not in CLEANUP_FOLDERS
    }
    missing = [item.folder_path for item in campaign_orgs if item.folder_path.rstrip("/") not in available_folders]
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
            detail={"code": "NAME_TEMPLATE_REQUIRED", "message": "Укажите шаблон имени файла. Например: ГБДОУ {номер} отчёт."},
        )
    if "{номер}" not in template:
        raise HTTPException(
            status_code=400,
            detail={"code": "NAME_TEMPLATE_PLACEHOLDER_REQUIRED", "message": "В шаблоне должен быть номер ОУ: добавьте {номер}."},
        )
    return template


def _file_name_from_template(template: str, organization: Organization, current_path: str) -> str:
    source = PurePosixPath(current_path)
    stem = clean_path_segment(template.replace("{номер}", organization.code), fallback=organization.code)
    return f"{stem}{source.suffix}"


async def _apply_scan_file_operations(
    session: Session,
    campaign: Campaign,
    request: Request,
    classified,
) -> tuple[dict[str, str], dict[str, dict[str, str | None]], dict[str, int]]:
    current_paths: dict[str, str] = {}
    operations: dict[str, dict[str, str | None]] = {}
    summary = {
        "trash_moved": 0,
        "duplicates_moved": 0,
        "renamed": 0,
        "operation_errors": 0,
    }

    async with NextcloudClient(_nc_credentials(session, request)) as client:
        await client.mkcol(join_nc_path(campaign.campaign_folder_path, CLEANUP_DUPLICATES_FOLDER))
        await client.mkcol(join_nc_path(campaign.campaign_folder_path, CLEANUP_TRASH_FOLDER))

        for item in classified:
            if _is_cleanup_path(campaign, item.candidate.path):
                continue
            if item.classification == "valid":
                continue
            folder = CLEANUP_DUPLICATES_FOLDER if item.classification == "duplicate" else CLEANUP_TRASH_FOLDER
            action = "move_duplicate" if item.classification == "duplicate" else "move_trash"
            target_path = _cleanup_target_path(campaign, folder, item.candidate.path)
            try:
                await client.move(item.candidate.path, target_path)
                current_paths[item.candidate.path] = target_path
                operations[item.candidate.path] = {"action": action, "target_path": target_path, "status": "success", "error": None}
                if item.classification == "duplicate":
                    summary["duplicates_moved"] += 1
                else:
                    summary["trash_moved"] += 1
            except Exception as exc:  # noqa: BLE001 - return per-file cleanup errors to UI
                summary["operation_errors"] += 1
                operations[item.candidate.path] = {"action": action, "target_path": target_path, "status": "failed", "error": str(exc)}

        for item in classified:
            if item.classification != "valid" or _is_cleanup_path(campaign, item.candidate.path):
                continue
            parent = PurePosixPath(item.candidate.path).parent
            target_path = str(parent / item.normalized_name)
            if not target_path.startswith("/"):
                target_path = f"/{target_path}"
            if target_path == item.candidate.path:
                continue
            try:
                await client.move(item.candidate.path, target_path)
                current_paths[item.candidate.path] = target_path
                operations[item.candidate.path] = {"action": "rename_kept", "target_path": target_path, "status": "success", "error": None}
                summary["renamed"] += 1
            except Exception as exc:  # noqa: BLE001 - return per-file cleanup errors to UI
                summary["operation_errors"] += 1
                operations[item.candidate.path] = {"action": "rename_kept", "target_path": target_path, "status": "failed", "error": str(exc)}

    return current_paths, operations, summary


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


@router.get("/organizations/{organization_id}")
def get_organization(organization_id: str, session: SessionDep) -> dict[str, Any]:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "ORGANIZATION_NOT_FOUND", "message": "Организация не найдена."})
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
    summary = apply_organization_import(session, rows)
    audit(session, user_id=_user(request), action="import", entity_type="organization", after=summary)
    session.commit()
    return {"apply": True, **summary}


@router.post("/organizations/import")
async def import_organizations(
    session: SessionDep,
    request: Request,
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
        apply=apply,
        allow_update=allow_update,
    )


@router.post("/organizations/import-json")
def import_organizations_json(
    payload: OrganizationImportJsonIn,
    session: SessionDep,
    request: Request,
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
        apply=apply,
        allow_update=allow_update,
    )


@router.get("/organizations/export.csv")
def export_organizations_csv(session: SessionDep) -> Response:
    organizations = session.exec(select(Organization).where(Organization.active == True)).all()  # noqa: E712
    organizations.sort(key=lambda org: (str(org.type), org.sort_order, organization_sort_key(org)))
    columns = ["Тип ОУ", "Номер", "Email"]
    rows = [
        {
            "Тип ОУ": organization_type_label(org.type.value if hasattr(org.type, "value") else org.type),
            "Номер": org.code,
            "Email": org.email or "",
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
    columns = ["Тип ОУ", "Номер", "Email"]
    data = [
        {
            "Тип ОУ": organization_type_label(org.type.value if hasattr(org.type, "value") else org.type),
            "Номер": org.code,
            "Email": org.email or "",
        }
        for org in organizations
    ]
    return Response(
        xlsx_bytes(data, columns, sheet_name="Список ОУ"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="organizations.xlsx"'},
    )


@router.post("/organizations/validate")
def validate_organizations(session: SessionDep) -> dict[str, Any]:
    organizations = session.exec(select(Organization)).all()
    rows = [org.model_dump(mode="json") for org in organizations]
    return {"rows": validate_organization_rows(session, rows)}


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
    try:
        parent = ensure_under_root(payload.parent_nc_path, root_path)
        folder = campaign_folder_path(parent, payload.date, payload.title)
        ensure_under_root(folder, root_path)
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            await client.mkcol(folder)
            entries = await client.propfind(folder, depth=0)
            if not entries or not entries[0].is_collection:
                raise AppError("WEBDAV_PATH_NOT_FOLDER", "Выбранный путь занят файлом, папку создать нельзя.", 400)
    except AppError as exc:
        _raise(exc)
    campaign = Campaign(
        title=payload.title,
        description=payload.description,
        date=payload.date,
        deadline=payload.deadline,
        parent_nc_path=parent,
        campaign_folder_path=folder,
        selected_org_types=[item.value for item in payload.selected_org_types],
        allowed_extensions=[item.lower().lstrip(".") for item in payload.allowed_extensions],
        allowed_mime_types=payload.allowed_mime_types,
        created_by=_user(request),
    )
    session.add(campaign)
    audit(session, user_id=_user(request), action="create", entity_type="campaign", entity_id=campaign.id, after=campaign.model_dump(mode="json"))
    session.commit()
    session.refresh(campaign)
    return _model_json(campaign)


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, session: SessionDep) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    return _model_json(campaign)


@router.put("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, payload: CampaignPatch, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    before = _model_json(campaign)
    data = payload.model_dump(exclude_unset=True)
    old_folder = campaign.campaign_folder_path
    if data.get("parent_nc_path") or data.get("date") or data.get("title"):
        date_value = data.get("date") or campaign.date
        title_value = data.get("title") or campaign.title
        parent_value = data.get("parent_nc_path") or campaign.parent_nc_path
        root_path = settings_map(session, include_secret=True).get("root_path") or parent_value
        data["parent_nc_path"] = ensure_under_root(parent_value, root_path)
        data["campaign_folder_path"] = campaign_folder_path(data["parent_nc_path"], date_value, title_value)
        ensure_under_root(data["campaign_folder_path"], root_path)
    if "selected_org_types" in data:
        data["selected_org_types"] = [str(item.value if hasattr(item, "value") else item) for item in data["selected_org_types"]]
    if "allowed_extensions" in data and data["allowed_extensions"] is not None:
        data["allowed_extensions"] = [str(item).lower().lstrip(".") for item in data["allowed_extensions"]]
    new_folder = data.get("campaign_folder_path", old_folder)
    if new_folder != old_folder:
        try:
            async with NextcloudClient(_nc_credentials(session, request)) as client:
                await client.move(old_folder, new_folder)
        except AppError as exc:
            _raise(exc)
    for key, value in data.items():
        setattr(campaign, key, value)
    campaign.updated_at = utcnow()
    session.add(campaign)
    if new_folder != old_folder:
        for item in session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all():
            if item.folder_path.startswith(old_folder.rstrip("/") + "/"):
                item.folder_path = new_folder.rstrip("/") + item.folder_path[len(old_folder.rstrip("/")) :]
                item.updated_at = utcnow()
                session.add(item)
        for file_record in session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all():
            for attr in ("original_path", "current_path"):
                value = getattr(file_record, attr)
                if value.startswith(old_folder.rstrip("/") + "/"):
                    setattr(file_record, attr, new_folder.rstrip("/") + value[len(old_folder.rstrip("/")) :])
            file_record.updated_at = utcnow()
            session.add(file_record)
    if "selected_org_types" in data:
        session.exec(delete(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id))
        if campaign.selected_org_types:
            organizations = session.exec(
                select(Organization).where(Organization.active == True, Organization.type.in_(campaign.selected_org_types))  # noqa: E712
            ).all()
            create_campaign_orgs(session, campaign, organizations)
    audit(session, user_id=_user(request), action="update", entity_type="campaign", entity_id=campaign.id, before=before, after=_model_json(campaign))
    session.commit()
    session.refresh(campaign)
    return _model_json(campaign)


@router.delete("/campaigns/{campaign_id}")
def archive_campaign(campaign_id: str, session: SessionDep, request: Request) -> dict[str, str]:
    campaign = _get_campaign(session, campaign_id)
    before = _model_json(campaign)
    campaign.status = CampaignStatus.archived
    campaign.updated_at = utcnow()
    session.add(campaign)
    audit(session, user_id=_user(request), action="archive", entity_type="campaign", entity_id=campaign.id, before=before, after=_model_json(campaign))
    session.commit()
    return {"status": "archived", "id": campaign.id}


@router.post("/campaigns/{campaign_id}/select-organizations")
def select_campaign_organizations(
    campaign_id: str,
    payload: SelectOrganizationsIn,
    session: SessionDep,
    request: Request,
) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    if payload.organization_ids:
        organizations = session.exec(select(Organization).where(Organization.id.in_(payload.organization_ids))).all()
    else:
        types = [item.value for item in (payload.organization_types or [])] or campaign.selected_org_types
        organizations = session.exec(select(Organization).where(Organization.active == True, Organization.type.in_(types))).all()  # noqa: E712
    session.exec(delete(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id))
    campaign_orgs = create_campaign_orgs(session, campaign, organizations)
    audit(session, user_id=_user(request), action="select_organizations", entity_type="campaign", entity_id=campaign.id, after={"count": len(campaign_orgs)})
    session.commit()
    return {"campaign_id": campaign.id, "count": len(campaign_orgs), "items": [_model_json(item) for item in campaign_orgs]}


@router.post("/campaigns/{campaign_id}/create-folders/preview")
def create_folders_preview(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    campaign_orgs = _campaign_orgs(session, campaign)
    organizations = _organizations_for_campaign(session, campaign)
    items = planned_folder_items(campaign, campaign_orgs, organizations)
    summary = {
        "folders_to_create": len([item for item in items if item["action"] == "create_folder"]),
        "folders_to_rename": len([item for item in items if item["action"] == "move"]),
        "organizations_without_email": len([org for org in organizations if not org.email]),
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
    if batch.status == BatchStatus.preview:
        batch.status = BatchStatus.running
        batch.started_at = utcnow()
        session.add(batch)
        session.commit()
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
                            campaign_org.folder_path = item.target_path or campaign_org.folder_path
                            if campaign_org.status == CampaignOrganizationStatus.pending:
                                campaign_org.status = CampaignOrganizationStatus.folder_created
                            campaign_org.updated_at = utcnow()
                            session.add(campaign_org)
                    session.commit()
        except AppError as exc:
            _raise(exc)
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
    for item in campaign_orgs:
        if item.folder_path in successful_targets:
            if item.status != CampaignOrganizationStatus.link_created:
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


@router.post("/campaigns/{campaign_id}/create-shares/preview")
async def create_shares_preview(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    campaign_orgs = _campaign_orgs(session, campaign)
    await _ensure_campaign_org_folders_exist(session, request, campaign, campaign_orgs)
    items = [
        {
            "action": "create_share",
            "target_path": item.folder_path,
            "reason": "public_upload_link",
            "metadata_json": {"campaign_org_id": item.id},
        }
        for item in campaign_orgs
        if not item.public_link
    ]
    summary = {"shares_to_create": len(items), "existing_links": len(campaign_orgs) - len(items)}
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
    options = options or ShareOptionsIn()
    template = _default_mail_template(session)
    org_by_id = {org.id: org for org in _organizations_for_campaign(session, campaign)}
    campaign_org_by_id = {item.id: item for item in _campaign_orgs(session, campaign)}
    await _ensure_campaign_org_folders_exist(session, request, campaign, list(campaign_org_by_id.values()))
    batch.status = BatchStatus.running
    batch.started_at = utcnow()
    session.add(batch)
    session.commit()
    success = 0
    failed = 0
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            items = session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id)).all()
            for item in items:
                campaign_org = campaign_org_by_id.get((item.metadata_json or {}).get("campaign_org_id"))
                if campaign_org is None:
                    item.status = OperationItemStatus.failed
                    item.error = "Campaign organization not found"
                    failed += 1
                    session.add(item)
                    continue
                try:
                    share = await client.create_public_share(
                        campaign_org.folder_path,
                        public_upload=options.public_upload,
                        expire_date=options.expire_date or campaign.deadline,
                        password=options.password,
                        permissions=options.permissions,
                        label=campaign.title,
                        note=f"{campaign_org.folder_path} · {campaign.title}",
                    )
                    campaign_org.share_id = str(share.get("share_id") or "")
                    campaign_org.public_link = str(share.get("public_link") or "")
                    campaign_org.status = CampaignOrganizationStatus.link_created
                    org = org_by_id.get(campaign_org.organization_id)
                    if org:
                        subject, body = mail_values_for_campaign_org(
                            campaign,
                            org,
                            campaign_org,
                            template.subject_template,
                            template.body_template,
                        )
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
    except AppError as exc:
        _raise(exc)
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
    return Response(
        csv_bytes(rows, LINK_COLUMNS),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-links.csv"'},
    )


@router.get("/campaigns/{campaign_id}/links.xlsx")
def campaign_links_xlsx(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    rows = link_rows(campaign, _organizations_for_campaign(session, campaign), _campaign_orgs(session, campaign))
    return Response(
        xlsx_bytes(rows, LINK_COLUMNS, sheet_name="Ссылки"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-links.xlsx"'},
    )


async def _scan_campaign(session: Session, campaign: Campaign, request: Request, payload: ScanIn | None = None):
    scan_path = payload.path if payload and payload.path else campaign.campaign_folder_path
    allowed_extensions = payload.allowed_extensions if payload and payload.allowed_extensions else campaign.allowed_extensions
    root = settings_map(session, include_secret=True).get("root_path") or campaign.parent_nc_path
    ensure_under_root(scan_path, root)
    organizations = _organizations_for_campaign(session, campaign)
    async with NextcloudClient(_nc_credentials(session, request)) as client:
        root_entries = await client.propfind(scan_path, depth=1)
        entries = [entry for entry in root_entries if not _is_cleanup_path(campaign, entry.path)]
        for entry in root_entries:
            if entry.is_collection and entry.path != scan_path and not _is_cleanup_path(campaign, entry.path):
                entries.extend([child for child in await client.propfind(entry.path, depth=1) if not _is_cleanup_path(campaign, child.path)])
    candidates = entries_to_candidates(entries, skip_root=scan_path)
    return classify_files(candidates, organizations, allowed_extensions)


def _scan_payload(
    session: Session,
    campaign: Campaign,
    classified,
    *,
    status: str,
    file_operations: dict[str, dict[str, str | None]] | None = None,
    extra_summary: dict[str, int] | None = None,
) -> dict[str, Any]:
    file_operations = file_operations or {}
    organizations = _organizations_for_campaign(session, campaign)
    org_by_id = {org.id: org for org in organizations}
    summary: dict[str, int] = {"total": len(classified)}
    for item in classified:
        summary[item.classification] = summary.get(item.classification, 0) + 1
    summary.update(extra_summary or {})
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
    try:
        classified = await _scan_campaign(session, campaign, request, payload)
    except AppError as exc:
        _raise(exc)
    return _scan_payload(session, campaign, classified, status="preview")


@router.post("/campaigns/{campaign_id}/scan/apply")
async def scan_apply(campaign_id: str, session: SessionDep, request: Request, payload: ScanIn | None = None) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    try:
        classified = await _scan_campaign(session, campaign, request, payload)
    except AppError as exc:
        _raise(exc)
    try:
        current_paths, file_operations, cleanup_summary = await _apply_scan_file_operations(session, campaign, request, classified)
    except AppError as exc:
        _raise(exc)
    persist_scan_results(session, campaign, classified, _campaign_orgs(session, campaign), current_paths)
    audit(session, user_id=_user(request), action="scan_apply", entity_type="campaign", entity_id=campaign.id, after={"files": len(classified), **cleanup_summary})
    status = "completed_with_errors" if cleanup_summary.get("operation_errors") else "completed"
    return _scan_payload(session, campaign, classified, status=status, file_operations=file_operations, extra_summary=cleanup_summary)


@router.get("/campaigns/{campaign_id}/files")
def campaign_files(campaign_id: str, session: SessionDep) -> list[dict[str, Any]]:
    _get_campaign(session, campaign_id)
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
    return Response(
        report_workbook(campaign, files, missing),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-report.xlsx"'},
    )


@router.get("/campaigns/{campaign_id}/message.txt")
def campaign_message(campaign_id: str, session: SessionDep) -> Response:
    campaign = _get_campaign(session, campaign_id)
    text = missing_message(campaign, missing_by_type(missing_organizations(session, campaign)))
    return Response(
        text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id}-missing.txt"'},
    )


@router.post("/bulk/rename/preview")
def bulk_rename_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    settings = settings_map(session, include_secret=True)
    root = settings.get("root_path") or "/"
    paths = payload.paths
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
        }
    else:
        try:
            items = planned_rename_items(root, paths)
        except AppError as exc:
            _raise(exc)
        summary = {"files_checked": len(paths), "planned": len(items), "already_ok": len(paths) - len(items)}
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
def bulk_create_folders_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.campaign_id:
        raise HTTPException(status_code=400, detail={"code": "CAMPAIGN_REQUIRED", "message": "Укажите campaign_id."})
    return create_folders_preview(payload.campaign_id, session, request)


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


@router.get("/settings")
def get_settings(session: SessionDep) -> dict[str, Any]:
    return settings_map(session)


@router.put("/settings")
def put_settings(payload: SettingsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    before = settings_map(session)
    values = payload.model_dump(exclude_unset=True)
    if values.get("root_path"):
        values["root_path"] = ensure_under_root(values["root_path"], values["root_path"])
    after = update_settings(session, values)
    audit(session, user_id=_user(request), action="update", entity_type="settings", before=before, after=after)
    session.commit()
    return after


@router.post("/settings/test-webdav")
async def test_webdav(session: SessionDep, request: Request) -> dict[str, Any]:
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            return await client.test_webdav()
    except AppError as exc:
        _raise(exc)


@router.post("/settings/test-share-api")
async def test_share_api(session: SessionDep, request: Request) -> dict[str, Any]:
    try:
        async with NextcloudClient(_nc_credentials(session, request)) as client:
            return await client.test_share_api()
    except AppError as exc:
        _raise(exc)
