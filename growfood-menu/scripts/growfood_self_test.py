#!/usr/bin/env python3
from __future__ import annotations

import copy
import io
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
from growfood_menu import GrowFoodAutoMenuService, _find_incomplete_target_dates, main as menu_main
from growfood_menu_core import (
    _apply_add_custom_menu_payload_to_full_data,
    _normalize_single_decision,
    _normalize_target_menu_payload,
    compare_target_menu_snapshot,
    load_preference_profile,
)
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
        prompt = spec["payload"]["message"]
        self.assertIn("NO_REPLY", prompt)
        self.assertIn("👨‍🍳 Меню на <даты> готово и сохранено.", prompt)
        self.assertIn("GF_AUTOMATION_RESULT", prompt)
        self.assertIn("Do not include order IDs", prompt)

    def test_cron_spec_accepts_timezone(self) -> None:
        spec = build_cron_spec("Europe/Moscow")
        self.assertEqual("Europe/Moscow", spec["schedule"]["tz"])

class TargetValidationTests(unittest.TestCase):
    def test_explicit_removal_and_integer_strings(self) -> None:
        for slot in ({"packId": None}, {"remove": True}):
            with self.subTest(slot=slot):
                result = _normalize_target_menu_payload({"days": {"2030-01-01": {"1": slot}}})
                self.assertIsNone(result["decisions"][0]["packId"])
        result = _normalize_target_menu_payload({"days": {"2030-01-01": {"01": {"packId": " 12 "}}}})
        self.assertEqual(1, result["decisions"][0]["mealNumber"])
        self.assertEqual(12, result["decisions"][0]["packId"])

    def test_snapshot_format_and_order_aliases(self) -> None:
        for key in ("order_id_H", "orderId_H", "orderId"):
            result = _normalize_target_menu_payload({key: "synthetic-draft", "dates": {
                "2030-01-01": {"meals": [{"mealNumber": "1", "packId": 12}]}
            }})
            self.assertEqual("synthetic-draft", result["order_id_H"])
            self.assertEqual(12, result["decisions"][0]["packId"])

    def test_rejects_malformed_target_shapes(self) -> None:
        for payload in (
            {"days": []}, {"days": {"2030-01-01": []}},
            {"days": {"2030-01-01": {"1": []}}},
            {"dates": {"2030-01-01": []}},
            {"dates": {"2030-01-01": {}}},
            {"dates": {"2030-01-01": {"meals": {}}}},
            {"dates": {"2030-01-01": {"meals": [None]}}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _normalize_target_menu_payload(payload)

    def test_rejects_missing_id_and_non_boolean_removal(self) -> None:
        for slot in ({}, {"reason": "keep"}, {"remove": False}, {"packId": 12, "remove": "false"}):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                _normalize_target_menu_payload({"days": {"2030-01-01": {"1": slot}}})

    def test_rejects_lossy_or_invalid_identifiers(self) -> None:
        for field in ("packId", "mealNumber"):
            for value in (True, False, 1.0, 12.9, 0, -1, "", "1.5", "1e1", "1_2", None, [], {}):
                if field == "packId" and value is None:
                    continue
                raw = {"mealDate": "2030-01-01", "mealNumber": 1, "packId": 12, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    _normalize_single_decision(raw)


class SyntheticMenuClient:
    """In-memory API fixture: never resolves credentials or opens a socket."""

    def __init__(self) -> None:
        self.full = {
            "order": {"id_H": "synthetic-draft", "custom_config": []},
            "menu": {date: {"customizable": True} for date in ("2030-01-01", "2030-01-02")},
            "customization_menu": {
                date: {"packs": [{"position": 1, "major": [11], "secondary": [12]}]}
                for date in ("2030-01-01", "2030-01-02")
            },
            "packs": {"11": {"name": "Synthetic first dish"}, "12": {"name": "Synthetic second dish"}},
        }
        self.calls = []
        self.fail_date = None
        self.ignore_save = False
        self.verification_error = False
        self.accept_error = False
        self.accept_success = True
        self.extra_slot = False
        self.accept_resets_menu = False
        self.post_accept_error = False
        self.reads = 0

    def get_full_menu(self, **kwargs):
        self.calls.append("read")
        self.reads += 1
        if self.reads > 2 and self.post_accept_error:
            raise OSError("synthetic post-accept read failure")
        if self.reads > 1 and self.verification_error:
            raise OSError("synthetic read failure")
        full = copy.deepcopy(self.full)
        if self.reads > 1 and self.extra_slot:
            full["customization_menu"]["2030-01-01"]["packs"].append({
                "position": 2, "major": [11], "secondary": []
            })
        return full

    def add_custom_menu(self, payload):
        self.calls.append("save:" + payload["mealDate"])
        if payload["mealDate"] == self.fail_date:
            return {"success": False}
        if not self.ignore_save:
            self.full = _apply_add_custom_menu_payload_to_full_data(self.full, payload)
        return {"success": True}

    def accept_draft(self, order_id):
        self.calls.append("accept")
        if self.accept_error:
            raise OSError("synthetic acceptance failure")
        if self.accept_resets_menu:
            self.full["order"]["custom_config"] = []
        return {"success": self.accept_success}


class MenuSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SyntheticMenuClient()
        self.service = GrowFoodAutoMenuService(self.client)
        self.decisions = [
            {"mealDate": date, "mealNumber": 1, "packId": 12}
            for date in ("2030-01-01", "2030-01-02")
        ]
        for mock in (
            patch("growfood_menu._progress"),
            patch("growfood_menu._write_last_save_result"),
            patch.dict(os.environ, {"GROWFOOD_HTTP_BATCH_SLEEP": "0"}),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def execute(self, decisions=None, **kwargs):
        return self.service.execute_plan(
            self.decisions if decisions is None else decisions,
            "synthetic-draft", require_complete_dates=True, **kwargs
        )

    def test_success_verifies_before_accepting(self) -> None:
        result = self.execute()
        self.assertEqual("updated", result["status"])
        self.assertTrue(result["accepted"])
        self.assertEqual("passed", result["verification"]["status"])
        self.assertEqual(["read", "save:2030-01-01", "save:2030-01-02", "read", "accept", "read"], self.client.calls)

    def test_failed_second_save_does_not_accept_partial_draft(self) -> None:
        self.client.fail_date = "2030-01-02"
        result = self.execute()
        self.assertEqual("partial_failure", result["status"])
        self.assertFalse(result["accepted"])
        self.assertEqual(1, len(result["applied"]))
        self.assertNotIn("accept", self.client.calls)

    def test_mismatch_does_not_accept(self) -> None:
        self.client.ignore_save = True
        result = self.execute()
        self.assertEqual("verification_failed", result["status"])
        self.assertFalse(result["accepted"])
        self.assertNotIn("accept", self.client.calls)

    def test_verification_error_does_not_accept(self) -> None:
        self.client.verification_error = True
        result = self.execute()
        self.assertEqual("verification_error", result["status"])
        self.assertFalse(result["accepted"])
        self.assertNotIn("accept", self.client.calls)

    def test_final_accepted_state_is_still_verified(self) -> None:
        self.client.accept_resets_menu = True
        result = self.execute()
        self.assertEqual("verification_failed", result["status"])
        self.assertTrue(result["accepted"])
        self.assertEqual(["accept", "read"], self.client.calls[-2:])

    def test_post_accept_read_failure_is_reported(self) -> None:
        self.client.post_accept_error = True
        result = self.execute()
        self.assertEqual("verification_error", result["status"])
        self.assertTrue(result["accepted"])
        self.assertEqual(["accept", "read"], self.client.calls[-2:])

    def test_acceptance_error_is_reported(self) -> None:
        self.client.accept_error = True
        result = self.execute()
        self.assertEqual("partial_failure", result["status"])
        self.assertFalse(result["accepted"])
        self.assertIn("accept-draft failed", result["issues"][0]["error"])

    def test_acceptance_false_is_reported(self) -> None:
        self.client.accept_success = False
        result = self.execute()
        self.assertFalse(result["accepted"])
        self.assertEqual("partial_failure", result["status"])

    def test_invalid_later_pack_is_rejected_before_any_save(self) -> None:
        self.decisions[1]["packId"] = 999
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertEqual(["read"], self.client.calls)

    def test_closed_later_date_is_rejected_before_any_save(self) -> None:
        self.client.full["menu"]["2030-01-02"]["customizable"] = False
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertEqual(["read"], self.client.calls)

    def test_unknown_later_slot_is_rejected_before_any_save(self) -> None:
        self.decisions[1]["mealNumber"] = 2
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertEqual(["read"], self.client.calls)

    def test_duplicate_slot_is_rejected_before_any_save(self) -> None:
        self.decisions.append({**self.decisions[0], "mealNumber": "01"})
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertEqual([], self.client.calls)

    def test_incomplete_target_is_rejected_before_any_save(self) -> None:
        self.client.full["customization_menu"]["2030-01-02"]["packs"].append({
            "position": 2, "major": [11], "secondary": []
        })
        result = self.execute()
        self.assertEqual("error", result["status"])
        self.assertEqual(["read"], self.client.calls)

    def test_unexpected_occupied_slot_blocks_acceptance(self) -> None:
        self.client.extra_slot = True
        result = self.execute()
        self.assertEqual("verification_failed", result["status"])
        self.assertFalse(result["accepted"])
        self.assertNotIn("accept", self.client.calls)

    def test_partial_service_api_still_allows_untargeted_slots(self) -> None:
        self.client.full["customization_menu"]["2030-01-01"]["packs"].append({
            "position": 2, "major": [11], "secondary": []
        })
        result = self.service.execute_plan(self.decisions[:1], "synthetic-draft")
        self.assertEqual("updated", result["status"])
        self.assertTrue(result["accepted"])

    def test_explicit_removal_is_saved_verified_and_accepted(self) -> None:
        self.decisions[0]["packId"] = None
        result = self.execute()
        self.assertEqual("updated", result["status"])
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["verification"]["target"]["2030-01-01"]["1"])

    def test_no_finalize_still_verifies_without_accepting(self) -> None:
        result = self.execute(finalize=False)
        self.assertEqual("updated_draft_only", result["status"])
        self.assertEqual("passed", result["verification"]["status"])
        self.assertFalse(result["accepted"])
        self.assertNotIn("accept", self.client.calls)

    def test_existing_target_is_verified_then_accepted(self) -> None:
        for decision in self.decisions:
            decision["packId"] = 11
        result = self.execute()
        self.assertEqual("no_changes", result["status"])
        self.assertTrue(result["accepted"])
        self.assertEqual(["read", "read", "accept", "read"], self.client.calls)

    def test_empty_target_does_not_read_or_accept(self) -> None:
        result = self.execute([])
        self.assertEqual("no_changes", result["status"])
        self.assertFalse(result["accepted"])
        self.assertEqual([], self.client.calls)

    def test_empty_cli_target_is_noop(self) -> None:
        with patch("growfood_menu._build_client_from_args", return_value=self.client), \
             patch("sys.stdin", io.StringIO('{"days": {}}')), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, menu_main(["save-menu", "--stdin", "--order-id", "synthetic-draft"]))
        self.assertFalse(json.loads(output.getvalue())["accepted"])
        self.assertEqual([], self.client.calls)

    def test_malformed_cli_target_never_accepts(self) -> None:
        with patch("growfood_menu._build_client_from_args", return_value=self.client), \
             patch("sys.stdin", io.StringIO('{"days": {"2030-01-01": []}}')), self.assertRaises(ValueError):
            menu_main(["save-menu", "--stdin", "--order-id", "synthetic-draft"])
        self.assertEqual([], self.client.calls)


class ExactTargetTests(unittest.TestCase):
    def test_untargeted_removed_placeholder_is_not_extra_dish(self) -> None:
        compact = {"dates": {"2030-01-01": {"meals": [
            {"mealNumber": 1, "packId": 12}, {"mealNumber": 2, "packId": None, "removed": True}
        ]}}}
        self.assertEqual([], compare_target_menu_snapshot(compact, {"2030-01-01": {"1": 12}}, True))


if __name__ == "__main__":
    unittest.main()
