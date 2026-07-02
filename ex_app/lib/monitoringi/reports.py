"""CSV, XLSX and message generation."""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from io import BytesIO, StringIO
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .algorithms import organization_sort_key
from .models import Campaign, CampaignOrganization, FileRecord, Organization

ORGANIZATION_TYPE_LABELS = {
    "school": "Школы",
    "kindergarten": "Сады",
    "odo": "ОДО",
    "udod": "УДОДы",
}
ORGANIZATION_TYPE_ORDER = ("school", "kindergarten", "odo", "udod", "other")

MAILING_COLUMNS = [
    "Тип организации",
    "Раздел ОУ",
    "Код",
    "Название организации",
    "Email",
    "Дополнительные email",
    "Папка Nextcloud",
    "Публичная ссылка",
    "Срок сдачи",
    "Тема письма",
    "Текст письма",
    "Статус",
    "Ошибка",
]

LINK_COLUMNS = [
    "Тип ОУ",
    "Раздел ОУ",
    "Номер",
    "Email",
    "Папка Nextcloud",
    "Ссылка для загрузки",
]


def render_template(template: str, values: dict[str, str | None]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value or "")
    return result


def _organization_type_value(organization: Organization) -> str:
    return organization.type.value if hasattr(organization.type, "value") else str(organization.type)


def _organization_type_label(value: str) -> str:
    return ORGANIZATION_TYPE_LABELS.get(value, value)


def _parse_deadline(value: date | datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59))
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        return datetime.combine(datetime.fromisoformat(text).date(), time(23, 59))
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)


