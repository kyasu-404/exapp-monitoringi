"""Domain errors with user-facing codes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400
    detail: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class PathSecurityError(AppError):
    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__("PATH_NOT_ALLOWED", message, 400, detail)


class NextcloudApiError(AppError):
    pass

