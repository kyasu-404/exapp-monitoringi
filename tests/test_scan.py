from datetime import date
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlmodel import Session, SQLModel, create_engine

from monitoringi import api
from monitoringi.errors import NextcloudApiError
from monitoringi.models import BatchStatus, Campaign, CampaignOrganization, CheckResult, MessageBatch, Organization
from monitoringi.nextcloud import NextcloudClient, NextcloudCredentials, WebDAVEntry
from monitoringi.schemas import ScanIn
from monitoringi.services import campaign_organization_upload_path


ROOT = "/Monitoringi/Test"
USER = SimpleNamespace(scope={"username": "operator"})


class FakeDav:
    def __init__(self):
        self.files = {}
        self.errors = {}
        self.moves = []
        self.reject_move = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def propfind(self, path, *, depth):
        assert depth in {0, 1}
        if path in self.errors:
            raise self.errors[path]
        entries = [WebDAVEntry(path, path.rsplit("/", 1)[-1], True, None, None, None, file_id=path)]
        if depth == 0:
            return entries
        for source, etag in self.files.items():
            if source.rsplit("/", 1)[0] == path:
                entries.append(WebDAVEntry(source, source.rsplit("/", 1)[-1], False, 12, None, None, etag))
        return entries

    async def mkcol(self, path):
        assert path.startswith(ROOT + "/")

    async def move(self, source, target, *, expected_etag):
        if source in self.reject_move:
            raise NextcloudApiError("SCAN_PRECONDITION_FAILED", "Изменена версия файла.", 409)
        assert self.files[source] == expected_etag
        assert target not in self.files
        self.moves.append((source, target, expected_etag))
        self.files[target] = self.files.pop(source)


