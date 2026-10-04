#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

JsonDict = Dict[str, Any]


SKILL_DIR = Path(__file__).resolve().parents[1]
BUNDLED_EXAMPLE_PROFILE_PATH = SKILL_DIR / "examples" / "preference-profile.json"
DEFAULT_USER_PROFILE_PATH = Path("~/.config/growfood-menu/preferences.json").expanduser()
REQUIRED_PREFERENCE_PROFILE_KEYS = (
    "profile_name",
    "selection_goal",
    "planning_rules",
    "hard_constraints",
    "soft_preferences",
    "rotation_preferences",
    "planner_notes",
)


def resolve_preference_profile_path(preferences_file: Optional[str] = None) -> Path:
    candidates = [
        preferences_file,
        os.environ.get("GROWFOOD_PREFERENCE_PROFILE"),
        os.environ.get("GROWFOOD_PREFERENCES_FILE"),
        str(DEFAULT_USER_PROFILE_PATH),
        str(BUNDLED_EXAMPLE_PROFILE_PATH),
    ]
    for raw_path in candidates:
        if not raw_path:
            continue
        path = Path(raw_path).expanduser().resolve()
        if path.exists() and path.is_file():
            return path
    raise SystemExit(
        "Preference profile not found. Use --preferences-file, "
        "GROWFOOD_PREFERENCE_PROFILE, ~/.config/growfood-menu/preferences.json, "
        "or copy the bundled examples/preference-profile.json."
    )


def load_preference_profile(preferences_file: Optional[str] = None) -> JsonDict:
    profile_path = resolve_preference_profile_path(preferences_file)
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid preference profile JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("Preference profile must contain a JSON object")

    missing = [key for key in REQUIRED_PREFERENCE_PROFILE_KEYS if key not in data]
    if missing:
        raise SystemExit(
            "Preference profile is missing required keys: " + ", ".join(missing)
        )

    planning_rules = data.get("planning_rules")
    if not isinstance(planning_rules, dict):
        raise SystemExit("Preference profile planning_rules must be an object")
    allowed_meal_numbers = planning_rules.get("allowed_meal_numbers")
    if not isinstance(allowed_meal_numbers, list) or not all(isinstance(item, int) for item in allowed_meal_numbers):
        raise SystemExit("Preference profile planning_rules.allowed_meal_numbers must be an integer array")

    return data


def build_decision_contract(preference_profile: JsonDict) -> JsonDict:
    planning_rules = preference_profile.get("planning_rules") or {}
    allowed_meal_numbers = [
        _safe_int(item) for item in (planning_rules.get("allowed_meal_numbers") or [])
    ]
    return {
        "format": "growfood-menu-decisions.v1",
        "preferred_format": "growfood-plan.v1",
        "allowed_meal_numbers": allowed_meal_numbers,
        "description": "Readable compact target menu payload that save-menu can execute without extra menu-selection logic.",
        "example": {
            "version": "growfood-plan.v1",
            "order_id_H": "<order_id_H>",
            "days": {
                "2026-03-22": {
                    str(allowed_meal_numbers[0] if allowed_meal_numbers else 1): {
                        "packId": 12345,
                        "reason": "Explain why this option best fits the profile.",
                    }
                }
            },
        },
    }


def _pack_summary(pack: JsonDict) -> JsonDict:
    summary: JsonDict = {
        "name": str(pack.get("name") or ""),
        "recipe": str(pack.get("recipe") or _pack_recipe(pack)),
    }
    protein_g = pack.get("protein_g")
    if protein_g is not None:
        summary["protein_g"] = protein_g
    return summary


