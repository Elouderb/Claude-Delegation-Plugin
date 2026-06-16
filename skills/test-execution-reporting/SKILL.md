---
name: test-execution-reporting
description: Preload for the test-engineer agent; use to execute tests, isolate failures, and report reproducible results without hiding uncertainty.
---

# Test Execution and Reporting

Run planned checks with explicit commands. Capture command, status, failing tests, relevant output, whether failures are new or pre-existing, and coverage gaps.

Use graph tools to trace failures to likely symbols or DB objects, but do not change production architecture unless assigned. Record results with `add_comment`. Return `PASS`, `FAIL`, or `BLOCKED` with reproducible next steps.
