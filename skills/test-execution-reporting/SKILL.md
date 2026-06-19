---
name: test-execution-reporting
description: Preload for the test-engineer agent; use to execute tests, isolate failures, and report reproducible results without hiding uncertainty.
---

# Test Execution and Reporting

Run planned checks with explicit commands. Capture command, status, failing tests, relevant output, whether failures are new or pre-existing, and coverage gaps.

Trace failures to likely symbols or DB objects with `code_find_callers`, `code_impact_analysis`, and (for persistence) `db_get_routine_dependencies`, but do not change production architecture unless assigned. Record results with `add_comment`. Return `PASS`, `FAIL`, or `BLOCKED` with reproducible next steps.
