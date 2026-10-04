#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

# Avoid generating __pycache__ noise during cron/automation runs.
sys.dont_write_bytecode = True

from growfood_auth import resolve_client_token
from growfood_menu_core import (
    _apply_add_custom_menu_payload_to_full_data,
    _find_meal,
    _is_success_response,
    _load_json_value_from_file_or_stdin,
    _normalize_single_decision,
    _normalize_target_menu_payload,
    _print_json,
    _progress,
    _resolve_name_from_pack_id,
    _safe_float,
    _safe_int,
    build_add_custom_menu_payload_batch,
    build_compact_menu,
    build_target_menu_snapshot,
    compare_target_menu_snapshot,
    build_planning_context,
    load_preference_profile,
    resolve_preference_profile_path,
)


JsonDict = Dict[str, Any]
TransportFn = Callable[[str, str, Optional[JsonDict], Optional[JsonDict], Optional[float]], JsonDict]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
LAST_SAVE_RESULT_FILE = WORKSPACE_ROOT / "tmp" / "growfood_last_save_result.json"


@dataclass(frozen=True)
class GrowFoodEndpoints:
    active_orders: str
    menu_full: str
    add_custom_menu: str
    accept_draft: str


class GrowFoodApiClient:
    def __init__(
        self,
        base_url: str,
        client_token: str,
        transport: Optional[TransportFn] = None,
        brand_id: str = "lY",
        endpoints: Optional[GrowFoodEndpoints] = None,
        timeout: float = 30.0,
        retries: int = 3,
        retry_sleep: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_token = client_token
        self.brand_id = brand_id
        self.endpoints = endpoints or GrowFoodEndpoints(
            active_orders=f"{self.base_url}/api/personal-cabinet/v2_0/orders/active",
            menu_full=f"{self.base_url}/api/personal-cabinet/v1_1/menu/full",
            add_custom_menu=f"{self.base_url}/api/personal-cabinet/v1_1/orders/add-custom-menu",
            accept_draft=f"{self.base_url}/api/personal-cabinet/v1/orders/accept-draft",
        )
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.retry_sleep = max(0.0, float(retry_sleep))
        self._transport = transport or self._urllib_transport

    def default_headers(self) -> JsonDict:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-client-token": self.client_token,
        }

    def get_active_orders(self) -> JsonDict:
        query = urlencode({"brandId_H": self.brand_id})
        url = f"{self.endpoints.active_orders}?{query}"
        return self._request_json("GET", url)

    def find_new_draft_order(self) -> Optional[JsonDict]:
        data = self.get_active_orders()
        orders = data.get("orders") or []
        for order in reversed(list(orders)):
            status = order.get("status") or {}
            code = str(status.get("code") or "").lower()
            name = str(status.get("name") or "").lower()
            if code in {"formed", "draft"} or name in {"formed", "draft"}:
                return order
        return None

    def get_full_menu(
        self,
        order_id_h: str,
        customizable: bool,
        is_draft: bool = True,
        show_first_custom_day: bool = True,
    ) -> JsonDict:
        body = {
            "showFirstCustomDay": bool(show_first_custom_day),
            "deliveryDate": None,
            "customizable": bool(customizable),
            "orderId_H": order_id_h,
            "page": 0,
            "withPagination": 1,
            "isDraft": bool(is_draft),
        }
        return self._request_json("POST", self.endpoints.menu_full, body)

    def add_custom_menu(self, payload: JsonDict) -> JsonDict:
        return self._request_json("POST", self.endpoints.add_custom_menu, payload)

    def accept_draft(self, order_id_h: str) -> JsonDict:
        return self._request_json("POST", self.endpoints.accept_draft, {"order_id_H": order_id_h})

    def _request_json(self, method: str, url: str, body: Optional[JsonDict] = None) -> JsonDict:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._transport(method, url, self.default_headers(), body, self.timeout)
            except Exception as exc:  # pragma: no cover
                last_exc = exc
                if attempt >= self.retries:
                    raise
                time.sleep(self.retry_sleep * attempt)
        raise last_exc or RuntimeError("GrowFood request failed")

    @staticmethod
    def _urllib_transport(
        method: str,
        url: str,
        headers: Optional[JsonDict] = None,
        body: Optional[JsonDict] = None,
        timeout: Optional[float] = None,
    ) -> JsonDict:
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = Request(url=url, data=data, method=method.upper())
        for key, value in (headers or {}).items():
            req.add_header(str(key), str(value))
        # Disable environment proxies to make local E2E tests deterministic and avoid
        # sending private API traffic to an accidental proxy configuration.
        opener = build_opener(ProxyHandler({}))
        with opener.open(req, timeout=timeout or 30.0) as resp:
            payload = resp.read()
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))


