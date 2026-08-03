# Preference profiles

User tastes are runtime data, not skill logic.

Resolution order:

1. `--preferences-file`
2. `GROWFOOD_PREFERENCE_PROFILE`
3. `GROWFOOD_PREFERENCES_FILE`
4. `~/.config/growfood-menu/preferences.json`
5. bundled neutral `examples/preference-profile.json`

For a personal setup, copy the example to `~/.config/growfood-menu/preferences.json`, keep it outside the skill directory, and edit only that private file.

Required top-level keys:

- `profile_name`
- `selection_goal`
- `planning_rules`
- `hard_constraints`
- `soft_preferences`
- `rotation_preferences`
- `planner_notes`

Never publish the runtime profile unless the user explicitly wants to share their tastes.
