"""HTTP clients for Nextcloud WebDAV and OCS APIs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from base64 import b64encode
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from urllib.parse import quote, unquote

import httpx

from .config import SETTINGS
from .errors import NextcloudApiError
from .security import ensure_under_root, normalize_nc_path

DAV_NS = {"d": "DAV:", "nc": "http://nextcloud.org/ns", "oc": "http://owncloud.org/ns"}


@dataclass(frozen=True)
class NextcloudCredentials:
    base_url: str
    user: str
    root_path: str
    app_secret: str | None = None
    app_id: str | None = None
    app_version: str | None = None
    aa_version: str | None = None
    app_password: str | None = None


@dataclass(frozen=True)
class WebDAVEntry:
    path: str
    name: str
    is_collection: bool
    size: int | None
    mtime: datetime | None
    mime_type: str | None
    etag: str | None = None
    file_id: str | None = None


def configured_credentials(overrides: dict | None = None) -> NextcloudCredentials:
    overrides = overrides or {}
    base_url = overrides.get("nc_base_url") or SETTINGS.nc_base_url
    user = overrides.get("nc_user") or SETTINGS.nc_user
    app_password = overrides.get("nc_app_password") or SETTINGS.nc_app_password
    root_path = overrides.get("root_path") or SETTINGS.root_path
    app_secret = overrides.get("app_secret") or SETTINGS.app_secret
    app_id = overrides.get("app_id") or SETTINGS.app_id
    app_version = overrides.get("app_version") or SETTINGS.app_version
    aa_version = overrides.get("aa_version") or SETTINGS.aa_version
    if not base_url or not user:
        raise NextcloudApiError(
            "NEXTCLOUD_SETTINGS_INCOMPLETE",
            "Не заполнены настройки подключения к Nextcloud API.",
            400,
        )
    if not app_password and not (app_secret and app_id and app_version):
        raise NextcloudApiError(
            "NEXTCLOUD_SETTINGS_INCOMPLETE",
            "Не найден AppAPI secret для подключения к Nextcloud API.",
            400,
        )
    base_url = base_url.rstrip("/")
    if base_url.endswith("/index.php"):
        base_url = base_url.removesuffix("/index.php")
    return NextcloudCredentials(
        base_url=base_url,
        user=user,
        root_path=normalize_nc_path(root_path),
        app_secret=app_secret,
        app_id=app_id,
        app_version=app_version,
        aa_version=aa_version or "2.2.0",
        app_password=app_password,
    )


def _quote_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in normalize_nc_path(path).split("/") if part)


def _mail_bridge_token() -> str | None:
    token_path = SETTINGS.storage_dir / "mail_bridge_token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class NextcloudClient:
    def __init__(self, credentials: NextcloudCredentials | None = None) -> None:
        self.credentials = credentials or configured_credentials()
        headers = {"User-Agent": "ExApp/oo_file_collector"}
        auth = None
        if self.credentials.app_password:
            auth = (self.credentials.user, self.credentials.app_password)
        else:
            auth_value = b64encode(f"{self.credentials.user}:{self.credentials.app_secret}".encode()).decode()
            headers.update(
                {
                    "AA-VERSION": self.credentials.aa_version or "2.2.0",
                    "EX-APP-ID": self.credentials.app_id or "oo_file_collector",
                    "EX-APP-VERSION": self.credentials.app_version or "0.0.0",
                    "AUTHORIZATION-APP-API": auth_value,
                }
            )
        self._client = httpx.AsyncClient(
            base_url=self.credentials.base_url,
            auth=auth,
            timeout=SETTINGS.request_timeout,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NextcloudClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def _safe_path(self, path: str) -> str:
        return ensure_under_root(path, self.credentials.root_path)

    def _dav_url(self, path: str) -> str:
        safe_path = self._safe_path(path)
        return f"/remote.php/dav/files/{quote(self.credentials.user, safe='')}/{_quote_path(safe_path)}"

    async def propfind(self, path: str, *, depth: int = 1) -> list[WebDAVEntry]:
        response = await self._client.request(
            "PROPFIND",
            self._dav_url(path),
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            content="""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <d:getcontenttype/>
    <d:getetag/>
    <oc:fileid/>
    <nc:fileid/>
  </d:prop>
