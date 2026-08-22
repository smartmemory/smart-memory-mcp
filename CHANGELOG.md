# Changelog

## [Unreleased]

### Added (2026-08-22) — `include_archived` on `memory_search` (CORE-ARCHIVED-RECALL-1)

- `memory_search` gains `include_archived`, forwarded through both backends
  (local passes `**kwargs` straight to the core facade; remote needed a new
  allowlist entry, the same third-forwarding-point silent-drop risk that
  `include_superseded` and `include_retracted` had).
- Third lifecycle-visibility flag. An archived memory is one the decay/prune
  evolvers retired, or the source an episodic-to-semantic promotion replaced —
  no replacement and no version chain, so it is not covered by the other two.
- **Behaviour change for agents:** archived memories used to be returned ranked
  exactly like live ones, because core read `archived` on no search path at all.
  They are hidden by default now. Pass `include_archived=true` for audit or
  maintenance views. The tool docstring says so, which is what an agent reads.


### Added — `memory_policy_bundle` (GOV-STRATUM-SEAM-1 P1)

The FREE-tier tool compiles active workspace decisions into the policy bundle a
Stratum runner needs before planning. It dispatches through both MCP backends:
the local backend calls the core facade and the hosted backend calls
`GET /memory/policy/bundle`. Optional `workflow` and `domain` selectors are
forwarded unchanged; the returned bundle is contract JSON.

### Fixed — the first `transcript_search` of a session was unranked

The cross-encoder reranker loads lazily and its first-query fallback returns fusion order
**unranked** (core `DIST-LITE-WARMSTART-1`). Nothing triggered that load except a search, so
the first `transcript_search` of every session — the impression-forming one — came back
unranked. Measured on the 386-chunk corpus, same process, same query
(`cloudflare flagging our proxy IP`): the first call put `/unflush` slash-command boilerplate
at ranks 1 and 2 and pushed the correct session out of the top 6 entirely, while the second
call returned it at rank 1.

`server.main()` now schedules core's existing warm-up hook (`warm_search_models_async`,
`CORE-SEARCH-WARMSTART-1` — the same one the API service runs in its lifespan) before serving.
Verified end to end: the first search after boot returns the correct session at rank 1, with
boot still returning in 0.02s.

Two gates keep it free for everyone else:

- **no transcript store on disk, no warm-up.** This module is imported for every Claude Code
  session and transcripts are opt-in, so the common case loads nothing.
- **`SMARTMEMORY_WARM_RERANKER=false` skips the ~1.2GB cross-encoder**, the same flag,
  spelling and default as the service. The embedding model still warms.

It runs from `main()`, not `register()`: registration is what the test suite drives, and
warming there would load a real model whenever the developer running the tests happened to
have a transcript store. It also does not run from `_get_memory()`, which executes inside the
first search and would turn a certain miss into a race. A failed warm-up degrades to the old
lazy path and never breaks boot.


### Added — `transcript_search` gains a `project` filter, and hits show provenance

Follows core's DIST-CC-INGEST-1 provenance change, which records each session's working
directory, transcript file, git branch and agent version at import time.

- `transcript_search(..., project="/path/to/repo")` keeps only sessions whose recorded
  working directory is that path or below it. **This was documented as impossible last
  change** — it became possible only because the importer now stores `cwd`.
- Hits render provenance: where the session ran, its branch, and the transcript file, so
  a result can be traced back to the conversation on disk.
- Matching is path-aware, not a string prefix: `/repo` does not match `/repo-old`.
- The filter runs AFTER retrieval, because search ranks semantically and has no `cwd`
  predicate. The tool over-fetches (bounded at 200 candidates) and narrows, and **says
  when the window bounded the answer** rather than presenting a truncated list as
  complete.
- Sessions imported before provenance carry no `cwd`. They are counted and reported
  ("could not be matched — re-import to make them filterable") rather than silently
  dropped, so an empty result never masquerades as "no such session".
- Provenance lines are omitted, not rendered empty, when a session has none — the
  absence stays visible.
- Hits show the canonical repository over the local path when both are known (two
  checkouts of one repo have different paths), the short commit, and the model(s) that
  produced the session.