def _build_compact_planning_menu(compact_menu: JsonDict) -> tuple[JsonDict, JsonDict]:
    menu: JsonDict = {}
    packs: JsonDict = {}

    for meal_date, day in (compact_menu.get("dates") or {}).items():
        if not isinstance(day, dict):
            continue
        slots: JsonDict = {}
        for meal in day.get("meals") or []:
            if not isinstance(meal, dict):
                continue
            meal_number = str(_safe_int(meal.get("mealNumber")))
            current_pack_id = meal.get("packId")
            option_pack_ids: List[int] = []

            if current_pack_id is not None:
                current_pack_int = _safe_int(current_pack_id)
                option_pack_ids.append(current_pack_int)
                packs[str(current_pack_int)] = _pack_summary(meal)

            for alternative in meal.get("alternatives") or []:
                if not isinstance(alternative, dict):
                    continue
                alt_pack_id = _safe_int(alternative.get("packId"))
                if alt_pack_id <= 0:
                    continue
                if alt_pack_id not in option_pack_ids:
                    option_pack_ids.append(alt_pack_id)
                packs[str(alt_pack_id)] = _pack_summary(alternative)

            slot_entry: JsonDict = {
                "current_pack_id": (None if current_pack_id is None else _safe_int(current_pack_id)),
                "option_pack_ids": option_pack_ids,
            }
            if meal.get("removed"):
                slot_entry["removed"] = True
            slots[meal_number] = slot_entry

        menu[str(meal_date)] = slots

    return menu, packs


def build_planning_context(
    compact_menu: JsonDict,
    preference_profile: JsonDict,
    preference_profile_path: Path,
) -> JsonDict:
    menu, packs = _build_compact_planning_menu(compact_menu)
    return {
        "version": "growfood-context.v1",
        "profile_path": str(preference_profile_path),
        "profile": copy.deepcopy(preference_profile),
        "decision_contract": build_decision_contract(preference_profile),
        "menu": menu,
        "menu_metadata": {
            str(date): {"editable": bool(day.get("editable", True))}
            for date, day in (compact_menu.get("dates") or {}).items()
            if isinstance(day, dict)
        },
        "packs": packs,
    }


def build_compact_menu(full_data: JsonDict) -> JsonDict:
    data = copy.deepcopy(full_data)
    custom_config = _flatten_custom_config((data.get("order") or {}).get("custom_config"))
    base_menu = copy.deepcopy(data.get("customization_menu") or {})
    actual_menu = copy.deepcopy(data.get("customization_menu") or {})

    # GrowFood may mark a still-planned delivery as non-customizable after the
    # normal UI deadline, while retaining accepted user changes in
    # order.custom_config. Keep such dates visible so we can validate and
    # repair the saved menu. Never resurrect delivered dates: only planned/new
    # deliveries qualify for this fallback.
    planned_delivery_ids = set()
    for delivery in (data.get("order") or {}).get("deliveries") or []:
        status = delivery.get("status") or {}
        status_code = str(status.get("code") or "").strip().lower()
        status_name = str(status.get("name") or "").strip().lower()
        if status_code in {"planned", "new"} or status_name in {"запланирован", "новый", "planned", "new"}:
            delivery_id = delivery.get("id_H")
            if delivery_id:
                planned_delivery_ids.add(str(delivery_id))

    for change in custom_config:
        meal_date = str(change.get("mealDate"))
        meal_number = _safe_int(change.get("mealNumber"))
        remove = bool(change.get("remove"))
        pack_id = _safe_int(change.get("packId"))
        day = actual_menu.get(meal_date)
        if not isinstance(day, dict):
            continue
        slots = day.get("packs") or []
        slot = next((p for p in slots if _safe_int(p.get("position")) == meal_number), None)
        if not slot:
            continue
        majors = [int(x) for x in (slot.get("major") or [])]
        if remove:
            slot["major"] = [pid for pid in majors if pid != pack_id]
        else:
            slot["major"] = [pack_id] if pack_id is not None else []

    result: JsonDict = {"order_id_H": (data.get("order") or {}).get("id_H"), "dates": {}}
    menu_meta = data.get("menu") or {}

    for date in sorted(actual_menu.keys()):
        date_custom_config = [c for c in custom_config if str(c.get("mealDate")) == date]
        date_meta = menu_meta.get(date) or {}
        saved_planned_override = (
            bool(date_custom_config)
            and str(date_meta.get("deliveryId_H") or "") in planned_delivery_ids
        )
        if not date_meta.get("customizable") and not saved_planned_override:
            continue
        day_menu = actual_menu.get(date) or {}
        base_day_menu = base_menu.get(date) or {}
        result["dates"][date] = {
            "meals": [],
            "custom_config_for_date": date_custom_config,
            "editable": bool(date_meta.get("customizable")),
        }
        for slot in sorted(day_menu.get("packs") or [], key=lambda s: _safe_int(s.get("position"))):
            position = _safe_int(slot.get("position"))
            base_slot = next((p for p in (base_day_menu.get("packs") or []) if _safe_int(p.get("position")) == position), None)
            majors = slot.get("major") or []
            is_removed = not majors
            selected_pack_id = _safe_int(majors[0]) if majors else None

            option_ids: List[int] = []
            if is_removed:
                seen_option_ids = set()
                for pid in (base_slot or {}).get("major") or []:
                    safe_pid = _safe_int(pid)
                    if safe_pid and safe_pid not in seen_option_ids:
                        option_ids.append(safe_pid)
                        seen_option_ids.add(safe_pid)
                for pid in (base_slot or {}).get("secondary") or []:
                    safe_pid = _safe_int(pid)
                    if safe_pid and safe_pid not in seen_option_ids:
                        option_ids.append(safe_pid)
                        seen_option_ids.add(safe_pid)
                if not option_ids:
                    continue
                display_pack_id = option_ids[0]
            else:
                display_pack_id = selected_pack_id

            pack = _get_pack((data.get("packs") or {}), display_pack_id)
            if not pack:
                continue

            meal_obj = {
                "name": pack.get("name"),
                "recipe": _pack_recipe(pack),
                "mealDate": date,
                "mealNumber": str(position),
                "type": "major",
                "packId": (None if is_removed else selected_pack_id),
                "alternatives": [],
            }
            if is_removed:
                meal_obj["removed"] = True
            protein_g = _extract_protein_g(pack)
            if protein_g is not None:
                meal_obj["protein_g"] = protein_g

            if is_removed:
                alt_ids = option_ids
            else:
                alt_ids = [_safe_int(x) for x in (slot.get("secondary") or []) if _safe_int(x)]

            base_major_ids = {_safe_int(x) for x in ((base_slot or {}).get("major") or []) if _safe_int(x)}
            for alt_pack_id in alt_ids:
                alt_pack = _get_pack((data.get("packs") or {}), alt_pack_id)
                if not alt_pack:
                    continue
                if bool(alt_pack.get("isRestricted")):
                    continue
                alt_obj = {
                    "name": alt_pack.get("name"),
                    "recipe": _pack_recipe(alt_pack),
                    "mealDate": date,
                    "mealNumber": str(position),
                    "type": "major" if (is_removed and alt_pack_id in base_major_ids) else "secondary",
                    "packId": alt_pack_id,
                }
                alt_protein = _extract_protein_g(alt_pack)
                if alt_protein is not None:
                    alt_obj["protein_g"] = alt_protein
                meal_obj["alternatives"].append(alt_obj)

            result["dates"][date]["meals"].append(meal_obj)

    return result