def _find_incomplete_target_dates(compact_menu: JsonDict, decisions: List[JsonDict]) -> List[JsonDict]:
    target_slots_by_date: Dict[str, set[int]] = {}
    for decision in decisions:
        meal_date = str(decision.get("mealDate") or "")
        meal_number = _safe_int(decision.get("mealNumber"))
        if meal_date and meal_number > 0:
            target_slots_by_date.setdefault(meal_date, set()).add(meal_number)

    incomplete: List[JsonDict] = []
    dates = compact_menu.get("dates") if isinstance(compact_menu, dict) else {}
    if not isinstance(dates, dict):
        dates = {}

    for meal_date, target_slots in sorted(target_slots_by_date.items()):
        day = dates.get(meal_date)
        meals = day.get("meals") if isinstance(day, dict) else []
        occupied_slots = {
            _safe_int(meal.get("mealNumber"))
            for meal in (meals or [])
            if isinstance(meal, dict)
            and _safe_int(meal.get("mealNumber")) > 0
            and meal.get("packId") is not None
        }
        missing_slots = sorted(occupied_slots - target_slots)
        if missing_slots:
            incomplete.append(
                {
                    "mealDate": meal_date,
                    "missingMealNumbers": [str(slot) for slot in missing_slots],
                }
            )
    return incomplete