- **Sessions launched by SmartMemory's own pipeline (`entrypoint` starting `sdk`) are
  flagged, not hidden**: `⚠ not a human session`. Excluding them is an import-time
  decision, not a display-time one, so the tool reports what is in the store rather than
  quietly editing it.

### Added — `transcript_search` / `transcript_status` (DIST-CC-INGEST-1 Phase 4)

- `transcript_search(query, top_k, source)` searches your own already-imported Claude Code
  and Codex sessions by meaning — the read half of `transcripts index`. PRO tier,
  local-backend only: the transcripts live on the developer's machine and are never
  uploaded.
- `transcript_status()` reports how much of the corpus is actually searchable, whether an
  import is running, and whether any files failed or were interrupted mid-ingest.
- **These are the only tools that bypass `resolve_backend()`.** The ingested corpus lives
  in its own store (`~/.smartmemory-transcripts`, override with
  `SMARTMEMORY_TRANSCRIPTS_DIR`), deliberately apart from the curated store — 79k
  conversation turns would swamp every `memory_search` result. The shared backend
  singleton is pinned to the main data dir and structurally cannot see it, so these tools
  open a second, read-only instance against the transcript dir. A per-directory instance,
  NOT a process-wide `SMARTMEMORY_DATA_DIR` override, which would repoint every other
  tool's backend too.
- Distinct from `code_read_transcript` (CORE-CODE-PROVENANCE-1), which reads raw JSONL
  anchored on a code span. This is the unanchored direction — semantic search over the
  whole corpus, which needs the embeddings only ingestion produces.
- **No project filter, deliberately.** The session's `cwd` is used at import time to
  select files and is never stored on the items, so it cannot be filtered on afterwards.
  Offering one would have matched nothing.
- Reports rather than degrades, per `no-silent-degradation.md`: a store that was never
  created returns the import instructions instead of an empty result set, and an
  embedding-dimension mismatch between the index and the currently configured embedder is
  named explicitly. That mismatch is the failure this feature is most exposed to and the
  one that looks least like a failure — `create_lite_memory` does not pin the embedding
  provider, so a provider key appearing in the environment after ingest silently changes
  the query width and semantic search returns nothing without raising.
- PRO tool count 67 -> 69, PRO_PLUS 98 -> 100 (`test_tier_registration.py` updated).

### Changed — recommended wake-up budget ~200 -> ~300 (CORE-TOKEN-ESTIMATOR-UNDERCOUNT-1)

- `memory_recall_pack(preset="wakeup")` guidance updated. The corrected core token
  estimator is conservative, and a verbose workspace lost orientation slots at 200.
  Typical workspaces are unaffected — the card is content-bounded at ~70 real tokens.

### Added — `memory_recall_pack` gains `preset` (CORE-RECALL-BUDGET-1 Phase 5)

- `preset="wakeup"` returns the L1 session-start card at a small budget (~200 tokens).
  Rides the existing tool rather than a second one — it is the same assembler with a
  different choice of sections.
- `hot_topics` and `last_session` added to the validated section names.
- Rejects an unknown preset, and rejects `preset` and `sections` together rather than
  silently resolving an ambiguous request.
- Forwarded through both backends (`LocalBackend` via `**kwargs`, `RemoteBackend` on the
  wire body).

### Added — `memory_recall_pack` tool (CORE-RECALL-BUDGET-1)

- `memory_recall_pack(budget_tokens, query=None, sections=None)` returns a token-budgeted,
  priority-ordered context block plus its accounting manifest. **FREE tier** (the
  `get_working_context` precedent): a budgeted context block is what an agent needs most
  when it has least, so it does not sit behind a tier.
- Wired through BOTH backends — `LocalBackend.recall_pack` delegates to the in-process
  facade, `RemoteBackend.recall_pack` POSTs `/memory/recall/pack`.
- Budget and section names are validated in the tool, so a typo fails fast with a usable
  message instead of travelling to core or over HTTP.
- FREE/PRO/PRO_PLUS tool counts move 15→16 / 66→67 / 97→98.