</d:propfind>""",
        )
        if response.status_code in {401, 403}:
            raise NextcloudApiError("WEBDAV_AUTH_FAILED", "Не удалось авторизоваться в WebDAV.", 401)
        if response.status_code == 404:
            raise NextcloudApiError("WEBDAV_PATH_NOT_FOUND", "Путь WebDAV не найден.", 404)
        if response.status_code >= 400:
            raise NextcloudApiError("WEBDAV_PROPFIND_FAILED", "Не удалось получить список файлов.", 502, response.text)
        return self._parse_multistatus(response.text)

    def _parse_multistatus(self, xml_text: str) -> list[WebDAVEntry]:
        root = ET.fromstring(xml_text)
        result: list[WebDAVEntry] = []
        prefix = f"/remote.php/dav/files/{self.credentials.user}"
        for item in root.findall("d:response", DAV_NS):
            href = unquote(item.findtext("d:href", default="", namespaces=DAV_NS))
            path = "/" + href.split(prefix, 1)[-1].strip("/")
            prop = item.find("d:propstat/d:prop", DAV_NS)
            if prop is None:
                continue
            is_collection = prop.find("d:resourcetype/d:collection", DAV_NS) is not None
            length = prop.findtext("d:getcontentlength", default=None, namespaces=DAV_NS)
            mime_type = prop.findtext("d:getcontenttype", default=None, namespaces=DAV_NS)
            modified = prop.findtext("d:getlastmodified", default=None, namespaces=DAV_NS)
            etag = prop.findtext("d:getetag", default=None, namespaces=DAV_NS)
            file_id = prop.findtext("oc:fileid", default=None, namespaces=DAV_NS) or prop.findtext("nc:fileid", default=None, namespaces=DAV_NS)
            result.append(
                WebDAVEntry(
                    path=normalize_nc_path(path),
                    name=PurePosixPath(path).name,
                    is_collection=is_collection,
                    size=int(length) if length and length.isdigit() else None,
                    mtime=_parse_http_date(modified),
                    mime_type=mime_type,
                    etag=etag,
                    file_id=str(file_id) if file_id else None,
                )
            )
        return result

    async def mkcol(self, path: str) -> None:
        response = await self._client.request("MKCOL", self._dav_url(path))
        if response.status_code in {201, 405}:
            return
        if response.status_code in {401, 403}:
            raise NextcloudApiError("WEBDAV_FORBIDDEN", "Нет прав на создание папки WebDAV.", response.status_code)
        raise NextcloudApiError("WEBDAV_MKCOL_FAILED", "Не удалось создать папку WebDAV.", 502, response.text)

    async def move(self, source_path: str, target_path: str, *, overwrite: bool = False) -> None:
        source = self._safe_path(source_path)
        target = self._safe_path(target_path)
        destination = f"{self.credentials.base_url}{self._dav_url(target)}"
        response = await self._client.request(
            "MOVE",
            self._dav_url(source),
            headers={"Destination": destination, "Overwrite": "T" if overwrite else "F"},
        )
        if response.status_code in {201, 204}:
            return
        if response.status_code == 412:
            raise NextcloudApiError("FILE_ALREADY_EXISTS", "Целевой файл уже существует.", 409, response.text)
        raise NextcloudApiError("WEBDAV_MOVE_FAILED", "Не удалось переместить файл через WebDAV.", 502, response.text)

    async def copy(self, source_path: str, target_path: str, *, overwrite: bool = False) -> None:
        source = self._safe_path(source_path)
        target = self._safe_path(target_path)
        destination = f"{self.credentials.base_url}{self._dav_url(target)}"
        response = await self._client.request(
            "COPY",
            self._dav_url(source),
            headers={"Destination": destination, "Overwrite": "T" if overwrite else "F"},
        )
        if response.status_code in {201, 204}:
            return
        raise NextcloudApiError("WEBDAV_COPY_FAILED", "Не удалось скопировать файл через WebDAV.", 502, response.text)

    async def delete(self, path: str) -> None:
        response = await self._client.request("DELETE", self._dav_url(path))
        if response.status_code in {200, 202, 204, 404}:
            return
        raise NextcloudApiError("WEBDAV_DELETE_FAILED", "Не удалось удалить файл через WebDAV.", 502, response.text)

    async def put(self, path: str, content: bytes, *, content_type: str = "application/octet-stream") -> None:
        response = await self._client.put(self._dav_url(path), content=content, headers={"Content-Type": content_type})
        if response.status_code in {200, 201, 204}:
            return
        raise NextcloudApiError("WEBDAV_PUT_FAILED", "Не удалось загрузить файл через WebDAV.", 502, response.text)

    async def create_public_share(
        self,
        path: str,
        *,
        public_upload: bool = True,
        expire_date: date | datetime | str | None = None,
        password: str | None = None,
        permissions: int = 4,
        label: str | None = None,
        note: str | None = None,
    ) -> dict[str, str | int | None]:
        safe_path = self._safe_path(path)
        form: dict[str, str | int | bool] = {
            "path": safe_path,
            "shareType": 3,
            "permissions": permissions,
            "publicUpload": "true" if public_upload else "false",
        }
        if expire_date:
            if isinstance(expire_date, datetime):
                form["expireDate"] = expire_date.date().isoformat()
            elif isinstance(expire_date, date):
                form["expireDate"] = expire_date.isoformat()
            else:
                form["expireDate"] = str(expire_date).replace(" ", "T").split("T", 1)[0]
        if password:
            form["password"] = password
        if label:
            form["label"] = label
        if note:
            form["note"] = note
        response = await self._client.post(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            data=form,
        )
        if response.status_code == 403:
            raise NextcloudApiError(
                "PUBLIC_UPLOAD_DISABLED",
                "Не удалось создать публичную ссылку: на сервере Nextcloud запрещена загрузка по публичным ссылкам.",
                403,
                response.text,
            )
        if response.status_code in {401, 403}:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "Нет прав на создание публичной ссылки.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_API_FAILED", "Не удалось создать публичную ссылку.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            code = "PUBLIC_UPLOAD_DISABLED" if int(meta.get("statuscode")) == 403 else "SHARE_API_FAILED"
            raise NextcloudApiError(code, str(meta.get("message") or "Ошибка OCS Share API."), 502)
        data = payload.get("data", {})
        return {
            "share_id": str(data.get("id")) if data.get("id") is not None else None,
            "public_link": data.get("url"),
            "token": data.get("token"),
        }

    async def delete_public_share(self, share_id: str) -> None:
        share_id = str(share_id or "").strip()
        if not share_id:
            return
        response = await self._client.delete(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{quote(share_id, safe='')}",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        if response.status_code == 404:
            return
        if response.status_code in {401, 403}:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "Нет прав на удаление публичной ссылки.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_DELETE_FAILED", "Не удалось удалить публичную ссылку.", 502, response.text)
        if response.status_code in {202, 204}:
            return
        try:
            payload = response.json().get("ocs", {})
        except ValueError:
            return
        meta = payload.get("meta", {})
        status_code = int(meta.get("statuscode") or 100)
        if status_code in {100, 200, 404}:
            return
        if status_code >= 400 and status_code != 404:
            raise NextcloudApiError("SHARE_DELETE_FAILED", str(meta.get("message") or "Не удалось удалить публичную ссылку."), 502)

    async def send_service_mail(self, *, to_email: str, subject: str, body: str) -> None:
        token = _mail_bridge_token()
        if not token:
            raise NextcloudApiError("MAIL_BRIDGE_TOKEN_MISSING", "?? ???????? bridge-????? ????????? ????? Nextcloud.", 500)
        response = await self._client.post(
            "/oo_file_collector_mail_bridge.php",
            json={"to": to_email, "subject": subject, "body": body},
            headers={"X-OO-File-Collector-Token": token},
        )
        if response.status_code in {200, 201, 202, 204}:
            return
        raise NextcloudApiError("NEXTCLOUD_MAIL_SEND_FAILED", "Nextcloud ?? ???????? ?????? ????? ????????? ?????.", 502, response.text)


    async def test_webdav(self) -> dict[str, str | int]:
        entries = await self.propfind(self.credentials.root_path, depth=0)
        return {"status": "ok", "entries": len(entries)}

    async def test_share_api(self) -> dict[str, str]:
        response = await self._client.get(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "OCS Share API недоступен.", response.status_code, response.text)
        return {"status": "ok"}
