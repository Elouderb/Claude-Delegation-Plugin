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

The `browser_*` tools come from whichever playwright MCP server is configured —
the official plugin (`mcp__plugin_playwright_playwright__*`) or a self-defined
chromium-backed server (`mcp__playwright__*`). Use whichever is available; the
tool suffixes are identical.

## Fallback — if the browser won't launch

If `browser_navigate` fails because no browser can be launched (e.g. the
playwright "chrome" channel is missing — `/opt/google/chrome/chrome` absent — and
no chromium-backed server is configured), do **NOT** silently downgrade to
`curl` + HTML/DOM string-matching and call it "verified": that checks markup, not
runtime behavior (clicks, polling, JS state). Instead drive the **bundled
Playwright chromium** directly via Bash, and say explicitly that you used this
fallback:

- Binary: `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome` (version
  bumps over time — glob it).
- Render / screenshot / DOM checks: `chrome --headless --no-sandbox --disable-gpu
  --dump-dom <url>` (or `--screenshot=<path>`); add `--virtual-time-budget=<ms>`
  so `setInterval`/poll-driven UIs render before the capture.
- Clicks / interaction / "does state survive a poll": launch with
  `--remote-debugging-port=<port> --remote-allow-origins=*`, read the page
  target's `webSocketDebuggerUrl` from `http://127.0.0.1:<port>/json`, and drive
  it over CDP with `Runtime.evaluate` (the `websocket-client` python pkg is
  available). This is how interactive board behavior gets verified when the MCP
  can't launch.

Always test on a **throwaway server on a free port** — never disturb a
long-running graph/preview server (e.g. :5000).