def build_add_custom_menu_payload(
    full_data: JsonDict,
    meal_date: str,
    meal_number: int,
    new_pack_id: Optional[int],
) -> JsonDict:
    order = full_data.get("order") or {}
    order_id = order.get("id_H")
    current_custom_config = _flatten_custom_config(order.get("custom_config"))
    default_menu = full_data.get("customization_menu") or {}

    custom_packs_for_request: List[JsonDict] = []
    for change in current_custom_config:
        if str(change.get("mealDate")) != str(meal_date):
            continue
        if _safe_int(change.get("mealNumber")) == _safe_int(meal_number):
            continue
        custom_packs_for_request.append(copy.deepcopy(change))

    default_day = default_menu.get(str(meal_date)) or {}
    default_slots = default_day.get("packs") or []
    default_slot = next((p for p in default_slots if _safe_int(p.get("position")) == _safe_int(meal_number)), None)
    default_pack_id = None
    if default_slot and (default_slot.get("major") or []):
        default_pack_id = _safe_int((default_slot.get("major") or [None])[0])

    current_compact = build_compact_menu(full_data)
    current_meal = _find_meal(current_compact, str(meal_date), meal_number)
    current_pack_id = _safe_int(current_meal.get("packId")) if current_meal else None

    remove_pack_ids: List[int] = []
    if current_pack_id is not None:
        remove_pack_ids.append(current_pack_id)
    if default_pack_id is not None and default_pack_id not in remove_pack_ids:
        remove_pack_ids.append(default_pack_id)

    for remove_pack_id in remove_pack_ids:
        custom_packs_for_request.append(
            {
                "remove": True,
                "mealDate": str(meal_date),
                "mealNumber": str(_safe_int(meal_number)),
                "type": "major",
                "packId": remove_pack_id,
                "count": -1,
            }
        )

    if new_pack_id is not None:
        custom_packs_for_request.append(
            {
                "remove": False,
                "mealDate": str(meal_date),
                "mealNumber": _safe_int(meal_number),
                "packId": _safe_int(new_pack_id),
            }
        )

    return {
        "order_id_H": order_id,
        "mealDate": str(meal_date),
        "custom_packs": custom_packs_for_request,
        "mealNumber": _safe_int(meal_number),
        "major_pack_id": default_pack_id,
    }


