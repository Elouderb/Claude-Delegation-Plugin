# Cross-machine sync test harness

`scripts/sync_harness.py` — one command to test cross-machine task sync
end-to-end from any machine (card `f20398be`, epic `78a6ac11`). It wraps the
existing sync building blocks — `mcp/task_inbox.py` (down), `outbox/drain.py`
(up), and `mcp/card_tools.py` — and adds the two pieces those didn't cover on
their own: creating a syncable card, and a local-vs-central status glance. No
sync logic is reimplemented; this is argument/env resolution and output
rendering only.

## Usage

Run as a **direct script**, not `-m` (matches every other script in this
directory — `scripts/` ships no `__init__.py`):

```bash
python scripts/sync_harness.py <subcommand> [options]
```

### Subcommands

| Subcommand | What it does |
|---|---|
| `new-card [--title T]` | Creates a local card (mints a `global_id`, queues its `task.created` outbox event) and prints its ids. |
| `up` | Drains the local outbox to the central API (`outbox.drain --once`). |
| `down --source-repository-id ID` | Pulls + applies central tasks from one source repository into the local card DB (`mcp/task_inbox.py --once`). |
| `status` | Local-vs-central glance: local cards (count + recent), central tasks, and registered machines. |
| `roundtrip [--title T]` | `new-card` → `up` → `status` in one shot; stops at the first failure. Confirms a freshly created card actually reaches the central API. |

Every subcommand accepts `--repo-root` (default: walk up from the current
directory to `.git`). `up`/`down`/`status`/`roundtrip` also accept
`--api-url`, `--api-key`, and `--timeout`. `down`/`roundtrip` also accept the
subcommand-specific options in the table above.

### Examples

```bash
# Create a card on this machine and confirm it reaches the central API.
python scripts/sync_harness.py roundtrip --title "sync smoke test"

# On a second machine/repo, pull that card down (repository_id from `status`
# or from the first machine's own new-card/up output via the API).
python scripts/sync_harness.py down --source-repository-id repo_XXXXXXXX

# Check where things stand without changing anything.
python scripts/sync_harness.py status
```

## Configuration

Same environment variable names/defaults as `outbox/drain.py` and
`mcp/task_inbox.py`, loaded via `python-dotenv` (a `.env` file at the repo
root works) with every value overridable by its matching flag:

| Variable | Default | Used by |
|---|---|---|
| `AGENT_OS_API_URL` | `http://127.0.0.1:8765` | `up`, `down`, `status`, `roundtrip` |
| `AGENT_OS_API_KEY` | *(none)* | `up`, `down`, `status`, `roundtrip` |
| `AGENT_OS_SOURCE_REPOSITORY_ID` | *(none — required)* | `down` |

## Notes

- Cross-platform by construction: everything runs in-process (no shelling
  out, no bash-isms), so it works identically on Linux/macOS and Windows.
- `new-card`/`status` bootstrap this repo's local card DB and sync identity
  exactly like a running MCP server session would — without that, a freshly
  created card's event would silently never queue.
- This tool is a **test/dev harness**, not a production monitoring surface;
  it prints whatever card/task titles and machine names the central API
  returns without additional sanitization. Do not point it at a store you do
  not trust, and do not put anything sensitive in a card title used to test
  it.
