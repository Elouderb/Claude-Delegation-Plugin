---
name: lsp-diagnostics
description: Preload for code-editing agents; after editing source, use the LSP tool to pull real diagnostics for the language before declaring the change done.
---

# LSP Diagnostics

Do not eyeball type, import, borrow, or syntax correctness — ask the language server.

1. After editing a source file, run the `LSP` tool against it (or the project) to get diagnostics for that language. The environment ships servers for Python (pyright), TypeScript, Rust, C/C++ (clangd), C#, Go, Swift, and Lua.
2. Treat errors as blocking: resolve them before reporting the change complete. Triage warnings — fix the ones your change introduced; do not chase pre-existing noise.
3. Use LSP hover / go-to-definition to confirm a symbol's type or origin instead of guessing from surrounding code.
4. If no server is available for the language, fall back to the project's own compiler/linter via Bash and say so.

Report the diagnostics you cleared — and any pre-existing ones you left, with a reason — in the card and the handoff.
