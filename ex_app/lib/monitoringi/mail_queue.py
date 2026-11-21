"""Durable delivery of message batches through the Nextcloud mail bridge."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, update
from sqlmodel import Session, select

from .config import SETTINGS
from .db import get_engine
from .errors import AppError
from .models import BatchStatus, MailItemStatus, MessageBatch, MessageItem, utcnow
from .nextcloud import NextcloudClient, configured_credentials
from .services import audit, nextcloud_overrides

logger = logging.getLogger(__name__)
MAX_CONNECT_ATTEMPTS = 3
DELIVERY_TIMEOUT = max(1.0, 2 * SETTINGS.request_timeout)
# Allow the bounded HTTP attempt to finish and its worker to record the result.
CLAIM_TIMEOUT = timedelta(seconds=DELIVERY_TIMEOUT + 30)
UNKNOWN_DELIVERY = "Почтовый мост не подтвердил результат. Проверьте доставку вручную."


@dataclass(frozen=True)
class Delivery:
    batch_id: str
    item_id: str
    user_id: str
    to_email: str
    subject: str
    body: str
    attempt: int


def _lock_batch(session: Session, batch_id: str, status: BatchStatus) -> MessageBatch | None:
    # The conditional write serializes claims on SQLite as well as row-locking
    # databases. No transaction stays open while waiting for the mail bridge.
    locked = session.execute(
        update(MessageBatch)
        .where(MessageBatch.id == batch_id, MessageBatch.status == status)
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    if not locked.rowcount:
        return None
    return session.get(MessageBatch, batch_id, populate_existing=True)


def _update_summary(session: Session, batch: MessageBatch) -> None:
    session.flush()
    counts = dict(session.exec(
        select(MessageItem.status, func.count())
        .where(MessageItem.batch_id == batch.id)
        .group_by(MessageItem.status)
    ).all())
    batch.summary_json = {
        **(batch.summary_json or {}),
        "planned": sum(counts.values()),
        "pending": counts.get(MailItemStatus.pending, 0),
        "sending": counts.get(MailItemStatus.sending, 0),
        "sent": counts.get(MailItemStatus.sent, 0),
        "failed": counts.get(MailItemStatus.failed, 0) + counts.get(MailItemStatus.bounced, 0),
        "delivery_unknown": counts.get(MailItemStatus.unknown, 0),
    }
    if batch.summary_json["pending"] or batch.summary_json["sending"]:
        return
    issues = batch.summary_json["failed"] or batch.summary_json["delivery_unknown"]
    batch.status = BatchStatus.completed_with_errors if issues else BatchStatus.completed
    batch.finished_at = utcnow()
    audit(session, user_id=batch.summary_json.get("queued_by"), action="mail_batch_send",
          entity_type="message_batch", entity_id=batch.id, after=batch.summary_json)


def queue_batch(session: Session, batch: MessageBatch, user_id: str) -> None:
    locked = _lock_batch(session, batch.id, BatchStatus.preview)
    if locked is None:
        session.refresh(batch)
        if batch.status == BatchStatus.cancelled:
            raise AppError("MESSAGE_BATCH_CANCELLED", "Пакет писем отменён. Сформируйте новый пакет.", 409)
        return
    locked.status = BatchStatus.running
    locked.queued_at = utcnow()
    locked.summary_json = {**(locked.summary_json or {}), "queued_by": user_id, "needs_confirmation": False}
    _update_summary(session, locked)


def _claim_delivery(batch_id: str) -> Delivery | None:
    with Session(get_engine()) as session:
        batch = _lock_batch(session, batch_id, BatchStatus.running)
        if batch is None:
            return None

        now = utcnow()
        # A worker may have died between committing the claim and recording
        # the bridge's response. Such a recipient must never be sent again.
        session.execute(
            update(MessageItem)
            .where(MessageItem.batch_id == batch_id, MessageItem.status == MailItemStatus.sending,
                   MessageItem.updated_at <= now - CLAIM_TIMEOUT)
            .values(status=MailItemStatus.unknown, last_error=UNKNOWN_DELIVERY, updated_at=now)
        )
        active = session.exec(select(MessageItem.id).where(
            MessageItem.batch_id == batch_id, MessageItem.status == MailItemStatus.sending
        ).limit(1)).first()
        if active:
            session.commit()
            return None

        user_id = (batch.summary_json or {}).get("queued_by")
        item = session.exec(select(MessageItem).where(
            MessageItem.batch_id == batch_id, MessageItem.status == MailItemStatus.pending
        ).order_by(MessageItem.created_at, MessageItem.id).limit(1)).first()
        delivery = None
        if item and user_id:
            item.status = MailItemStatus.sending
            item.attempts += 1
            item.updated_at = now
            delivery = Delivery(batch_id, item.id, user_id, item.to_email, item.subject, item.body, item.attempts)
        elif item:
            # Old versions saved the preview creator only. A different user
            # could have queued it, so require a fresh authenticated send.
            batch.status = BatchStatus.preview
            batch.summary_json = {**(batch.summary_json or {}), "needs_confirmation": True}
        _update_summary(session, batch)
        session.commit()
        return delivery


async def _deliver(delivery: Delivery) -> tuple[MailItemStatus, str | None]:
    try:
        with Session(get_engine()) as session:
            credentials = configured_credentials(nextcloud_overrides(session, delivery.user_id))
        async with asyncio.timeout(DELIVERY_TIMEOUT), NextcloudClient(credentials) as client:
            await client.send_service_mail(to_email=delivery.to_email, subject=delivery.subject, body=delivery.body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        # No connection was acquired, so retrying cannot duplicate delivery.
        status = MailItemStatus.pending if delivery.attempt < MAX_CONNECT_ATTEMPTS else MailItemStatus.failed
        return status, "Не удалось установить соединение с почтовым мостом."
    except AppError as exc:
        if exc.code == "MAIL_DELIVERY_UNCONFIRMED":
            return MailItemStatus.unknown, UNKNOWN_DELIVERY
        return MailItemStatus.failed, str(exc)
    except (TimeoutError, httpx.TransportError):
        return MailItemStatus.unknown, UNKNOWN_DELIVERY
    return MailItemStatus.sent, None


async def _process_batch(batch_id: str) -> None:
    delivery = _claim_delivery(batch_id)
    if delivery is None:
        return
    status, error = await _deliver(delivery)
    with Session(get_engine()) as session:
        batch = _lock_batch(session, batch_id, BatchStatus.running)
        if not batch:
            return
        item = session.get(MessageItem, delivery.item_id)
        if item is None or item.status != MailItemStatus.sending or item.attempts != delivery.attempt:
            return
        item.status = status
        item.last_error = error
        item.updated_at = utcnow()
        if status == MailItemStatus.sent:
            item.sent_at = item.updated_at
        elif status in {MailItemStatus.failed, MailItemStatus.unknown}:
            audit(session, user_id=delivery.user_id, action="mail_send_" + status.value,
                  entity_type="message_item", entity_id=item.id, after={"batch_id": batch_id},
                  result=status.value, error=error)
        _update_summary(session, batch)
        session.commit()


async def process_mail_queue() -> None:
    with Session(get_engine()) as session:
        batch_ids = session.exec(select(MessageBatch.id).where(
            MessageBatch.status == BatchStatus.running
        ).order_by(MessageBatch.queued_at)).all()
    for batch_id in batch_ids:
        try:
            await _process_batch(batch_id)
        except Exception:
            logger.exception("Could not process queued mail batch %s", batch_id)


async def run_mail_queue() -> None:
    while True:
        try:
            await process_mail_queue()
        except Exception:
            logger.exception("Could not read the mail queue")
        await asyncio.sleep(1)
