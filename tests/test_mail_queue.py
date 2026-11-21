import asyncio
from datetime import date, timedelta
from io import BytesIO

import httpx
import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.requests import Request

from monitoringi import api, mail_queue as queue, nextcloud
from monitoringi.models import (
    BatchStatus, Campaign, CampaignOrganization, CampaignOrganizationStatus, CheckResult,
    MailItemStatus, MessageBatch, MessageItem, Organization, utcnow,
)


def request(user):
    return Request({"type": "http", "username": user, "headers": [(b"remote-user", b"header-user")]})


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mail.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(queue, "get_engine", lambda: engine)
    with Session(engine) as session:
        campaign = Campaign(id="campaign", title="Files", date=date.today(),
                            parent_nc_path="/root", campaign_folder_path="/root/files")
        session.add(campaign)
        session.add(CheckResult(id="check", campaign_id="campaign"))
        for code in ("1", "2"):
            session.add(Organization(id=code, type="school", code=code, display_name=code, email=f"{code}@example.test"))
            session.add(CampaignOrganization(campaign_id="campaign", organization_id=code,
                                            folder_path=f"/root/files/{code}", public_link=f"https://nc.test/s/{code}",
                                            status=CampaignOrganizationStatus.missing))
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def bridge(monkeypatch):
    calls = []

    async def send(user, email):
        calls.append((user, email))

    class FakeClient:
        def __init__(self, credentials):
            self.user = credentials["nc_user"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_service_mail(self, *, to_email, subject, body):
            await send(self.user, to_email)

    monkeypatch.setattr(queue, "configured_credentials", lambda data: data)
    monkeypatch.setattr(queue, "NextcloudClient", FakeClient)
    return FakeClient, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "all_links"])
