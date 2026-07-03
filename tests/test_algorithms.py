from datetime import datetime, timedelta

from monitoringi.algorithms import FileCandidate, classify_files, extract_organization, normalize_filename, org_code_sort_key


ORGS = [
    {
        "id": "school-1",
        "type": "school",
        "prefix": "ГБОУ",
        "code": "1",
        "display_name": "ГБОУ 1",
        "aliases": ["ГБОУ №1"],
        "active": True,
    },
    {
        "id": "school-123",
        "type": "school",
        "prefix": "ГБОУ",
        "code": "123",
        "display_name": "ГБОУ 123",
        "aliases": ["ГБОУ123"],
        "active": True,
    },
    {
        "id": "kindergarten-baby",
        "type": "kindergarten",
        "prefix": "ДОУ",
        "code": "Малыш",
        "display_name": "ДОУ Малыш",
        "aliases": [],
        "active": True,
    },
    {
        "id": "udod-imc",
        "type": "udod",
        "prefix": None,
        "code": "ИМЦ",
        "display_name": "ИМЦ",
        "aliases": ["Информационно-методический центр"],
        "active": True,
    },
]


def test_clean_filename_examples_from_spec_and_old_script():
    assert normalize_filename("ГБОУ123.pdf").normalized_name == "ГБОУ 123.pdf"
    assert normalize_filename("ГБОУ №123 .pdf").normalized_name == "ГБОУ 123.pdf"
    assert normalize_filename("ДОУ45 (1).DOCX").normalized_name == "ДОУ 45.DOCX"
    assert normalize_filename("ОДО7..xlsx").normalized_name == "ОДО 7.xlsx"


def test_extract_organization_uses_exact_numeric_boundary():
    match = extract_organization("ГБОУ 123.pdf", ORGS)

    assert match.status == "matched"
    assert match.organization_id == "school-123"
    assert "school-1" not in match.matches


def test_extract_organization_supports_udod_without_hardcoded_list():
    match = extract_organization("ИМЦ сведения.pdf", ORGS)

    assert match.status == "matched"
    assert match.organization_id == "udod-imc"


def test_generic_type_alias_matches_with_code_without_ambiguity_and_normalizes_name():
    orgs = [
        {"id": "school-1", "type": "school", "prefix": None, "code": "1", "display_name": "1", "aliases": [], "active": True},
        {"id": "school-2", "type": "school", "prefix": None, "code": "2", "display_name": "2", "aliases": [], "active": True},
    ]

    match = extract_organization("ГБОУ1.xlsx", orgs)

    assert match.status == "matched"
    assert match.organization_id == "school-1"
    assert match.normalized_name == "ГБОУ 1.xlsx"


def test_udod_normalization_preserves_matching_alias():
    orgs = [
        {"id": "udod-444", "type": "udod", "prefix": None, "code": "444", "display_name": "444", "aliases": [], "active": True},
    ]

    match = extract_organization("ИМЦ444.pdf", orgs)

    assert match.status == "matched"
    assert match.organization_id == "udod-444"
    assert match.normalized_name == "ИМЦ 444.pdf"


def test_org_code_sort_keeps_special_names_after_numbers():
    values = ["Морская", "12", "1", "Малыш", "2"]

    assert sorted(values, key=org_code_sort_key) == ["1", "2", "12", "Малыш", "Морская"]


def test_duplicate_detection_keeps_latest_file():
    now = datetime.now()
    files = [
        FileCandidate("/Мониторинги/ГБОУ123.pdf", "ГБОУ123.pdf", size=20, mtime=now - timedelta(days=1)),
        FileCandidate("/Мониторинги/ГБОУ 123 (1).pdf", "ГБОУ 123 (1).pdf", size=20, mtime=now),
    ]

    result = classify_files(files, ORGS, ["pdf"])
    by_path = {item.candidate.path: item for item in result}

    assert by_path["/Мониторинги/ГБОУ123.pdf"].classification == "duplicate"
    assert by_path["/Мониторинги/ГБОУ 123 (1).pdf"].classification == "valid"


def test_invalid_extension_is_rejected_before_matching():
    result = classify_files([FileCandidate("/Мониторинги/ГБОУ 123.exe", "ГБОУ 123.exe", size=20)], ORGS, ["pdf"])

    assert result[0].classification == "invalid_extension"
    assert result[0].reason == "invalid_extension"

