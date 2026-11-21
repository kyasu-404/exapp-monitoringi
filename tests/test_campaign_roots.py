import asyncio
from datetime import date
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from monitoringi import api, db
from monitoringi.errors import AppError
from monitoringi.models import Campaign, CampaignOrganization, OperationBatch, SettingsEntry
from monitoringi.nextcloud import WebDAVEntry
from monitoringi.schemas import BulkPathsIn, CampaignIn, CampaignMoveIn, CampaignPatch

ROOT = "/Monitoringi"
REQUEST = SimpleNamespace(scope={"username": "tester"}, headers={})


class DAV:
    def __init__(self):
        self.credentials = SimpleNamespace(root_path=ROOT)
        self.entries = {}
        self.add(ROOT, "root")
        self.moves = []
        self.deleted = []

    def add(self, path, file_id, *, is_collection=True):
        self.entries[path] = WebDAVEntry(path, PurePosixPath(path).name, is_collection, None, None, None, file_id=file_id)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def propfind(self, path, *, depth):
        await asyncio.sleep(0)
        if path not in self.entries:
            raise AppError("WEBDAV_PATH_NOT_FOUND", "Missing", 404)
        children = [item for item in self.entries.values() if str(PurePosixPath(item.path).parent) == path]
        return [self.entries[path], *(children if depth else [])]

    async def mkcol(self, path, *, exclusive=False):
        await asyncio.sleep(0)
        if path in self.entries:
            if exclusive:
                raise AppError("CAMPAIGN_TARGET_EXISTS", "Already exists", 409)
        else:
            self.add(path, str(len(self.entries)))

    async def move(self, source, target):
        assert target not in self.entries
        self.add(target, self.entries.pop(source).file_id)
        self.moves.append((source, target))

    async def delete_public_share(self, share_id):
        self.deleted.append(share_id)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    SQLModel.metadata.create_all(engine)
    dav = DAV()
    monkeypatch.setattr(api, "_nc_credentials", lambda *_: None)
    monkeypatch.setattr(api, "NextcloudClient", lambda _: dav)
    with Session(engine) as session:
        session.add(SettingsEntry(key="root_path", value_json=ROOT))
        session.commit()
    return engine, dav


def stored(session, leaf, *, folder_id=None, title=None, conflict=False):
    record = Campaign(title=title or leaf, date=date(2026, 1, 1), parent_nc_path=ROOT,
                      campaign_folder_path=f"{ROOT}/{leaf}", folder_file_id=folder_id, folder_conflict=conflict)
    session.add(record)
    session.commit()
    return record


@pytest.mark.asyncio
async def test_concurrent_creates_same_sanitized_title_get_independent_roots(env):
    engine, dav = env

    async def create(title):
        with Session(engine) as session:
            return await api.create_campaign(CampaignIn(title=title, parent_nc_path=ROOT), session, REQUEST)

    first, second = await asyncio.gather(create("№Review"), create("Review"))
    for key in ("campaign_folder_path", "folder_file_id"):
        assert first[key] != second[key]
    assert {first["campaign_folder_path"], second["campaign_folder_path"]} == set(dav.entries) - {ROOT}
    suffixed = next(row for row in [first, second] if row["campaign_folder_path"] != f"{ROOT}/Review")
    with Session(engine) as session:
        result = await api.update_campaign(suffixed["id"], CampaignPatch(title=suffixed["title"], date=date(2026, 2, 2)), session, REQUEST)
        assert result["campaign_folder_path"] == suffixed["campaign_folder_path"]
        result = await api.move_campaign(suffixed["id"], CampaignMoveIn(target_nc_path=f"{ROOT}/New"), session, REQUEST)
        assert result["campaign_folder_path"] == f"{ROOT}/New/{PurePosixPath(suffixed['campaign_folder_path']).name}"


@pytest.mark.asyncio
async def test_existing_unrelated_remote_folder_or_file_is_not_adopted(env):
    engine, dav = env
    for leaf, is_folder in (("Old", True), ("Document", False)):
        path = f"{ROOT}/{leaf}"
        dav.add(path, leaf, is_collection=is_folder)
        with Session(engine) as session:
            new = await api.create_campaign(CampaignIn(title=leaf, parent_nc_path=ROOT), session, REQUEST)
            assert new["campaign_folder_path"] != path
            assert new["folder_file_id"] != leaf
            assert dav.entries[path].file_id == leaf


