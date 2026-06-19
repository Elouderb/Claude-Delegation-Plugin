# Plugin-Centric Agent OS Hooks

This package contains **only Claude Code plugin hooks**. It does not install or
modify `.git/hooks` or `core.hooksPath` in individual repositories.

## Why

Native Git hooks are repository-scoped unless a user configures a global
`core.hooksPath`. A global Git hook directory affects every repository on the
machine, can conflict with existing hooks, and is not naturally managed by a
Claude Code plugin. Plugin hooks are therefore the safer portable default.

## Graph update behavior

The plugin runs only the baseline incremental Graphify update:

```bash
graphify update .
```

It never adds semantic, deep, force, or LLM-deduplication flags.

`graphify` must be on your `PATH` for these hooks to do anything. When no LLM API
key is configured, Graphify's semantic-extraction step is skipped quietly and the
baseline structural update still runs. You can pass additional flags to the
update via the `AGENT_OS_GRAPHIFY_ARGS` environment variable.

The repository graph is marked dirty when Claude creates or modifies a relevant
source file. It is updated at bounded lifecycle points:

- after tool batches
- after Bash operations
- after subagents stop
- at the end of an agent turn
- when the session ends

The plugin also stores a hash of:

- the current Git `HEAD`
- staged changes
- unstaged changes
- untracked files
- additions, deletions, and renames

It checks this state on session start and before each submitted prompt. This
detects branch changes, commits, rebases, merges, and worktree merges even when
they occurred outside Claude Code. The graph is rebuilt on the next Claude
lifecycle event.

## Worktree behavior

When an agent works in an isolated worktree, that worktree has its own Git and
working-directory state. After its branch is merged into the main worktree, the
main worktree fingerprint changes. The plugin detects that change at the next:

- `UserPromptSubmit`
- `SessionStart`
- `PostToolUse` for Bash
- `Stop` or `SessionEnd`

and runs the baseline Graphify update in the active worktree.

This is not an operating-system file watcher. A merge performed externally while
Claude is idle is detected the next time Claude receives a lifecycle event.

## Files

```text
hooks/hooks.json
scripts/hook_common.py
scripts/mark_repo_graph_dirty.py
scripts/protect_generated_files.py
scripts/sync_repo_graph.py
```

Install these at the plugin root. Hook commands use `${CLAUDE_PLUGIN_ROOT}` so
the package remains portable when installed natively.

## Local state

The plugin writes only project-local transient state:

```text
.agent-os/hooks/
├── graphify.dirty
├── git-state.sha256
├── graphify.lock
└── hooks.log
```

Recommended `.gitignore` entry:

```gitignore
.agent-os/hooks/
```

## Validation

After placing this into the plugin, run:

```bash
claude plugin validate .
```

Then install the plugin via the local marketplace (see the project `README.md`
for the canonical flow):

```text
/plugin marketplace add /path/to/agent-os
/plugin install agent-os@agent-os-local
```

After `/reload-plugins` (or a restart), inspect active hooks with:

```text
/hooks
```