class GrowFoodAutoMenuService:
    def __init__(self, client: GrowFoodApiClient) -> None:
        self.client = client

    def prepare_draft(self) -> JsonDict:
        order = self.client.find_new_draft_order()
        if not order:
            return {
                "status": "no_draft",
                "order_id_H": None,
                "compact_menu": None,
                "message": "No newly formed draft menu found",
            }
        order_id = str(order.get("id_H"))
        full = self.client.get_full_menu(order_id_h=order_id, customizable=True)
        return {
            "status": "ready",
            "order_id_H": order_id,
            "compact_menu": build_compact_menu(full),
        }

    def prepare_planning_context(
        self,
        preferences_file: Optional[str] = None,
        order_id_h: Optional[str] = None,
    ) -> JsonDict:
        order_id = order_id_h
        if not order_id:
            order = self.client.find_new_draft_order()
            if not order:
                return {
                    "status": "no_draft",
                    "order_id_H": None,
                    "compact_menu": None,
                    "message": "No newly formed draft menu found",
                }
            order_id = str(order.get("id_H"))

        full = self.client.get_full_menu(order_id_h=str(order_id), customizable=True)
        compact_menu = build_compact_menu(full)
        profile_path = resolve_preference_profile_path(preferences_file)
        preference_profile = load_preference_profile(str(profile_path))

        result = build_planning_context(
            compact_menu=compact_menu,
            preference_profile=preference_profile,
            preference_profile_path=profile_path,
        )
        result["status"] = "ready"
        result["order_id_H"] = str(order_id)
        return result

    def execute_plan(
        self,
        decisions: List[JsonDict],
        order_id_h: Optional[str] = None,
        finalize: bool = True,
        require_complete_dates: bool = False,
    ) -> JsonDict:
        summary: JsonDict = {
            "status": "unknown",
            "order_id_H": None,
            "applied": [],
            "skipped": [],
            "issues": [],
            "accepted": False,
        }

        if not order_id_h:
            order = self.client.find_new_draft_order()
            if not order:
                summary["status"] = "no_draft"
                return summary
            order_id_h = str(order.get("id_H"))

        summary["order_id_H"] = order_id_h
        normalized_decisions: List[JsonDict] = []
        seen_slots = set()
        for idx, raw_decision in enumerate(decisions):
            try:
                decision = _normalize_single_decision(raw_decision)
                slot = (decision["mealDate"], decision["mealNumber"])
                if slot in seen_slots:
                    raise ValueError("duplicate date/meal slot")
                seen_slots.add(slot)
                normalized_decisions.append(decision)
            except Exception as exc:
                summary["issues"].append({"index": idx, "error": f"Invalid decision: {exc}"})
                break

        if not summary["issues"] and normalized_decisions:
            try:
                _progress("Loading draft menu")
                working_full = self.client.get_full_menu(order_id_h=order_id_h, customizable=False)
                working_compact = build_compact_menu(working_full)
            except Exception as exc:  # pragma: no cover
                summary["issues"].append({"error": str(exc)})
                working_full = None
                working_compact = None

            if working_full is not None and working_compact is not None:
                decisions_by_date: Dict[str, List[tuple[int, JsonDict]]] = {}
                for idx, decision in enumerate(normalized_decisions):
                    decisions_by_date.setdefault(decision["mealDate"], []).append((idx, decision))

                editable_by_date = {
                    str(date): bool(day.get("editable", True))
                    for date, day in (working_compact.get("dates") or {}).items()
                    if isinstance(day, dict)
                }

                if require_complete_dates:
                    incomplete_dates = _find_incomplete_target_dates(
                        working_compact,
                        normalized_decisions,
                    )
                    for incomplete in incomplete_dates:
                        summary["issues"].append(
                            {
                                "mealDate": incomplete["mealDate"],
                                "missingMealNumbers": incomplete["missingMealNumbers"],
                                "error": (
                                    "incomplete target date; include every currently occupied slot "
                                    "and set unwanted slots to packId=null"
                                ),
                            }
                        )

                changes_by_date: Dict[str, List[JsonDict]] = {}
                for meal_date, date_decisions in sorted(decisions_by_date.items()):
                    if summary["issues"]:
                        break
                    try:
                        if not editable_by_date.get(meal_date, False):
                            summary["issues"].append(
                                {
                                    "mealDate": meal_date,
                                    "error": "date is not currently editable; preserve existing saved menu",
                                }
                            )
                            break
                        # IMPORTANT: keep one slot change per request (API ignores multi-slot batches).
                        per_date_changes: List[JsonDict] = []
                        for idx, decision in date_decisions:
                            meal_number = decision["mealNumber"]
                            new_pack_id = decision["packId"]

                            current_meal = _find_meal(working_compact, meal_date, meal_number)
                            if current_meal is None:
                                summary["issues"].append(
                                    {
                                        "index": idx,
                                        "mealDate": meal_date,
                                        "mealNumber": str(meal_number),
                                        "error": "Meal slot not found in compact menu",
                                    }
                                )
                                break

                            if new_pack_id == current_meal.get("packId"):
                                summary["skipped"].append(
                                    {
                                        "mealDate": meal_date,
                                        "mealNumber": str(meal_number),
                                        "reason": decision.get("reason") or "No-op decision",
                                    }
                                )
                                continue

                            if new_pack_id is not None:
                                valid_pack_ids = {current_meal.get("packId")}
                                valid_pack_ids.update(alt.get("packId") for alt in current_meal.get("alternatives", []))
                                if new_pack_id not in valid_pack_ids:
                                    summary["issues"].append(
                                        {
                                            "index": idx,
                                            "mealDate": meal_date,
                                            "mealNumber": str(meal_number),
                                            "error": f"packId {new_pack_id} is not available in this slot",
                                        }
                                    )
                                    break

                            per_date_changes.append(
                                {
                                    "meal": current_meal,
                                    "decision": decision,
                                }
                            )

                        if summary["issues"]:
                            break
                        changes_by_date[meal_date] = per_date_changes
                    except Exception as exc:
                        summary["issues"].append({"mealDate": meal_date, "error": str(exc)})
                        break

                batch_sleep = _safe_float(os.environ.get("GROWFOOD_HTTP_BATCH_SLEEP", "1"))
                for date_index, (meal_date, per_date_changes) in enumerate(changes_by_date.items()):
                    if summary["issues"]:
                        break
                    try:
                        for change_index, item in enumerate(per_date_changes):
                            current_meal = item["meal"]
                            decision = item["decision"]
                            decisions_payload = [
                                {
                                    "mealNumber": _safe_int(decision.get("mealNumber")),
                                    "packId": decision.get("packId"),
                                }
                            ]
                            payload = build_add_custom_menu_payload_batch(
                                full_data=working_full,
                                meal_date=meal_date,
                                decisions=decisions_payload,
                            )
                            _progress(
                                f"Saving {meal_date} ({date_index + 1}/{len(decisions_by_date)})"
                            )
                            response = self.client.add_custom_menu(payload)
                            if not _is_success_response(response):
                                summary["issues"].append(
                                    {
                                        "mealDate": meal_date,
                                        "error": "add-custom-menu returned success=false",
                                    }
                                )
                                break
                            _progress(f"Saved {meal_date}")

                            draft_info = (response.get("draft") or {}) if isinstance(response, dict) else {}
                            draft_accept = (draft_info.get("draft_accept") or {}) if isinstance(response, dict) else {}

                            new_pack_id = decision.get("packId")
                            summary["applied"].append(
                                {
                                    "mealDate": meal_date,
                                    "mealNumber": str(decision.get("mealNumber")),
                                    "oldPackId": current_meal.get("packId"),
                                    "oldName": current_meal.get("name"),
                                    "newPackId": new_pack_id,
                                    "newName": decision.get("newName")
                                    or _resolve_name_from_pack_id(current_meal, new_pack_id),
                                    "action": "remove" if new_pack_id is None else "replace",
                                    "reason": decision.get("reason", ""),
                                    "saveDraft": {
                                        "success": bool((response or {}).get("success"))
                                        if isinstance(response, dict)
                                        else True,
                                        "confirmCustomizationUntil": (response or {}).get("confirmCustomizationUntil")
                                        if isinstance(response, dict)
                                        else None,
                                        "draftAcceptShow": draft_accept.get("show"),
                                        "draftAcceptShowReset": draft_accept.get("showReset"),
                                    },
                                }
                            )

                            working_full = _apply_add_custom_menu_payload_to_full_data(working_full, payload)
                            working_compact = build_compact_menu(working_full)

                            if batch_sleep > 0 and not (
                                date_index == len(decisions_by_date) - 1
                                and change_index == len(per_date_changes) - 1
                            ):
                                time.sleep(batch_sleep)

                        if summary["issues"]:
                            break
                    except Exception as exc:  # pragma: no cover
                        summary["issues"].append(
                            {
                                "mealDate": meal_date,
                                "error": str(exc),
                            }
                        )
                        break

        verification = {
            "checked": False,
            "status": "skipped",
            "target": build_target_menu_snapshot(normalized_decisions),
            "mismatches": [],
            "error": "",
        }
        if not summary["issues"] and normalized_decisions:
            verification = self._verify_saved_state(
                order_id_h=str(summary.get("order_id_H") or order_id_h or ""),
                decisions=normalized_decisions,
                require_complete_dates=require_complete_dates,
            )
        if finalize and not summary["issues"] and verification.get("status") == "passed":
            try:
                _progress("Accepting draft")
                accept_response = self.client.accept_draft(order_id_h)
                if _is_success_response(accept_response):
                    summary["accepted"] = True
                    _progress("Draft accepted")
                    verification = self._verify_saved_state(
                        order_id_h=str(order_id_h),
                        decisions=normalized_decisions,
                        require_complete_dates=require_complete_dates,
                    )
                else:
                    summary["issues"].append({"error": "accept-draft returned success=false"})
            except Exception as exc:
                summary["issues"].append({"error": f"accept-draft failed: {exc}"})

        summary["verification"] = verification
        if verification.get("status") == "error":
            summary["issues"].append(
                {"error": "post-save verification failed", "details": [verification.get("error") or "unknown verification error"]}
            )
        elif verification.get("mismatches"):
            summary["issues"].append(
                {
                    "error": "saved menu differs from target plan",
                    "details": list(verification.get("mismatches") or [])[:5],
                }
            )

        if summary["issues"]:
            summary["status"] = "partial_failure" if summary["applied"] else "error"
        elif summary["applied"]:
            summary["status"] = "updated" if summary["accepted"] else "updated_draft_only"
        else:
            summary["status"] = "no_changes"

        if verification.get("status") == "mismatch":
            summary["status"] = "verification_failed"
        elif verification.get("status") == "error":
            summary["status"] = "verification_error"

        _write_last_save_result(summary)

        return summary

    def _verify_saved_state(
        self, order_id_h: str, decisions: List[JsonDict], require_complete_dates: bool = False
    ) -> JsonDict:
        result: JsonDict = {
            "checked": False,
            "status": "skipped",
            "target": build_target_menu_snapshot(decisions),
            "mismatches": [],
            "error": "",
        }

        if not order_id_h or not decisions:
            return result

        try:
            full_menu = self.client.get_full_menu(order_id_h=str(order_id_h), customizable=True)
            compact_menu = build_compact_menu(full_menu)
            mismatches = compare_target_menu_snapshot(
                compact_menu, result["target"], require_complete_dates=require_complete_dates
            )
            result["checked"] = True
            result["status"] = "mismatch" if mismatches else "passed"
            result["mismatches"] = mismatches
            return result
        except Exception as exc:  # pragma: no cover
            result["checked"] = True
            result["status"] = "error"
            result["error"] = str(exc)
            return result


