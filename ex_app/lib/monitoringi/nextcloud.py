"""HTTP clients for Nextcloud WebDAV and OCS APIs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import json
from base64 import b64encode
from contextlib import suppress
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
    permissions: str | None = None
    mount_type: str | None = None
    is_mount_root: bool | None = None
    owner_id: str | None = None


def configured_credentials(overrides: dict | None = None) -> NextcloudCredentials:
    overrides = overrides or {}
    if "nc_base_url" in overrides:
        raise NextcloudApiError("NEXTCLOUD_URL_OVERRIDE", "Адрес Nextcloud задаётся при развёртывании.", 400)
    base_url = SETTINGS.nc_base_url
    user = overrides.get("nc_user") or SETTINGS.nc_user
    app_password = None if SETTINGS.app_secret else (overrides.get("nc_app_password") or SETTINGS.nc_app_password)
    root_path = overrides.get("root_path") or SETTINGS.root_path
    app_secret = SETTINGS.app_secret
    app_id = SETTINGS.app_id
    app_version = SETTINGS.app_version
    aa_version = SETTINGS.aa_version
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


def _share_attributes(*, file_request_enabled: bool | None = None) -> str | None:
    attributes = []
    if file_request_enabled is not None:
        attributes.append({"scope": "fileRequest", "key": "enabled", "value": file_request_enabled})
    return json.dumps(attributes, ensure_ascii=False) if attributes else None


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
            follow_redirects=False,
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
    <oc:permissions/>
    <nc:mount-type/>
    <nc:is-mount-root/>
    <oc:owner-id/>
  </d:prop>
</d:propfind>""",
        )
        if response.status_code in {401, 403}:
            raise NextcloudApiError("WEBDAV_AUTH_FAILED", "Не удалось авторизоваться в WebDAV.", 401)
        if response.status_code == 404:
            raise NextcloudApiError("WEBDAV_PATH_NOT_FOUND", "Путь WebDAV не найден.", 404)
        if response.status_code >= 400:
            raise NextcloudApiError("WEBDAV_PROPFIND_FAILED", "Не удалось получить список файлов.", 502, response.text)
        try:
            return self._parse_multistatus(response.text)
        except ET.ParseError as exc:
            raise NextcloudApiError("WEBDAV_INVALID_LISTING", "WebDAV вернул неполный список файлов.", 502) from exc

    def _prop_from_response(self, item: ET.Element) -> ET.Element | None:
        for propstat in item.findall("d:propstat", DAV_NS):
            prop = propstat.find("d:prop", DAV_NS)
            if prop is None:
                continue
            status = propstat.findtext("d:status", default="", namespaces=DAV_NS)
            if " 200 " in f" {status} ":
                return prop
        return None

    def _parse_multistatus(self, xml_text: str) -> list[WebDAVEntry]:
        root = ET.fromstring(xml_text)
        result: list[WebDAVEntry] = []
        prefix = f"/remote.php/dav/files/{self.credentials.user}"
        for item in root.findall("d:response", DAV_NS):
            href = unquote(item.findtext("d:href", default="", namespaces=DAV_NS))
            path = "/" + href.split(prefix, 1)[-1].strip("/")
            prop = self._prop_from_response(item)
            if prop is None or prop.find("d:resourcetype", DAV_NS) is None:
                raise NextcloudApiError("WEBDAV_INCOMPLETE_LISTING", "WebDAV не вернул сведения обо всех запрошенных объектах.", 502)
            is_collection = prop.find("d:resourcetype/d:collection", DAV_NS) is not None
            length = prop.findtext("d:getcontentlength", default=None, namespaces=DAV_NS)
            mime_type = prop.findtext("d:getcontenttype", default=None, namespaces=DAV_NS)
            modified = prop.findtext("d:getlastmodified", default=None, namespaces=DAV_NS)
            etag = prop.findtext("d:getetag", default=None, namespaces=DAV_NS)
            file_id = prop.findtext("oc:fileid", default=None, namespaces=DAV_NS) or prop.findtext("nc:fileid", default=None, namespaces=DAV_NS)
            mount_root = prop.findtext("nc:is-mount-root", default=None, namespaces=DAV_NS)
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
                    permissions=prop.findtext("oc:permissions", default=None, namespaces=DAV_NS),
                    mount_type=prop.findtext("nc:mount-type", default=None, namespaces=DAV_NS),
                    is_mount_root=str(mount_root).casefold() == "true" if mount_root is not None else None,
                    owner_id=prop.findtext("oc:owner-id", default=None, namespaces=DAV_NS),
                )
            )
        return result

    async def mkcol(self, path: str, *, exclusive: bool = False) -> None:
        response = await self._client.request("MKCOL", self._dav_url(path))
        if response.status_code == 405 and exclusive:
            raise NextcloudApiError("CAMPAIGN_TARGET_EXISTS", "Путь новой папки уже занят.", 409, path)
        if response.status_code in {201, 405}:
            return
        if response.status_code in {401, 403}:
            raise NextcloudApiError("WEBDAV_FORBIDDEN", "Нет прав на создание папки WebDAV.", response.status_code)
        raise NextcloudApiError("WEBDAV_MKCOL_FAILED", "Не удалось создать папку WebDAV.", 502, response.text)

    async def move(self, source_path: str, target_path: str, *, overwrite: bool = False, expected_etag: str | None = None) -> None:
        source = self._safe_path(source_path)
        target = self._safe_path(target_path)
        destination = f"{self.credentials.base_url}{self._dav_url(target)}"
        headers = {"Destination": destination, "Overwrite": "T" if overwrite else "F"}
        if expected_etag is not None:
            headers["If-Match"] = expected_etag
        response = await self._client.request(
            "MOVE",
            self._dav_url(source),
            headers=headers,
        )
        if response.status_code in {201, 204}:
            return
        if expected_etag is not None and response.status_code in {404, 412}:
            raise NextcloudApiError("SCAN_PRECONDITION_FAILED", "Исходный файл изменился или целевой путь занят. Повторите проверку.", 409)
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
        file_request: bool = False,
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
        attributes = _share_attributes(file_request_enabled=file_request)
        if attributes:
            form["attributes"] = attributes
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
        share = {
            "share_id": str(data.get("id")) if data.get("id") is not None else None,
            "public_link": data.get("url"),
            "token": data.get("token"),
        }
        share_id = str(share.get("share_id") or "")
        if share_id:
            try:
                await self.update_share_permissions(
                    share_id,
                    permissions,
                    public_upload=public_upload,
                    file_request=file_request,
                )
            except Exception:
                with suppress(Exception):
                    await self.delete_public_share(share_id)
                raise
        return share

    async def shares_for_path(self, path: str) -> list[dict]:
        safe_path = self._safe_path(path)
        response = await self._client.get(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            params={"format": "json", "path": safe_path, "reshares": "true"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        if response.status_code in {401, 403}:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "Нет прав на просмотр общих доступов.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_API_FAILED", "Не удалось получить список общих доступов.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            raise NextcloudApiError("SHARE_API_FAILED", str(meta.get("message") or "Ошибка OCS Share API."), 502)
        data = payload.get("data", [])
        return data if isinstance(data, list) else []

    async def current_user_groups(self) -> set[str]:
        """Read group IDs from the deployment's profile for this authenticated caller."""
        unavailable = NextcloudApiError("GROUP_LOOKUP_FAILED", "Не удалось подтвердить группы пользователя Nextcloud.", 503)
        try:
            response = await self._client.get(
                "/ocs/v2.php/cloud/user",
                params={"format": "json"},
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            )
            response.raise_for_status()
            ocs = response.json()["ocs"]
            if ocs["meta"]["statuscode"] not in (100, 200):
                raise unavailable
            profile = ocs["data"]
            if profile["id"] != self.credentials.user:
                raise unavailable
            groups = profile["groups"]
            if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
                raise unavailable
            return set(groups)
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise unavailable from exc

    async def list_groups(self, *, search: str = "", limit: int = 200) -> list[str]:
        params = {"format": "json", "search": search, "limit": limit}
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        response = await self._client.get("/ocs/v2.php/cloud/groups", params=params, headers=headers)
        if response.status_code == 404:
            response = await self._client.get("/ocs/v1.php/cloud/groups", params=params, headers=headers)
        if response.status_code in {401, 403}:
            raise NextcloudApiError("GROUPS_API_FORBIDDEN", "Нет прав на получение списка групп Nextcloud.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("GROUPS_API_FAILED", "Не удалось получить список групп Nextcloud.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            raise NextcloudApiError("GROUPS_API_FAILED", str(meta.get("message") or "Ошибка OCS Groups API."), 502)
        data = payload.get("data", {})
        groups = data.get("groups", data) if isinstance(data, dict) else data
        return sorted({str(group).strip() for group in groups or [] if str(group).strip()}, key=str.casefold)

    async def user_email(self, user_id: str) -> str:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise NextcloudApiError("CURRENT_USER_NOT_FOUND", "Не удалось определить текущего пользователя Nextcloud.", 400)
        params = {"format": "json"}
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        response = await self._client.get(f"/ocs/v2.php/cloud/users/{quote(user_id, safe='')}", params=params, headers=headers)
        if response.status_code == 404:
            response = await self._client.get(f"/ocs/v1.php/cloud/users/{quote(user_id, safe='')}", params=params, headers=headers)
        if response.status_code in {401, 403}:
            raise NextcloudApiError("USERS_API_FORBIDDEN", "Нет прав на получение email текущего пользователя Nextcloud.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("USERS_API_FAILED", "Не удалось получить данные текущего пользователя Nextcloud.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            raise NextcloudApiError("USERS_API_FAILED", str(meta.get("message") or "Ошибка OCS Users API."), 502)
        data = payload.get("data", {})
        email = str(data.get("email") or data.get("emailAddress") or "").strip()
        if not email:
            raise NextcloudApiError("CURRENT_USER_EMAIL_MISSING", "У текущего пользователя Nextcloud не указан email.", 400)
        return email

    async def _put_share_update(self, share_id: str, data: dict[str, str | int]) -> None:
        response = await self._client.put(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{quote(str(share_id), safe='')}",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            data=data,
        )
        if response.status_code in {401, 403}:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "Нет прав на изменение общего доступа.", response.status_code)
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_UPDATE_FAILED", "Не удалось обновить общий доступ.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            raise NextcloudApiError("SHARE_UPDATE_FAILED", str(meta.get("message") or "Не удалось обновить общий доступ."), 502)

    async def update_share_permissions(
        self,
        share_id: str,
        permissions: int,
        *,
        public_upload: bool | None = None,
        file_request: bool | None = None,
    ) -> None:
        updates: list[dict[str, str | int]] = []
        if public_upload is not None:
            updates.append({"publicUpload": "true" if public_upload else "false"})
        updates.append({"permissions": permissions})
        attributes = _share_attributes(file_request_enabled=file_request)
        if attributes:
            updates.append({"attributes": attributes})
        for update in updates:
            await self._put_share_update(share_id, update)

    async def ensure_group_share(
        self,
        path: str,
        group_id: str,
        *,
        permissions: int = 31,
        label: str | None = None,
        note: str | None = None,
    ) -> dict[str, str | int | None]:
        group_id = str(group_id or "").strip()
        if not group_id:
            return {"status": "skipped", "share_id": None, "permissions": None}
        safe_path = self._safe_path(path)
        for share in await self.shares_for_path(safe_path):
            share_type = str(share.get("share_type") or share.get("shareType") or "")
            share_with = str(share.get("share_with") or share.get("shareWith") or "")
            if share_type == "1" and share_with == group_id:
                share_id = str(share.get("id") or "")
                current_permissions = int(share.get("permissions") or 0)
                if share_id and current_permissions != permissions:
                    await self.update_share_permissions(share_id, permissions)
                return {"status": "existing", "share_id": share_id, "permissions": permissions}

        form: dict[str, str | int] = {
            "path": safe_path,
            "shareType": 1,
            "shareWith": group_id,
            "permissions": permissions,
        }
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
        if response.status_code in {401, 403}:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "Нет прав на выдачу доступа группе.", response.status_code, response.text)
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_API_FAILED", "Не удалось выдать доступ группе.", 502, response.text)
        payload = response.json().get("ocs", {})
        meta = payload.get("meta", {})
        if int(meta.get("statuscode", 100)) >= 400:
            raise NextcloudApiError("SHARE_API_FAILED", str(meta.get("message") or "Ошибка OCS Share API."), 502)
        data = payload.get("data", {})
        share_id = str(data.get("id") or "")
        if share_id:
            await self.update_share_permissions(share_id, permissions)
        return {"status": "created", "share_id": share_id, "permissions": permissions}

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
            raise NextcloudApiError("MAIL_BRIDGE_TOKEN_MISSING", "Не найден bridge-токен почтового сервиса Nextcloud.", 500)
        response = await self._client.post(
            "/oo_file_collector_mail_bridge.php",
            json={"to": to_email, "subject": subject, "body": body},
            headers={"X-OO-File-Collector-Token": token},
        )
        if response.status_code in {400, 401, 403, 404, 405, 413, 415, 422}:
            raise NextcloudApiError("NEXTCLOUD_MAIL_SEND_FAILED", "Почтовый мост отклонил запрос.", 502)
        if response.is_success:
            with suppress(ValueError):
                if response.json() == {"status": "sent"}:
                    return
        raise NextcloudApiError("MAIL_DELIVERY_UNCONFIRMED", "Почтовый мост не подтвердил отправку письма.", 502)

    async def test_webdav(self) -> dict[str, str | int | bool | None | list[str]]:
        entries = await self.propfind(self.credentials.root_path, depth=0)
        entry = entries[0] if entries else None
        if entry is None:
            return {"status": "missing", "entries": 0}
        permissions = entry.permissions or ""
        required = {"G", "C", "K"}
        missing = sorted(required.difference(permissions))
        return {
            "status": "ok" if entry.is_collection and not missing else "warning",
            "entries": len(entries),
            "path": entry.path,
            "is_collection": entry.is_collection,
            "permissions": permissions,
            "missing_permissions": missing,
            "mount_type": entry.mount_type or "local",
            "is_group_folder": entry.mount_type == "group",
            "is_mount_root": entry.is_mount_root,
            "owner_id": entry.owner_id,
        }

    async def test_share_api(self) -> dict[str, str]:
        response = await self._client.get(
            "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            params={"format": "json"},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise NextcloudApiError("SHARE_API_FORBIDDEN", "OCS Share API недоступен.", response.status_code, response.text)
        return {"status": "ok"}
