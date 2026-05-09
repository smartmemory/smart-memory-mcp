# Changelog

All notable changes to the standalone `smart-memory-mcp` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added (RECALL-CITATIONS-1, 2026-05-10)

- **`memory_recall` and `memory_search` accept `cite=True`** — when set, return a structured payload `{items, citations, footnote_block}` (recall also returns `session_id`) instead of the legacy formatted string. `footnote_block` is a markdown-footnote block ready for the consuming agent (Claude Code, Cursor, Codex) to paste verbatim into its reply, allowing inline `[^1]`/`[^2]`/`[^3]` references to SmartMemory items. Default shape (string output) unchanged when `cite=False`. Empty result set with `cite=True` returns `citations: []` and `footnote_block: ""` rather than omitting the fields, so consumers can distinguish "no results" from "no citations requested".

### Added

- **CORE-ADHERENCE-1 (substrate half): read-only adherence pattern tools.** New module `smartmemory_mcp/tools/pattern_tools.py` registers three tools at PRO tier:
  - `pattern_query(scope?, severity?, free_text?, limit=100)` — conjunctive filter over the active catalog. Scope and free_text are case-insensitive substring matches; severity is one of `info|warn|error`.
  - `pattern_get(pattern_id)` — returns the Pattern dict or `null`.
  - `pattern_list(limit=200)` — every active pattern (no filters).
  Write-side is intentionally REST-only at `/memory/patterns` — agents shouldn't mutate the rule catalog they're being held to. Compose's `COMP-POLICY-CHECK` is the first MCP consumer of this surface.
- **CORE-EXPERTISE-1 Phase 1: `decision_create` accepts `rejected_alternatives`, `rationale`, `constraints`.** New optional kwargs on the `decision_create` MCP tool in `smartmemory_mcp/tools/decision_tools.py`. Forwards to `DecisionManager.create()`. Feature folder: `smart-memory-docs/docs/features/CORE-EXPERTISE-1/phase-1-decision-schema/`.
- **CORE-CRUD-UPDATE-1: `memory_update` MCP tool exposes `properties` and `write_mode`.** Signature extended: `memory_update(item_id, content?, metadata?, properties?, write_mode?)`. Advanced callers can now do direct node-property updates (not just content/metadata conveniences) and control merge-vs-replace write semantics. LocalBackend routes through `SmartMemory.update_properties()`; RemoteBackend forwards all new fields to `PUT /memory/{item_id}`. Contract: `smart-memory-docs/docs/features/CORE-CRUD-UPDATE-1/update-contract.json`.

### Changed — BREAKING

- **CORE-MEMORY-DYNAMICS-1 M1b-fixup (2026-04-20):** `commit_working_to_episodic` + `commit_working_to_procedural` protocol methods + implementations removed from `smartmemory_mcp/backends/interface.py`, `local.py`, `remote.py` — the underlying core façades were deleted in M1b, making these stubs AttributeError traps. `memory_distill` tool also corrected to write `memory_type="pending"` (prev still wrote `"working"`). Test fixture updates in `tests/test_normalize.py`, `tests/test_confidence_display.py`, `tests/test_stale_display.py`. Commits `75a54d5`.
- **CORE-MEMORY-DYNAMICS-1 M1b: `working` → `pending` rename.** Standalone MCP consumers follow the core rename: `_LEGACY_RECALL_TYPE_SCOPE` in `smartmemory_mcp/tools/memory_tools.py` updated to `{"pending"}` (regression test against the service repo's scope also updated). `evolution_dream` tool becomes a no-op with deprecation notice — the underlying `commit_working_to_*` façade was removed in core. `evolution_status` now counts `memory_type="pending"` items and reports `"Pending memory items (formerly 'working')"`.

### Added

- **CORE-MEMORY-DYNAMICS-1 M1a: `get_working_context` MCP tool + `memory_recall` deprecated shim.** Mirrors the service repo's migration (`smart-memory-service` Tasks 5.2 + 5.3) but composes directly against `backend.search` since the standalone has no `SmartMemory` instance. New `_build_working_context(backend, session_id, query, k, max_tokens, strategy)` helper produces contract-shape response per `smart-memory-docs/docs/features/CORE-MEMORY-DYNAMICS-1/context-api-contract.json` (items with `score_breakdown`, `strategy_used="fast:recency"` literal, `tokens_used` with `max(1, len//4)` estimator, `BudgetTooSmall` when smallest item > `max_tokens`). New `get_working_context` MCP tool validates `k` in 1..100 and calls the helper. `memory_recall` becomes a deprecated shim: preserves the native `backend.recall()` fast path when present and `session_id` is None; otherwise delegates to `_build_working_context` with 10× over-fetch clamped to 100, applies **legacy-scope post-filter** via module-level `_LEGACY_RECALL_TYPE_SCOPE = {"working"}` (derived from pre-shim body at `smartmemory_mcp/tools/memory_tools.py:241`, asserted identical to the service repo by a regression test), then applies the original per-session `metadata.conversation_id`/`session_id` filter before `_format_recall`. One-shot `DeprecationWarning` logged per process. Standalone does not compose anchors (no `AnchorQueries`). 13 unit tests cover contract shape, exact-fit budget, empty/`None` search results, scope-filter correctness, deprecation-once, session filtering, and the scope-match regression against the service repo. Design/plan/report: `smart-memory-docs/docs/features/CORE-MEMORY-DYNAMICS-1/`.