def _write_last_save_result(summary: JsonDict) -> None:
    try:
        LAST_SAVE_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SAVE_RESULT_FILE.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass



def _build_client_from_args(args: argparse.Namespace) -> GrowFoodApiClient:
    token = resolve_client_token(
        explicit_token=args.token,
        auth_file=getattr(args, "auth_file", None),
    )

    base_url = args.base_url or "https://admin.growfood.pro"
    brand_id = args.brand_id or "lY"

    timeout = _safe_float(os.environ.get("GROWFOOD_HTTP_TIMEOUT", "45"))
    if timeout <= 0:
        timeout = 45.0
    retries = _safe_int(os.environ.get("GROWFOOD_HTTP_RETRIES", "3"))
    if retries <= 0:
        retries = 3
    retry_sleep = _safe_float(os.environ.get("GROWFOOD_HTTP_RETRY_SLEEP", "2"))
    if retry_sleep < 0:
        retry_sleep = 0.0

    # Local mock transport mode for offline testing / automation.
    local_mock = os.environ.get("GROWFOOD_LOCAL_MOCK")
    if local_mock:
        def _local_transport(method: str, url: str, headers: Optional[JsonDict] = None, body: Optional[JsonDict] = None, timeout: Optional[float] = None) -> JsonDict:
            try:
                with open(local_mock, "r", encoding="utf-8") as f:
                    full_data = json.load(f)
            except Exception:
                full_data = {}

            # menu/full
            if "/menu/full" in url:
                return full_data

            # add-custom-menu
            if "/orders/add-custom-menu" in url:
                try:
                    tmp_root = Path(os.environ.get("GROWFOOD_TMP_DIR", str(WORKSPACE_ROOT / "tmp"))).expanduser()
                    log_path = tmp_root / "last_add_payloads.json"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as lf:
                        lf.write(json.dumps({"url": url, "body": body}, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                return {"success": True, "draft": {"draft_accept": {"show": False}, "confirmCustomizationUntil": None}}

            # accept-draft
            if "/orders/accept-draft" in url:
                return {"success": True}

            # active orders
            if "/orders/active" in url:
                order_id = None
                try:
                    order_id = (full_data.get("order") or {}).get("id_H")
                except Exception:
                    order_id = None
                order_id = order_id or os.environ.get("GROWFOOD_LOCAL_MOCK_ORDER_ID") or ""
                return {"orders": [{"id_H": order_id, "status": {"code": "draft", "name": "draft"}}]}

            return {}

        return GrowFoodApiClient(
            base_url=base_url,
            client_token=token,
            transport=_local_transport,
            brand_id=brand_id,
            timeout=timeout,
            retries=retries,
            retry_sleep=retry_sleep,
        )

    return GrowFoodApiClient(
        base_url=base_url,
        client_token=token,
        brand_id=brand_id,
        timeout=timeout,
        retries=retries,
        retry_sleep=retry_sleep,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="GrowFood menu sync helper (API + planning context)")
    parser.add_argument("--token", help="Explicit GrowFood x-client-token (highest priority; avoid shell history)")
    parser.add_argument("--auth-file", help="Path to auth JSON; defaults to GROWFOOD_AUTH_FILE or ~/.config/growfood-menu/auth.json")
    parser.add_argument("--base-url", help="GrowFood admin API base URL")
    parser.add_argument("--brand-id", help="GrowFood brandId_H for active orders")

    sub = parser.add_subparsers(dest="command")

    get_menu = sub.add_parser("get-menu", help="External method #1: return current compact draft menu")
    get_menu.add_argument("--order-id", help="Order id_H. If omitted, auto-detect draft order")

    get_planning_context = sub.add_parser(
        "get-planning-context",
        help="External method #2: return compact draft menu plus preference profile and decision contract",
    )
    get_planning_context.add_argument("--order-id", help="Order id_H. If omitted, auto-detect draft order")
    get_planning_context.add_argument("--preferences-file", help="Path to preference profile JSON override")

    save_menu = sub.add_parser(
        "save-menu",
        help="External method #3: save target menu snapshot produced by the agent",
    )
    save_menu.add_argument("--order-id", help="Order id_H override")
    save_menu.add_argument("--menu-file", help="Path to target compact menu JSON")
    save_menu.add_argument("--stdin", action="store_true", help="Read target compact menu JSON from stdin")
    save_menu.add_argument("--no-finalize", action="store_true", help="Skip final accept-draft call")

    args = parser.parse_args(argv)
    if not args.command:
        args.command = "get-planning-context"

    client = _build_client_from_args(args)
    service = GrowFoodAutoMenuService(client=client)

    if args.command == "get-menu":
        if args.order_id:
            full = client.get_full_menu(order_id_h=str(args.order_id), customizable=True)
            _print_json({"status": "ready", "order_id_H": str(args.order_id), "compact_menu": build_compact_menu(full)})
            return 0
        _print_json(service.prepare_draft())
        return 0

    if args.command == "get-planning-context":
        result = service.prepare_planning_context(
            preferences_file=getattr(args, "preferences_file", None),
            order_id_h=getattr(args, "order_id", None),
        )
        _print_json(result)
        return 0

    if args.command == "save-menu":
        payload_raw = _load_json_value_from_file_or_stdin(args.menu_file, bool(args.stdin))
        target = _normalize_target_menu_payload(payload_raw)
        if not target["decisions"]:
            _print_json(
                {
                    "status": "no_changes",
                    "message": "Target menu has no meals to sync",
                    "applied": [],
                    "skipped": [],
                    "accepted": False,
                    "issues": [],
                }
            )
            return 0
        result = service.execute_plan(
            decisions=target["decisions"],
            order_id_h=args.order_id or target.get("order_id_H"),
            finalize=not bool(args.no_finalize),
            require_complete_dates=True,
        )
        _print_json(result)
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
