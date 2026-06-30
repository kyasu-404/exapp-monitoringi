"""Nextcloud path validation helpers.

The ExApp never touches the Nextcloud data directory directly. These helpers
only normalize virtual WebDAV paths and keep all operations under the configured
monitoring root.
"""

from __future__ import annotations

import re
from datetime import date

from .errors import PathSecurityError

_UNSAFE_SEGMENT_CHARS = re.compile(r"[\x00/\\]")
_MULTI_SPACES = re.compile(r"\s+")


def clean_path_segment(value: str, *, fallback: str = "Без названия") -> str:
    segment = _MULTI_SPACES.sub(" ", value.replace("№", "").strip())
    segment = _UNSAFE_SEGMENT_CHARS.sub("_", segment)
    segment = segment.strip(" .")
    return segment or fallback


def normalize_nc_path(path: str) -> str:
    if "\x00" in path:
        raise PathSecurityError("Путь содержит запрещённый нулевой байт.")
    path = path.replace("\\", "/").strip()
    if not path:
        return "/"
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PathSecurityError("Путь не должен содержать '..'.")
        parts.append(part)
    return "/" + "/".join(parts)


def path_parts(path: str) -> tuple[str, ...]:
    normalized = normalize_nc_path(path)
    return tuple(part for part in normalized.split("/") if part)


def ensure_under_root(path: str, root: str) -> str:
    normalized_path = normalize_nc_path(path)
    normalized_root = normalize_nc_path(root)
    root_parts = path_parts(normalized_root)
    current_parts = path_parts(normalized_path)
    if root_parts and current_parts[: len(root_parts)] != root_parts:
        raise PathSecurityError(
            f"Операция вне разрешённой корневой папки {normalized_root} запрещена.",
            detail=f"path={normalized_path}",
        )
    return normalized_path


def join_nc_path(*parts: str) -> str:
    joined: list[str] = []
    for value in parts:
        for part in normalize_nc_path(value).split("/"):
            if part:
                joined.append(part)
    return "/" + "/".join(joined)


def campaign_folder_path(parent: str, campaign_date: date, title: str) -> str:
    folder_name = clean_path_segment(f"{campaign_date.isoformat()} {title}")
    return join_nc_path(parent, folder_name)


def organization_folder_path(campaign_path: str, organization_display_name: str) -> str:
    return join_nc_path(campaign_path, clean_path_segment(organization_display_name))

