from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from monitoringi import api
from monitoringi.db import get_session
from monitoringi.models import Campaign, CampaignOrganization, Organization
from monitoringi.nextcloud import WebDAVEntry


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.asyncio
async def test_export_routes_return_files_and_keep_organization_lookup(session):
    org = Organization(type="school", code="15", display_name="School 15")
    session.add(org)
    session.commit()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[api.require_operator] = lambda: False

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path, filename in [
            ("export.csv", "organizations.csv"),
            ("export.xlsx", "organizations.xlsx"),
            ("export/xlsx", "organizations.xlsx"),
        ]:
            response = await client.get(f"/api/organizations/{path}")
            assert response.status_code == 200
            assert filename in response.headers["Content-Disposition"]
        response = await client.get(f"/api/organizations/{org.id}")
        assert response.json()["id"] == org.id


@pytest.mark.asyncio
async def test_shared_folder_recent_uploads_are_read_once_and_attributed_by_name(session, monkeypatch):
    folder = "/Monitoring/Autumn"
    campaign = Campaign(
        title="Autumn", date=date(2026, 10, 1), parent_nc_path="/Monitoring",
        campaign_folder_path=folder, upload_mode="campaign_root",
    )
    session.add(campaign)
    schools = []
    for code in ("15", "25"):
        org = Organization(type="school", code=code, display_name=f"School {code}")
        schools.append(org)
        session.add(org)
        session.add(CampaignOrganization(campaign_id=campaign.id, organization_id=org.id,
                                         public_link="shared", folder_path=folder))
    session.commit()

    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def propfind(self, path, *, depth):
            calls.append((path, depth))
            return [WebDAVEntry(f"{path}/{name}", name, False, 10, None, "application/pdf")
                    for name in ("15.pdf", "unknown.pdf")]

    monkeypatch.setattr(api, "_nc_credentials", lambda *_: None)
    monkeypatch.setattr(api, "NextcloudClient", lambda *_: Client())
    rows = await api.recent_uploads(session, SimpleNamespace(scope={}), limit=10)
    assert calls == [(folder, 1)]
    assert {row["name"]: row["organization_id"] for row in rows} == {
        "15.pdf": schools[0].id, "unknown.pdf": None,
    }
