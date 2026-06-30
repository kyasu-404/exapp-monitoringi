"""FastAPI routes for the ExApp backend."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlmodel import Session, delete, select

from .algorithms import classify_files, organization_sort_key
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
from .reports import MAILING_COLUMNS, csv_bytes, mailing_rows, missing_message, report_workbook, xlsx_bytes
from .schemas import (
    BulkPathsIn,
    CampaignIn,
    CampaignPatch,
    OrganizationIn,
    OrganizationPatch,
    ScanIn,
    SelectOrganizationsIn,
    SettingsIn,
    ShareOptionsIn,
)
from .security import campaign_folder_path, ensure_under_root
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
    parse_organization_table,
    persist_scan_results,
    planned_folder_items,
    planned_rename_items,
    settings_map,
    target_invalid_path,
    update_settings,
    validate_organization_rows,
)

router = APIRouter(prefix="/api")


SessionDep = Annotated[Session, Depends(get_session)]


def _user(request: Request) -> str | None:
    return current_user_id(dict(request.headers))


def _raise(exc: AppError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
    ) from exc


def _model_json(model) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _get_campaign(session: Session, campaign_id: str) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail={"code": "CAMPAIGN_NOT_FOUND", "message": "Кампания не найдена."})
    return campaign


def _get_batch(session: Session, batch_id: str) -> OperationBatch:
    batch = session.get(OperationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND", "message": "Пакет операций не найден."})
    return batch


def _campaign_orgs(session: Session, campaign: Campaign) -> list[CampaignOrganization]:
    items = session.exec(select(CampaignOrganization).where(CampaignOrganization.campaign_id == campaign.id)).all()
    if items:
        return items
    if campaign.selected_org_types:
        organizations = session.exec(
            select(Organization).where(Organization.active == True, Organization.type.in_(campaign.selected_org_types))  # noqa: E712
        ).all()
        return create_campaign_orgs(session, campaign, organizations)
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
    organization = Organization(**payload.model_dump())
    session.add(organization)
    audit(session, user_id=_user(request), action="create", entity_type="organization", entity_id=organization.id, after=payload.model_dump(mode="json"))
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
    for key, value in payload.model_dump(exclude_unset=True).items():
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


@router.post("/organizations/import")
async def import_organizations(
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    apply: bool = Query(False),  # noqa: A002 - public API field
    allow_update: bool = Query(False),
) -> dict[str, Any]:
    content = await file.read()
    rows = parse_organization_table(file.filename or "organizations.csv", content)
    validation = validate_organization_rows(session, rows)
    errors = [item for item in validation if item["errors"]]
    blocking_errors = [
        item
        for item in errors
        if not allow_update or any(error not in {"DUPLICATE_ORGANIZATION", "DUPLICATE_EMAIL"} for error in item["errors"])
    ]
    if not apply:
        return {"apply": False, "rows": validation, "errors_count": len(errors)}
    if blocking_errors:
        return {"apply": False, "rows": validation, "errors_count": len(errors), "message": "Импорт требует подтверждения конфликтов."}
    summary = apply_organization_import(session, rows)
    audit(session, user_id=_user(request), action="import", entity_type="organization", after=summary)
    session.commit()
    return {"apply": True, **summary}


@router.get("/organizations/export.csv")
def export_organizations_csv(session: SessionDep) -> Response:
    rows = [_model_json(org) for org in session.exec(select(Organization)).all()]
    columns = ["type", "prefix", "code", "display_name", "email", "extra_emails", "aliases", "active", "notes"]
    return Response(
        csv_bytes([{key: str(row.get(key) or "") for key in columns} for row in rows], columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="organizations.csv"'},
    )


@router.get("/organizations/export.xlsx")
def export_organizations_xlsx(session: SessionDep) -> Response:
    rows = [_model_json(org) for org in session.exec(select(Organization)).all()]
    columns = ["type", "prefix", "code", "display_name", "email", "extra_emails", "aliases", "active", "notes"]
    data = [{key: ", ".join(row[key]) if isinstance(row.get(key), list) else str(row.get(key) or "") for key in columns} for row in rows]
    return Response(
        xlsx_bytes(data, columns, sheet_name="Организации"),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="organizations.xlsx"'},
    )


@router.post("/organizations/validate")
def validate_organizations(session: SessionDep) -> dict[str, Any]:
    organizations = session.exec(select(Organization)).all()
    rows = [org.model_dump(mode="json") for org in organizations]
    return {"rows": validate_organization_rows(session, rows)}


@router.get("/campaigns")
def list_campaigns(session: SessionDep, status: str | None = None) -> list[dict[str, Any]]:
    campaigns = session.exec(select(Campaign)).all()
    if status:
        campaigns = [campaign for campaign in campaigns if campaign.status == status or campaign.status.value == status]
    campaigns.sort(key=lambda campaign: (campaign.date, campaign.created_at), reverse=True)
    return [_model_json(campaign) for campaign in campaigns]


@router.post("/campaigns")
def create_campaign(payload: CampaignIn, session: SessionDep, request: Request) -> dict[str, Any]:
    settings = settings_map(session, include_secret=True)
    root_path = settings.get("root_path") or payload.parent_nc_path
    try:
        parent = ensure_under_root(payload.parent_nc_path, root_path)
        folder = campaign_folder_path(parent, payload.date, payload.title)
        ensure_under_root(folder, root_path)
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
def update_campaign(campaign_id: str, payload: CampaignPatch, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    before = _model_json(campaign)
    data = payload.model_dump(exclude_unset=True)
    if data.get("parent_nc_path") or data.get("date") or data.get("title"):
        date_value = data.get("date") or campaign.date
        title_value = data.get("title") or campaign.title
        parent_value = data.get("parent_nc_path") or campaign.parent_nc_path
        root_path = settings_map(session, include_secret=True).get("root_path") or parent_value
        data["parent_nc_path"] = ensure_under_root(parent_value, root_path)
        data["campaign_folder_path"] = campaign_folder_path(data["parent_nc_path"], date_value, title_value)
    if data.get("selected_org_types"):
        data["selected_org_types"] = [str(item.value if hasattr(item, "value") else item) for item in data["selected_org_types"]]
    if data.get("allowed_extensions"):
        data["allowed_extensions"] = [str(item).lower().lstrip(".") for item in data["allowed_extensions"]]
    for key, value in data.items():
        setattr(campaign, key, value)
    campaign.updated_at = utcnow()
    session.add(campaign)
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
    items = planned_folder_items(campaign, campaign_orgs)
    summary = {
        "folders_to_create": len(items),
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
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не подходит для этой кампании."})
    try:
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
            batch = await apply_batch(session, batch, client)
    except AppError as exc:
        _raise(exc)
    successful_targets = {
        item.target_path
        for item in session.exec(select(OperationItem).where(OperationItem.batch_id == batch.id, OperationItem.status == OperationItemStatus.success)).all()
    }
    campaign_orgs = _campaign_orgs(session, campaign)
    for item in campaign_orgs:
        if item.folder_path in successful_targets:
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
def create_shares_preview(campaign_id: str, session: SessionDep, request: Request) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    campaign_orgs = _campaign_orgs(session, campaign)
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
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "Пакет операций не подходит для этой кампании."})
    options = options or ShareOptionsIn()
    template = _default_mail_template(session)
    org_by_id = {org.id: org for org in _organizations_for_campaign(session, campaign)}
    campaign_org_by_id = {item.id: item for item in _campaign_orgs(session, campaign)}
    batch.status = BatchStatus.running
    batch.started_at = utcnow()
    session.add(batch)
    session.commit()
    success = 0
    failed = 0
    try:
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
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


async def _scan_campaign(session: Session, campaign: Campaign, payload: ScanIn | None = None):
    scan_path = payload.path if payload and payload.path else campaign.campaign_folder_path
    allowed_extensions = payload.allowed_extensions if payload and payload.allowed_extensions else campaign.allowed_extensions
    root = settings_map(session, include_secret=True).get("root_path") or campaign.parent_nc_path
    ensure_under_root(scan_path, root)
    organizations = _organizations_for_campaign(session, campaign)
    async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
        root_entries = await client.propfind(scan_path, depth=1)
        entries = list(root_entries)
        for entry in root_entries:
            if entry.is_collection and entry.path != scan_path:
                entries.extend(await client.propfind(entry.path, depth=1))
    candidates = entries_to_candidates(entries, skip_root=scan_path)
    return classify_files(candidates, organizations, allowed_extensions)


@router.post("/campaigns/{campaign_id}/scan/preview")
async def scan_preview(campaign_id: str, session: SessionDep, payload: ScanIn | None = None) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    try:
        classified = await _scan_campaign(session, campaign, payload)
    except AppError as exc:
        _raise(exc)
    summary: dict[str, int] = {}
    for item in classified:
        summary[item.classification] = summary.get(item.classification, 0) + 1
    return {
        "campaign_id": campaign.id,
        "summary": summary,
        "files": [
            {
                "path": item.candidate.path,
                "original_name": item.candidate.name,
                "normalized_name": item.normalized_name,
                "classification": item.classification,
                "reason": item.reason,
                "organization_id": item.organization_id,
                "size": item.candidate.size,
                "mtime": item.candidate.mtime.isoformat() if item.candidate.mtime else None,
            }
            for item in classified
        ],
    }


@router.post("/campaigns/{campaign_id}/scan/apply")
async def scan_apply(campaign_id: str, session: SessionDep, request: Request, payload: ScanIn | None = None) -> dict[str, Any]:
    campaign = _get_campaign(session, campaign_id)
    try:
        classified = await _scan_campaign(session, campaign, payload)
    except AppError as exc:
        _raise(exc)
    persist_scan_results(session, campaign, classified, _campaign_orgs(session, campaign))
    audit(session, user_id=_user(request), action="scan_apply", entity_type="campaign", entity_id=campaign.id, after={"files": len(classified)})
    return await scan_preview(campaign_id, session, payload)


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
        paths = [
            item.current_path
            for item in session.exec(select(FileRecord).where(FileRecord.campaign_id == payload.campaign_id)).all()
        ]
    try:
        items = planned_rename_items(root, paths)
    except AppError as exc:
        _raise(exc)
    batch = create_batch(
        session,
        operation_type="rename",
        campaign_id=payload.campaign_id,
        user_id=_user(request),
        items=items,
        summary={"planned": len(items)},
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
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
            batch = await apply_batch(session, batch, client)
    except AppError as exc:
        _raise(exc)
    audit(session, user_id=_user(request), action="bulk_rename_apply", entity_type="operation_batch", entity_id=batch.id, after=batch.summary_json)
    session.commit()
    return batch_payload(session, batch)


@router.post("/bulk/move/preview")
def bulk_move_preview(payload: BulkPathsIn, session: SessionDep, request: Request) -> dict[str, Any]:
    if not payload.campaign_id:
        raise HTTPException(status_code=400, detail={"code": "CAMPAIGN_REQUIRED", "message": "Для MVP перемещение планируется по результатам проверки кампании."})
    campaign = _get_campaign(session, payload.campaign_id)
    settings = settings_map(session, include_secret=True)
    invalid_folder = settings.get("invalid_folder") or "_Неподходящие"
    duplicates_folder = settings.get("duplicates_folder") or "_Дубли"
    files = session.exec(select(FileRecord).where(FileRecord.campaign_id == campaign.id)).all()
    items = []
    for file_record in files:
        if file_record.classification == FileClassification.valid:
            continue
        reason = file_record.reason or file_record.classification.value
        folder = duplicates_folder if file_record.classification == FileClassification.duplicate else invalid_folder
        items.append(
            {
                "action": "move",
                "source_path": file_record.current_path,
                "target_path": target_invalid_path(campaign.campaign_folder_path, file_record.current_path, folder, reason),
                "reason": reason,
            }
        )
    batch = create_batch(
        session,
        operation_type="move",
        campaign_id=campaign.id,
        user_id=_user(request),
        items=items,
        summary={"planned": len(items)},
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
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
            batch = await apply_batch(session, batch, client)
    except AppError as exc:
        _raise(exc)
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
async def test_webdav(session: SessionDep) -> dict[str, Any]:
    try:
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
            return await client.test_webdav()
    except AppError as exc:
        _raise(exc)


@router.post("/settings/test-share-api")
async def test_share_api(session: SessionDep) -> dict[str, Any]:
    try:
        async with NextcloudClient(configured_credentials(nextcloud_overrides(session))) as client:
            return await client.test_share_api()
    except AppError as exc:
        _raise(exc)