### Added — `peer_chat` tool (CORE-ZERO-SCHEMA-1 Phase 1)

- `peer_chat(peer_id, query, reasoning_level="medium", session_id=None, top_k=10)` — the
  zero-schema front door: ask a question about a peer, get an answer synthesized from
  their stored memory. Registered at the PRO tier. Returns the `PeerChatResult` dict
  (`answer`, `peer_id`, `reasoning_level`, `model`, `sources`) per `peer-chat-contract.json`.
- **Local-backend only.** No `/memory/peer/*` service route exists yet, so a remote
  backend gets a parked-capability message rather than a broken proxy call — the same
  deferral shape the provenance tools use. The hosted surface lands with Phase 4.
- `LocalBackend.peer_chat` passthrough delegates to core's `smartmemory.peer.synthesis`.

## [1.4.60] - 2026-08-06

### Added (2026-08-05) — `include_retracted` on `memory_search` (CORE-RETRACTED-RECALL-1)

- `memory_search` gains `include_retracted`, forwarded through both backends (local
  kwargs → core; remote → POST body — the same third forwarding point that made
  `include_superseded` a silent-drop risk).
- **The local hop was not actually working, for any of these params.** Tracing this one
  end to end found that `smartmemory_app.storage.search()` — the local backend's only
  path to core — forwards kwargs through an allowlist that drops unknown keys silently,
  and `include_superseded`, `as_of_date` and `as_of_strict` were all missing from it.
  So the PLAT-AUDITABLE-MEMORY-1 entry below claiming they were "forwarded through both
  backends" was true only of the remote half; in local mode an as-of audit request
  returned plain present-day results with no error. Fixed in `smartmemory` (wrapper)
  `40e6764` with a regression test covering all four params.
- **Agents no longer see withdrawn decisions by default.** This is the point of the
  change: a retracted decision has no replacement to redirect to, so an agent quoting
  one has no signal that it was killed. Pass `include_retracted=true` for audit views.

## [1.4.59] - 2026-08-04

### Added (2026-08-04) — as-of recall + memory_explain (PLAT-AUDITABLE-MEMORY-1 T10)

- `memory_search` gains `as_of_date` (ISO-8601 transaction-time travel) and
  `include_superseded`; forwarded through both backends (local kwargs →
  core; remote → POST body — the third forwarding point, a missed one is a
  silent param drop).
- New `memory_explain(memory_id)` tool, FREE tier by design: the single-call
  provenance answer (origin tier, version audit with chain hashes,
  supersession, lineage, decision provenance, chain verification).
  `chain_verified: null` is "nothing to verify", not a tamper warning.
  Backends: local → core `SmartMemory.explain`; remote →
  `GET /memory/{id}/explain` (404 = absence; other errors raise).
- No `audit_verify` MCP tool — the verify surface is enterprise-gated REST
  only (decision 10).

### Fixed

- **Three local-mode tools raised `AttributeError` on every call.**
  `LocalBackend.list_memories`, `.search_by_metadata` and `.clear_user_memories`
  delegated to `list_memories` / `search_by_metadata` / `clear_user_memories` on
  `smartmemory.SmartMemory` — **none of which exist** on the core facade (verified
  by sweeping all 28 delegations against it: 25 resolve, these 3 do not). So
  `memory_list`, `memory_search_by_metadata` and `memory_clear` were inert in local
  mode, and `memory_clear` in particular never cleared anything. `clear_user_memories`
  now calls core's `clear()`; the other two filter a `search("*")` scan, since core
  exposes no listing API (`get_all_items_debug()` returns a stats summary with
  *sample* items, not a page). Both log a WARNING when the local scan window is hit
  rather than presenting a possibly-truncated result as complete.

### Added

- **`memory_list` accepts `metadata_key` / `metadata_value`** (GRAPH-API-1l), the
  supported replacement for the deprecated `memory_search_by_metadata`. Nested keys
  use dot syntax (`profile.tier`). Previously `RemoteBackend.list_memories` accepted
  arbitrary `**kwargs` but forwarded only `limit`/`offset`, so filters passed by a
  caller were silently dropped and the result was an unfiltered list — wrong answers,
  no error. Both halves must be supplied together; a half-filter now raises locally
  with a clear message instead of taking an HTTP 422 round trip.
