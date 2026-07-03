"""Filename normalization, organization matching and dry-run planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable, Protocol

FORBIDDEN_EXTENSIONS = {"exe", "bat", "cmd", "com", "js", "jse", "vbs", "sh", "ps1", "msi", "scr"}
DEFAULT_ORGANIZATION_ALIASES = {
    "school": ["ГБОУ"],
    "kindergarten": ["ДОУ"],
    "odo": ["ОДО"],
    "udod": ["ЦФКСиЗ", "ДДЮТ", "ЦДЮТТ", "ЦППМСП", "ИМЦ"],
}

_MULTI_SPACES = re.compile(r"\s+")
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_PREFIX_PATTERN = "|".join(
    re.escape(alias)
    for aliases in DEFAULT_ORGANIZATION_ALIASES.values()
    for alias in sorted(aliases, key=len, reverse=True)
)
_PREFIX_WITHOUT_SPACE = re.compile(rf"(?iu)\b({_PREFIX_PATTERN})\s*(?=[^\W_])")


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


def _organization_type_key(value: Any) -> str:
    raw = value.value if hasattr(value, "value") else value
    text = str(raw or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def default_organization_aliases(org_type: Any) -> list[str]:
    return list(DEFAULT_ORGANIZATION_ALIASES.get(_organization_type_key(org_type), []))


def _dedupe_values(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = normalize_text(text) if text else ""
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def effective_organization_aliases(org: OrganizationLike | dict[str, Any]) -> list[str]:
    return _dedupe_values(
        [
            *default_organization_aliases(_get_org_value(org, "type")),
            _get_org_value(org, "prefix"),
            *(_get_org_value(org, "aliases", []) or []),
        ]
    )


def _alias_contains_code(alias: str, code: str) -> bool:
    alias_norm = normalize_text(alias)
    code_norm = normalize_text(code)
    return bool(code_norm and re.search(rf"(^|[\s_.-]){re.escape(code_norm)}($|[\s_.-])", alias_norm))


def _alias_code_prefix_match(base: str, alias: str, code: str) -> bool:
    alias_norm = normalize_text(alias)
    code_norm = normalize_text(code)
    if not alias_norm or not code_norm:
        return False
    pattern = re.compile(rf"^{re.escape(alias_norm)}[\s_.-]*{re.escape(code_norm)}(?=$|[\s_.-])", re.IGNORECASE)
    return bool(pattern.search(base))


def _canonical_alias(org: OrganizationLike | dict[str, Any], matched_alias: str | None = None) -> str:
    code = str(_get_org_value(org, "code", ""))
    aliases = effective_organization_aliases(org)
    if matched_alias and not _alias_contains_code(matched_alias, code):
        return matched_alias.strip()
    for alias in aliases:
        if not _alias_contains_code(alias, code):
            return alias
    return str(_get_org_value(org, "prefix") or "").strip()


def normalized_organization_file_stem(org: OrganizationLike | dict[str, Any], matched_alias: str | None = None) -> str:
    code = str(_get_org_value(org, "code", "")).strip()
    alias = _canonical_alias(org, matched_alias)
    if alias and code:
        return _MULTI_SPACES.sub(" ", f"{alias} {code}").strip()
    return str(_get_org_value(org, "display_name") or code).strip()


def normalized_organization_filename(
    org: OrganizationLike | dict[str, Any],
    current_name: str,
    matched_alias: str | None = None,
) -> str:
    _stem, suffix = split_name(current_name)
    return f"{normalized_organization_file_stem(org, matched_alias)}{suffix}"


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
    matched_aliases: dict[str, str | None] = {}
    org_by_id: dict[str, OrganizationLike | dict[str, Any]] = {}

    for org in organizations:
        if not _get_org_value(org, "active", True):
            continue
        org_id = str(_get_org_value(org, "id"))
        org_by_id[org_id] = org
        code = str(_get_org_value(org, "code", ""))
        display_name = str(_get_org_value(org, "display_name", ""))
        aliases = effective_organization_aliases(org)

        matched_alias = next((alias for alias in aliases if _alias_code_prefix_match(base, alias, code)), None)
        if matched_alias:
            matches.add(org_id)
            matched_aliases[org_id] = matched_alias
            continue

        explicit_aliases = [alias for alias in aliases if _alias_contains_code(alias, code)]
        tokens = [display_name, code, *explicit_aliases]
        if any(_boundary_prefix_match(base, token) for token in tokens):
            matches.add(org_id)
            matched_aliases[org_id] = None

    ordered = sorted(matches)
    if len(ordered) == 1:
        org_id = ordered[0]
        return OrganizationMatch(
            "matched",
            org_id,
            ordered,
            normalized_organization_filename(org_by_id[org_id], normalized, matched_aliases.get(org_id)),
        )
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


def mtime_sort_value(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.timestamp()


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
        if mtime_sort_value(item.candidate.mtime) > mtime_sort_value(current.candidate.mtime):
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
