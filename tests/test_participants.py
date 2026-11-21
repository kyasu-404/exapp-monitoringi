from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from monitoringi import api
from monitoringi.errors import AppError
from monitoringi.models import (
    BatchStatus,
    Campaign,
    CampaignOrganization,
    CampaignOrganizationStatus,
    MessageBatch,
    OperationBatch,
    Organization,
)
from monitoringi.nextcloud import WebDAVEntry
from monitoringi.schemas import CampaignPatch, SelectOrganizationsIn
from monitoringi.services import campaign_participant_paths, campaign_share_targets, create_batch, planned_folder_items


REQUEST = SimpleNamespace(scope={"username": "tester"})


@pytest.fixture
def catalog():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        campaign = Campaign(
            id="c", title="Exam", date=date(2026, 7, 1), parent_nc_path="/root",
            campaign_folder_path="/root/Exam", selected_org_types=["school", "kindergarten"], org_folder_layout="flat",
        )
        school = Organization(id="s", type="school", code="1", display_name="School 1")
        garden = Organization(id="k", type="kindergarten", code="2", display_name="Garden 2")
        session.add_all([campaign, school, garden])
        session.commit()
        yield session, campaign, school, garden


def select_ids(session, *ids):
    return api.select_campaign_organizations("c", SelectOrganizationsIn(organization_ids=list(ids)), session, REQUEST)


@pytest.mark.asyncio
async def test_form_and_reselection_preserve_link_status_and_reference(catalog):
    session, campaign, school, garden = catalog
    select_ids(session, school.id)
    record = session.exec(select(CampaignOrganization)).one()
    record.share_id = "123"
    record.public_link = "https://nc/s/abc"
    record.mail_body = "A saved message"
    record.mail_subject = "Subject"
    record.status = CampaignOrganizationStatus.uploaded
    record.last_file_detected_at = datetime(2026, 7, 2)
    session.commit()
    session.refresh(record)
    original = record.model_dump()

    await api.update_campaign("c", CampaignPatch(
        description="Changed", selected_org_types=["kindergarten", "school"],
        upload_mode="organization", org_folder_layout="flat",
    ), session, REQUEST)
    select_ids(session, school.id)
    listed = api._campaign_orgs(session, campaign)
    session.refresh(record)
    assert [participant.id for participant in listed] == [record.id]
    assert record.model_dump() == original
    assert (record.share_id, record.public_link, record.mail_body, record.status) == (
        "123", "https://nc/s/abc", "A saved message", CampaignOrganizationStatus.uploaded,
    )

    preview = create_batch(session, campaign_id="c", operation_type="create_shares", user_id=None)
    select_ids(session, school.id, garden.id)
    assert session.get(OperationBatch, preview.id).status == BatchStatus.cancelled
    assert session.get(CampaignOrganization, record.id).share_id == "123"
    with pytest.raises(AppError, match="PREVIEW_STALE"):
        await api.create_shares_apply("c", session, REQUEST, batch_id=preview.id)
    with pytest.raises(AppError, match="PARTICIPANT_LINKS_IN_USE"):
        select_ids(session, garden.id)
    session.rollback()
    select_ids(session, school.id)
    assert [participant.id for participant in api._campaign_orgs(session, campaign)] == [record.id]


@pytest.mark.asyncio
async def test_shared_link_membership_and_revocation(catalog, monkeypatch):
    session, campaign, school, garden = catalog
    campaign.upload_mode = "campaign_root"
    session.commit()
    select_ids(session, school.id, garden.id)
    participants = api._campaign_orgs(session, campaign)
    for row in participants:
        row.share_id = "77"
        row.public_link = "https://nc/s/shared"
        row.status = CampaignOrganizationStatus.link_created
    session.commit()
    with pytest.raises(AppError, match="PARTICIPANT_LINKS_IN_USE"):
        select_ids(session, school.id)
    session.rollback()
    with pytest.raises(AppError, match="PARTICIPANT_LINKS_IN_USE"):
        await api.update_campaign("c", CampaignPatch(upload_mode="organization"), session, REQUEST)
    session.rollback()

    deleted = []

    class Client:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def propfind(self, path, *, depth):
            assert depth == 0
            return [WebDAVEntry(path, "Exam", True, None, None, None, file_id="42")]

        async def delete_public_share(self, share_id):
            deleted.append(share_id)

    monkeypatch.setattr(api, "NextcloudClient", Client)
    monkeypatch.setattr(api, "_nc_credentials", lambda *_: None)
    result = await api.delete_campaign_shares("c", session, REQUEST)
    assert result["summary"]["deleted"] == 1
    assert deleted == ["77"]
    select_ids(session, school.id)
    assert [row.organization_id for row in api._campaign_orgs(session, campaign)] == [school.id]
    select_ids(session)
    assert not api._campaign_orgs(session, campaign)