- Local metadata matching mirrors the service contract, including bool-is-not-int
  (`flag=1` must not match `flag=true`) — the same divergence guarded service-side,
  where Python's `True == 1` disagrees with type-aware FalkorDB.

## [1.4.57]

Tracks product version 1.4.57 (single-source lockstep with `smart-memory-core/VERSION`);
1.4.54-1.4.56 were core-only trains with no MCP change.

### Fixed

- **`memory_search_by_metadata` returned one blank memory and discarded every real
  hit (remote backend).** `RemoteBackend.search_by_metadata` wrapped the service's
  `{"items": [...], "count": N}` envelope as if it were a single item
  (`normalize_items([result])`). Because `normalize_item` reads every field with
  `.get()` and a default, the envelope did not raise — it normalized into one
  perfectly well-formed *empty* `MemoryResult`, so the tool reported exactly one
  contentless memory no matter how many matched. Same failure class as the
  CORE-RECALL-LINEAGE-1 envelope bug already guarded in `search()` directly above it.
  Now reads `items`, with the pre-GRAPH-API-1l bare-array shape still handled.
- **`top_k` was dropped on the same call**, silently pinning every remote
  metadata search to the service default of 25 results. Now forwarded as `limit`,
  clamped to the service's documented 1–200 range.

### Notes

- The bug survived because `test_search_by_metadata_returns_item_on_success` asserted
  a bare-item shape that `GET /memory/by-metadata` has never returned — the test was
  written against the code instead of the endpoint, so it stayed green while the path
  was broken in production. Corrected and expanded to four cases (envelope, empty
  envelope, `top_k`→`limit` forwarding incl. the 200 cap, legacy bare array).
- `/memory/by-metadata` is deprecated in favour of `/memory/list`'s `metadata_key`/
  `metadata_value` filters (GRAPH-API-1l), but migrating this caller is blocked on an
  unresolved item-**shape** difference (`/by-metadata` rows carry `superseded` /
  `superseded_by` plus synthesized content for code rows). Fixing the envelope here is
  independent of that migration.

## [1.4.53]

### Fixed

- **Tier tool-count expectations were stale** — `code_blame` and `code_read_transcript`
  (CORE-CODE-PROVENANCE-1 Phase 2c) were registered without bumping the counts, so PRO
  and PRO_PLUS asserted 62/93 against an actual 64/95. Counts corrected; the tools are
  legitimate, the test was behind. Nothing caught it because **`publish.yml` runs no
  pytest step** — these 24 test files only ever run locally.

### Notes

- The FREE-tier count test is **not hermetic**: `_clean_env` strips the two tier env vars
  but cannot reach the keyring/file credential store `tier.get_api_key()` also consults,
  so a logged-in dev machine sees 64 instead of 14. Documented in the test. Verified
  correct (14) in a clean container.
- **Tests are pinned to fastmcp 2.x internals.** `fastmcp>=2.0` is unpinned, and a fresh
  install now resolves 3.4.4, where the private `_tool_manager` the tests enumerate is
  gone — 44 tests fail on a clean install. The **package itself is unaffected**: no
  product code touches `_tool_manager`, and the server imports and registers fine on
  3.4.4. Test-only fragility; worth a follow-up.


All notable changes to the standalone `smart-memory-mcp` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]
### Changed (auto, lockstep) — track product version 1.4.51 (1.4.51)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.50 (1.4.50)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.49 (1.4.49)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.48 (1.4.48)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.47 (1.4.47)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.46 (1.4.46)
- Version copied from the smartmemory-core release (single-source lockstep).

### Fixed
- Remote-mode `memory_search` (and the recall built on it) always returned "No results": `RemoteBackend.search()` predated the CORE-RECALL-LINEAGE-1 `SearchResponse` envelope (`{"results": [...]}`) and silently emptied every dict response. It now unwraps the envelope; bare-array responses from pre-LINEAGE-1 services still work. Found live in DEMO-WALKTHROUGH-4 spike 0.10.
- `whoami` reported the env-default API URL (`api.smartmemory.ai`) and an empty team even when the resolved backend targeted a different service, and printed the "Backend:" line twice — it now reports the actual resolved backend.

