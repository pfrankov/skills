#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from growfood_auth import normalize_phone, resolve_client_token, write_auth_file
from growfood_menu import _find_incomplete_target_dates
from growfood_menu_core import load_preference_profile
from growfood_setup import DECLARATION_KEY, build_cron_spec, initialize_preferences


class AuthTests(unittest.TestCase):
    def test_phone_normalization(self) -> None:
        self.assertEqual("+7 (999) 123-45-67", normalize_phone("+7 999 123 45 67"))
        self.assertEqual("+7 (999) 123-45-67", normalize_phone("8 999 123 45 67"))

    def test_auth_file_precedes_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "auth.json"
            write_auth_file("file-token", str(auth_path))
            with patch.dict(os.environ, {"GROWFOOD_CLIENT_TOKEN": "env-token"}, clear=False):
                self.assertEqual("file-token", resolve_client_token(auth_file=str(auth_path)))

    def test_explicit_token_precedes_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "auth.json"
            write_auth_file("file-token", str(auth_path))
            self.assertEqual("explicit-token", resolve_client_token("explicit-token", str(auth_path)))


class ProfileTests(unittest.TestCase):
    def test_bundled_example_is_valid_and_neutral(self) -> None:
        profile_path = SCRIPT_DIR.parent / "examples" / "preference-profile.json"
        profile = load_preference_profile(str(profile_path))
        self.assertEqual("Example GrowFood profile", profile["profile_name"])
        self.assertEqual([], profile["hard_constraints"])
        self.assertEqual(1, len(profile["soft_preferences"]))
        self.assertNotIn("client_token", json.dumps(profile, ensure_ascii=False))

    def test_setup_creates_private_profile_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "config" / "preferences.json"
            first = initialize_preferences(str(target))
            self.assertTrue(first["created"])
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            original = target.read_text(encoding="utf-8")
            second = initialize_preferences(str(target))
            self.assertFalse(second["created"])
            self.assertEqual(original, target.read_text(encoding="utf-8"))


class MenuTargetTests(unittest.TestCase):
    def test_incomplete_target_reports_occupied_omitted_slots(self) -> None:
        compact = {
            "dates": {
                "2026-08-06": {
                    "meals": [
                        {"mealNumber": slot, "packId": 1000 + slot}
                        for slot in range(1, 7)
                    ]
                }
            }
        }
        decisions = [
            {"mealDate": "2026-08-06", "mealNumber": slot, "packId": 2000 + slot}
            for slot in (1, 3, 5)
        ]

        self.assertEqual(
            [
                {
                    "mealDate": "2026-08-06",
                    "missingMealNumbers": ["2", "4", "6"],
                }
            ],
            _find_incomplete_target_dates(compact, decisions),
        )

    def test_complete_target_accepts_explicit_null_removals(self) -> None:
        compact = {
            "dates": {
                "2026-08-06": {
                    "meals": [
                        {"mealNumber": slot, "packId": 1000 + slot}
                        for slot in range(1, 7)
                    ]
                }
            }
        }
        decisions = [
            {
                "mealDate": "2026-08-06",
                "mealNumber": slot,
                "packId": 2000 + slot if slot in {1, 3, 5} else None,
            }
            for slot in range(1, 7)
        ]

        self.assertEqual([], _find_incomplete_target_dates(compact, decisions))


class AutomationTests(unittest.TestCase):
    def test_cron_spec_is_idempotent_and_quiet_on_no_change(self) -> None:
        spec = build_cron_spec()
        self.assertEqual(DECLARATION_KEY, spec["declarationKey"])
        self.assertEqual({"kind": "cron", "expr": "0 12 * * *"}, spec["schedule"])
        self.assertEqual("isolated", spec["sessionTarget"])
        self.assertIn("NO_REPLY", spec["payload"]["message"])

    def test_cron_spec_accepts_timezone(self) -> None:
        spec = build_cron_spec("Europe/Moscow")
        self.assertEqual("Europe/Moscow", spec["schedule"]["tz"])


if __name__ == "__main__":
    unittest.main()