@pytest.fixture
def fixture(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    dav = FakeDav()
    monkeypatch.setattr(api, "_nc_credentials", lambda *args: {})
    monkeypatch.setattr(api, "NextcloudClient", lambda *args: dav)
    with Session(engine) as session:
        yield session, dav


def make_campaign(session, mode, participants):
    campaign = Campaign(title="Test", date=date(2026, 7, 8), parent_nc_path="/Monitoringi", campaign_folder_path=ROOT,
                        allowed_extensions=["pdf"], upload_mode=mode, selected_org_types=sorted({typ for typ, _ in participants}))
    session.add(campaign)
    orgs = []
    for typ, code in participants:
        org = Organization(type=typ, code=code, display_name=f"{typ} {code}", email=f"{typ}{code}@example.test")
        orgs.append(org)
        session.add(org)
        session.add(CampaignOrganization(campaign_id=campaign.id, organization_id=org.id,
                                         folder_path=campaign_organization_upload_path(campaign, org)))
    session.commit()
    return campaign, orgs


@pytest.mark.asyncio
async def test_type_folders_match_same_number_independently(fixture):
    session, dav = fixture
    campaign, orgs = make_campaign(session, "org_type", [("school", "42"), ("kindergarten", "42")])
    for org in orgs:
        dav.files[campaign_organization_upload_path(campaign, org) + "/42.pdf"] = '"v1"'
    dav.files[ROOT + "/42.pdf"] = '"unassigned"'
    preview = await api.scan_preview(campaign.id, session, USER)
    assert {(file["organization_id"], file["classification"]) for file in preview["files"]} == {(org.id, "valid") for org in orgs}
    result = await api.scan_apply(campaign.id, session, USER, ScanIn(batch_id=preview["batch_id"]))
    assert (result["summary"]["valid"], result["summary"]["missing"], dav.moves) == (2, 0, [])


@pytest.mark.asyncio
async def test_partial_folder_failure_excludes_reminders_and_recovers(fixture):
    session, dav = fixture
    campaign, (empty, denied, offline) = make_campaign(session, "organization", [("school", "1"), ("school", "2"), ("school", "3")])
    dav.errors[ROOT + "/2"] = NextcloudApiError("WEBDAV_AUTH_FAILED", "Нет доступа.", 403)
    dav.errors[ROOT + "/3"] = httpx.ReadTimeout("timeout")
    preview = await api.scan_preview(campaign.id, session, USER)
    assert preview["summary"]["missing_organization_codes"] == ["1"]
    applied = await api.scan_apply(campaign.id, session, USER)
    assert applied["status"] == "completed_with_errors"
    summary = applied["summary"]
    assert sorted(summary["unverified_organization_ids"]) == sorted([denied.id, offline.id])
    assert (summary["folders_without_files"], summary["missing"], summary["unverified"]) == (1, 1, 2)
    assert session.get(CheckResult, applied["check_result_id"]).summary_json == summary
    assert [item["organization_id"] for item in api.messages_preview(campaign.id, session)["items"]] == [empty.id]
    assert [row["id"] for row in api.campaign_missing(campaign.id, session)] == [empty.id]
    book = load_workbook(BytesIO(api.campaign_report(campaign.id, session).body))
    assert set(book["Ошибки"].values) >= {(ROOT + "/2", "Нет доступа.")}
    old_preview = MessageBatch(campaign_id=campaign.id, check_result_id=applied["check_result_id"])
    all_links = MessageBatch(campaign_id=campaign.id, check_result_id=applied["check_result_id"], summary_json={"mailing_kind": "all_links"})
    queued = MessageBatch(campaign_id=campaign.id, check_result_id=applied["check_result_id"], status=BatchStatus.running)
    session.add_all([old_preview, all_links, queued])
    session.commit()
    dav.errors.clear()
    dav.files[ROOT + "/2/submitted.txt"] = '"ok"'
    recovered = await api.scan_apply(campaign.id, session, USER)
    assert (recovered["status"], recovered["summary"]["unverified"]) == ("completed", 0)
    assert set(recovered["summary"]["missing_organization_codes"]) == {"1", "3"}
    assert old_preview.status == BatchStatus.cancelled
    assert all_links.status == BatchStatus.preview
    assert queued.status == BatchStatus.running


@pytest.mark.asyncio
async def test_cleanup_only_applies_unchanged_preview_items(fixture):
    session, dav = fixture
    campaign, orgs = make_campaign(session, "campaign_root", [("school", str(n)) for n in range(1, 6)])
    good, changed, new, raced, absent = orgs
    trash = ROOT + "/bad.txt"
    stale = ROOT + "/2 (1).pdf"
    late = ROOT + "/3.pdf"
    raced_path = ROOT + "/4 (1).pdf"
    dav.files.update({ROOT + "/1.pdf": '"ok"', trash: '"trash"', stale: '"old"', raced_path: '"race"'})
    preview = await api.scan_preview(campaign.id, session, USER)
    targets = {file["path"]: file["target_path"] for file in preview["files"]}
    dav.files.update({stale: '"changed"', late: '"new"'})
    dav.reject_move.add(raced_path)
    result = await api.scan_apply(campaign.id, session, USER, ScanIn(batch_id=preview["batch_id"]))
    assert dav.moves == [(trash, targets[trash], '"trash"')]
    assert {w["path"] for w in result["summary"]["scan_warnings"]} == {stale, late, raced_path}
    assert {f["path"] for f in result["files"]} == {ROOT + "/1.pdf", trash}
    assert set(result["summary"]["unverified_organization_ids"]) == {changed.id, new.id, raced.id}
    assert result["summary"]["missing_organization_codes"] == [absent.code]
    assert [item["organization_id"] for item in api.messages_preview(campaign.id, session)["items"]] == [absent.id]
    assert result["summary"]["skipped"] == 3
    assert {row["current_path"] for row in api.campaign_files(campaign.id, session)} == {ROOT + "/1.pdf", targets[trash]}
    with pytest.raises(HTTPException) as stale_preview:
        await api.scan_apply(campaign.id, session, USER, ScanIn(batch_id=preview["batch_id"]))
    assert stale_preview.value.status_code == 409
    assert len(dav.moves) == 1


@pytest.mark.asyncio
async def test_scan_preview_bound_to_user_campaign_context_and_latest_check(fixture):
    session, dav = fixture
    campaign, (participant,) = make_campaign(session, "campaign_root", [("school", "1")])
    dav.files[ROOT + "/bad.txt"] = '"1"'
    for outside in ["/Monitoringi/Other", ROOT + "/subfolder"]:
        with pytest.raises(HTTPException):
            await api.scan_preview(campaign.id, session, USER, ScanIn(path=outside))
    with pytest.raises(HTTPException) as missing_preview:
        await api.scan_apply(campaign.id, session, USER)
    assert missing_preview.value.detail["code"] == "PREVIEW_REQUIRED"
    preview = await api.scan_preview(campaign.id, session, USER)
    payload = ScanIn(batch_id=preview["batch_id"])
    other_user = SimpleNamespace(scope={"username": "colleague"})
    with pytest.raises(HTTPException) as wrong_user:
        await api.scan_apply(campaign.id, session, other_user, payload)
    assert wrong_user.value.detail["code"] == "INVALID_SCAN_PREVIEW"
    other = Campaign(title="Other", date=date.today(), parent_nc_path="/Monitoringi", campaign_folder_path="/Monitoringi/Other", upload_mode="campaign_root")
    session.add(other)
    session.commit()
    with pytest.raises(HTTPException) as wrong_campaign:
        await api.scan_apply(other.id, session, USER, payload)
    assert wrong_campaign.value.detail["code"] == "INVALID_SCAN_PREVIEW"
    participant.aliases = ["Changed"]
    session.add(participant)
    session.commit()
    with pytest.raises(HTTPException) as changed:
        await api.scan_apply(campaign.id, session, USER, payload)
    assert changed.value.detail["code"] == "STALE_SCAN_PREVIEW"
    assert not dav.moves
    first = await api.scan_preview(campaign.id, session, USER)
    second = await api.scan_preview(campaign.id, session, USER)
    await api.scan_apply(campaign.id, session, USER, ScanIn(batch_id=second["batch_id"]))
    with pytest.raises(HTTPException) as superseded:
        await api.scan_apply(campaign.id, session, USER, ScanIn(batch_id=first["batch_id"]))
    assert superseded.value.detail["code"] == "STALE_SCAN_PREVIEW"


@pytest.mark.asyncio
async def test_move_checks_version_on_the_mutation():
    def handler(request):
        assert request.method == "MOVE"
        assert request.headers["If-Match"] == '"before"'
        assert request.headers["Overwrite"] == "F"
        return httpx.Response(412)

    client = NextcloudClient(NextcloudCredentials(base_url="https://cloud.example.test", user="u", root_path="/", app_password="test"))
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example.test")
    async with client:
        with pytest.raises(NextcloudApiError) as failure:
            await client.move("/source", "/target", expected_etag='"before"')
    assert failure.value.code == "SCAN_PRECONDITION_FAILED"
