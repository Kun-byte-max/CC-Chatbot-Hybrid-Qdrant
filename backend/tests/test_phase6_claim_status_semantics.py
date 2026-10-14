"""
test_phase6_claim_status_semantics.py — Verification of decoupled claim_status vs is_verified semantics.

Guarantees:
  1. claim_status (1 / 0 / None) survives extractor -> service -> results_payload.
  2. claim_status=1 + is_verified=false -> Claimed, Domain Not Verified (Case B).
  3. claim_status=0 + is_verified=true -> Not Claimed, Domain Verified (Case C).
  4. claim_status=None + is_verified=true -> Claim Status Unknown, Domain Verified (Case D).
  5. Existing verification semantics (is_verified for domain verification, approved+status for employment verification) are preserved.
"""

import unittest
from unittest.mock import patch
from backend.services.user_data_extractor import extract_employment_history
from backend.services.user_data_service import handle_query
from backend.services.user_data_intent_parser import UserIntent
import asyncio


class TestClaimStatusSemantics(unittest.TestCase):

    def test_1_claim_status_reaches_results_payload(self):
        """Verify claim_status=1 survives extractor -> service -> results_payload."""
        raw_api_payload = {
            "status": True,
            "data": [
                {
                    "id": "71505",
                    "company": "Collar Check Pvt. Ltd.",
                    "company_id": "2",
                    "is_verified": True,
                    "claim_status": 1,
                    "joining_date": "2026-07-13",
                    "worked_till_date": "",
                    "approved": "1",
                    "status": "1",
                    "totalExperienceMonths": 2,
                    "lists": [
                        {
                            "id": "1001",
                            "designation": "Software Engineer",
                            "approved": "1",
                            "status": "1",
                            "claim_status": 1,
                            "verificationProcess": {"level1": True, "level2": True, "level3": True}
                        }
                    ]
                }
            ]
        }

        extracted_history = extract_employment_history(raw_api_payload)
        self.assertEqual(len(extracted_history), 1)
        self.assertEqual(extracted_history[0]["company_claim_status"], 1)

        with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
            service_res = asyncio.run(handle_query("check employment details", token="fake_token", user_slug="fake_slug"))

        self.assertTrue(service_res.handled)
        self.assertTrue(len(service_res.results_payload) >= 1)
        comp_verif = service_res.results_payload[0].get("company_verification", {})
        self.assertEqual(comp_verif.get("claim_status"), 1)
        self.assertIs(comp_verif.get("is_verified"), True)

    def test_2_claimed_independent_of_is_verified_false(self):
        """Case B: claim_status=1, is_verified=false -> Claimed, Domain Not Verified."""
        raw_api_payload = {
            "status": True,
            "data": [
                {
                    "id": "71506",
                    "company": "Acme Startup",
                    "company_id": "3",
                    "is_verified": False,
                    "claim_status": 1,
                    "lists": [
                        {
                            "id": "1002",
                            "designation": "Product Manager",
                            "approved": "0",
                            "status": "0",
                            "claim_status": 1
                        }
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
            service_res = asyncio.run(handle_query("check employment details", token="fake_token", user_slug="fake_slug"))

        comp_verif = service_res.results_payload[0]["company_verification"]
        self.assertEqual(comp_verif["claim_status"], 1)
        self.assertIs(comp_verif["is_verified"], False)

    def test_3_unclaimed_independent_of_is_verified_true(self):
        """Case C: claim_status=0, is_verified=true -> Not Claimed, Domain Verified."""
        raw_api_payload = {
            "status": True,
            "data": [
                {
                    "id": "71507",
                    "company": "Enterprise Corp",
                    "company_id": "4",
                    "is_verified": True,
                    "claim_status": 0,
                    "lists": [
                        {
                            "id": "1003",
                            "designation": "Consultant",
                            "approved": "1",
                            "status": "1",
                            "claim_status": 0
                        }
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
            service_res = asyncio.run(handle_query("check employment details", token="fake_token", user_slug="fake_slug"))

        comp_verif = service_res.results_payload[0]["company_verification"]
        self.assertEqual(comp_verif["claim_status"], 0)
        self.assertIs(comp_verif["is_verified"], True)

    def test_4_unknown_claim_status_remains_none(self):
        """Case D: claim_status=None, is_verified=true -> claim_status remains None (not converted to 0)."""
        raw_api_payload = {
            "status": True,
            "data": [
                {
                    "id": "71508",
                    "company": "Legacy Corp",
                    "company_id": "5",
                    "is_verified": True,
                    # claim_status missing/None
                    "lists": [
                        {
                            "id": "1004",
                            "designation": "Analyst",
                            "approved": "1",
                            "status": "1"
                        }
                    ]
                }
            ]
        }

        with patch("backend.services.user_data_service.platform_api._get", return_value=raw_api_payload):
            service_res = asyncio.run(handle_query("check employment details", token="fake_token", user_slug="fake_slug"))

        comp_verif = service_res.results_payload[0]["company_verification"]
        self.assertIsNone(comp_verif["claim_status"])
        self.assertIs(comp_verif["is_verified"], True)

    def test_5_existing_verification_semantics_intact(self):
        """Verify approved='1' and status='1' drives role level 3 verification status."""
        raw_api_payload = {
            "status": True,
            "data": [
                {
                    "id": "71509",
                    "company": "Verified Work LLC",
                    "company_id": "6",
                    "is_verified": True,
                    "claim_status": 1,
                    "lists": [
                        {
                            "id": "1005",
                            "designation": "Developer",
                            "approved": "1",
                            "status": "1",
                            "verificationProcess": {"level1": True, "level2": True, "level3": True}
                        }
                    ]
                }
            ]
        }

        extracted_history = extract_employment_history(raw_api_payload)
        role_verif = extracted_history[0]["roles"][0]["verification"]
        self.assertEqual(role_verif["status"], "verified")
        self.assertIs(role_verif["is_verified"], True)
        self.assertEqual(role_verif["level"], 3)


if __name__ == "__main__":
    unittest.main()