def build_add_custom_menu_payload_batch(
    full_data: JsonDict,
    meal_date: str,
    decisions: List[JsonDict],
) -> JsonDict:
    order = full_data.get("order") or {}
    order_id = order.get("id_H")
    current_custom_config = _flatten_custom_config(order.get("custom_config"))
    default_menu = full_data.get("customization_menu") or {}

    target_meal_numbers = {_safe_int(d.get("mealNumber")) for d in decisions}
    custom_packs_for_request: List[JsonDict] = []
    for change in current_custom_config:
        if str(change.get("mealDate")) != str(meal_date):
            continue
        if _safe_int(change.get("mealNumber")) in target_meal_numbers:
            continue
        custom_packs_for_request.append(copy.deepcopy(change))

    default_day = default_menu.get(str(meal_date)) or {}
    default_slots = default_day.get("packs") or []
    current_compact = build_compact_menu(full_data)

    first_meal_number: Optional[int] = None
    first_default_pack_id: Optional[int] = None

    for decision in decisions:
        meal_number = _safe_int(decision.get("mealNumber"))
        new_pack_id = decision.get("packId")
        default_slot = next(
            (p for p in default_slots if _safe_int(p.get("position")) == _safe_int(meal_number)),
            None,
        )
        default_pack_id = None
        if default_slot and (default_slot.get("major") or []):
            default_pack_id = _safe_int((default_slot.get("major") or [None])[0])
        current_meal = _find_meal(current_compact, str(meal_date), meal_number)
        current_pack_id = _safe_int(current_meal.get("packId")) if current_meal else None
        if first_meal_number is None:
            first_meal_number = meal_number
            first_default_pack_id = default_pack_id

        remove_pack_ids: List[int] = []
        if current_pack_id is not None:
            remove_pack_ids.append(current_pack_id)
        if default_pack_id is not None and default_pack_id not in remove_pack_ids:
            remove_pack_ids.append(default_pack_id)

        for remove_pack_id in remove_pack_ids:
            custom_packs_for_request.append(
                {
                    "remove": True,
                    "mealDate": str(meal_date),
                    "mealNumber": str(_safe_int(meal_number)),
                    "type": "major",
                    "packId": remove_pack_id,
                    "count": -1,
                }
            )

        if new_pack_id is not None:
            custom_packs_for_request.append(
                {
                    "remove": False,
                    "mealDate": str(meal_date),
                    "mealNumber": _safe_int(meal_number),
                    "packId": _safe_int(new_pack_id),
                }
            )

    return {
        "order_id_H": order_id,
        "mealDate": str(meal_date),
        "custom_packs": custom_packs_for_request,
        "mealNumber": _safe_int(first_meal_number or 0),
        "major_pack_id": first_default_pack_id,
    }


def _apply_add_custom_menu_payload_to_full_data(full_data: JsonDict, payload: JsonDict) -> JsonDict:
    data = copy.deepcopy(full_data)
    order = data.get("order")
    if not isinstance(order, dict):
        order = {}
        data["order"] = order

    target_date = str(payload.get("mealDate"))
    existing = _flatten_custom_config(order.get("custom_config"))
    keep = [change for change in existing if str(change.get("mealDate")) != target_date]

    payload_changes = payload.get("custom_packs") or []
    if isinstance(payload_changes, list):
        for change in payload_changes:
            if isinstance(change, dict):
                keep.append(copy.deepcopy(change))

    order["custom_config"] = keep
    return data


def _flatten_custom_config(custom_config: Any) -> List[JsonDict]:
    if isinstance(custom_config, list):
        return [copy.deepcopy(x) for x in custom_config if isinstance(x, dict)]
    if isinstance(custom_config, dict):
        flat: List[JsonDict] = []
        for _, by_meal in custom_config.items():
            if not isinstance(by_meal, dict):
                continue
            for _, changes in by_meal.items():
                if isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict):
                            flat.append(copy.deepcopy(change))
        return flat
    return []


