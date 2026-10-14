import pytest
from backend.services.user_data_formatter import format_single_intent
from backend.services.user_data_intent_parser import UserIntent


# -------------------------------------------------------------------
# A. Basic Source Integration Tests
# -------------------------------------------------------------------

def test_role_level_document_displayed():
    data = {
        "employment_history": [
            {
                "company": "Acme Corp",
                "roles": [{"designation": "Developer", "document": "offer_letter.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Acme Corp:**" in res
    assert "• **Developer:**" in res
    assert "Uploaded Employment Document" in res
    assert "offer_letter.pdf" not in res


def test_verification_process_document_displayed():
    data = {
        "employment_history": [
            {
                "company": "Beta Inc",
                "roles": [{"designation": "Manager", "verificationProcess": {"document": "verif_doc.pdf"}}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Beta Inc:**" in res
    assert "• **Manager:**" in res
    assert "Verification Document" in res
    assert "verif_doc.pdf" not in res


def test_relieving_letter_displayed():
    data = {
        "employment_history": [
            {
                "company": "Gamma LLC",
                "roles": [{"designation": "Lead", "verificationProcess": {"relieving_letter": "relieving.pdf"}}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Gamma LLC:**" in res
    assert "• **Lead:**" in res
    assert "Relieving Letter" in res
    assert "relieving.pdf" not in res


def test_all_three_document_sources_displayed():
    data = {
        "employment_history": [
            {
                "company": "Delta Systems",
                "roles": [
                    {
                        "designation": "Architect",
                        "document": "role_doc.pdf",
                        "verificationProcess": {
                            "document": "verif_doc.pdf",
                            "relieving_letter": "relieving.pdf"
                        }
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Delta Systems:**" in res
    assert "• **Architect:**" in res
    assert "Uploaded Employment Document" in res
    assert "Verification Document" in res
    assert "Relieving Letter" in res


# -------------------------------------------------------------------
# B. Multiple References and Deduplication Tests
# -------------------------------------------------------------------

def test_multiple_references_within_role_document():
    data = {
        "employment_history": [
            {
                "company": "Epsilon",
                "roles": [{"designation": "Engineer", "document": ["doc1.pdf", "doc2.pdf"]}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res.count("Uploaded Employment Document") == 1


def test_duplicate_references_across_sources_handled():
    data = {
        "employment_history": [
            {
                "company": "Zeta",
                "roles": [
                    {
                        "designation": "Analyst",
                        "document": "same_doc.pdf",
                        "verificationProcess": {"document": "same_doc.pdf"}
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Uploaded Employment Document" in res
    assert "Verification Document" in res


def test_different_document_types_preserved():
    data = {
        "employment_history": [
            {
                "company": "Eta Corp",
                "roles": [
                    {
                        "designation": "Consultant",
                        "document": "docA.pdf",
                        "verificationProcess": {"relieving_letter": "docB.pdf"}
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Uploaded Employment Document" in res
    assert "Relieving Letter" in res


def test_repeated_entries_do_not_produce_duplicate_labels_in_same_role():
    data = {
        "employment_history": [
            {
                "company": "Theta",
                "roles": [
                    {"designation": "Dev", "document": ["doc1.pdf", "doc1.pdf"]}
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res.count("Uploaded Employment Document") == 1


# -------------------------------------------------------------------
# C. Role Association and Hierarchy Tests
# -------------------------------------------------------------------

def test_two_roles_same_company_same_document_category():
    data = {
        "employment_history": [
            {
                "company": "Google",
                "roles": [
                    {"designation": "Software Engineer", "document": "offer.pdf"},
                    {"designation": "Senior Software Engineer", "document": "exp.pdf"}
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Google:**" in res
    assert "• **Software Engineer:**" in res
    assert "• **Senior Software Engineer:**" in res
    assert res.count("Uploaded Employment Document") == 2


def test_two_roles_same_company_different_document_categories():
    data = {
        "employment_history": [
            {
                "company": "Microsoft",
                "roles": [
                    {"designation": "SDE 1", "document": "doc1.pdf"},
                    {"designation": "SDE 2", "verificationProcess": {"relieving_letter": "letter.pdf"}}
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Microsoft:**" in res
    assert "• **SDE 1:**" in res
    assert "• **SDE 2:**" in res
    assert "Uploaded Employment Document" in res
    assert "Relieving Letter" in res


def test_two_companies_identical_document_categories():
    data = {
        "employment_history": [
            {"company": "Comp A", "roles": [{"designation": "Dev", "document": "docA.pdf"}]},
            {"company": "Comp B", "roles": [{"designation": "Dev", "document": "docB.pdf"}]}
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Comp A:**" in res
    assert "**Comp B:**" in res
    assert res.count("Uploaded Employment Document") == 2


def test_two_roles_with_missing_titles():
    data = {
        "employment_history": [
            {
                "company": "NoTitleCorp",
                "roles": [
                    {"document": "doc1.pdf"},
                    {"document": "doc2.pdf"}
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**NoTitleCorp:**" in res
    assert "• **Employment Role:**" in res
    assert res.count("Uploaded Employment Document") == 2


def test_role_with_no_documents_alongside_role_with_documents():
    data = {
        "employment_history": [
            {
                "company": "MixedCorp",
                "roles": [
                    {"designation": "Intern"},  # No docs
                    {"designation": "Fulltime", "document": "offer.pdf"}
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Intern" not in res
    assert "• **Fulltime:**" in res
    assert "Uploaded Employment Document" in res


# -------------------------------------------------------------------
# D. Presence-Flag Handling Tests
# -------------------------------------------------------------------

def test_have_document_true_no_explicit_ref():
    data = {
        "employment_history": [
            {
                "company": "Iota",
                "roles": [{"designation": "Dev", "have_document": True}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "**Iota:**" in res
    assert "Document Record Available" in res


def test_have_document_false_no_explicit_ref():
    data = {
        "employment_history": [
            {
                "company": "Kappa",
                "roles": [{"designation": "Dev", "have_document": False}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_have_document_none_no_explicit_ref():
    data = {
        "employment_history": [
            {
                "company": "Lambda",
                "roles": [{"designation": "Dev", "have_document": None}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_explicit_ref_displayable_when_have_document_false():
    data = {
        "employment_history": [
            {
                "company": "Mu Corp",
                "roles": [{"designation": "Dev", "document": "my_doc.pdf", "have_document": False}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Uploaded Employment Document" in res


def test_explicit_ref_displayable_when_have_document_none():
    data = {
        "employment_history": [
            {
                "company": "Nu Corp",
                "roles": [{"designation": "Dev", "document": "my_doc.pdf", "have_document": None}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "Uploaded Employment Document" in res


# -------------------------------------------------------------------
# E. Empty and Malformed Inputs Tests
# -------------------------------------------------------------------

def test_all_document_fields_missing_returns_no_documents():
    data = {
        "employment_history": [
            {
                "company": "Xi Corp",
                "roles": [{"designation": "Dev"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_empty_strings_do_not_produce_document_entries():
    data = {
        "employment_history": [
            {
                "company": "Omicron",
                "roles": [
                    {
                        "designation": "Dev",
                        "document": "",
                        "verificationProcess": {"document": "", "relieving_letter": ""}
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_whitespace_strings_do_not_produce_document_entries():
    data = {
        "employment_history": [
            {
                "company": "Pi Corp",
                "roles": [{"designation": "Dev", "document": "   "}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_empty_dicts_do_not_produce_document_entries():
    data = {
        "employment_history": [
            {
                "company": "Rho Corp",
                "roles": [{"designation": "Dev", "document": {}, "verificationProcess": {}}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_empty_lists_do_not_produce_document_entries():
    data = {
        "employment_history": [
            {
                "company": "Sigma",
                "roles": [{"designation": "Dev", "document": []}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_malformed_nested_structures_do_not_crash():
    data = {
        "employment_history": [
            {
                "company": "Tau",
                "roles": [
                    {
                        "designation": "Dev",
                        "document": [None, 123, False],
                        "verificationProcess": "not_a_dict"
                    }
                ]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


def test_unexpected_boolean_numeric_values_handled():
    data = {
        "employment_history": [
            {
                "company": "Upsilon",
                "roles": [{"designation": "Dev", "document": True, "verificationProcess": {"document": 999}}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res == "No employment documents found."


# -------------------------------------------------------------------
# F. Security Tests
# -------------------------------------------------------------------

def test_internal_windows_paths_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp1",
                "roles": [{"designation": "Dev", "document": "C:\\private\\secret_doc.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "C:\\private\\secret_doc.pdf" not in res
    assert "Uploaded Employment Document" in res


def test_unix_filesystem_paths_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp2",
                "roles": [{"designation": "Dev", "document": "/var/uploads/secret.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "/var/uploads/secret.pdf" not in res
    assert "Uploaded Employment Document" in res


def test_internal_urls_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp3",
                "roles": [{"designation": "Dev", "document": "http://localhost:8000/doc.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "http://localhost:8000/doc.pdf" not in res
    assert "Uploaded Employment Document" in res


def test_public_urls_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp4",
                "roles": [{"designation": "Dev", "document": "https://example.com/public.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "https://example.com/public.pdf" not in res
    assert "Uploaded Employment Document" in res


def test_filenames_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp5",
                "roles": [{"designation": "Dev", "document": "my_confidential_offer.pdf"}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "my_confidential_offer.pdf" not in res
    assert "Uploaded Employment Document" in res


def test_raw_document_ids_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp6",
                "roles": [{"designation": "Dev", "document": {"id": 888999, "name": "doc.pdf"}}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "888999" not in res
    assert "Uploaded Employment Document" in res


def test_dictionary_list_contents_not_exposed():
    data = {
        "employment_history": [
            {
                "company": "SecCorp7",
                "roles": [{"designation": "Dev", "document": [{"file": "private1.pdf"}, {"file": "private2.pdf"}]}]
            }
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert "private1.pdf" not in res
    assert "private2.pdf" not in res
    assert "Uploaded Employment Document" in res


# -------------------------------------------------------------------
# G. Response Structure and Stability Tests
# -------------------------------------------------------------------

def test_existing_markdown_structure_preserved():
    data = {
        "employment_history": [
            {"company": "FormatCorp", "roles": [{"designation": "Engineer", "document": "doc.pdf"}]}
        ]
    }
    res = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, data, {})
    assert res.startswith("**Work Documents:**")
    assert "**FormatCorp:**" in res
    assert "• **Engineer:**" in res


def test_no_document_response_remains_stable():
    res1 = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, {}, {})
    res2 = format_single_intent(UserIntent.EMPLOYMENT_DOCUMENTS, {"employment_history": []}, {})
    assert res1 == "No employment documents found."
    assert res2 == "No employment documents found."