async def test_restart_recovers_authorized_queue_but_never_resends_claim(database, bridge, monkeypatch, kind):
    fake, calls = bridge
    with Session(database) as session:
        if kind == "all_links":
            result = await api.all_links_messages_prepare_send("campaign", session, request("sender"))
        else:
            prepared = api.messages_prepare("campaign", session, request("planner"))
            result = await api.messages_send(prepared["batch_id"], session, request("sender"))
        batch_id = result["batch_id"]
        assert result["summary"]["queued_by"] == "sender"
        # Retrying a send request must not change who authorized this batch.
        replay = await api.messages_send(batch_id, session, request("other"))
        assert replay["summary"]["queued_by"] == "sender"

    original_send = fake.send_service_mail

    async def interrupted(self, **kwargs):
        await original_send(self, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(fake, "send_service_mail", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await queue.process_mail_queue()

    database.dispose()
    restarted_engine = create_engine(database.url)
    monkeypatch.setattr(queue, "get_engine", lambda: restarted_engine)
    monkeypatch.setattr(fake, "send_service_mail", original_send)
    await queue.process_mail_queue()
    assert len(calls) == 1  # another process might still own the fresh claim
    with Session(restarted_engine) as session:
        interrupted_item = session.exec(select(MessageItem).where(MessageItem.status == MailItemStatus.sending)).one()
        interrupted_item.updated_at = utcnow() - queue.CLAIM_TIMEOUT - timedelta(seconds=1)
        session.commit()

    await queue.process_mail_queue()
    await queue.process_mail_queue()
    with Session(restarted_engine) as session:
        batch = session.get(MessageBatch, batch_id)
        assert batch.status == BatchStatus.completed_with_errors
        assert batch.finished_at is not None
        assert {k: batch.summary_json[k] for k in ("sent", "failed", "pending", "sending", "delivery_unknown")} == {
            "sent": 1, "failed": 0, "pending": 0, "sending": 0, "delivery_unknown": 1,
        }
        assert "Status: unknown" in api.message_batch_txt(batch_id, session).body.decode()
        sheet = load_workbook(BytesIO(api.message_batch_xlsx(batch_id, session).body)).active
        assert any(row[3] == "unknown" and row[4] for row in sheet.values)
        assert (await api.messages_send(batch_id, session, request("other")))["status"] == "completed_with_errors"
    restarted_engine.dispose()
    assert sorted(calls) == [("sender", "1@example.test"), ("sender", "2@example.test")]


@pytest.mark.asyncio
async def test_overlapping_workers_claim_recipient_once(database, bridge, monkeypatch):
    fake, calls = bridge
    started, release = asyncio.Event(), asyncio.Event()
    original_send = fake.send_service_mail

    async def slow_send(self, **kwargs):
        await original_send(self, **kwargs)
        started.set()
        await release.wait()

    monkeypatch.setattr(fake, "send_service_mail", slow_send)
    with Session(database) as session:
        result = await api.messages_prepare_send("campaign", session, request("sender"))
    worker = asyncio.create_task(queue.process_mail_queue())
    await asyncio.wait_for(started.wait(), timeout=3)
    await queue.process_mail_queue()
    assert len(calls) == 1
    with Session(database) as session:
        batch = session.get(MessageBatch, result["batch_id"])
        assert batch.status == BatchStatus.running and batch.finished_at is None
    release.set()
    await worker
    await queue.process_mail_queue()
    assert len(set(calls)) == len(calls) == 2
    with Session(database) as session:
        assert session.get(MessageBatch, result["batch_id"]).status == BatchStatus.completed


@pytest.mark.asyncio
async def test_legacy_pending_waits_for_authenticated_confirmation(database, bridge):
    _, calls = bridge
    with Session(database) as session:
        prepared = api.messages_prepare("campaign", session, request("planner"))
        batch = session.get(MessageBatch, prepared["batch_id"])
        batch.status = BatchStatus.running  # legacy: creator recorded but sender is unknown
        session.commit()
    await queue.process_mail_queue()
    with Session(database) as session:
        batch = session.get(MessageBatch, prepared["batch_id"])
        assert batch.status == BatchStatus.preview
        assert batch.summary_json["needs_confirmation"]
        with pytest.raises(HTTPException) as exc:
            await api.messages_send(batch.id, session, request(None))
        assert exc.value.status_code == 401
        assert not calls
        await api.messages_send(batch.id, session, request("confirmed"))
    await queue.process_mail_queue()
    assert calls[0][0] == "confirmed"


@pytest.mark.asyncio
async def test_only_definitely_unsent_transient_errors_are_retried(database, bridge, monkeypatch):
    fake, _ = bridge
    attempts = []

    async def unavailable(self, *, to_email, **kwargs):
        attempts.append(to_email)
        if to_email == "1@example.test":
            raise httpx.ConnectError("offline")
        raise httpx.ReadTimeout("unknown")

    monkeypatch.setattr(fake, "send_service_mail", unavailable)
    with Session(database) as session:
        result = await api.messages_prepare_send("campaign", session, request("sender"))
    for _ in range(6):
        await queue.process_mail_queue()
    assert attempts.count("1@example.test") == queue.MAX_CONNECT_ATTEMPTS
    assert attempts.count("2@example.test") == 1
    with Session(database) as session:
        batch = session.get(MessageBatch, result["batch_id"])
        assert batch.status == BatchStatus.completed_with_errors
        assert batch.summary_json["failed"] == 1
        assert batch.summary_json["delivery_unknown"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), [
    (httpx.Response(200, json={"status": "sent"}), None),
    (httpx.Response(403), "NEXTCLOUD_MAIL_SEND_FAILED"),
    (httpx.Response(200, text="<html>Login</html>"), "MAIL_DELIVERY_UNCONFIRMED"),
    (httpx.Response(502), "MAIL_DELIVERY_UNCONFIRMED"),
])
async def test_bridge_requires_delivery_confirmation(response, expected, monkeypatch):
    monkeypatch.setattr(nextcloud, "_mail_bridge_token", lambda: "test-token")
    client = object.__new__(nextcloud.NextcloudClient)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response), base_url="https://nc.test") as mock:
        client._client = mock
        if expected:
            with pytest.raises(nextcloud.NextcloudApiError) as exc:
                await client.send_service_mail(to_email="a@example.test", subject="Test", body="Test")
            assert exc.value.code == expected
        else:
            await client.send_service_mail(to_email="a@example.test", subject="Test", body="Test")
