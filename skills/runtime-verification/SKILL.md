---
name: runtime-verification
description: Preload for the verification-engineer agent; run the actual application and observe its behavior to confirm a change works, beyond what the test suite covers.
---

# Runtime Verification

Tests passing is not the same as the app working. Run it and watch.

1. Use the `run` skill to launch the app the way this project runs it (CLI, server, TUI, browser-driven, or library), and the `verify` skill to confirm a specific change behaves as intended.
2. Drive the actual path the change affects — start the server, hit the endpoint, run the command, click through the flow.
3. Capture exact commands, output, status codes, and logs. Distinguish a real regression from environment / setup noise.
4. For a web UI, pair this with `agent-os:browser-verification`.

Return PASS / FAIL / BLOCKED with the exact reproduction steps and the evidence you observed. Do not edit code — report findings to the lead.
