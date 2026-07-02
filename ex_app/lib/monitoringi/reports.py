"""CSV, XLSX and message generation."""

from __future__ import annotations

import csv
from io import BytesIO, StringIO
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .algorithms import organization_sort_key
from .models import Campaign, CampaignOrganization, FileRecord, Organization


MAILING_COLUMNS = [
    "Тип организации",
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


def mailing_rows(
    campaign: Campaign,
    organizations: list[Organization],
    campaign_orgs: list[CampaignOrganization],
) -> list[dict[str, str]]:
    org_by_id = {org.id: org for org in organizations}
    rows: list[dict[str, str]] = []
    for item in campaign_orgs:
        org = org_by_id.get(item.organization_id)
        if org is None:
            continue
        rows.append(
            {
                "Тип организации": org.type.value if hasattr(org.type, "value") else str(org.type),
                "Код": org.code,
                "Название организации": org.display_name,
                "Email": org.email or "",
                "Дополнительные email": ", ".join(org.extra_emails or []),
                "Папка Nextcloud": item.folder_path,
                "Публичная ссылка": item.public_link or "",
                "Срок сдачи": campaign.deadline.isoformat() if campaign.deadline else "",
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
    for item in campaign_orgs:
        org = org_by_id.get(item.organization_id)
        if org is None:
            continue
        rows.append(
            {
                "Тип ОУ": org.type.value if hasattr(org.type, "value") else str(org.type),
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
        missing_sheet.append([org.type.value, org.code, org.display_name, org.email or ""])
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