def _get_pack(packs: JsonDict, pack_id: Optional[int]) -> Optional[JsonDict]:
    if pack_id is None:
        return None
    if str(pack_id) in packs:
        return packs[str(pack_id)]
    if pack_id in packs:
        return packs[pack_id]
    return None


def _pack_recipe(pack: JsonDict) -> str:
    dishes = pack.get("dishes") or []
    parts: List[str] = []
    for dish in dishes:
        if not isinstance(dish, dict):
            continue
        ingredients = dish.get("ingredients")
        if ingredients is None:
            continue
        parts.append(str(ingredients).strip())
    return "\n".join(p for p in parts if p)


def _extract_protein_g(pack: JsonDict) -> Optional[float]:
    direct_keys = ["protein_g", "protein", "proteins"]
    for key in direct_keys:
        value = pack.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for nested_key in ["nutrition", "nutrients", "macros"]:
        nested = pack.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in ["protein_g", "protein", "proteins", "b", "proteinGrams"]:
            value = nested.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_success_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return bool(response.get("success"))


def _print_json(data: JsonDict) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _progress(message: str) -> None:
    if str(os.environ.get("GROWFOOD_PROGRESS", "1")).lower() in {"0", "false", "no", "off"}:
        return
    sys.stderr.write(f"[growfood] {message}\n")
    sys.stderr.flush()



def _load_json_value_from_file_or_stdin(path: Optional[str], use_stdin: bool) -> Any:
    if use_stdin:
        text = sys.stdin.read()
        if not text.strip():
            raise ValueError("stdin is empty")
        return json.loads(text)
    if not path:
        raise ValueError("A JSON source is required: use --menu-file or --stdin")
    return json.loads(Path(path).read_text())


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be a positive integer")
    if isinstance(value, str) and not (value.strip().isascii() and value.strip().isdecimal()):
        raise ValueError(f"{field} must be a positive integer")
    number = int(value)
    if number <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return number


def _normalize_single_decision(raw: JsonDict) -> JsonDict:
    if not isinstance(raw, dict):
        raise ValueError("decision must be an object")
    meal_date = raw.get("mealDate")
    if not isinstance(meal_date, str) or not meal_date.strip():
        raise ValueError("mealDate is required")
    meal_number = _positive_int(raw.get("mealNumber"), "mealNumber")
    remove = raw.get("remove", False)
    if not isinstance(remove, bool):
        raise ValueError("remove must be a boolean")
    if "packId" not in raw and not remove:
        raise ValueError("packId or remove=true is required")
    pack_id = None if remove else raw.get("packId")
    if pack_id is not None:
        pack_id = _positive_int(pack_id, "packId")
    return {
        "mealDate": meal_date,
        "mealNumber": meal_number,
        "packId": pack_id,
        "reason": str(raw.get("reason", "")),
        "newName": (None if raw.get("newName") is None else str(raw.get("newName"))),
    }


def _normalize_execute_plan_payload(data: Any) -> JsonDict:
    if isinstance(data, list):
        return {"order_id_H": None, "decisions": [dict(x) for x in data]}
    if not isinstance(data, dict):
        raise ValueError("plan payload must be an object or a decisions array")
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("plan payload must include a decisions array")
    return {
        "order_id_H": data.get("order_id_H") or data.get("orderId_H") or data.get("orderId"),
        "decisions": [dict(x) for x in decisions],
    }


def _find_meal(compact_menu: JsonDict, meal_date: str, meal_number: int) -> Optional[JsonDict]:
    day = (compact_menu.get("dates") or {}).get(str(meal_date))
    if not isinstance(day, dict):
        return None
    for meal in day.get("meals") or []:
        if _safe_int(meal.get("mealNumber")) == _safe_int(meal_number):
            return meal
    return None


def _resolve_name_from_pack_id(current_meal: JsonDict, pack_id: Optional[int]) -> Optional[str]:
    if pack_id is None:
        return None
    if pack_id == current_meal.get("packId"):
        return current_meal.get("name")
    for alt in current_meal.get("alternatives") or []:
        if alt.get("packId") == pack_id:
            return alt.get("name")
    return None