### Changed (auto, lockstep) — track product version 1.4.45 (1.4.45)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.44 (1.4.44)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.44 (1.4.44)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.43 (1.4.43)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.42 (1.4.42)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.41 (1.4.41)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.40 (1.4.40)
- Version copied from the smartmemory-core release (single-source lockstep).

### Changed (auto, lockstep) — track product version 1.4.39 (1.4.39)
- Version copied from the smartmemory-core release (single-source lockstep).


### Added (CORE-CODE-PROVENANCE-1 Phase 2c, 2026-06-23) — `code_read_transcript` tool + `code_blame` chaining
- New `code_read_transcript` MCP tool (`tools/code_tools.py`, local-backend only): the *read* half of the
  blame→read chain. Given a `code_blame` match's read handle, renders a centered window of the authoring
  Claude Code / Codex transcript (delegates to `SmartMemory.read_transcript_centered` via a new
  `LocalBackend.read_transcript_centered` passthrough). For live CC it accepts `file` + `norm_hash` to locate
  the authoring edit by content hash; `next_line`/`prev_line` page directionally.
- **`code_blame` now emits a copyable read handle** per match — a ready-to-paste `code_read_transcript(...)`
  call carrying `line_no` (Codex / CC-import) or `file`+`norm_hash` (live CC) — so the two tools chain.
- **Remote-path wording corrected:** both `code_blame` and `code_read_transcript` now say the hosted REST
  surface is *parked* (it is, as of 2c) rather than "the REST surface is Phase 2c" (which implied it was
  coming). The capability is local-only.

### Added (CORE-CODE-PROVENANCE-1 Phase 2b, 2026-06-22) — `code_blame` tool
- PRO-tier `code_blame` MCP tool (`tools/code_tools.py`): given a git commit (or file + line) in a local repo, returns the Claude Code / Codex session whose edits structurally authored that code, by a typed query over the persisted `code_provenance` rows (no transcript re-scan). Renders the authoring session(s) with method/coverage/survival and flags repo-unconfirmed leads. Local-backend only — a `LocalBackend.blame_code` passthrough delegates to the in-process lite `SmartMemory.blame_code`; the remote path returns a local-only/parked message (updated in Phase 2c — see above). `git_error` maps to a graceful string.

### Added (CORE-RECALL-CENTERED-1 Phase 2, IDEA-441, 2026-06-22) — `read_around` tool
- FREE-tier `read_around` MCP tool (`tools/memory_tools.py`) forwarding to `backend.read_around` (local + remote backends): given a matched conversation-chunk `item_id`, returns the char-budgeted centered window of the surrounding chunks plus a continue-cursor. Also corrected the pre-existing-stale PRO/PRO_PLUS tier-count assertions in `tests/test_tier_registration.py` to the true `read_around`-inclusive counts (FREE 13→14, PRO 58→62, PRO_PLUS 89→93; the +3 beyond read_around was prior committed drift).

### Changed (DIST-LITE-QUIET-1, 2026-06-07) — local writes carry a real origin
- `LocalBackend.add` tags writes `mcp:memory_add` and `LocalBackend.ingest` tags writes
  `mcp:memory_ingest` (the `/remember` skill surface), so local MCP memories land as tier-2
  recall+search-visible user content instead of `origin='unknown'` (tier 4, hidden). A
  caller-supplied origin (e.g. an importer) is preserved; the `origin` convenience key is
  not leaked into stored metadata.

### Added (CORE-GRAPH-ALIAS-DISAMBIG-1, 2026-06-03) — `disambiguate` arg (0.2.4)
- `memory_resolve_aliases(dry_run=False, disambiguate=False)` threads the opt-in collision-disambiguation
  flag (default off) through both the REST and local-backend paths; the summary notes how many collisions
  were recovered. Contract: `docs/features/CORE-GRAPH-ALIAS-DISAMBIG-1/disambiguate-contract.json`.

