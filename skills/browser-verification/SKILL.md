---
name: browser-verification
description: Preload for the frontend-engineer and verification-engineer agents; drive the UI in a real browser via playwright to confirm a change actually works.
---

# Browser Verification (playwright)

Confirm UI behavior in a real browser instead of assuming the change works.

1. `browser_navigate` to the running app — start it first via Bash or the `run` skill if needed.
2. `browser_snapshot` for the accessibility tree (prefer it over screenshots for locating elements); `browser_take_screenshot` when a visual record matters.
3. Exercise the change: `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_hover`, `browser_press_key`, then `browser_wait_for` the expected state.
4. Check for hidden failures: `browser_console_messages` for JS errors and `browser_network_requests` for failed / 4xx / 5xx calls.
5. Report what you did, what you observed, and a screenshot or snapshot as evidence.

Drive only the case under test; do not wander the app. Leave the running server and browser cleaned up when done.
