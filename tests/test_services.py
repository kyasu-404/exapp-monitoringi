from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from monitoringi.models import Campaign, CampaignOrganization, CampaignOrganizationStatus, OperationItem, OperationItemStatus, Organization
from monitoringi.reports import link_rows, mailing_rows, render_template
from monitoringi.services import apply_batch, create_batch, parse_organization_table, planned_folder_items


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


def test_organization_type_aliases_support_model_enum_values():
    from monitoringi.models import OrganizationType
    from monitoringi.services import organization_type_aliases

    assert organization_type_aliases(OrganizationType.kindergarten) == ["\u0414\u041e\u0423"]
    assert organization_type_aliases(OrganizationType.odo) == ["\u041e\u0414\u041e"]


def test_import_organizations_adds_default_aliases_when_missing():
    content = "\u0422\u0438\u043f \u041e\u0423;\u041d\u043e\u043c\u0435\u0440;Email\n\u0421\u0430\u0434\u044b;222;garden@example.test\n".encode("cp1251")

    rows = parse_organization_table("orgs.csv", content)

    assert rows[0]["type"] == "kindergarten"
    assert rows[0]["aliases"] == ["\u0414\u041e\u0423"]


def test_generate_mail_template():
    text = render_template(
        "{organization_name}: {public_link} до {deadline}",
        {"organization_name": "ГБОУ 123", "public_link": "https://nc/s/abc", "deadline": "2026-07-05"},
    )

    assert text == "ГБОУ 123: https://nc/s/abc до 2026-07-05"


def test_planned_folder_items_single_type_creates_organization_code_folders_only():
    campaign = Campaign(
        id="campaign-123",
        title="123",
        date=date(2026, 7, 2),
        parent_nc_path="/Мониторинги",
        campaign_folder_path="/Мониторинги/123",
        selected_org_types=["school"],
    )
    organization = Organization(id="org-42", type="school", code="42", display_name="ГБОУ 42")
    campaign_org = CampaignOrganization(
        id="campaign-org-42",
        campaign_id=campaign.id,
        organization_id=organization.id,
        folder_path="/Мониторинги/123/ГБОУ 42",
    )

    items = planned_folder_items(campaign, [campaign_org], [organization])

    assert items == [
        {
            "action": "create_folder",
            "target_path": "/Мониторинги/123/42",
            "reason": "organization_folder",
            "metadata_json": {
                "campaign_org_id": "campaign-org-42",
                "organization_id": "org-42",
                "organization_code": "42",
                "organization_name": "ГБОУ 42",
                "organization_type": "school",
                "organization_type_folder": "Школы",
            },
        }
    ]


def test_planned_folder_items_multiple_types_creates_type_sections():
    campaign = Campaign(
        id="campaign-123",
        title="123",
        date=date(2026, 7, 2),
        parent_nc_path="/Мониторинги",
        campaign_folder_path="/Мониторинги/123",
        selected_org_types=["school", "kindergarten"],
    )
    organizations = [
        Organization(id="org-school", type="school", code="42", display_name="ГБОУ 42"),
        Organization(id="org-kindergarten", type="kindergarten", code="8", display_name="ГБДОУ 8"),
    ]
    campaign_orgs = [
        CampaignOrganization(
            id="campaign-org-school",
            campaign_id=campaign.id,
            organization_id="org-school",
            folder_path="/Мониторинги/123/42",
        ),
        CampaignOrganization(
            id="campaign-org-kindergarten",
            campaign_id=campaign.id,
            organization_id="org-kindergarten",
            folder_path="/Мониторинги/123/8",
        ),
    ]

    items = planned_folder_items(campaign, campaign_orgs, organizations)

    assert [item["target_path"] for item in items] == [
        "/Мониторинги/123/Школы",
        "/Мониторинги/123/Сады",
        "/Мониторинги/123/Школы/42",
        "/Мониторинги/123/Сады/8",
    ]
    assert [item["reason"] for item in items] == [
        "organization_type_folder",
        "organization_type_folder",
        "organization_folder",
        "organization_folder",
    ]


def test_planned_folder_items_rechecks_existing_webdav_folders():
    campaign = Campaign(
        id="campaign-777",
        title="777",
        date=date(2026, 7, 2),
        parent_nc_path="/Мониторинги",
        campaign_folder_path="/Мониторинги/777",
        selected_org_types=["school", "kindergarten"],
    )
    organizations = [
        Organization(id="org-school", type="school", code="42", display_name="ГБОУ 42"),
        Organization(id="org-kindergarten", type="kindergarten", code="8", display_name="ГБДОУ 8"),
    ]
    campaign_orgs = [
        CampaignOrganization(
            id="campaign-org-school",
            campaign_id=campaign.id,
            organization_id="org-school",
            folder_path="/Мониторинги/777/Школы/42",
            status=CampaignOrganizationStatus.folder_created,
        ),
        CampaignOrganization(
            id="campaign-org-kindergarten",
            campaign_id=campaign.id,
            organization_id="org-kindergarten",
            folder_path="/Мониторинги/777/Сады/8",
            status=CampaignOrganizationStatus.folder_created,
        ),
    ]

    items = planned_folder_items(
        campaign,
        campaign_orgs,
        organizations,
        existing_folder_paths={"/Мониторинги/777", "/Мониторинги/777/Школы", "/Мониторинги/777/Сады"},
    )

    assert [item["target_path"] for item in items] == [
        "/Мониторинги/777/Школы/42",
        "/Мониторинги/777/Сады/8",
    ]
    assert all(item["reason"] == "organization_folder" for item in items)


def test_link_and_mailing_rows_use_type_sections():
    campaign = Campaign(
        id="campaign-123",
        title="123",
        date=date(2026, 7, 2),
        parent_nc_path="/Мониторинги",
        campaign_folder_path="/Мониторинги/123",
        selected_org_types=["school", "kindergarten"],
    )
    organization = Organization(id="org-42", type="school", code="42", display_name="ГБОУ 42", email="school@example.test")
    campaign_org = CampaignOrganization(
        id="campaign-org-42",
        campaign_id=campaign.id,
        organization_id=organization.id,
        folder_path="/Мониторинги/123/Школы/42",
        public_link="https://nc/s/abc",
    )

    links = link_rows(campaign, [organization], [campaign_org])
    mailing = mailing_rows(campaign, [organization], [campaign_org])

    assert links[0]["Тип ОУ"] == "Школы"
    assert links[0]["Раздел ОУ"] == "Школы"
    assert links[0]["Папка Nextcloud"] == "/Мониторинги/123/Школы/42"
    assert mailing[0]["Тип организации"] == "Школы"
    assert mailing[0]["Раздел ОУ"] == "Школы"
    assert mailing[0]["Папка Nextcloud"] == "/Мониторинги/123/Школы/42"


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
