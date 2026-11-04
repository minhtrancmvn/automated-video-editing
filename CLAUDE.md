<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **automated-video-editing** (4875 symbols, 9676 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/automated-video-editing/context` | Codebase overview, check index freshness |
| `gitnexus://repo/automated-video-editing/clusters` | All functional areas |
| `gitnexus://repo/automated-video-editing/processes` | All execution flows |
| `gitnexus://repo/automated-video-editing/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Agent Orchestration

### Model Routing

- Use Opus as the primary model for planning and synthesis.
- Delegate independent, bounded work to Sonnet.
- Use Haiku for mechanical research.
- Use Fable only when architectural ambiguity warrants its cost.
- Verify every agent's output before relying on it.

### Agent Worktree Lifecycle

- Clean up agent-created worktrees only after the agent's task is complete and its changes are committed and pushed.
- Then remove that agent-created worktree immediately, run `git worktree prune`, and verify `git worktree list` has no stale agent-created worktrees.
- Never remove active or user-created worktrees.

### Background Work

- Always tell the user when they must wait for a subagent or background task to finish before sending the next message.
- Name what is still running and what completion signal to expect. When nothing is pending, no notice is needed.
