# Automatic checks

During first-time setup, initialize private preferences and install one recurring OpenClaw cron job.

1. Run `growfood_setup.py cron-spec`.
2. Pass the returned JSON object to the cron tool's `add` action unchanged.
3. The declaration key `growfood-menu.autopilot.v1` is the stable identity. Re-running setup reconciles that declaration instead of creating a duplicate.
4. The default schedule is daily at 12:00 in the Gateway host timezone. Pass `--tz <IANA timezone>` only when the user's timezone is known and different.
5. The isolated job announces only a completed menu, an authentication request, or a failure. `NO_REPLY` suppresses no-change runs.
6. Do not install a second job with another name or declaration key.

Before adding a job on an existing installation, account for an already-operational legacy scheduler. Do not run two GrowFood automations in parallel. Either keep the working legacy scheduler and record setup as satisfied, or migrate it deliberately after verifying the replacement.
