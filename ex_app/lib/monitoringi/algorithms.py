"""Filename normalization, organization matching and dry-run planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable, Protocol

FORBIDDEN_EXTENSIONS = {"exe", "bat", "cmd", "com", "js", "jse", "vbs", "sh", "ps1", "msi", "scr"}
PREFIXES = ("ГБОУ", "ДОУ", "ОДО")

_MULTI_SPACES = re.compile(r"\s+")
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_PREFIX_WITHOUT_SPACE = re.compile(r"(?iu)\b(ГБОУ|ДОУ|ОДО)\s*(?=[^\W_])")
_PREFIX_CODE = re.compile(r"(?iu)^(ГБОУ|ДОУ|ОДО)\s+(.+?)(?:\s|_|-|\.|$)")


class OrganizationLike(Protocol):
    id: str
    type: str
    prefix: str | None
    code: str
    display_name: str
    aliases: list[str]
    active: bool


@dataclass(frozen=True)
class NormalizeResult:
    original_name: str
    normalized_name: str
    changes: list[str]


@dataclass(frozen=True)
class OrganizationMatch:
    status: str
    organization_id: str | None
    matches: list[str]
    normalized_name: str
    reason: str | None = None


@dataclass(frozen=True)
class FileCandidate:
    path: str
    name: str
    size: int | None = None
    mtime: datetime | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class ClassifiedFile:
    candidate: FileCandidate
    normalized_name: str
    classification: str
    reason: str | None
    organization_id: str | None


def split_name(name: str) -> tuple[str, str]:
    path = PurePosixPath(name)
    suffix = path.suffix
    stem = name[: -len(suffix)] if suffix else name
    return stem, suffix


def normalize_filename(name: str, *, lowercase_extension: bool = False) -> NormalizeResult:
    original = name
    stem, suffix = split_name(name.strip())
    changes: list[str] = []

    before = stem
    stem = stem.replace("№", "")
    if stem != before:
        changes.append("removed_number_sign")

    before = stem
    stem = _COPY_SUFFIX.sub("", stem)
    if stem != before:
        changes.append("removed_copy_suffix")

    before = stem
    stem = stem.strip(" .")
    if stem != before:
        changes.append("trimmed_stem")

    before = stem
    stem = _PREFIX_WITHOUT_SPACE.sub(lambda m: f"{m.group(1).upper()} ", stem)
    if stem != before:
        changes.append("added_prefix_space")

    before = stem
    stem = _MULTI_SPACES.sub(" ", stem).strip()
    if stem != before:
        changes.append("collapsed_spaces")

    before_suffix = suffix
    suffix = suffix.strip()
    if lowercase_extension:
        suffix = suffix.lower()
    if suffix != before_suffix:
        changes.append("normalized_extension")

    normalized = f"{stem}{suffix}"
    if normalized != original and not changes:
        changes.append("normalized")
    return NormalizeResult(original_name=original, normalized_name=normalized, changes=changes)


def normalize_text(value: str) -> str:
    result = value.replace("ё", "е").replace("Ё", "Е").replace("№", "")
    result = _PREFIX_WITHOUT_SPACE.sub(lambda m: f"{m.group(1).upper()} ", result)
    result = _MULTI_SPACES.sub(" ", result).strip().casefold()
    return result


def _get_org_value(org: OrganizationLike | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(org, dict):
        return org.get(key, default)
    return getattr(org, key, default)


def _boundary_prefix_match(base: str, token: str) -> bool:
    token_norm = normalize_text(token)
    if not token_norm:
        return False
    pattern = re.compile(rf"^{re.escape(token_norm)}(?=$|[\s_.-])", re.IGNORECASE)
    return bool(pattern.search(base))


def extract_organization(
    filename: str,
    organizations: Iterable[OrganizationLike | dict[str, Any]],
) -> OrganizationMatch:
    normalized = normalize_filename(filename).normalized_name
    base = normalize_text(split_name(normalized)[0])
    matches: set[str] = set()

    prefix_match = _PREFIX_CODE.search(base)
    parsed_prefix = prefix_match.group(1).casefold() if prefix_match else None
    parsed_code = normalize_text(prefix_match.group(2)) if prefix_match else None

    for org in organizations:
        if not _get_org_value(org, "active", True):
            continue
        org_id = str(_get_org_value(org, "id"))
        prefix = _get_org_value(org, "prefix")
        code = str(_get_org_value(org, "code", ""))
        display_name = str(_get_org_value(org, "display_name", ""))
        aliases = _get_org_value(org, "aliases", []) or []

        if prefix and parsed_prefix == normalize_text(prefix) and parsed_code == normalize_text(code):
            matches.add(org_id)
            continue

        tokens = [display_name, code, *aliases]
        if any(_boundary_prefix_match(base, token) for token in tokens):
            matches.add(org_id)

    ordered = sorted(matches)
    if len(ordered) == 1:
        return OrganizationMatch("matched", ordered[0], ordered, normalized)
    if len(ordered) > 1:
        return OrganizationMatch("ambiguous", None, ordered, normalized, "ambiguous_match")
    return OrganizationMatch("unknown", None, [], normalized, "unknown_organization")


def extension_of(name: str) -> str:
    return PurePosixPath(name).suffix.lstrip(".").casefold()


def org_code_sort_key(value: str) -> tuple[int, int | str]:
    normalized = normalize_text(value)
    if normalized.isdigit():
        return (0, int(normalized))
    return (1, normalized)


def organization_sort_key(org: OrganizationLike | dict[str, Any]) -> tuple[str, tuple[int, int | str], str]:
    org_type = str(_get_org_value(org, "type", ""))
    if "." in org_type:
        org_type = org_type.rsplit(".", 1)[-1]
    return (
        org_type,
        org_code_sort_key(str(_get_org_value(org, "code", ""))),
        normalize_text(str(_get_org_value(org, "display_name", ""))),
    )


def classify_files(
    files: Iterable[FileCandidate],
    organizations: Iterable[OrganizationLike | dict[str, Any]],
    allowed_extensions: Iterable[str],
) -> list[ClassifiedFile]:
    allowed = {ext.casefold().lstrip(".") for ext in allowed_extensions}
    organizations_list = list(organizations)
    classified: list[ClassifiedFile] = []

    for candidate in files:
        normalized = normalize_filename(candidate.name).normalized_name
        ext = extension_of(normalized)
        if not ext or ext in FORBIDDEN_EXTENSIONS or (allowed and ext not in allowed):
            classified.append(ClassifiedFile(candidate, normalized, "invalid_extension", "invalid_extension", None))
            continue
        if candidate.size == 0:
            classified.append(ClassifiedFile(candidate, normalized, "invalid_name", "empty_file", None))
            continue
        match = extract_organization(candidate.name, organizations_list)
        if match.status == "matched":
            classified.append(ClassifiedFile(candidate, normalized, "valid", None, match.organization_id))
        elif match.status == "ambiguous":
            classified.append(ClassifiedFile(candidate, normalized, "invalid_name", "ambiguous_match", None))
        else:
            classified.append(ClassifiedFile(candidate, normalized, "unknown", "unknown_organization", None))

    newest_by_name: dict[str, ClassifiedFile] = {}
    for item in classified:
        if item.classification != "valid":
            continue
        key = normalize_text(item.normalized_name)
        current = newest_by_name.get(key)
        if current is None:
            newest_by_name[key] = item
            continue
        current_mtime = current.candidate.mtime or datetime.min
        item_mtime = item.candidate.mtime or datetime.min
        if item_mtime > current_mtime:
            newest_by_name[key] = item

    result: list[ClassifiedFile] = []
    for item in classified:
        if item.classification == "valid":
            key = normalize_text(item.normalized_name)
            if newest_by_name.get(key) is not item:
                result.append(
                    ClassifiedFile(
                        item.candidate,
                        item.normalized_name,
                        "duplicate",
                        "duplicate_old_version",
                        item.organization_id,
                    )
                )
                continue
        result.append(item)
    return result


def plan_renames(paths: Iterable[str]) -> list[dict[str, str]]:
    planned: list[dict[str, str]] = []
    for path in paths:
        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            parent = "/"
        original_name = PurePosixPath(path).name
        normalized = normalize_filename(original_name).normalized_name
        if normalized == original_name:
            continue
        target = str(PurePosixPath(parent) / normalized)
        if not target.startswith("/"):
            target = "/" + target
        planned.append(
            {
                "action": "rename",
                "source_path": path,
                "target_path": target,
                "reason": "normalized_filename",
            }
        )
    return planned
