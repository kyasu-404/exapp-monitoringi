import re
from base64 import b64decode, b64encode
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from monitoringi import api, nextcloud
from monitoringi.db import get_session
from monitoringi.errors import AppError
from monitoringi.models import Organization, SettingsEntry
from monitoringi.services import nextcloud_overrides


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SettingsEntry(key="root_path", value_json="/Monitoringi"))
        session.add(SettingsEntry(key="operator_group", value_json="operators"))
        session.add(SettingsEntry(key="collaboration_group", value_json="folder-team"))
        session.add(SettingsEntry(key="nc_base_url", value_json="https://retired.invalid"))
        session.add(Organization(type="school", code="1", display_name="School"))
        session.commit()

    app = FastAPI()
    app.include_router(api.router)

    def sessions():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = sessions

    @app.exception_handler(AppError)
    async def handle_app_error(_request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    state = SimpleNamespace(user="operator", groups=["operators"], profile_id=None, redirect=False, requests=[], engine=engine)
    monkeypatch.setattr(nextcloud, "SETTINGS", replace(
        nextcloud.SETTINGS, nc_base_url="https://deployed.invalid/cloud/index.php",
        app_secret="test-secret", app_id="collector", app_version="1", nc_app_password="unused",
    ))

    def handle_profile(request):
        state.requests.append(request)
        assert str(request.url) == "https://deployed.invalid/cloud/ocs/v2.php/cloud/user?format=json"
        assert b64decode(request.headers["authorization-app-api"]).decode() == f"{state.user}:test-secret"
        if state.redirect:
            return httpx.Response(302, headers={"Location": "https://elsewhere.invalid"})
        return httpx.Response(200, json={"ocs": {
            "meta": {"statuscode": 200}, "data": {"id": state.profile_id or state.user, "groups": state.groups},
        }})

    monkeypatch.setattr(nextcloud.httpx, "AsyncClient", partial(
        httpx.AsyncClient, transport=httpx.MockTransport(handle_profile), trust_env=False,
    ))

    # Supply the ASGI identity at the same boundary as AppAPIAuthMiddleware.
    async def authenticated_app(scope, receive, send):
        if scope["type"] == "http":
            scope["username"] = state.user
        await app(scope, receive, send)

    with TestClient(authenticated_app) as client:
        state.client = client
        yield state
    engine.dispose()


def test_appapi_identity_and_operator_membership_are_required(deployment):
    d = deployment
    d.user = None
    assert d.client.get("/api/organizations", headers={"X-Nextcloud-User-Id": "admin"}).status_code == 401
    assert d.requests == []
    d.user = "folder-member"
    d.groups = ["folder-team"]
    for method, path in (
        ("GET", "/api/campaigns/example/report.xlsx"), ("GET", "/api/campaigns/example/links.csv"),
        ("GET", "/api/organizations"), ("POST", "/api/message-batches/example/send"),
        ("GET", "/api/batches/example"), ("POST", "/api/files-actions/open"),
    ):
        assert d.client.request(method, path, json={}, headers={"Remote-User": "admin"}).status_code == 403


def test_operator_roles_are_rechecked_and_settings_stay_private(deployment):
    d = deployment
    assert d.client.get("/api/access").json() == {"is_admin": False, "defaults": {"root_path": "/Monitoringi"}}
    assert len(d.requests) == 1
    assert d.client.get("/api/organizations").json()[0]["code"] == "1"
    for method, path in (
        ("GET", "/api/settings"), ("PUT", "/api/settings"), ("GET", "/api/settings/groups"),
        ("POST", "/api/settings/test-webdav"), ("POST", "/api/settings/test-share-api"),
        ("POST", "/api/settings/test-mail"), ("DELETE", "/api/organizations"),
    ):
        assert d.client.request(method, path, json={}).status_code == 403
    csv = "type;code;active\nschool;1;false\n".encode()
    assert d.client.post("/api/organizations/import-json?apply=true&allow_update=true", json={
        "filename": "orgs.csv", "content_base64": b64encode(csv).decode(),
    }).status_code == 403
    assert len(d.client.get("/api/organizations").json()) == 1
    d.groups = []
    assert d.client.get("/api/organizations").status_code == 403


def test_first_run_is_admin_only_even_with_legacy_folder_group(deployment, monkeypatch):
    d = deployment
    with Session(d.engine) as session:
        session.delete(session.get(SettingsEntry, "operator_group"))
        session.commit()
    d.groups = ["operators", "folder-team"]
    assert d.client.get("/api/access").status_code == 403
    d.groups = ["admin"]
    assert d.client.get("/api/access").json()["is_admin"] is True
    assert "nc_base_url" not in d.client.get("/api/settings").json()
    assert d.client.delete("/api/organizations").json()["count"] == 1

    async def checked_root(*args, **kwargs):
        return {"status": "ok"}
    monkeypatch.setattr(api, "_root_access_status", checked_root)
    assert d.client.put("/api/settings", json={"operator_group": "new-team"}).status_code == 200
    d.groups = ["folder-team"]
    assert d.client.get("/api/access").status_code == 403
    d.groups = ["new-team"]
    assert d.client.get("/api/access").json()["is_admin"] is False


@pytest.mark.parametrize("profile_id, groups", [("another-user", ["admin"]), (None, "admin"), (None, [None])])
def test_invalid_profile_never_grants_access(deployment, profile_id, groups):
    deployment.profile_id = profile_id
    deployment.groups = groups
    assert deployment.client.get("/api/settings").status_code == 503


def test_role_lookup_rejects_redirects_without_forwarding_credentials(deployment):
    deployment.redirect = True
    assert deployment.client.get("/api/access").status_code == 503
    assert len(deployment.requests) == 1


def test_base_url_is_deployment_owned_including_for_legacy_records(deployment):
    d = deployment
    d.groups = ["admin"]
    for method, path in [("PUT", "/api/settings"), ("POST", "/api/settings/test-webdav")]:
        assert d.client.request(method, path, json={"nc_base_url": "https://supplied.invalid"}).status_code == 422
    with Session(d.engine) as session:
        assert nextcloud.configured_credentials(nextcloud_overrides(session, d.user)).base_url == "https://deployed.invalid/cloud"
    with pytest.raises(AppError, match="NEXTCLOUD_URL_OVERRIDE"):
        nextcloud.configured_credentials({"nc_base_url": "https://supplied.invalid"})


def test_admin_routes_do_not_overlap_user_manifest_routes():
    routes = ElementTree.parse(Path(__file__).resolve().parents[1] / "appinfo/info.xml").findall(".//route")
    for endpoint in api.router.routes:
        path = re.sub(r"\{[^}]+\}", "individual", endpoint.path)
        for method in endpoint.methods:
            admin = path.startswith("/api/settings") or (method == "DELETE" and path == "/api/organizations")
            matches = [
                route.findtext("access_level") for route in routes
                if route.findtext("verb") == method and re.fullmatch(route.findtext("url"), path)
            ]
            assert matches == ["ADMIN" if admin else "USER"], (method, path)
