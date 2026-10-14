import pytest
from typing import Dict, Any

from backend.services.user_data_extractor import (
    parse_optional_bool,
    extract_employment_history,
    extract_employment_salary,
    normalize_employment_verification,
)
from backend.services.user_data_service import _get_employment_db_fallback
from backend.services.user_data_formatter import format_single_intent
from backend.services.user_data_intent_parser import UserIntent


# 1. Boolean parser tests
def test_parse_optional_bool_valid_and_invalid():
    assert parse_optional_bool(1) is True
    assert parse_optional_bool(0) is False
    assert parse_optional_bool("1") is True
    assert parse_optional_bool("0") is False
    assert parse_optional_bool(True) is True
    assert parse_optional_bool(False) is False
    assert parse_optional_bool(None) is None
    assert parse_optional_bool("") is None
    assert parse_optional_bool("true") is True
    assert parse_optional_bool("false") is False
    assert parse_optional_bool("yes") is True
    assert parse_optional_bool("no") is False
    assert parse_optional_bool("pending") is None
    assert parse_optional_bool(99) is None
    assert parse_optional_bool(-1) is None


# 2. Employment metadata tests
def test_employment_metadata_flags_tri_state():
    raw_api = {
        "data": [
            {
                "company": "Tech Corp",
                "lists": [
                    {
                        "designation": "Developer",
                        "haveSalary": "1",
                        "haveDocument": 0,
                        "haveReview": None,
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    role = extracted[0]["roles"][0]
    assert role["have_salary"] is True
    assert role["have_document"] is False
    assert role["have_review"] is None


def test_company_and_role_claim_status_separation():
    raw_api = {
        "data": [
            {
                "company": "Acme Inc",
                "claim_status": "company_claimed",
                "lists": [
                    {
                        "designation": "Engineer",
                        "claim_status": "role_pending",
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    company = extracted[0]
    role = company["roles"][0]
    assert company["company_claim_status"] == "company_claimed"
    assert role["claim_status"] == "role_pending"
    assert "claim_status" not in company
    assert "company_claim_status" not in role


def test_department_string_preserved():
    raw_api = {
        "data": [
            {
                "company": "Corp A",
                "lists": [{"designation": "Manager", "department": "Engineering"}]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    assert extracted[0]["roles"][0]["department"] == "Engineering"


def test_department_dictionary_preserved():
    dept_obj = {"id": 10, "name": "Research & Development"}
    raw_api = {
        "data": [
            {
                "company": "Corp B",
                "lists": [{"designation": "Scientist", "department": dept_obj}]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    assert extracted[0]["roles"][0]["department"] == dept_obj


def test_employment_status_mapping():
    raw_api = {
        "data": [
            {
                "company": "Corp C",
                "lists": [{"designation": "Lead", "employment_status": "active"}]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    assert extracted[0]["roles"][0]["employment_status"] == "active"


# 3. Documents and sensitivity tests
def test_role_document_mapping_preserves_source():
    doc_data = {"id": 101, "url": "https://example.com/doc.pdf"}
    raw_api = {
        "data": [
            {
                "company": "DocCorp",
                "lists": [{"designation": "Analyst", "document": doc_data}]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    assert extracted[0]["roles"][0]["document"] == doc_data


def test_verification_process_documents_remain_separate():
    verif_proc = {
        "level1": "1",
        "document": "verif_doc.pdf",
        "relieving_letter": "relieving.pdf"
    }
    raw_api = {
        "data": [
            {
                "company": "DocCorp2",
                "lists": [
                    {
                        "designation": "Analyst",
                        "document": "role_doc.pdf",
                        "verificationProcess": verif_proc
                    }
                ]
            }
        ]
    }
    extracted = extract_employment_history(raw_api)
    role = extracted[0]["roles"][0]
    assert role["document"] == "role_doc.pdf"
    assert role["verificationProcess"]["document"] == "verif_doc.pdf"
    assert role["verificationProcess"]["relieving_letter"] == "relieving.pdf"


def test_document_reference_not_exposed_in_generic_history():
    extracted = {
        "employment_history": [
            {
                "company": "DocCorp",
                "roles": [
                    {
                        "designation": "Analyst",
                        "document": "SECRET_DOC_PATH.pdf",
                        "have_document": True
                    }
                ]
            }
        ]
    }
    response = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, extracted, None)
    assert "SECRET_DOC_PATH.pdf" not in response


def test_document_reference_not_exposed_in_verification_response():
    extracted = {
        "employment_history": [
            {
                "company": "DocCorp",
                "roles": [
                    {
                        "designation": "Analyst",
                        "document": "SECRET_DOC_PATH.pdf",
                        "verification": {"status": "verified"}
                    }
                ]
            }
        ]
    }
    response = format_single_intent(UserIntent.EMPLOYMENT_VERIFICATION, extracted, None)
    assert "SECRET_DOC_PATH.pdf" not in response


# 4. Salary and bonus tests
def test_bonus_extracted_for_salary_intent():
    raw_api = {
        "data": [
            {
                "company": "SalaryCorp",
                "lists": [
                    {
                        "designation": "Developer",
                        "salary": "1000000",
                        "salary_inhand": "80000",
                        "salary_mode": "Per Annum",
                        "bonus": "50000"
                    }
                ]
            }
        ]
    }
    salaries = extract_employment_salary(raw_api)
    assert len(salaries) == 1
    assert salaries[0]["bonus"] == "50000"

    formatted = format_single_intent(
        UserIntent.EMPLOYMENT_SALARY,
        {"employment_salary": salaries},
        None
    )
    assert "[Bonus: ₹50000]" in formatted


def test_bonus_isolated_from_employment_history():
    extracted = {
        "employment_history": [
            {
                "company": "AcmeCorp",
                "roles": [
                    {
                        "designation": "Dev",
                        "bonus": "100000"
                    }
                ]
            }
        ]
    }
    response = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, extracted, None)
    assert "100000" not in response
    assert "Bonus" not in response


def test_bonus_isolated_from_verification_response():
    extracted = {
        "employment_history": [
            {
                "company": "BonusCorp",
                "roles": [
                    {
                        "designation": "Dev",
                        "bonus": "100000",
                        "verification": {"status": "verified"}
                    }
                ]
            }
        ]
    }
    response = format_single_intent(UserIntent.EMPLOYMENT_VERIFICATION, extracted, None)
    assert "100000" not in response
    assert "Bonus" not in response


def test_salary_fields_not_exposed_in_generic_history():
    extracted = {
        "employment_history": [
            {
                "company": "SalaryCorp",
                "roles": [
                    {
                        "designation": "Dev",
                        "salary": "1500000",
                        "salary_inhand": "100000",
                        "have_salary": True
                    }
                ]
            }
        ]
    }
    response = format_single_intent(UserIntent.EMPLOYMENT_HISTORY, extracted, None)
    assert "1500000" not in response
    assert "100000" not in response


# 5. DB fallback tests
def test_db_fallback_missing_metadata_defaults(monkeypatch):
    class DummyCursor:
        def execute(self, query, params):
            pass
        def fetchall(self):
            return [
                {
                    "id": 1,
                    "company": "101",
                    "company_name": "Test DB Corp",
                    "designation": "Software Engineer",
                    "joining_date": "2020-01-01",
                    "worked_till_date": "2022-01-01",
                    "still_working": "0",
                    "salary": "500000",
                    "salary_inhand": "40000",
                    "salary_mode": "Per Annum",
                    "approved": "1",
                    "status": "1",
                    "company_label": "Test DB Corp"
                }
            ]
        def close(self):
            pass

    class DummyConn:
        def cursor(self):
            return DummyCursor()
        def close(self):
            pass

    import backend.repositories.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: DummyConn())

    fallback = _get_employment_db_fallback(123)
    assert fallback["status"] is True
    data = fallback["data"]
    assert len(data) == 1
    comp = data[0]
    assert comp["is_verified"] is None
    assert comp["company_claim_status"] is None
    
    role = comp["lists"][0]
    assert role["skill"] == []
    assert role["verificationProcess"] == {}
    assert role["have_salary"] is None
    assert role["have_document"] is None
    assert role["have_review"] is None
    assert role["claim_status"] is None
    assert role["employment_status"] is None
    assert role["employment_type"] is None
    assert role["department"] is None
    assert role["bonus"] is None
    assert role["document"] is None


# 6. Verification regression invariants
def test_verification_normalization_invariance():
    # Rule 1: approved '1' and status '1' -> verified
    res1 = normalize_employment_verification("1", "1", {"level1": "1"})
    assert res1["status"] == "verified"
    assert res1["is_verified"] is True
    assert res1["level"] == 1

    # Rule 2: non-'1' approved -> not_verified
    res2 = normalize_employment_verification("0", "1", {"level1": "1", "level2": "1"}, company_is_verified=True)
    assert res2["status"] == "not_verified"
    assert res2["is_verified"] is False
    assert res2["level"] == 2

    # Rule 3: missing approved -> unknown (highest active flag level3=1 -> level=3)
    res3 = normalize_employment_verification(None, "1", {"level1": "1", "level2": "1", "level3": "1"}, company_is_verified=True)
    assert res3["status"] == "unknown"
    assert res3["is_verified"] is False
    assert res3["level"] == 3

    # Rule 4: Highest active level (level4 > level2)
    res4 = normalize_employment_verification("1", "1", {"level1": "1", "level2": "1", "level3": "1", "level4": "1"}, company_is_verified=True)
    assert res4["level"] == 4
    assert res4["level_label"] == "Level 4"
