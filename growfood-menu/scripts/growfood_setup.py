#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

sys.dont_write_bytecode = True

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLE = SKILL_DIR / "examples" / "preference-profile.json"
DEFAULT_PREFERENCES = Path("~/.config/growfood-menu/preferences.json").expanduser()
DECLARATION_KEY = "growfood-menu.autopilot.v1"


def initialize_preferences(
    preferences_file: Optional[str] = None,
    example_file: Optional[str] = None,
) -> Dict[str, Any]:
    target = Path(preferences_file).expanduser().resolve() if preferences_file else DEFAULT_PREFERENCES.resolve()
    source = Path(example_file).expanduser().resolve() if example_file else DEFAULT_EXAMPLE.resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("bundled preference profile must be a JSON object")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    if target.exists():
        os.chmod(target, 0o600)
        return {"status": "ready", "created": False, "preferences_file": str(target)}

    fd, tmp_name = tempfile.mkstemp(prefix=".preferences.", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, target)
            created = True
        except FileExistsError:
            created = False
        os.chmod(target, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return {"status": "ready", "created": created, "preferences_file": str(target)}


def build_cron_spec(timezone_name: Optional[str] = None) -> Dict[str, Any]:
    schedule: Dict[str, Any] = {"kind": "cron", "expr": "0 12 * * *"}
    if timezone_name:
        schedule["tz"] = timezone_name
    prompt = (
        "Use the growfood-menu skill to check the newest editable GrowFood menu. "
        "If there is no new menu, respond exactly NO_REPLY. "
        "If a menu is available, select dishes using the private preferences, save and verify every change, "
        "then reply in the user's language with one short human message: "
        "'Меню на <даты> готово. Блюда выбраны по твоим предпочтениям, изменения сохранены.' "
        "If authentication is missing or returns HTTP 401, reply only: "
        "'Нужно снова войти в GrowFood. Пришли номер телефона — затем я попрошу код из SMS.' "
        "For any other failure, explain it in one short human sentence. "
        "Never expose credentials or implementation details."
    )
    return {
        "name": "GrowFood: automatic menu",
        "displayName": "GrowFood automatic menu",
        "description": "Daily check and verified processing of new GrowFood menus.",
        "declarationKey": DECLARATION_KEY,
        "schedule": schedule,
        "payload": {
            "kind": "agentTurn",
            "message": prompt,
            "thinking": "high",
            "timeoutSeconds": 1200,
        },
        "sessionTarget": "isolated",
        "delivery": {"mode": "announce", "channel": "last", "bestEffort": True},
        "enabled": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize GrowFood private setup and automation")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init-preferences")
    init_parser.add_argument("--preferences-file")
    init_parser.add_argument("--example-file")

    cron_parser = sub.add_parser("cron-spec")
    cron_parser.add_argument("--tz")

    args = parser.parse_args()
    try:
        if args.command == "init-preferences":
            result = initialize_preferences(args.preferences_file, args.example_file)
        else:
            result = build_cron_spec(args.tz)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
