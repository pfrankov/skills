# Automatic checks

During first-time setup, initialize private preferences and install one recurring OpenClaw cron job.

1. Run `growfood_setup.py cron-spec`.
2. Pass the returned JSON object to the cron tool's `add` action unchanged.
3. The declaration key `growfood-menu.autopilot.v1` is the stable identity. Re-running setup reconciles that declaration instead of creating a duplicate.
4. The default schedule is daily at 12:00 in the Gateway host timezone. Pass `--tz <IANA timezone>` only when the user's timezone is known and different.
5. The isolated job announces only a completed menu, an authentication request, or a failure. `NO_REPLY` suppresses no-change runs.
6. Deliver success as one human sentence: `👨‍🍳 Меню на <даты> готово и сохранено.` If dates are unavailable, use `👨‍🍳 Новое меню готово и сохранено.`
7. Never deliver order IDs, profile details, schema/contract names, plan payloads, reasons, action counts, raw JSON, or `GF_AUTOMATION_RESULT`. If a wrapper uses a machine result, it must parse and strip it before sending the separate human notification.
8. Do not install a second job with another name or declaration key.

Before adding a job on an existing installation, account for an already-operational legacy scheduler. Do not run two GrowFood automations in parallel. Either keep the working legacy scheduler and record setup as satisfied, or migrate it deliberately after verifying the replacement.
## Draft ID idempotency

The automation wrapper must persist successful draft IDs in `state/growfood_draft_autopilot_state.json` under `processed_orders`. Before invoking the agent or saving a menu, compare the exact `order_id_H` with that map. If the ID is present, stop with a silent no-op. Do not use menu fingerprints or payload changes to reclassify a recorded ID as new. Add and persist the ID only after save, exact verification, and draft acceptance succeed, but before attempting the external notification. A notification failure must not make that ID eligible for processing again; failed menu-processing attempts remain retryable.