def _deadline_text(value: date | datetime | str | None) -> str:
    parsed = _parse_deadline(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else ""


def _campaign_org_sort_key(item: CampaignOrganization, org_by_id: dict[str, Organization]) -> tuple[int, str, tuple]:
    org = org_by_id.get(item.organization_id)
    if org is None:
        return (len(ORGANIZATION_TYPE_ORDER), "", (1, ""))
    org_type = _organization_type_value(org)
    type_order = ORGANIZATION_TYPE_ORDER.index(org_type) if org_type in ORGANIZATION_TYPE_ORDER else len(ORGANIZATION_TYPE_ORDER)
    return (type_order, _organization_type_label(org_type), organization_sort_key(org))


def mailing_rows(
    campaign: Campaign,
    organizations: list[Organization],
    campaign_orgs: list[CampaignOrganization],
) -> list[dict[str, str]]:
    org_by_id = {org.id: org for org in organizations}
    rows: list[dict[str, str]] = []
    for item in sorted(campaign_orgs, key=lambda row: _campaign_org_sort_key(row, org_by_id)):
        org = org_by_id.get(item.organization_id)
        if org is None:
            continue
        org_type = _organization_type_value(org)
        org_type_label = _organization_type_label(org_type)
        rows.append(
            {
                "Тип организации": org_type_label,
                "Раздел ОУ": org_type_label,
                "Код": org.code,
                "Название организации": org.display_name,
                "Email": org.email or "",
                "Дополнительные email": ", ".join(org.extra_emails or []),
                "Папка Nextcloud": item.folder_path,
                "Публичная ссылка": item.public_link or "",
                "Срок сдачи": _deadline_text(campaign.deadline),
                "Тема письма": item.mail_subject or "",
                "Текст письма": item.mail_body or "",
                "Статус": item.status.value if hasattr(item.status, "value") else str(item.status),
                "Ошибка": "",
            }
        )
    return rows


def link_rows(
    campaign: Campaign,
    organizations: list[Organization],
    campaign_orgs: list[CampaignOrganization],
) -> list[dict[str, str]]:
    org_by_id = {org.id: org for org in organizations}
    rows: list[dict[str, str]] = []
    for item in sorted(campaign_orgs, key=lambda row: _campaign_org_sort_key(row, org_by_id)):
        org = org_by_id.get(item.organization_id)
        if org is None:
            continue
        org_type = _organization_type_value(org)
        org_type_label = _organization_type_label(org_type)
        rows.append(
            {
                "Тип ОУ": org_type_label,
                "Раздел ОУ": org_type_label,
                "Номер": org.code,
                "Email": org.email or "",
                "Папка Nextcloud": item.folder_path,
                "Ссылка для загрузки": item.public_link or "",
            }
        )
    return rows


def csv_bytes(rows: Iterable[dict[str, str]], columns: list[str]) -> bytes:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def xlsx_bytes(rows: Iterable[dict[str, str]], columns: list[str], *, sheet_name: str = "Данные") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(columns)
    fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
    for row in rows:
        ws.append([row.get(column, "") for column in columns])
    for column_cells in ws.columns:
        width = min(max(len(str(cell.value or "")) for cell in column_cells) + 2, 60)
        ws.column_dimensions[column_cells[0].column_letter].width = width
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def report_workbook(campaign: Campaign, files: list[FileRecord], missing: list[Organization]) -> bytes:
    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    counts = {}
    for item in files:
        key = item.classification.value if hasattr(item.classification, "value") else str(item.classification)
        counts[key] = counts.get(key, 0) + 1
    summary.append(["Показатель", "Значение"])
    summary.append(["Мониторинг", campaign.title])
    summary.append(["Всего файлов", len(files)])
    summary.append(["Корректных файлов", counts.get("valid", 0)])
    summary.append(["Дублей", counts.get("duplicate", 0)])
    summary.append(["Неподходящих файлов", counts.get("invalid_extension", 0) + counts.get("invalid_name", 0)])
    summary.append(["Неизвестных организаций", counts.get("unknown", 0)])
    summary.append(["Организаций без файла", len(missing)])

    _append_files_sheet(wb, "Корректные файлы", [item for item in files if str(item.classification) == "FileClassification.valid" or item.classification == "valid"])
    missing_sheet = wb.create_sheet("Не предоставили")
    missing_sheet.append(["Тип", "Код", "Организация", "Email"])
    for org in sorted(missing, key=organization_sort_key):
        missing_sheet.append([_organization_type_label(_organization_type_value(org)), org.code, org.display_name, org.email or ""])
    _append_files_sheet(wb, "Дубли", [item for item in files if str(item.classification).endswith("duplicate") or item.classification == "duplicate"])
    _append_files_sheet(
        wb,
        "Неподходящие файлы",
        [
            item
            for item in files
            if item.classification in {"invalid_extension", "invalid_name", "unknown"} or str(item.classification).split(".")[-1] in {"invalid_extension", "invalid_name", "unknown"}
        ],
    )
    errors = wb.create_sheet("Ошибки")
    errors.append(["Файл", "Причина"])
    for item in files:
        if item.reason:
            errors.append([item.original_name, item.reason])

    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
        for column_cells in ws.columns:
            ws.column_dimensions[column_cells[0].column_letter].width = min(
                max(len(str(cell.value or "")) for cell in column_cells) + 2,
                70,
            )

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _append_files_sheet(wb: Workbook, title: str, files: list[FileRecord]) -> None:
    ws = wb.create_sheet(title)
    ws.append(["Исходный путь", "Текущий путь", "Имя", "Расширение", "Размер", "Классификация", "Причина"])
    for item in files:
        classification = item.classification.value if hasattr(item.classification, "value") else str(item.classification)
        ws.append([item.original_path, item.current_path, item.current_name, item.extension, item.size or "", classification, item.reason or ""])


def missing_message(campaign: Campaign, missing_by_type: dict[str, list[Organization]], template: str | None = None) -> str:
    template = template or """Уважаемые коллеги!

По запросу "{campaign_title}" от {campaign_date} файлы до сих пор не предоставили следующие организации:

Школы: {missing_schools}
Сады: {missing_kindergartens}
ОДО: {missing_odo}
УДОДы: {missing_udod}

Если ваша организация в этом списке, но вы уверены, что файл загружали по ссылке, проверьте корректность имени файла и формат документа."""
    values = {
        "campaign_title": campaign.title,
        "campaign_date": campaign.date.isoformat(),
        "missing_schools": ", ".join(org.display_name for org in sorted(missing_by_type.get("school", []), key=organization_sort_key)),
        "missing_kindergartens": ", ".join(org.display_name for org in sorted(missing_by_type.get("kindergarten", []), key=organization_sort_key)),
        "missing_odo": ", ".join(org.display_name for org in sorted(missing_by_type.get("odo", []), key=organization_sort_key)),
        "missing_udod": ", ".join(org.display_name for org in sorted(missing_by_type.get("udod", []), key=organization_sort_key)),
    }
    return render_template(template, values)
