import asyncio
from urllib.parse import parse_qs

import httpx

from monitoringi.api import GROUP_SHARE_PERMISSIONS, _root_access_status
from monitoringi.nextcloud import NextcloudClient, NextcloudCredentials
from monitoringi.nextcloud import WebDAVEntry


def test_propfind_parses_nextcloud_mount_and_permission_properties():
    client = NextcloudClient(
        NextcloudCredentials(
            base_url="https://nextcloud.example.test",
            user="alice",
            root_path="/Мониторинги",
            app_password="secret",
        )
    )
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/alice/%D0%9C%D0%BE%D0%BD%D0%B8%D1%82%D0%BE%D1%80%D0%B8%D0%BD%D0%B3%D0%B8/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection /></d:resourcetype>
        <oc:fileid>42</oc:fileid>
        <oc:permissions>RGDNVCK</oc:permissions>
        <nc:mount-type>group</nc:mount-type>
        <nc:is-mount-root>true</nc:is-mount-root>
        <oc:owner-id>admin</oc:owner-id>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

    entries = client._parse_multistatus(xml)

    assert entries[0].path == "/Мониторинги"
    assert entries[0].is_collection is True
    assert entries[0].permissions == "RGDNVCK"
    assert entries[0].mount_type == "group"
    assert entries[0].is_mount_root is True
    assert entries[0].owner_id == "admin"
    asyncio.run(client.aclose())


def test_public_file_request_share_is_reconfigured_after_create():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"ocs": {"meta": {"statuscode": 100}, "data": {"id": 7, "url": "https://nc/s/token", "token": "token"}}},
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"ocs": {"meta": {"statuscode": 100}, "data": {}}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def scenario():
        client = NextcloudClient(
            NextcloudCredentials(
                base_url="https://nextcloud.example.test",
                user="alice",
                root_path="/",
                app_password="secret",
            )
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://nextcloud.example.test")
        try:
            share = await client.create_public_share("/drop", public_upload=True, permissions=4, file_request=True)
        finally:
            await client.aclose()
        return share

    share = asyncio.run(scenario())

    assert share["share_id"] == "7"
    assert [request.method for request in requests] == ["POST", "PUT", "PUT", "PUT"]
    update_bodies = [parse_qs(request.content.decode()) for request in requests[1:]]
    assert update_bodies[0] == {"publicUpload": ["true"]}
    assert update_bodies[1] == {"permissions": ["4"]}
    assert "fileRequest" in update_bodies[2]["attributes"][0]
    assert '"value": true' in update_bodies[2]["attributes"][0]


class RootAccessClient:
    def __init__(self, entry: WebDAVEntry, shares: list[dict] | None = None) -> None:
        self.entry = entry
        self.shares = shares or []
        self.created_share = False

    async def propfind(self, _path: str, *, depth: int = 0) -> list[WebDAVEntry]:
        assert depth == 0
        return [self.entry]

    async def shares_for_path(self, _path: str) -> list[dict]:
        return self.shares

    async def ensure_group_share(self, _path: str, group_id: str, *, permissions: int, label: str | None = None, note: str | None = None) -> dict:
        self.created_share = True
        return {"status": "created", "share_id": "99", "permissions": permissions, "group_id": group_id}


def root_entry(*, mount_type: str | None, is_mount_root: bool | None) -> WebDAVEntry:
    return WebDAVEntry(
        path="/Monitoringi",
        name="Monitoringi",
        is_collection=True,
        size=None,
        mtime=None,
        mime_type=None,
        permissions="RMGDNVCK",
        mount_type=mount_type,
        is_mount_root=is_mount_root,
        owner_id="kyasu",
    )


def test_root_access_requires_group_mount_root_for_team_folder():
    client = RootAccessClient(root_entry(mount_type="group", is_mount_root=True))

    result = asyncio.run(_root_access_status(client, "/Monitoringi", "Test", ensure_share=False))

    assert result["status"] == "ok"
    assert result["mode"] == "team_folder"
    assert result["is_group_folder"] is True
    assert result["is_team_folder_root"] is True


def test_root_access_warns_for_owner_folder_without_existing_group_share():
    client = RootAccessClient(root_entry(mount_type=None, is_mount_root=None))

    result = asyncio.run(_root_access_status(client, "/Monitoringi", "Test", ensure_share=False))

    assert result["status"] == "warning"
    assert result["mode"] == "owner_folder"
    assert result["is_group_folder"] is False
    assert result["missing_group_share"] is True


def test_root_access_fallback_can_create_group_share_on_save():
    client = RootAccessClient(root_entry(mount_type=None, is_mount_root=None))

    result = asyncio.run(_root_access_status(client, "/Monitoringi", "Test", ensure_share=True))

    assert result["status"] == "ok"
    assert result["mode"] == "group_share_fallback"
    assert client.created_share is True
    assert result["group_share_permissions"] == GROUP_SHARE_PERMISSIONS


def test_exclusive_mkcol_detects_existing_collection():
    from monitoringi.errors import NextcloudApiError

    async def scenario():
        client = NextcloudClient(NextcloudCredentials(base_url="https://nc.test", user="alice", root_path="/", app_password="test"))
        await client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(405)), base_url="https://nc.test",
        )
        async with client:
            await client.mkcol("/existing")
            try:
                await client.mkcol("/existing", exclusive=True)
            except NextcloudApiError as exc:
                assert (exc.code, exc.status_code) == ("CAMPAIGN_TARGET_EXISTS", 409)
            else:
                raise AssertionError("exclusive MKCOL accepted an existing collection")

    asyncio.run(scenario())
