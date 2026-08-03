# Authentication

The user never needs to extract or paste a GrowFood token.

## First login or HTTP 401

1. Ask only for the phone number.
2. After receiving it, ask the user to request the GrowFood SMS and send the 4-digit code. Keep this message short and do not explain internal authentication details.
3. Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 {baseDir}/scripts/growfood_auth.py login \
  --phone "+7..." --code "1234"
```

4. The helper exchanges the code, validates access, and atomically stores the token in `~/.config/growfood-menu/auth.json` with mode `0600`.
5. Verify without exposing the token:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 {baseDir}/scripts/growfood_auth.py status
```

If GrowFood requires an interactive browser check, let the user complete it on the official login page. Do not use CAPTCHA-solving services or mention this possibility before it actually occurs. The SMS code may be supplied to the agent; the token must never be printed, logged, committed, or sent in chat.

## Token precedence

1. Explicit `--token` for diagnostics only
2. Auth file: `--auth-file`, then `GROWFOOD_AUTH_FILE`, then `~/.config/growfood-menu/auth.json`
3. `GROWFOOD_CLIENT_TOKEN` environment fallback

A newly written auth file takes precedence over a stale environment token.