### Added (CORE-GRAPH-ALIAS-RESOLVE-2 B2, 2026-06-02)

- **`memory_resolve_aliases(dry_run=False)`** (PRO tier) — graph-maintenance tool that consolidates fragmented single-token entity aliases ("Hudson") into their multi-token canonical ("Rock Hudson") over the COMPLETE workspace graph, redirecting the alias's edges onto the canonical and abstaining on collisions. Run after a bulk ingest, when the full ambiguity picture is present. New module `smartmemory_mcp/tools/graph_tools.py`, registered in the PRO block of `_register_tools()`. Dispatch mirrors `code_tools` exactly: REMOTE (`hasattr(backend, "request")`) POSTs `/memory/graph/resolve-aliases` with `dry_run` as a **query parameter** (`?dry_run=true`, lowercase string per the `clear_user_memories` `nuclear` precedent); LOCAL calls `backend._mem.resolve_aliases(dry_run=...)` on the real SmartMemory (never the MCP wrapper, never passing `workspace_id` — scope derives from auth) and consumes `AliasResolveReport.to_dict()`. Returns an agent-readable summary (resolved / abstained / redirected-edges / ambiguous list); a dry run is clearly flagged as a no-change preview. Forcing-function tests in `tests/test_graph_tools_local_backend.py` (6) cover both branches, the dry-run preview wording, the no-`_mem` refusal, and the error-dict path. PRO tier count 57→58, PRO+ 88→89 (`tests/test_tier_registration.py`). Contract: `smart-memory-docs/docs/features/CORE-GRAPH-ALIAS-RESOLVE-2/resolve-aliases-contract.json`. Bump 0.2.3.

### Fixed (bug-hunt 2026-06-02) — managed-type & evaluation tools passed the wrong object in local mode
- **CRITICAL — decision tools silently corrupted data in local mode.** All 10 `decision_*`
  tools built `DecisionManager`/`DecisionQueries` from the MCP backend *wrapper*. The managed
  framework then called `LocalBackend.add(memory_item)` — signature `add(content: str, ...)` —
  so the `MemoryItem` was received as `content` and re-wrapped: decision_type/confidence/
  rationale/item_id discarded, stored as a semantic node, unreadable afterward, while the tool
  reported success. Now resolve the real SmartMemory via `backend._mem` (new `_local_sm` helper);
  in remote mode (no `_mem`) refuse with a clear message instead of corrupting data.
- **`agent_evaluation_get` always returned None.** It passed the wrapper to `get_evaluation`,
  which reads from the graph the wrapper doesn't expose. Now passes `backend._mem`; remote mode
  returns None without a client-side read (follow-on).
- Forcing-function tests: `tests/test_decision_tools_local_backend.py` (3) and an updated
  `tests/test_agent_evaluation_get_contract.py` assert the SmartMemory (`backend._mem`) is used,
  not the wrapper, and that remote mode refuses/returns-None rather than corrupting. Bump 0.2.2.

### Added (CORE-ADHERENCE-1, 2026-05-29)

- **`memory_get_violation_patterns(rule_id?, rule_type="feedback", memory_dir?)`** (PRO tier) — returns the `## Detection patterns` catalog parsed from local `{rule_type}_*.md` rule files via `smartmemory.adherence.load_rule_patterns`. Filesystem-backed (resolves `memory_dir` arg → `SMARTMEMORY_RULES_DIR` env); unlike `pattern_query`/`pattern_get`/`pattern_list` it does **not** read the graph backend. Harnesses fetch this once per session to run their own pre-response adherence check. A missing/invalid directory returns a path-specific explanatory string, never a silently-empty list.

### Added (PLAT-PRE-PUSH-1, 2026-05-10)

- **`scripts/hooks/pre-push`** — git pre-push hook that validates cross-repo Python imports against `origin/main` of sibling repos (`smart-memory-common`, `smart-memory-core`). Same hook code as `smart-memory-service` (vendored). Catches imports referencing symbols not yet on origin of the sibling.
- **`scripts/install-git-hooks.sh`** — idempotent symlink installer.

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
