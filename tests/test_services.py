import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from monitoringi.models import OperationItem, OperationItemStatus
from monitoringi.reports import render_template
from monitoringi.services import apply_batch, create_batch, parse_organization_table


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_import_organizations_csv_cp1251():
    content = "Тип;Префикс;Код;Название;Почта;Алиасы\nschool;ГБОУ;123;ГБОУ 123;school@example.test;ГБОУ123\n".encode(
        "cp1251"
    )

    rows = parse_organization_table("orgs.csv", content)

    assert rows == [
        {
            "type": "school",
            "prefix": "ГБОУ",
            "code": "123",
            "display_name": "ГБОУ 123",
            "short_name": None,
            "email": "school@example.test",
            "extra_emails": [],
            "aliases": ["ГБОУ123"],
            "active": True,
            "notes": None,
        }
    ]


def test_generate_mail_template():
    text = render_template(
        "{organization_name}: {public_link} до {deadline}",
        {"organization_name": "ГБОУ 123", "public_link": "https://nc/s/abc", "deadline": "2026-07-05"},
    )

    assert text == "ГБОУ 123: https://nc/s/abc до 2026-07-05"


@pytest.mark.asyncio
async def test_apply_executes_planned_operations():
    session = make_session()
    batch = create_batch(
        session,
        operation_type="rename",
        user_id="tester",
        items=[
            {
                "action": "rename",
                "source_path": "/Мониторинги/ГБОУ123.pdf",
                "target_path": "/Мониторинги/ГБОУ 123.pdf",
                "reason": "normalized_filename",
            }
        ],
        summary={"planned": 1},
    )

    class FakeClient:
        def __init__(self):
            self.moves = []

        async def move(self, source_path, target_path):
            self.moves.append((source_path, target_path))

    client = FakeClient()
    applied = await apply_batch(session, batch, client)
    item = session.exec(select(OperationItem)).first()

    assert applied.status == "completed"
    assert client.moves == [("/Мониторинги/ГБОУ123.pdf", "/Мониторинги/ГБОУ 123.pdf")]
    assert item.status == OperationItemStatus.success