@pytest.mark.asyncio
async def test_move_rename_and_archive_refuse_a_claimed_but_remotely_missing_target(env):
    engine, dav = env
    with Session(engine) as session:
        old = stored(session, "Original", folder_id="11")
        dav.add(old.campaign_folder_path, "11")
        stored(session, "Taken", folder_id="22")
        stored(session, "_Архив/Original", folder_id="33")
        org = CampaignOrganization(campaign_id=old.id, organization_id="x", folder_path=old.campaign_folder_path,
                                   share_id="55", public_link="https://example.test/s/old")
        session.add(org)
        session.commit()
        with pytest.raises(HTTPException) as rename:
            await api.update_campaign(old.id, CampaignPatch(title="Taken"), session, REQUEST)
        assert rename.value.status_code == 409
        with pytest.raises(HTTPException) as move:
            await api.move_campaign(old.id, CampaignMoveIn(target_nc_path=f"{ROOT}/Taken"), session, REQUEST)
        assert move.value.status_code == 409
        with pytest.raises(HTTPException) as archive:
            await api.archive_campaign(old.id, session, REQUEST)
        assert archive.value.status_code == 409
        assert dav.moves == dav.deleted == []
        assert old.campaign_folder_path == f"{ROOT}/Original"


@pytest.mark.asyncio
async def test_auto_relink_requires_matching_file_id_and_unclaimed_path(env):
    engine, dav = env
    with Session(engine) as session:
        campaign = stored(session, "Original", folder_id="123")
        dav.add(f"{ROOT}/Sub", "222")
        dav.add(f"{ROOT}/Sub/Original", "wrong")
        await api._prune_missing_campaigns(session, REQUEST, [campaign])
        assert campaign.campaign_folder_path == f"{ROOT}/Original"
        with pytest.raises(HTTPException) as err:
            await api.move_campaign(campaign.id, CampaignMoveIn(target_nc_path=f"{ROOT}/Sub"), session, REQUEST)
        assert err.value.status_code == 409
        dav.add(f"{ROOT}/Taken", "123")
        stored(session, "Taken")
        await api._prune_missing_campaigns(session, REQUEST, [campaign])
        assert campaign.campaign_folder_path == f"{ROOT}/Original"
        dav.entries.pop(f"{ROOT}/Taken")
        dav.add(f"{ROOT}/Sub/Renamed", "123")
        await api._prune_missing_campaigns(session, REQUEST, [campaign])
        assert campaign.campaign_folder_path == f"{ROOT}/Sub/Renamed"


@pytest.mark.asyncio
async def test_remote_identity_checked_before_operations(env):
    engine, dav = env
    with Session(engine) as session:
        record = stored(session, "Report", folder_id="original-id")
        dav.add(record.campaign_folder_path, "replacement-id")
        with pytest.raises(AppError) as error:
            await api.create_folders_preview(record.id, session, REQUEST)
        assert error.value.status_code == 409
        assert dav.moves == []


@pytest.mark.asyncio
async def test_upgrade_preserves_conflicts_and_can_archive_only_the_record(env, monkeypatch):
    engine, dav = env
    with Session(engine) as session:
        for ddl in ("DROP INDEX uq_campaign_root_path", "DROP INDEX uq_campaign_root_file_id",
                    "ALTER TABLE campaign DROP COLUMN participant_selection_explicit",
                    "ALTER TABLE campaign DROP COLUMN folder_conflict", "ALTER TABLE campaign DROP COLUMN folder_file_id"):
            session.exec(text(ddl))
        for row_id, leaf in (("first", "Shared"), ("second", "Shared"), ("unique", "Valid")):
            session.exec(text("""
                INSERT INTO campaign (id, title, date, parent_nc_path, campaign_folder_path, upload_mode,
                    org_folder_layout, status, created_at, updated_at)
                VALUES (:id, :leaf, CURRENT_DATE, '/Monitoringi', :path, 'organization', 'auto', 'draft', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """).bindparams(id=row_id, leaf=leaf, path=f"{ROOT}/{leaf}"))
        shared_path = f"{ROOT}/Shared"
        session.add(CampaignOrganization(campaign_id="first", organization_id="org", folder_path=shared_path, share_id="old"))
        batch = OperationBatch(campaign_id="first", operation_type="rename")
        session.add(batch)
        session.commit()
        batch_id = batch.id
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    db.init_db()
    db.init_db()
    with Session(engine) as session:
        assert [(row.id, row.folder_conflict) for row in session.exec(select(Campaign).order_by(Campaign.id))] == [
            ("first", True), ("second", True), ("unique", False)]
        with pytest.raises(HTTPException) as error:
            await api.bulk_rename_apply(BulkPathsIn(batch_id=batch_id), session, REQUEST)
        assert error.value.status_code == 409
        response = await api.archive_campaign_to_configured_folder("first", session, REQUEST)
        assert response["archived_record_only"] and response["status"] == "archived"
        assert session.get(Campaign, "second").campaign_folder_path == shared_path
        assert session.exec(select(CampaignOrganization)).one().share_id == "old"
        assert dav.moves == dav.deleted == []
        with pytest.raises(IntegrityError):
            stored(session, "Valid")