def _normalize_target_menu_payload(data: Any) -> JsonDict:
    if not isinstance(data, dict):
        raise ValueError("target menu payload must be an object")

    order_id = data.get("order_id_H") or data.get("orderId_H") or data.get("orderId")
    decisions: List[JsonDict] = []

    if "days" in data:
        days = data["days"]
        if not isinstance(days, dict):
            raise ValueError("days must be an object")
        for meal_date, slots in days.items():
            if not isinstance(slots, dict):
                raise ValueError(f"day payload must be an object for {meal_date}")
            for meal_number, slot in slots.items():
                if not isinstance(slot, dict):
                    raise ValueError(f"slot payload must be an object for {meal_date}/{meal_number}")
                decisions.append(_normalize_single_decision({
                    **slot,
                    "mealDate": meal_date,
                    "mealNumber": meal_number,
                    "reason": slot.get("reason", "Synced from compact target menu payload"),
                    "newName": slot.get("name"),
                }))
    else:
        dates = data.get("dates")
        if not isinstance(dates, dict):
            raise ValueError("target menu payload must include days or dates object")
        for meal_date, day in dates.items():
            if not isinstance(day, dict):
                raise ValueError(f"day payload must be an object for {meal_date}")
            meals = day.get("meals")
            if not isinstance(meals, list):
                raise ValueError(f"meals must be an array for {meal_date}")
            for meal in meals:
                if not isinstance(meal, dict):
                    raise ValueError(f"meal payload must be an object for {meal_date}")
                decisions.append(_normalize_single_decision({
                    **meal,
                    "mealDate": meal_date,
                    "reason": meal.get("reason", "Synced from target menu snapshot"),
                    "newName": meal.get("name"),
                }))

    return {
        "order_id_H": (None if order_id is None else str(order_id)),
        "decisions": decisions,
    }


def build_target_menu_snapshot(decisions: List[JsonDict]) -> JsonDict:
    snapshot: JsonDict = {}
    for decision in decisions:
        meal_date = str(decision.get("mealDate") or "")
        meal_number = str(_safe_int(decision.get("mealNumber")))
        if not meal_date or meal_number == "0":
            continue
        day = snapshot.setdefault(meal_date, {})
        if isinstance(day, dict):
            pack_id = decision.get("packId")
            day[meal_number] = (None if pack_id is None else _safe_int(pack_id))
    return snapshot


def compare_target_menu_snapshot(
    compact_menu: JsonDict, target_snapshot: JsonDict, require_complete_dates: bool = False
) -> List[str]:
    mismatches: List[str] = []
    dates = (compact_menu.get("dates") or {}) if isinstance(compact_menu, dict) else {}

    for meal_date, slots in sorted((target_snapshot or {}).items()):
        if not isinstance(slots, dict):
            continue
        day = dates.get(str(meal_date)) if isinstance(dates, dict) else None
        meals = {}
        if isinstance(day, dict):
            for meal in day.get("meals") or []:
                if isinstance(meal, dict):
                    meals[str(_safe_int(meal.get("mealNumber")))] = meal

        if require_complete_dates:
            for meal_number, meal in meals.items():
                if meal_number not in slots and meal.get("packId") is not None:
                    mismatches.append(f"{meal_date} слот {meal_number}: unexpected occupied slot outside target.")

        for meal_number, expected_pack_id in sorted(slots.items(), key=lambda item: _safe_int(item[0])):
            meal = meals.get(str(meal_number))
            if not isinstance(meal, dict):
                mismatches.append(
                    f"{meal_date} слот {meal_number}: слот не найден при пост-проверке."
                )
                continue

            actual_pack_id = meal.get("packId")
            if expected_pack_id is None:
                if actual_pack_id is not None or not bool(meal.get("removed")):
                    mismatches.append(
                        f"{meal_date} слот {meal_number}: ожидалось удаление, фактически packId={actual_pack_id}."
                    )
                continue

            if _safe_int(actual_pack_id) != _safe_int(expected_pack_id):
                actual_name = str(meal.get("name") or "").strip()
                actual_label = f"packId={actual_pack_id}" if actual_pack_id is not None else "без блюда"
                if actual_name:
                    actual_label += f" ({actual_name})"
                mismatches.append(
                    f"{meal_date} слот {meal_number}: ожидался packId={expected_pack_id}, фактически {actual_label}."
                )

            if len(mismatches) >= 50:
                return mismatches

    return mismatches
