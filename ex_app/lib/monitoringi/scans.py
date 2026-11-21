"""WebDAV reads and the immutable file plan for a campaign check."""

from collections import defaultdict
from pathlib import PurePosixPath
import re

import httpx

from .algorithms import ClassifiedFile, classify_files
from .errors import AppError
from .models import Campaign, OperationAction, OperationItem, OperationItemStatus, Organization
from .nextcloud import NextcloudClient
from .security import ensure_under_root
from .services import campaign_type_folder_path, campaign_upload_mode, entries_to_candidates, organization_type_value


async def list_scan_folder(client: NextcloudClient, folder: str):
    try:
        entries = await client.propfind(folder, depth=1)
    except httpx.RequestError as exc:
        raise AppError("WEBDAV_UNAVAILABLE", "Не удалось прочитать папку через WebDAV.", 502) from exc
    if not any(entry.path == folder and entry.is_collection for entry in entries):
        raise AppError("WEBDAV_INCOMPLETE_LISTING", "WebDAV не подтвердил содержимое папки.", 502)
    children = []
    for entry in entries:
        if entry.path == folder:
            continue
        ensure_under_root(entry.path, folder)
        if str(PurePosixPath(entry.path).parent) != folder:
            raise AppError("WEBDAV_INVALID_LISTING", "WebDAV вернул объект за пределами запрошенной папки.", 502)
        children.append(entry)
    return children


async def shared_files(client, campaign: Campaign, organizations: list[Organization], extensions, cleanup_folders):
    groups = defaultdict(list)
    if campaign_upload_mode(campaign) == "org_type":
        for org in organizations:
            groups[campaign_type_folder_path(campaign, organization_type_value(org))].append(org)
    else:
        groups[campaign.campaign_folder_path] = organizations

    classified = []
    for root, group in groups.items():
        folders = [root]
        candidates = []
        while folders:
            folder = folders.pop()
            children = await list_scan_folder(client, folder)
            for child in children:
                if child.is_collection and (folder != campaign.campaign_folder_path or child.name not in cleanup_folders):
                    folders.append(child.path)
            candidates.extend(entries_to_candidates(children, skip_root=folder))
        classified.extend(classify_files(candidates, group, extensions))
    return classified


def strong_etag(etag: str | None) -> str | None:
    if not etag:
        return None
    if re.fullmatch(r'"[!#-~]*"', etag):
        return etag
    if re.fullmatch(r'[a-zA-Z0-9_.:-]+', etag):
        return f'"{etag}"'
    return None


def scan_evidence(item: ClassifiedFile) -> dict:
    return {
        "etag": strong_etag(item.candidate.etag),
        "classification": item.classification,
        "organization_id": item.organization_id,
        "normalized_name": item.normalized_name,
    }


def scan_plan_items(classified, operations):
    items = []
    for item in classified:
        source = item.candidate.path
        operation = operations.get(source, {})
        action = operation.get("action")
        items.append({
            "action": OperationAction.rename if action == "rename_kept" else OperationAction.move if action else OperationAction.classify,
            "source_path": source,
            "target_path": operation.get("target_path"),
            "reason": item.reason,
            "metadata_json": {**scan_evidence(item), "scan_action": action},
        })
    return items


async def execute_scan_plan(client, campaign, planned: list[OperationItem], current: list[ClassifiedFile]):
    current_by_path = {item.candidate.path: item for item in current}
    verified = []
    paths = {}
    operations = {}
    warnings = []
    uncertain = set()
    counts = {"trash_moved": 0, "duplicates_moved": 0, "renamed": 0, "skipped": 0, "operation_errors": 0}
    count_by_action = {"move_trash": "trash_moved", "move_duplicate": "duplicates_moved", "rename_kept": "renamed"}
    created_folders = set()
    # Free duplicate and trash destinations before normalizing kept filenames.
    for row in sorted(planned, key=lambda item: item.action == OperationAction.rename):
        source = ensure_under_root(row.source_path, campaign.campaign_folder_path)
        item = current_by_path.pop(source, None)
        evidence = row.metadata_json
        action = evidence["scan_action"]
        error = None
        row.status = OperationItemStatus.skipped
        if item is None:
            error = "Исходный файл больше не найден."
        elif not evidence["etag"]:
            error = "WebDAV не предоставил ETag для проверки версии файла."
        elif any(evidence[key] != value for key, value in scan_evidence(item).items()):
            error = "Версия или классификация файла изменилась после предпросмотра."
        elif action:
            target = ensure_under_root(row.target_path, campaign.campaign_folder_path)
            try:
                if row.action == OperationAction.move:
                    folder = str(PurePosixPath(target).parent)
                    if folder not in created_folders:
                        await client.mkcol(folder)
                        created_folders.add(folder)
                await client.move(source, target, expected_etag=evidence["etag"])
            except (AppError, httpx.RequestError) as exc:
                error = exc.message if isinstance(exc, AppError) else "Связь с WebDAV прервалась."
                if not isinstance(exc, AppError) or exc.code != "SCAN_PRECONDITION_FAILED":
                    row.status = OperationItemStatus.failed
            else:
                paths[source] = target
                counts[count_by_action[action]] += 1

        if error:
            row.error = error
            counts["skipped" if row.status == OperationItemStatus.skipped else "operation_errors"] += 1
            warnings.append({"path": source, "error": error})
            uncertain.update(filter(None, [evidence["organization_id"], item.organization_id if item else None]))
        else:
            row.status = OperationItemStatus.success
            verified.append(item)
        if action or error:
            operations[source] = {"action": action, "target_path": row.target_path, "status": row.status.value, "error": error}
    for item in current_by_path.values():
        warnings.append({"path": item.candidate.path, "error": "Новый файл после предпросмотра. Выполните следующую проверку."})
        counts["skipped"] += 1
        if item.organization_id:
            uncertain.add(item.organization_id)
    return verified, paths, operations, {**counts, "scan_warnings": warnings}, uncertain
