import asyncio

from monitoringi.nextcloud import NextcloudClient, NextcloudCredentials


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
