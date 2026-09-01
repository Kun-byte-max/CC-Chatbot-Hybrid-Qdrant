"""
Normalization Layer Test Suite.
Verifies normalize_role_title for surface variations without aggressive semantic collapsing.
"""

import unittest
from backend.services.normalization_service import normalize_role_title, normalize_skills


class TestNormalizationLayer(unittest.TestCase):
    def test_surface_variation_normalization(self):
        self.assertEqual(normalize_role_title("frontend dev"), "Frontend Developer")
        self.assertEqual(normalize_role_title("front-end developer"), "Frontend Developer")
        self.assertEqual(normalize_role_title("front end developer"), "Frontend Developer")
        self.assertEqual(normalize_role_title("backend dev"), "Backend Developer")
        self.assertEqual(normalize_role_title("fullstack dev"), "Full Stack Developer")

    def test_semantic_role_non_merging(self):
        # Must NOT merge distinct semantic roles into Data Analyst or Frontend Developer
        self.assertEqual(normalize_role_title("Data Analyst"), "Data Analyst")
        self.assertEqual(normalize_role_title("Data Scientist"), "Data Scientist")
        self.assertEqual(normalize_role_title("Data Engineer"), "Data Engineer")
        self.assertEqual(normalize_role_title("Business Analyst"), "Business Analyst")
        self.assertEqual(normalize_role_title("UI Developer"), "Ui Developer")
        self.assertEqual(normalize_role_title("Frontend Engineer"), "Frontend Engineer")

    def test_skill_normalization(self):
        self.assertEqual(normalize_skills(["js", "reactjs", "node"]), ["JavaScript", "React", "Node.js"])


if __name__ == "__main__":
    unittest.main()

