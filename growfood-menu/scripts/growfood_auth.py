#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

sys.dont_write_bytecode = True

JsonDict = Dict[str, Any]
DEFAULT_AUTH_FILE = Path("~/.config/growfood-menu/auth.json").expanduser()
DEFAULT_BASE_URL = "https://admin.growfood.pro"
DEFAULT_BRAND_ID = "lY"
DEFAULT_CITY_ID = 1


def resolve_auth_file(auth_file: Optional[str] = None) -> Path:
    raw = auth_file or os.environ.get("GROWFOOD_AUTH_FILE")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_AUTH_FILE.resolve()


def read_auth_file(auth_file: Optional[str] = None) -> JsonDict:
    path = resolve_auth_file(auth_file)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid GrowFood auth file: {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def resolve_client_token(explicit_token: Optional[str] = None, auth_file: Optional[str] = None) -> str:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    stored = read_auth_file(auth_file)
    stored_token = stored.get("client_token")
    if isinstance(stored_token, str) and stored_token.strip():
        return stored_token.strip()

    env_token = os.environ.get("GROWFOOD_CLIENT_TOKEN", "").strip()
    if env_token:
        return env_token

    raise SystemExit(
        "GrowFood authentication is missing. Request an SMS at "
        "https://lk.growfood.pro/login, then run growfood_auth.py login "
        "--phone <phone> --code <4-digit-code>."
    )


def write_auth_file(client_token: str, auth_file: Optional[str] = None) -> Path:
    token = client_token.strip()
    if not token:
        raise ValueError("client token is empty")

    path = resolve_auth_file(auth_file)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": "growfood-auth.v1",
        "client_token": token,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    fd, tmp_name = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError("expected a Russian phone number with 10 digits after +7")
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _request_json(
    method: str,
    url: str,
    body: Optional[JsonDict] = None,
    headers: Optional[JsonDict] = None,
    timeout: float = 45.0,
) -> JsonDict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(url=url, data=data, method=method.upper())
    for key, value in {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://lk.growfood.pro",
        "referer": "https://lk.growfood.pro/login",
        **(headers or {}),
    }.items():
        request.add_header(str(key), str(value))
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _active_orders(base_url: str, brand_id: str, token: str) -> JsonDict:
    query = urlencode({"brandId_H": brand_id})
    return _request_json(
        "GET",
        f"{base_url.rstrip('/')}/api/personal-cabinet/v2_0/orders/active?{query}",
        headers={"x-client-token": token},
    )


def login(phone: str, code: str, auth_file: Optional[str], base_url: str, brand_id: str, city_id: int) -> JsonDict:
    normalized_phone = normalize_phone(phone)
    if not re.fullmatch(r"\d{4}", code or ""):
        raise ValueError("SMS code must contain exactly 4 digits")

    ga_value = f"GA1.2.{os.getpid()}.{int(time.time())}"
    efst_response = _request_json(
        "POST",
        f"{base_url.rstrip('/')}/api/personal-cabinet/v1/efst",
        {"ga": ga_value},
    )
    efst = str(efst_response.get("efst") or "").strip()
    if not efst:
        raise RuntimeError("GrowFood did not return an EFST value")

    login_response = _request_json(
        "POST",
        f"{base_url.rstrip('/')}/api/personal-cabinet/v1/authentication/login",
        {
            "phone": normalized_phone,
            "password": code,
            "brandId_H": brand_id,
            "cityId": int(city_id),
        },
        headers={"cookie": f"_efst={efst}; _ga={ga_value}"},
    )
    token = str(login_response.get("client_token") or "").strip()
    if not token:
        raise RuntimeError("GrowFood login succeeded without a client token")

    active = _active_orders(base_url, brand_id, token)
    orders = active.get("orders") or []
    path = write_auth_file(token, auth_file)
    return {
        "status": "success",
        "auth_file": str(path),
        "active_orders": len(orders) if isinstance(orders, list) else 0,
        "token_printed": False,
    }


def status(auth_file: Optional[str], base_url: str, brand_id: str) -> JsonDict:
    token = resolve_client_token(auth_file=auth_file)
    active = _active_orders(base_url, brand_id, token)
    orders = active.get("orders") or []
    return {
        "status": "authenticated",
        "auth_file": str(resolve_auth_file(auth_file)),
        "active_orders": len(orders) if isinstance(orders, list) else 0,
        "token_printed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GrowFood SMS login and secure token storage")
    parser.add_argument("--auth-file", help="Auth JSON path (default: ~/.config/growfood-menu/auth.json)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--brand-id", default=DEFAULT_BRAND_ID)
    sub = parser.add_subparsers(dest="command", required=True)

    login_parser = sub.add_parser("login", help="Exchange phone + SMS code for a token and store it securely")
    login_parser.add_argument("--phone", required=True)
    login_parser.add_argument("--code", required=True)
    login_parser.add_argument("--city-id", type=int, default=DEFAULT_CITY_ID)

    sub.add_parser("status", help="Validate the currently resolved token without printing it")
    args = parser.parse_args()

    try:
        if args.command == "login":
            result = login(
                phone=args.phone,
                code=args.code,
                auth_file=args.auth_file,
                base_url=args.base_url,
                brand_id=args.brand_id,
                city_id=args.city_id,
            )
        else:
            result = status(args.auth_file, args.base_url, args.brand_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except HTTPError as exc:
        print(json.dumps({"status": "failed", "error": f"GrowFood HTTP {exc.code}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