@pytest.mark.parametrize("second_code", ["1", " №1. "])
def test_flat_targets_require_unique_normalized_paths(catalog, second_code):
    session, campaign, school, garden = catalog
    garden.code = second_code
    session.commit()
    with pytest.raises(AppError, match="ORGANIZATION_FOLDER_CONFLICT"):
        select_ids(session, school.id, garden.id)
    session.rollback()
    assert not session.exec(select(CampaignOrganization)).all()
    with pytest.raises(AppError, match="ORGANIZATION_FOLDER_CONFLICT"):
        api._campaign_orgs(session, campaign)
    campaign.org_folder_layout = "by_type"
    assert len(set(campaign_participant_paths(campaign, [school, garden]).values())) == 2


@pytest.mark.asyncio
async def test_legacy_overlap_requires_new_distinct_targets(catalog):
    session, campaign, school, garden = catalog
    records = [
        CampaignOrganization(campaign_id="c", organization_id=org.id, folder_path="/root/Exam/1")
        for org in [school, garden]
    ]
    session.add_all(records)
    session.commit()
    organizations = [school, garden]
    for planner in (planned_folder_items, campaign_share_targets):
        with pytest.raises(AppError, match="ORGANIZATION_FOLDER_CONFLICT"):
            planner(campaign, records, organizations)
    with pytest.raises(AppError, match="ORGANIZATION_FOLDER_CONFLICT"):
        select_ids(session, school.id)
    session.rollback()
    await api.update_campaign("c", CampaignPatch(org_folder_layout="by_type"), session, REQUEST)
    items = planned_folder_items(campaign, records, organizations)
    assert not any(item["action"] == "move" for item in items)
    assert {row.folder_path for row in records} == {"/root/Exam/Школы/1", "/root/Exam/Сады/2"}


@pytest.mark.asyncio
async def test_change_invalidates_mail_and_folder_previews_and_running_operation_blocks_edits(catalog):
    session, campaign, school, garden = catalog
    select_ids(session, school.id)
    mail = MessageBatch(campaign_id="c", check_result_id="check")
    session.add(mail)
    folder = create_batch(session, campaign_id="c", operation_type="create_folders", user_id=None)
    folder.status = BatchStatus.running
    session.commit()
    with pytest.raises(AppError, match="PARTICIPANT_OPERATION_RUNNING"):
        select_ids(session, garden.id)
    session.rollback()
    assert session.get(MessageBatch, mail.id).status == BatchStatus.preview
    folder.status = BatchStatus.preview
    session.commit()
    select_ids(session, garden.id)
    assert mail.status == folder.status == BatchStatus.cancelled
    with pytest.raises(AppError, match="PREVIEW_STALE"):
        await api.create_folders_apply("c", session, REQUEST, batch_id=folder.id)


def test_removed_participants_folder_cannot_be_reused_later(catalog):
    session, campaign, school, garden = catalog
    select_ids(session, school.id)
    select_ids(session)
    garden.code = school.code
    session.commit()
    with pytest.raises(AppError, match="ORGANIZATION_FOLDER_CONFLICT"):
        select_ids(session, garden.id)
    session.rollback()
    select_ids(session, school.id)
    assert [item.organization_id for item in api._campaign_orgs(session, campaign)] == [school.id]


@pytest.mark.asyncio
async def test_stale_legacy_share_preview_is_rejected_before_remote_access(catalog):
    session, campaign, school, garden = catalog
    selected = select_ids(session, school.id)
    stale = create_batch(
        session, campaign_id="c", operation_type="create_shares", user_id=None,
        items=[{
            "action": "create_share", "target_path": "/root/Exam/obsolete",
            "metadata_json": {"campaign_org_ids": [selected["items"][0]["id"]], "share_scope": "organization"},
        }],
    )
    with pytest.raises(AppError, match="PREVIEW_STALE"):
        await api.create_shares_apply("c", session, REQUEST, batch_id=stale.id)
    assert stale.status == BatchStatus.preview
    assert session.exec(select(CampaignOrganization)).one().public_link is None
