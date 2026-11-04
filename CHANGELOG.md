- SM-ASOF-DECISIONS-1 (feat, 2026-11-04; pairs with core, common and service on `wk/SM-ASOF-DECISIONS-1`): `as_of: Optional[str]` (ISO 8601; omit for now) on `decision_get`, `decision_list`, `decision_search`, `decision_provenance`, `memory_policy_bundle` and `memory_recall_pack`, through both backends. New `tools/common.checked_as_of` refuses an unparseable value with a `ToolError` naming it before any backend call, never a silent "now". RemoteBackend sends `as_of` as a query param on the GET routes and a body field on `POST /memory/recall/pack`, only when set (the undated requests are unchanged). LocalBackend sends a dated read through the SmartMemory facade (`get_decision`, `get_active_decisions`, `get_decisions_about`, `get_decision_provenance`, `compile_policy_bundle(as_of=)`), which binds its history read gate; an undated read is the call shipped before. Local-mode `memory_policy_bundle` on a real Lite store still needs SM-LOCAL-TOOLS-1 (the Lite workspace scope). Tests: new `tests/test_decision_tools_as_of.py` (remote request shapes with and without `as_of`, ToolError per tool, local routing through the facade, and the D1 -> D2 -> retract headline on a real Lite store through LocalBackend).


# Changelog

## Unreleased
- OPS-SERVICE-PREPUSH-PER-LINE-TARGET-1 (mcp twin; tooling, 2026-11-04; s2-coord mixed push `main`+`2.0`) [review tier: small bounded tooling, batched post-land]: **the cross-repo pre-push hook checks each pushed line against its own target branch.** `main()` used to check every pushed sha against every target branch (a cross product), so a mixed `git push origin <sha>:refs/heads/main <sha2>:refs/heads/2.0` blocked the main sha on 39 symbols that exist only on the sibling's main. New `push_targets()` keeps the line-to-branch pairing and `main()` validates each branch with only its own shas; `target_branches()` stays as a thin wrapper. Fallbacks unchanged (no stdin, non-branch target, unresolvable sha). Two new mixed-push rows.

- OPS-SERVICE-PREPUSH-PUSHED-SHA-1 (tooling, 2026-11-04; s2-coord push_round round 10) [review tier: small bounded tooling, batched post-land]: **the cross-repo pre-push hook checks the commit being pushed, not HEAD, and finds the sibling repos from a linked worktree.** It diffs `<base>..<pushed local sha>` for every stdin line (union, deletes skipped), reads each file's imports at that sha (`git show`, not the working tree), and resolves `../smart-memory-common` and `../smart-memory-core` against the main checkout (`git rev-parse --git-common-dir`). No stdin, or a sha this repo cannot resolve, falls back to HEAD with a WARNING; a missing sibling names the full path tried. Fixes the false block on 39 common symbols when push_round pushed e35ffec2 from the shared checkout on main. mcp twin of the service change.
- SM-EVIDENCE-TYPE-1 (2026-11-04; pairs with core, common and service on `wk/SM-EVIDENCE-TYPE-1`; contract smart-memory-docs `docs/features/SM-EVIDENCE-TYPE-1/evidence-contract.json`): **two new FREE tools for checked Compose receipts.** `evidence_record(record)` stores one Compose `ev:` record as an `evidence` memory (idempotent on its id); `decision_link_evidence(decision_id, evidence_id)` links one (node id or `ev:` ref) to a live decision with no confidence change. LocalBackend calls the core facade on `_mem` (`add_evidence`, `link_decision_evidence`, `get_evidence`); RemoteBackend calls `POST /memory/evidence` and `POST /memory/decisions/{id}/evidence` (decision id through `path_id`). A store refusal (missing decision or evidence, wrong type, ended decision, malformed record) is `EvidenceRefused` in both modes and renders as `Evidence not linked: <cause>` / `Evidence not recorded: <cause>`; any other API error raises. `decision_provenance` lists each evidence receipt (ref, kind, check time). `evidence` reserved under `/memory/` in `path_guard`; route fixture gains the three service routes. Tier counts: FREE 33, PRO 68, PRO_PLUS 99. Tests: new `tests/test_evidence_tools.py` (real Lite store, remote request shapes), parity, free-tier, tier, path-id rows.
- SM-EVIDENCE-TYPE-1 (2026-11-04; pairs with core, common and service on `wk/SM-EVIDENCE-TYPE-1`; contract smart-memory-docs `docs/features/SM-EVIDENCE-TYPE-1/evidence-contract.json`): **two new FREE tools for checked Compose receipts.** `evidence_record(record)` stores one Compose `ev:` record as an `evidence` memory (idempotent on its id); `decision_link_evidence(decision_id, evidence_id)` links one (node id or `ev:` ref) to a live decision with no confidence change. LocalBackend calls the core facade on `_mem` (`add_evidence`, `link_decision_evidence`, `get_evidence`); RemoteBackend calls `POST /memory/evidence` and `POST /memory/decisions/{id}/evidence` (decision id through `path_id`). A store refusal (missing decision or evidence, wrong type, ended decision, malformed record) is `EvidenceRefused` in both modes and renders as `Evidence not linked: <cause>` / `Evidence not recorded: <cause>`; any other API error raises. `decision_provenance` lists each evidence receipt (ref, kind, check time). `evidence` reserved under `/memory/` in `path_guard`; route fixture gains the three service routes. Tier counts: FREE 33, PRO 68, PRO_PLUS 99. Tests: new `tests/test_evidence_tools.py` (real Lite store, remote request shapes), parity, free-tier, tier, path-id rows. Fix round (2026-11-05): the full `-n 4` run no longer fails ~136 tests with `HostedAuthError: no identity bound to this call`; `tests/test_hosted_session_record.py` built hosted servers through `test_hosted_tools_allowlist._Harness` without that file's autouse restore, so the process-wide hosted flag stayed on for the rest of the xdist worker (pre-existing, exposed when the two new test files reshuffled worker scheduling). `tests/conftest.py` now turns the flag off after every test and logs a WARNING naming the test that left it on; the stdio PRO+ listing pin in `tests/test_switch_workspace.py` is 99 (the two new tools).

- SM-LOCAL-TOOLS-1 (2026-11-04, owner D-0089, user-visible; needs core SM-LOCAL-TOOLS-1): **the fourteen tools MCP-LOCAL-BROKEN-TOOLS-1 hid in local mode work there again**, and `LOCAL_BROKEN_TOOLS` is `{}` (the hide mechanism stays for the next one). Pattern, plan, anchor and failure-journal tools (`pattern_list`, `pattern_query`, `memory_plan_active`, `memory_plan_create`, `memory_anchor_list`, `memory_anchor_check_drift`, `memory_log_failure` and their siblings) handed the MCP wrapper to core managers that need the real SmartMemory; new `LocalBackend.pattern_*`, `plan_*`, `anchor_*`, `log_failure`, `check_failure` build each manager on `self._mem` (the MCP-REMOTE-DECISIONS-1 seam), so a failure entry is stored as a MemoryItem, never re-wrapped as text. `RemoteBackend` keeps the three reads that reached the service (`pattern_get`, `plan_get`, `check_failure`) and refuses the rest by name ("Not available in remote mode. Use local backend.", as its writes already answered); a remote client for the service's pattern/plan/anchor/failure routes is a follow-up. Zettel tools use core's `ZettelMemory.for_memory(mem)` (they called a constructor that does not exist). `memory_search_advanced` runs through the facade's `similarity_graph_traversal` (it built the traversal outside the facade, so the vector store fell to the process-default FalkorDB backend and raised CORE-GRAPHCONFIG-1). `memory_policy_bundle` compiles for Lite's one workspace (`workspace_id="default"`, honoured by core only on an unscoped Lite store). `dev_load_context` treats a None rationale or title as empty. Tests: new `tests/test_local_tools_contract.py` (one test per tool through FastMCP on a real Lite store, plus mechanism tests that each manager gets `_mem`); `tests/test_local_broken_tools.py` flipped to the empty table and `tests/test_policy_bundle_tool.py` pins the `workspace_id="default"` call (assertions listed in the unit report).

- WEB-2.0-NDA-DOWNSTREAM-TESTS-1 (2026-11-04, owner D-0093, test fixture + path guard): **the service route table is regenerated against service 2.0 `c357367283cc`**, which drops the three removed `/memory/beta/nda/*` routes and adds nine routes the older snapshot (`bf6be16117f3`) missed: `/auth/impersonate/{end,exchange}`, `/memory/routing/session` (GET/PUT/DELETE), and four superadmin offboard-conflict / pending-clerk-delete routes. `routing` joins the `/memory/` reserved segments in `path_guard.py`, so an item id of `routing` is refused instead of reaching `/memory/routing/session` (fail-closed, found by `test_reserved_lists_cover_every_static_sibling_in_the_service`). Tests: `tests/test_path_id_encoding.py` 67 passed.
- SM-PATTERN-CRUD-1 (2026-11-04, OWNER D-0089, user-visible; stacked on SM-LOCAL-TOOLS-1, needs core and service SM-PATTERN-CRUD-1): **agents can write the adherence pattern catalog.** **KILLED (2026-11-04):** the "read-only over MCP, writes through REST" design (`tools/pattern_tools.py` docstring). New tools `pattern_create`, `pattern_update` (only title, content, severity, scope, examples, rationale; `version` goes up), `pattern_retire` (destructive annotation; a soft retract that keeps the node and its reason) and `pattern_seed(dir)` (one pattern per rule linked from `<dir>/MEMORY.md`, titles already in the catalog skipped). Provenance: `source` and `updated_by` are `mcp:<agent id>` or `mcp:local` in local mode, `mcp:remote` plus the server's `@rest:<user>` in remote mode. Remote mode now calls `/memory/patterns` for all seven tools (reads included, ids through `path_guard.path_id`) and never imports core; `pattern_seed` reads the MEMORY.md and only the in-dir linked `.md` files in this process (new `backends/pattern_seed_reader.py`, the same link rules and caps as core's `read_memory_dir`: no absolute link, no `..`, no symlink out, each refusal a WARNING naming the link) and POSTs text to `/memory/patterns/seed`, never a path. `pattern_update` takes `pattern_id` positional-only in every backend, so a `pattern_id` field is the protected-field error. None of the pattern tools is on the hosted allowlist (README). Tests: new `tests/test_pattern_write_tools.py`; `tests/test_local_broken_tools.py`, `tests/test_local_tools_contract.py` and `tests/test_hosted_tools_allowlist.py` updated where they pinned the remote pattern stubs.

- SM-LOCAL-TOOLS-1 (2026-11-04, owner D-0089, user-visible; needs core SM-LOCAL-TOOLS-1): **the fourteen tools MCP-LOCAL-BROKEN-TOOLS-1 hid in local mode work there again**, and `LOCAL_BROKEN_TOOLS` is `{}` (the hide mechanism stays for the next one). Pattern, plan, anchor and failure-journal tools (`pattern_list`, `pattern_query`, `memory_plan_active`, `memory_plan_create`, `memory_anchor_list`, `memory_anchor_check_drift`, `memory_log_failure` and their siblings) handed the MCP wrapper to core managers that need the real SmartMemory; new `LocalBackend.pattern_*`, `plan_*`, `anchor_*`, `log_failure`, `check_failure` build each manager on `self._mem` (the MCP-REMOTE-DECISIONS-1 seam), so a failure entry is stored as a MemoryItem, never re-wrapped as text. `RemoteBackend` keeps the three reads that reached the service (`pattern_get`, `plan_get`, `check_failure`) and refuses the rest by name ("Not available in remote mode. Use local backend.", as its writes already answered); a remote client for the service's pattern/plan/anchor/failure routes is a follow-up. Zettel tools use core's `ZettelMemory.for_memory(mem)` (they called a constructor that does not exist). `memory_search_advanced` runs through the facade's `similarity_graph_traversal` (it built the traversal outside the facade, so the vector store fell to the process-default FalkorDB backend and raised CORE-GRAPHCONFIG-1). `memory_policy_bundle` compiles for Lite's one workspace (`workspace_id="default"`, honoured by core only on an unscoped Lite store). `dev_load_context` treats a None rationale or title as empty. Tests: new `tests/test_local_tools_contract.py` (one test per tool through FastMCP on a real Lite store, plus mechanism tests that each manager gets `_mem`); `tests/test_local_broken_tools.py` flipped to the empty table (assertions listed in the unit report).

- FWD-MERGE-4 mcp (2026-11-04, owner D-0066; security-adjacent: hosted workspace routing): **main (frozen 2026-10-30) is merged into 2.0 whole**, 28 main-only commits since the 10-10 merge base. Kept as 2.0 has them (twins already on this line): the pre-push hook fixes, CI manual-only, the decision tools in FREE, the path-id guard and its route table (2.0 service snapshot), and the hosted saved workspace pick (SM20-WS-ROUTING-PARITY-1, owner shape B, over main's WS-ROUTING-4). New on 2.0: `switch_workspace` with `switch_team` as its deprecated alias (MCP-SWITCH-WORKSPACE-1 and its 7 follow-ups; tool counts FREE 31, PRO 66, PRO+ 97, hosted 26, measured), the REST-first default (MCP-PERF-1), the hosted first-run CLI hint and sign-in guidance on auth refusals (MCP-FIRST-RUN-HINT-1), the shared-DB test guard, the remote-only registry entry, and the routing instructions (MCP-CAPTURE-ROUTING-1, ported to `memory_save`: 2.0 has no `memory_ingest`/`memory_add`, so the instructions, `decision_create`'s docstring and `tests/test_capture_routing.py` name `memory_save`, and the `add_runs_extraction` flag that only fed the retired `memory_add` warning is gone). The hosted middleware keeps 2.0's fail-closed read of the saved pick and now exempts both `switch_workspace` and `switch_team` from it. **Not carried:** WS-ROUTING-5's header rule (`fcf61ad`), reverted on the merge input `e1115f4`, because owner shape B keeps it off 2.0 until a post-release port and 2.0 service/common have no resolver, `X-SM-Client` handling or `/memory/routing/explain`. README's hosted switch paragraph now says the pick survives a reconnect (it said "current session only"). Post-merge fixes: `export_okf` under a live pick sends the active workspace header again (the merge had dropped it, `test_portability` caught it); the switch tests write through `memory_save` and count 97/26 tools; the sweep drops its 6 hosted header-rule rows and the export-no-pick row (WS-ROUTING-5 is not on 2.0; MCP-2.0-SWITCH-WORKSPACE-NAMING-TWIN-1 restores them from main `d54734b`); the session-record refusal and exemption rows cover both switch tools.

- SVC-OKF-IMPORT-TAR-HEADER-BOMB-1 (c) (security, availability, 2026-11-03, twin of the smart-memory-service OKF import fix): **local OKF extraction (`memory_import` / `memory_export` of a `.tar.gz`) no longer holds a whole PAX or GNU long-name header in memory: a ~130 KB archive with a 128 MiB header made the process hold 512 MiB.** `_extract_bundle` streams the gunzipped archive twice (validate, then extract) through the same decoded byte budget as the service: 1 MiB of slack, one header block plus the padded data of each regular file, and each extension header. A header over 64 KiB, more than 4 chained extension headers, more than 8 global headers or 64 MiB of extension headers in total, and any GNU sparse member are refused with `ArchiveBudgetError` (a `ValueError`) from the 512-byte header. There is no cap on regular-file data (a local bundle may be any size). Members are validated as they stream, so the first unsupported member stops the read, and the member list is no longer kept. A corrupt or truncated gzip is a `tarfile.ReadError`. Tests: `tests/test_portability_tar_budget.py` (new; includes the parity row pinning the service values).

- MCP-2.0-LOGIN-KEY-PERSIST-1 (2026-11-03, security): **a failed `login` never replaces your stored API key.** `RemoteBackend.login` now returns a typed `LoginResult(ok, message)` (`ok` only after `/auth/me` answered 2xx) and the `login` tool stores the key only on `ok`. Before, the tool looked for "error" in the reply text, so a 401 whose body had no such word (and any non-2xx or network failure) overwrote the working key. `store_api_key`'s POSIX fallback now creates a temp file with `os.open(..., 0o600)` before writing and `os.replace`s it onto the key file, so the file is never wider than 0600 and a crash keeps the prior key. Tests: new `tests/test_login_key_persist.py` (5 rows).

- MCP-2.0-DECISION-TOOLS-FREE-PORT-1 (2026-10-31, user-visible): port of main `31fdb1e8` to 2.0. The 13 decision tools are registered in the FREE tier again, so a keyless local server lists and accepts them as 1.x does. Before, 2.0 `_register_tools` registered them only at `tier >= PRO`, although the entry below already described the change. Only the decision suite moved: every other PRO and PRO+ tool keeps its tier. On 2.0 FREE is 30 registered tools (was 17), PRO stays 65 and PRO+ stays 96. `memory_add` no longer exists on 2.0, so the stays-PRO row uses `memory_save_document`. Tests: `tests/test_decision_tools_free_tier.py` (new) and `tests/test_tier_registration.py` (FREE count and names).

- ci, CI-2.0-MANUAL-ONLY-SWEEP-1 (2.0 port of main GH-ACTIONS-MANUAL-1, owner policy 2026-10-29): the Actions workflow triggers on this line are `workflow_dispatch` only (no push or pull_request triggers). Workflow trigger blocks only, no job body change.

- SM20-WS-ROUTING-PARITY-1 fix r1+r2 (2026-10-31, user-visible): **a hosted call never silently runs in the wrong workspace: if your saved workspace pick cannot be read, the call is refused with a clear error instead of falling back to your default workspace, and a removed workspace no longer blocks `switch_team`.** Saved picks no longer expire (svc-api and common dropped the idle TTL, a 2.0 divergence from main Lever 3), so a 200 with `workspace_id: null` honestly means "never picked or cleared" and the call uses your default workspace (DEBUG log only, no WARNING). An unreachable svc-api, a non-200, a non-JSON body or a body that is not an object is refused with `could not read your saved workspace pick (<reason>); retry, or run switch_team.`, and no backend call is made. `switch_team` is the one exemption (`SWITCH_TOOL`): it skips the read, lists your workspaces with the bearer only (so a pick for a workspace you were removed from cannot 403 the listing), and saves the new pick. Writes still go through the server's membership check, unchanged.

- SM20-WS-ROUTING-PARITY-1 (2026-10-31, user-visible, shape B step 3 of 3): **a hosted `switch_team` / workspace switch now survives a reconnect and a restart of the hosted server.** The pick used to live in a per-process dict (`_session_teams`) in `hosted/middleware.py`, so a deploy sent the next write to the token's default workspace. Now `switch_team` saves it with `PUT /memory/routing/session {workspace_id, client_key}` (svc-api re-checks membership: a 403 or 409 is surfaced and nothing is stored) and every tool call reads it back with `GET /memory/routing/session?client_key=...` (bearer only) before the backend is built, then sends the pick as `X-Workspace-Id` as before. The client key is `mcp:<clientInfo.name>` (fallback `mcp:hosted`), so every session of the same person in the same MCP client shares the pick. A failed read (service down, 5xx, 404) logs a WARNING and uses the token's default workspace, never another one. `_session_teams` is removed. Deploy svc-api (with the routing routes) first. Tests: new `tests/test_hosted_session_record.py` (switch, restart with a new instance and session id, non-member refusal, read 503/404, bearer-only read, no dict left), `_hosted_fixtures.RoutingSessionStore`; `test_hosted_identity.py` and `test_hosted_tools_allowlist.py` run against that store (the per-session isolation test now asserts sharing across sessions of one caller and client).

- CORE-2.0-VISIBILITY-GATE-ALWAYS-ON-1 (tenancy, 2026-10-31, owner decision): the read visibility gate is always on. `LocalBackend.list_memories` always pages on core's frozen walk (the legacy `search("*")` window arm is deleted), `transcript_tools` no longer reads `sm2_search_visibility_gate` to decide the fetch-bound note, and `memory_tools._gate_on()` is replaced by `_core_marks()` (core importable). OFF-arm rows in `tests/test_gated_output_local_clients.py` and `tests/test_read_status_normalizers.py` are deleted (arm retired by owner decision). Needs the core `wk/CORE-2.0-VISIBILITY-GATE-ALWAYS-ON-1` change.
- MCP-2.0-PATH-ID-GUARD-PORT-1 (2026-10-31, security, Urgency: security): port of main's MCP-HOSTED-DECISION-TOOLS-1 fix rounds 1-2 (`60d023a`, `7d1dbee`) to 2.0. Every id a tool argument places in a request path (15 sites: `RemoteBackend` get/explain/update/delete and the nine decision methods, `agent_set_recall_profile`, `agent_get_recall_profile`) now passes through `path_id(value, under=<prefix>)` in `backends/path_guard.py`, which refuses (`ValueError`, no request sent) an id containing `/` or `\`, an empty id, `.`, `..`, control characters or a static route name at that position, and percent-encodes everything else as one segment. Before, `memory_delete("clear-all")` sent `DELETE /memory/clear-all` (wipes every memory the caller authored) and `teams/<id>` reached `DELETE /memory/teams/{team_id}`. `RESERVED_SEGMENTS` and `tests/fixtures/service_route_table.json` come from smart-memory-service origin/2.0 `bf6be16117f3` (`gen_route_table.py` in the unit folder). The test no longer auto-discovers a sibling service checkout (it may sit on another branch): the fixture is the table unless `SMARTMEMORY_SERVICE_CHECKOUT` names one. Tests: one row per request site, one row per tool with an id argument, every reserved segment at every site.
- MCP-LITE-BIND-BILLING-MODE-1 (2026-10-29, Urgency: user-visible): `main()` on the non-hosted (Lite) path now calls core `smartmemory.billing.mode.bind_local_mode_for_lite()` after the hosted return, so a keyless local server resolves `billing.mode=local` (an explicit `SMARTMEMORY_BILLING_MODE` is kept) instead of the `enforce` default that fails closed once BILL-6 meters LLM calls. The hosted branch is unchanged and stays `enforce`. Minimum core for the bind: core 2.0 (has `smartmemory.billing.mode`); an older core logs one WARNING ("core without smartmemory.billing; billing mode not bound for Lite") and the server starts without the bind. Test: `tests/test_lite_bind_billing_mode.py`, one table covering both core arms and the hosted arm.
- WRAPPER-2.0-RECEIPT-COLLAPSE-1 (2026-10-26, Urgency: user-visible): fix(save): `memory_save` on Lite now answers the real core save receipt (`receipt`, `summary`, and `status` from the receipt) for a text save. `LocalBackend.ingest_with_receipt` reads the wrapper's new receipt doorway `storage.ingest_with_receipt` instead of `storage.ingest`, whose legacy return reads a receipt as its id; a wrapper without the doorway answers the legacy arm with one WARNING. `LocalBackend.ingest` is unchanged. `memory_type="pending"` is stored as is and still has no receipt on Lite; the tool description now says `receipt` and `summary` are None then, never made up. Test: `tests/integration/test_receipt_doorway_lite.py`, one 4-row table on a real Lite store over the wrapper and the tool, with its save-then-read census dispositions (`local_inline`).
- REL-CALLERS-ORIGIN-TAGS-1 (2026-10-26): `LocalBackend.add` and `LocalBackend.ingest` (the `memory_save` paths) default the write origin to `mcp:memory_save` (was `mcp:memory_add` / `mcp:memory_ingest`). Core registers `mcp:memory_save*` at tier 2 first; the legacy tags stay registered for items already stored. A caller-supplied origin still wins. `tests/test_dist_lite_quiet_local_origin.py` updated.
- MCP-CODE-DEPS-REMOTE-CONFORMANCE-1 (2026-10-26, Urgency: user-visible): `RemoteBackend` (and so `HostedRemoteBackend`) now implements `code_dependencies`, delegating to `GET /memory/code/dependencies` with `entity_name`, `direction` and the `repo`, `file_path`, `item_id` selectors sent only when set, so `test_backend_conformance[RemoteBackend|HostedRemoteBackend]` is green again (it failed on `code_tools.py:549` since CODE-INDEXER-HARDEN-1 U5 added the tool call with only a local method). The `code_dependencies` tool now has one dispatch (`backend.code_dependencies`) instead of a duplicate inline REST arm, so the request the hosted tool sends is unchanged. A backend with no dependency analysis now logs one WARNING before returning "Dependency analysis is unavailable on this backend.". Tests: new `tests/test_remote_code_dependencies.py` (7 rows: 6 request rows across both remote classes, each through the tool and the direct method, and the unsupported-backend arm).
- DEPS-LATEST-2.0-1-py (2026-10-22): no exact pins and no lock or Dockerfile in this repo; `fastmcp>=4.0,<5` already admits the latest 4.1.3 (the `<5` major cap is deliberate) and `httpx>=0.27` admits 0.28.1, so nothing was bumped. Verified `tests/` on fastmcp 4.1.3 / starlette 1.9.0 against fastmcp 4.0.0 / starlette 0.52.1: the same 30 failures and 16 errors on both (spaCy model, `memory_service` and `smartmemory_app` not installed in the test venv), none new.

- OPS-PREPUSH-BASE-FROM-MERGEBASE-1 (user-visible, tooling, 2026-10-30): **the cross-repo pre-push hook no longer refuses a new `wk/*` branch cut from `2.0` for imports that already exist on 2.0.** The hook picked the comparison branch from the push target, so any target other than `2.0`/`main` fell back to `origin/main`. A target that is neither now resolves its base from the pushed commit: whichever of `origin/2.0` / `origin/main` is the nearest merge base (fewest commits the pushed commit carries beyond it, tie to `main`), compared against the same-named branch of each sibling (one WARNING fallback as before). Tags and an unreadable sha keep the old behaviour. Kill switch `PREPUSH_BASE_FROM_MERGEBASE=0`; `PREPUSH_TARGET_BRANCH=0` still works. Test: one new 10-row table in `test_pre_push_hook_target_branch.py`.

- OPS-PREPUSH-HOOK-WORKTREE-GITDIR-1 (user-visible, tooling, 2026-10-28): the cross-repo `scripts/hooks/pre-push` no longer falsely blocks a push from a linked git worktree. Git exports an absolute `GIT_DIR` into hooks, which sent every `git -C <sibling>` call at this repo, so every symbol was reported missing from the sibling. All git calls in the hook go through one `run()` that drops `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR` and the other repo-locating variables (the service copy already had this from an earlier fix; the MCP copy gains it here). The BLOCKED text no longer offers `git push --no-verify`: it says to verify the symbol is really missing from the sibling ref, and if it is there to report it to your lane lead and not bypass. Test: `tests/test_pre_push_hook_worktree_rows.py` (one ROWS table: worktree and main checkout, symbol present and absent, remedy text; red at base: 3 of 4 rows).

- MCP-SWITCH-WORKSPACE-NAMING-SWEEP-1 items 5 and 8 (security-adjacent, 2026-10-28): **a `switch_workspace` made while a successful `memory_migrate('local')` is exporting is no longer dropped without a word.** The migrate parks the workspace the export read from (that is where the data came from, so `memory_migrate('remote')` returns it there), but before this the switch's own reply said "Switched to workspace: Alpha (ws-a)" and the migrate reply never mentioned ws-a again. Now the reply adds `Your switch to ws-a during the export was not kept: the migrated memories came from ws-b. After memory_migrate('remote'), run switch_workspace('ws-a') to use it.` and one WARNING names both workspaces (also when there was no pick before the export). The failure paths were already covered by MCP-SWITCH-REVERT-LIVE-WS-1 and MCP-SWITCH-INFLIGHT-REBUILD-1. Item 5 (hosted Team-vs-workspace conflation) was fixed by WS-ROUTING-5 (`GET /memory/workspaces`, not `/memory/teams`), but no test served a Team that is not a workspace, so a regression would have passed. Tests: new `tests/test_switch_workspace_sweep.py`, one 12-row table: hosted and stdio switch by workspace id and name, a Team id or Team name refused, a non-member refused, the hosted listing shows workspaces only (fakes serve `/memory/teams` with a Team that is no workspace, and omit a shared workspace from it), and four migrate rows (switch then round trip keeps the switch, mid-export switch named on a move to local with and without a prior pick, round trip after it returns the data to its source). Mutants killed: re-reading Teams on hosted (5 rows) or stdio (11), dropping the parked-workspace restore (2), dropping the new notice (3).
- MCP-SWITCH-WORKSPACE-NAMING-SWEEP-1 fix round 1 (review M1, L1-L3, 2026-10-28): **`memory_migrate('local')` now pins its export to the workspace it parks.** `RemoteBackend.export_okf(archive_path, workspace_id=...)` sends `X-Workspace-Id` for that id, or no header when none was picked, so a `switch_workspace` that commits between the pick capture and the export's header build can no longer make the export read another workspace (the park, the notice and a later `memory_migrate('remote')` always agree on the source). The mid-export WARNING no longer logs workspace ids, the header-less notice says "the workspace the service routed the export to", and the stale live test `tests/integration/test_hosted_team_workspace_live.py` is rewritten against the workspace listing (it imported the removed `_list_team_ids`). New sweep row `migrate-switch-before-export-headers-then-local`.

- MCP-REGISTRY-REMOTE-ONLY-1 (user-visible, 2026-10-27; owner install paths locked 2026-10-22): **the official MCP registry entry (`server.json`) is remote-only.** It now lists one `remotes` entry (`streamable-http`, `https://mcp.smartmemory.ai/mcp`) and no `packages` block, so the registry points everyone at the hosted server instead of a pypi/stdio install. Version corrected from the stale 1.4.96 to 1.5.23 (`pyproject.toml`). README Quick start now leads with the hosted URL (`claude mcp add --transport http ...`), then the pip/local install. No other listing files exist in this repo. Publishing the entry to the registry is owner-only and not done here.

- WS-ROUTING-5 (security-tagged, user-visible, 2026-10-27; WS-ROUTING-1 header rule; folds in WS-ROUTING-4-FU-MCP-SESSION-REFRESH-1): **the MCP sends `X-Workspace-Id` only for a workspace you picked, and a hosted `switch_workspace` no longer lapses after 24 h of use.** Before, stdio copied the `/auth/me` default into the header and hosted echoed the session's workspace as the header, so every write was routed `call`, the server never refreshed the session pick, and 24 h after a switch writes went silently to the default workspace. Now: `X-SM-Client` (`mcp:<clientInfo.name>`, else `mcp:stdio`) is always sent; `X-Workspace-Id` only for a tool `workspace_id` argument (new, optional, on `memory_add` and `memory_ingest`; Lite refuses it), `SMARTMEMORY_WORKSPACE_ID` / `SMARTMEMORY_TEAM_ID` / the saved config team, a `login` team, or `switch_workspace` on stdio. Hosted holds no pick at all (the container env can never become one): svc-api applies the switch from the session lever and refreshes it on each write. The `/auth/me` default is display only (`default_team_id`; `active_workspace_id` displays the pick else the default, new `selected_workspace_id` is the pick). The MCP never turns the default, an explain answer or a cached value into a header: a header-less call the service cannot route (400 `missing_team_context`) is never retried and comes back as a tool error that says how to pick (`switch_workspace`, `list_workspaces`, or the `workspace_id` argument). The flag-off floor to the account default is the service's job (WS-ROUTING-5-PRE-1, which also makes the `user_only` write routes such as `/memory/add` resolve the session pick); this unit lands after it. There is no client-side floor stand-in (removed in fix round 1 after the security review found it wrote to the default when explain was unreadable or after a later server-side pick). Every write tool's result ends with `Workspace: <name> (<id>), routed by <lever>.`; `whoami` prints `Workspace:` and `Routed by:` with how to change it. Hosted `switch_workspace` and the new `list_workspaces` tool (stdio and hosted) read `GET /memory/workspaces`, not the caller's Teams (MCP-TOOL-SURFACE-1 section 2), and a hosted listing failure no longer shows the internal svc-api URL. `X-SM-Context` is not sent yet (WS-ROUTING-6). Tests: `tests/test_header_rule.py` (one row table per behavior, plus the 26 h continuous-use row, red on `11de00a`), `tests/test_no_literal_workspace_ids.py` (S3), and the resolver fake in `tests/_hosted_fixtures.py` (`RoutingSessionStore`: session TTL, refresh on write, floor flag, and `MCP_ROUTE_POLICY`, the real service policy of every route the MCP calls; its default models WS-ROUTING-5-PRE-1's target and `pre_landed=False` today's service). The review's three probes (unreadable explain, a server-side pick after a refusal, hosted explain reporting the session store unavailable) are rows in `test_header_rule.py`, red on the review's commit `b56958a`.

- WS-ROUTING-4 (user-visible, 2026-10-27; lever 3, WS-ROUTING-1 gap 6): **a hosted `switch_workspace` now survives a reconnect and a restart of the hosted server.** The switch used to live in a per-MCP-session dict (`_session_teams`) in `hosted/middleware.py`, so a new MCP session id or a new process lost it and the next write went to the account default. Now `switch_workspace`/`switch_team` save it with `PUT /memory/routing/session {workspace_id, client_key}` (svc-api re-checks membership: 403 not a member, 409 for a workspace-bound key, and nothing changes on a refusal) and every tool call reads it back with `GET /memory/routing/explain?client_key=...` (bearer only, no `X-Workspace-Id`) before the backend is built. The client key is `mcp:<clientInfo.name>` from the MCP `initialize` handshake (fallback `mcp:hosted`), so every session of the same person in the same MCP client shares the pick, and another person or another client never sees it. The read fails closed: a 5xx or transport error refuses the call with a WARNING instead of guessing the default, a 401 is Unauthorized, and a 404 (an svc-api older than WS-ROUTING-4) falls back to the default with a WARNING. Deploy svc-api first. Each hosted tool call now costs one extra svc-api round trip (no cache, so every process sees the same pick). `whoami` reports the routed workspace. Tests: new `tests/test_hosted_session_record.py` (reconnect, restart, no switch, 403, 409, explain 503/404/401, no workspace header on the read, no `_session_teams` left), `tests/test_hosted_identity.py` (the per-session isolation test now asserts same-caller-same-client sharing and other-caller isolation), and the hosted switch rows in `test_hosted_tools_allowlist.py` and `test_switch_workspace.py` run against an emulated `routing_sessions` store (`tests/_hosted_fixtures.RoutingSessionStore`).

- MCP-FIRST-RUN-HINT-1 (user-visible, 2026-10-26): **the hosted (remote) MCP server's first successful tool result in each session now carries one line, "`pip install smartmemory` for the sm CLI", and never again in that session.** It is an extra text block after the tool's own result, claimed after the tool succeeds (a first call that errors leaves it for the next), keyed by the MCP session id (no session id, no hint; the set is capped at 4096 sessions). It never mentions local mode. Auth refusals in the hosted middleware (no access token, a rejected or expired credential) now say what to do next: sign in with the client's OAuth prompt, or send a SmartMemory API key as `Authorization: Bearer <key>` created in the web app, instead of a bare `Unauthorized: ...`. All in `hosted/middleware.py`, no service-side change. Tests: `tests/test_hosted_first_run_hint.py` (8 rows: once per session, per-session, error-first, no session id, eviction bound, both auth messages, and a wire-level run over an API key and an OAuth session asserting no response text contains "local mode" or "--local").

- MCP-INSTALL-LOCAL-MODE-1 (user-visible, 2026-10-26; MCP-PERF-1 Question 1): **the "mode=local but REST" warning now says how to fix it.** MCP-PERF-1 made REST the default backend, so a Lite user whose entry lacks `SMARTMEMORY_MCP_MODE=local` gets REST and a WARNING instead of their local store. `smartmemory mcp install` (smartmemory wrapper, same unit) now writes `SMARTMEMORY_MCP_MODE=local` into the entry for a saved `mode=local` install, and the warning tells an existing user to re-run `sm mcp install <client>`. Land this with the wrapper change, before any release that carries MCP-PERF-1.

- MCP-TEST-SHARED-DB-GUARD-1 (data-continuity, 2026-10-26; MCP-PERF-1 land-report Finding 1): **the mcp test suite can no longer reach the shared FalkorDB 9010, Redis 9012 or Mongo 9013.** `test_hold_receipt.py`, `test_code_disambiguation.py` and `test_framework_semantics.py` hardcoded 9010 (the last honoured `FALKORDB_PORT` but defaulted to it), so a plain run wrote `test_*` graphs onto the dev graph. New `tests/_falkor.py` resolves `FALKORDB_*` then `GRAPH_DB_*`, defaulting to port 1 so FalkorDB rows skip; the three files use it. New `tests/conftest.py` exports the endpoint into both families (so product-built clients follow) and refuses a loopback socket connection to 9010/9012/9013 with one WARNING naming the port (`SMARTMEMORY_TEST_ALLOW_SHARED_DB=1` opts out). Test: `tests/test_shared_db_guard.py` (refuse rows, opt-in, remote host, private port, default port, no literal shared port under `tests/`, and a child pytest run of the three files that records every connect and sees none to a shared port).

- MCP-SWITCH-INFLIGHT-REBUILD-1 (data-continuity, 2026-10-25; MCP-SWITCH-FUS review M2 + L2): **a `switch_workspace` still looking up your workspaces when `memory_migrate` rebuilds the backend is now refused, not applied to the old backend.** Before, the switch took the process backend, waited on `GET /memory/workspaces`, and then wrote its workspace onto that object even though the migrate had replaced it. After a failed import the revert had already rebuilt on the previous workspace, so the next write went there while the switch's reply, arriving last, said "Switched to workspace: Alpha (ws-a). The next request uses it." The stdio switch now resolves first (`RemoteBackend.resolve_switch`) and commits under `dispatch.rebuild_lock` only while that object is still the process backend (`tools/common.commit_if_current`). Otherwise it replies `Workspace not switched: memory_migrate replaced the backend ...` and names the workspace in use. The revert reads the live workspace and rebuilds under the same lock, and its comment now says why nothing writes the old backend after the forward rebuild. L2: the forward rebuild (including a local-to-remote build carrying the parked workspace) now runs inside the revert `try`, so a build that raises reverts the config and the process mode. Tests: `tests/test_switch_workspace.py` REBUILD_ROWS gain the in-flight switch across a failed and a successful move (thread held on an Event inside the listing, no sleeps, bounded joins) and the forward-rebuild-raises row. A new bounded test races 4 switching threads against 4 migrations to check nothing deadlocks.

- MCP-WORKSPACE-ENV-PRECEDENCE-1 (user-visible, 2026-10-25; MCP-SWITCH-FUS review M1 + L1 + L3): **`SMARTMEMORY_WORKSPACE_ID` now wins over `SMARTMEMORY_TEAM_ID` on installs that have the `smartmemory` package too.** MCP-SWITCH-FALLBACK-ENV-HINT-1 changed the order only in `RemoteBackend`, but `build_backend` passed the smartmemory config's `team_id` first, and `load_config()` overlays `SMARTMEMORY_TEAM_ID` onto it, so on every `pip install smartmemory` the legacy variable still won with no warning. `build_backend` now applies the env rule before the config: `SMARTMEMORY_WORKSPACE_ID`, then `SMARTMEMORY_TEAM_ID`, then the saved config workspace, then the account default, with one WARNING when both variables differ. `switch_workspace(save_as_default=True)` and `memory_migrate` now save the config file layer only (`load_config(apply_env=False)`), so they no longer write the shell's `SMARTMEMORY_TEAM_ID` into the file as the saved default; a smartmemory without `apply_env` gets one WARNING. The save reply says when an env var will override the saved default in new sessions, and the package-absent hint no longer claims a saved config overrides both variables. Hosted `switch_workspace` now says why the listing failed (`read:teams` missing for an API-key caller, or the HTTP error), as stdio does; OAuth callers use an exchanged JWT that the service never scope-checks. Tests: the precedence table now goes through `build_backend` for both install shapes (bare mcp, real `smartmemory_app.config`), plus save-reply and hosted-refusal rows.

- MCP-SWITCH-FALLBACK-ENV-HINT-1 (user-visible, 2026-10-25; MCP-SWITCH-WORKSPACE-1 review M2): **`SMARTMEMORY_WORKSPACE_ID` now wins over the legacy `SMARTMEMORY_TEAM_ID`.** The save-default fallback told users to set `SMARTMEMORY_WORKSPACE_ID`, but `remote.py` read `SMARTMEMORY_TEAM_ID` first, so a user with the old variable set stayed on the old workspace without a warning. The new name now takes precedence, one WARNING names both when they differ, and the hint says the older name is overridden. (Only on installs without the `smartmemory` package, and the hint's last sentence was wrong: fixed by MCP-WORKSPACE-ENV-PRECEDENCE-1 above.) A test follows the hint literally against the precedence table. Users with only `SMARTMEMORY_TEAM_ID` set see no change.

- MCP-SWITCH-READ-TEAMS-REFUSAL-1 (user-visible, 2026-10-25; MCP-SWITCH-WORKSPACE-1 review M1): **`switch_workspace` now says why it cannot list your workspaces.** A key without the `read:teams` scope got `could not list your workspaces at <url>`, which read as an outage. The refusal now says the key lacks `read:teams` and to create one with Read teams enabled, and an outage names the HTTP status. The service still checks membership on every request, so no id is accepted unverified. README notes the scope. The docs page `mcp-integration.mdx` (smart-memory-docs) still needs the same sentence.

- MCP-SWITCH-REVERT-LIVE-WS-1 (security-adjacent, 2026-10-25; MCP-SWITCH-WORKSPACE-1 review-r2 M1 + L1): **a failed `memory_migrate` now reverts to the workspace the session is on at revert time.** Before, the revert carried the workspace captured before the export, so a `switch_workspace` made while the export ran was dropped and the next write went to the old workspace. The park after a successful remote-to-local move still keeps the pre-export workspace. The process-backend rebuild and the first build now hold one reentrant lock (`dispatch.rebuild_lock`), so a parallel tool call waits for the rebuild instead of building the configured default workspace in the gap.

- MCP-SWITCH-WORKSPACE-1 (owner P0, security-tagged, 2026-10-25): **`switch_workspace(workspace)` replaces `switch_team`, and the stdio server now switches live.** The tool takes a workspace id or name (id first, then a unique case-insensitive name) on stdio and hosted, and the next request sends the new `X-Workspace-Id` with no restart. Before, stdio `switch_team` on a remote backend was a stub that told the user to restart. Stdio remote resolves the name against `GET /memory/workspaces` (sent without `X-Workspace-Id`, so a stale selection cannot block switching away from it); hosted keeps its `/memory/teams` membership check. The service still enforces membership on every request. A refused switch (not a member, ambiguous name, listing failure) logs one WARNING and says which workspace is still in use. Stdio takes `save_as_default=False`: `True` writes the workspace to the CLI config. Local (Lite) mode accepts only `default`. `switch_team(team_id)` stays registered for one release as a deprecated alias (one WARNING per process, a notice in the result, never persists). `whoami` prints User, Tenant and Workspace on every backend, and `login` reports "Logged in as X, workspace: Y". No tool takes a tenant argument. Counts: stdio FREE 31, PRO 69, PRO+ 100 (was 30/68/99), hosted 28 (was 27). `list_workspaces` is deferred to WS-ROUTING-5. Tests: new `tests/test_switch_workspace.py` (backend x name/id/unknown rows, 403 after a switch, 503 on the listing, save_as_default, alias warning, whoami fields, the `/memory/workspaces` route against the service OpenAPI snapshot, a schema walk with no tenant parameter anywhere), plus the tier, hosted allowlist, annotation and route-contract counts.
- MCP-SWITCH-WORKSPACE-1 fix round 1 (security, review H1): **`memory_migrate` no longer drops a live `switch_workspace`.** Before, a failed migrate rebuilt the remote backend from the config, so the next write silently went to the configured default workspace. Now the live workspace is captured before any rebuild. Both revert paths (all items failed, import raised) carry it into the rebuilt `RemoteBackend` and say `Still on workspace: <id>.` Remote to local says the remote workspace is no longer active and logs one WARNING naming it; the next local to remote migrate in the process carries it back. Every move to remote names the workspace the import landed in. `dispatch.set_process_mode(mode, workspace_id=None)` and `build_backend(mode, team_id=None)` take the carried id. Tests: one table row per rebuild site in `tests/test_switch_workspace.py` (next write's `X-Workspace-Id` and the message text).

- MCP-PERF-1 (owner P1, 2026-10-24): **the MCP uses the REST backend by default.** `backends/dispatch.py` no longer picks `LocalBackend` because `smartmemory` is importable or because the CLI config says `mode = "local"`: the in-process store needs `SMARTMEMORY_MCP_MODE=local`. Measured before: each stdio session's first `memory_search` took 61 s and grew the process to 4.1 GB (embedding model, spaCy, reranker), and the boot warm-up alone added ~530 MB and ~10 CPU-s per session with a transcript store present. Now: `SMARTMEMORY_MCP_MODE` unset/`remote` gives `RemoteBackend` (URL from `SMARTMEMORY_API_URL`, else the CLI config's remote URL, else api.smartmemory.ai); `local` gives `LocalBackend`; any other value logs one WARNING and uses REST. At boot a background `GET <url>/health` logs one WARNING naming the URL when the service does not answer, and tool calls return the existing "API unreachable at <url>" error; there is no in-process fallback. The transcript-model warm-up runs only in local mode, and the transcript tools (local-only store) log a WARNING when they open it from a REST-mode process. `memory_migrate` now switches the process mode with the config and refuses a migration onto the backend it already runs on (before, with the new default, `migrate remote` would have copied remote onto itself). A configured install with no API key still gets an unauthenticated REST backend (tools/list works, `whoami` points at `login`); no package and no key still raises, now naming `SMARTMEMORY_MCP_MODE=local`. Tests: `tests/test_backend_dispatch.py` (rewritten: one row per mode x service state, a real loopback `/health` server, every REST row fails if the in-process store is touched), new `tests/test_rest_default_boot.py` (warm-up and probe per mode, transcript-store warning), `tests/test_portability.py` (migrate rows), `tests/test_local_backend_lazy.py` (opts into local).

- OPS-PREPUSH-HOOK-DYNAMIC-EXPORTS-1 (internal tooling, security-adjacent, 2026-10-22; LAND-GAP-1 land-report4 Q1): `scripts/hooks/pre-push` no longer calls an import missing when the sibling module exports the name without a plain top-level `def`. A name now counts as exported when the module binds it (def, class, assignment including tuple unpacking, `import x as y`, `from m import x` re-export), lists it in `__all__` (`=`, `+=`, `.extend`, `.append`), answers it from a module `__getattr__` that names it in a string literal (`name == "auth_provider"`, `name in (...)`, `name in _LAZY`, `_LAZY[name]`, `_LAZY.get(name)`: the common `service_common.auth.core.auth_provider` case that forced a `--no-verify` push), or star-imports it (`from .impl import *`, followed through the sibling, cycle-safe, underscore names excluded). A `__getattr__` with no literal names, or a star import the hook cannot resolve (stdlib, third party), accepts the name with one WARNING line saying so; a name nothing exports, a name outside a literal `__getattr__` or `__all__`, and a missing module are still refused. Tests: new `tests/test_pre_push_hook_dynamic_exports.py` (29 rows); the 37 existing hook cases pass unchanged. Same change as `smart-memory-service` `scripts/hooks/pre-push`; this copy also gains the nested-block lookup (try/if blocks) it lacked.
- OPS-PREPUSH-HOOK-TARGET-BRANCH-1 (internal tooling, security-adjacent, 2026-10-22; LAND-GAP-1 land-report3 Q2, FWD-MERGE-3): `scripts/hooks/pre-push` compares new cross-repo imports against the branch the push targets, not always `origin/main`. The hook reads the target from its stdin (`refs/heads/2.0` gives `origin/2.0` of this repo and of each sibling, `refs/heads/main` gives `origin/main`), so a service 2.0 push that uses a 2.0-only common symbol (`get_write_scope_provider`, common `service_common/auth/core.py:318`) no longer needs `--no-verify`. A non-branch target (tag, no stdin) or a sibling with no such branch falls back to `origin/main` with one WARNING, deletes check nothing, several refs in one push are each checked, and an unreachable sibling is still skipped with a notice. `PREPUSH_TARGET_BRANCH=0` restores the old always-`origin/main` behaviour. Tests: new `tests/test_pre_push_hook_target_branch.py` (17 cases: main, 2.0, unknown, tag, missing sibling branch, own-baseline, delete, multi-ref, kill switch, offline); the 20 existing hook cases pass unchanged. Same change as `smart-memory-service` `scripts/hooks/pre-push`.
- DEV-ONBOARD-FU-MCP-DECISION-TOOLS-FREE-1 (owner pick, option a): the 13 decision tools (`decision_create`, `_get`, `_list`, `_search`, `_supersede`, `_retract`, `_reinforce`, `_contradict`, `_provenance`, `_find_conflicts`, `_create_pending`, `_resolve_requirement`, `_try_activate`) are registered in the FREE tier, so a keyless local server lists and accepts them, matching the Python SDK where `add_decision` needs no key. Before, `_register_tools` registered them only at `tier >= PRO`, so a Claude Code or Codex user with no API key never saw them. Only the decision suite moved: every other PRO and PRO+ tool keeps its tier, and the hosted allowlist (`hosted/tools.py`, no decision tools) is unchanged. FREE is now 30 registered tools (was 17), PRO 68 and PRO+ 99 are unchanged totals. The local server instructions no longer say "when it is available" for `decision_create`. Tests: `tests/test_decision_tools_free_tier.py` (13 rows listed keyless, 13 rows called keyless through FastMCP, 4 rows proving other PRO tools stay PRO, a completeness guard), `tests/test_tier_registration.py` (FREE count and names).

- SM-MCP-FLAT-LAYOUT-1: `pyproject.toml` now declares the package explicitly (`[tool.setuptools.packages.find] include = ["smartmemory_mcp*"]`). Before, setuptools flat-layout auto-discovery ran on every build, so any stray top-level directory next to `smartmemory_mcp/` (an empty `scratch/` was enough) failed the build with `Multiple top-level packages discovered in a flat-layout` and `uv run smartmemory-mcp` exited before any MCP traffic, which Claude Code reports as CONNECTION_CLOSED. The built wheel is identical (same 58 files). Checked with an empty `scratch/`, `build/` and `scripts/` present.

## 1.5.23 — 2026-10-17

- MCP-HOSTED-DECISION-TOOLS-1 fix rounds 1-2 (security): every caller- or model-supplied id placed in a request path now passes through `path_id(value, under=<prefix>)` in `smartmemory_mcp/backends/path_guard.py`, which REFUSES (raises `ValueError`, no request sent) any id containing `/` or `\`, an empty id, `.`, `..`, control characters, and any id equal to a static route segment the service has under that prefix (`RESERVED_SEGMENTS`, derived from the service OpenAPI snapshot). Everything else is percent-encoded as one segment, so normal ids (uuid, `mem_...`, `dec_...`) go out unchanged. Before, all of these were reachable from a tool call: `decision_id="../../memory/clear?"` turned `POST /memory/decisions/{id}/conflicts` into `POST /memory/clear`; `memory_delete("clear-all")` (no tricks needed) sent `DELETE /memory/clear-all` and wiped the caller's memories; and because uvicorn decodes `%2F` to `/` before routing, an encoded id such as `memory_delete("api-keys/K1")` or `memory_delete("teams/T1")` still reached `DELETE /memory/api-keys/{key_id}` or `DELETE /memory/teams/{team_id}` after percent-encoding alone (fix round 1). All are refused now. Covers the 13 by-id sites in `RemoteBackend` (`get`, `explain`, `update`, `delete`, the 9 decision methods) and the two agent recall-profile tools. Tests: `tests/test_path_id_encoding.py` decodes each sent URL the way uvicorn does and matches it against the service route table (snapshot, or the checked-in `tests/fixtures/service_route_table.json`): every site x hostile id must be refused or reach only its own route; a completeness test fails when the service adds a sibling route the reserved list lacks; an AST guard fails when a new path-building site skips `path_id`.

## 1.5.22

- LOCAL-FROZEN-PAGE-1 (R11(d)): gate ON, local `list_memories` pages on core's frozen walk (`read_gated_roots_page`) instead of re-running the wildcard search per page, so membership and `total` are fixed at a walk's first page whatever is written or deleted between pages. `memory_list` keeps `limit`/`offset` (no cursor reaches a client): the backend remembers, per metadata filter, the cursor that continues each page it served, offset 0 starts a new walk, an offset with no page on record starts a walk and skips to it, and a rejected cursor logs one WARNING and restarts. The page carries the frozen total, so `memory_list` prints `Showing N of TOTAL`; the 200-row fetch window no longer applies ON. Rows carry core's `read_status` built from the stored node. A row still pending in the intake store is not a graph root, so it is not listed until it lands (merged pending listing is U5.8); search and get still return it. A core refusal is logged once by core and marked so the tool does not log it again. Gate OFF is unchanged.

- U5.21: `transcript_search` and the local `list_memories` read core's search coverage. Gate ON, a partial answer (scan budget spent, `complete` False, ...) adds "The search was partial (core reported: ...)" to the transcript answer, empty or not, and one WARNING to `list_memories`, instead of presenting a short page as all there is. An unreadable coverage is a typed refusal, never read as complete. Gate OFF text, results and logs are unchanged, and local `search` already passes the `read_status` carrier through (U5.22).
- U5.21 fix round 1: a partial answer with no visible row is no longer "No results found". `memory_search`, `memory_recall`, `memory_list`, `get_working_context` (new `partial_answer` key, only when partial), `dev_query_decisions`, `dev_load_context`, `code_search`, `reasoning_query_traces` and `evolution_status` read the completeness core's answer carries through the wrapper and `LocalBackend` normalizers (`partial_reasons`, `partial_note`, `none_found` in `backends/models.py`) and say "No visible X in the part core examined ... not proof that none exist". `memory_list` no longer logs a second WARNING for a core refusal that core already logged (it reads core's `refusal_logged` mark). Gate OFF text is unchanged.
- U5.21 fix round 2: `memory_list`'s failure handler reads core's refusal mark only with the gate ON; OFF it logs the base's line for every failure, marked or not (the fix-1 handler dropped it for a marked one). Local `list_memories` and `transcript_search` never use a row past their fetch (core's `gated_fetch_bound`), and a full list window carries `fetch_bound` when ON. The partial note now reads "The answer was partial (<reasons>), ...", since `fetch_bound` and the lost-lookup reasons are not core's.
- U5.21 fix round 3: `transcript_search` with a `project` filter and the gate ON decides its fetch-bound note from the rows fetched, before the project filter, so an empty or short filtered answer from a full fetch still says "Filtered from the top N semantic matches; a session ranked below that is not shown" (it was dropped when the filter left nothing). OFF is unchanged.

- U5.21 landing (tests only): the 21 save-seeding units of `tests/test_gated_output_local_clients.py` carry `local_inline` dispositions in the save-then-read census and the fixture is regenerated, so the U4.42 census guard passes on the merged tree.

- U4.42-FU-TOOLS (G56/G140, breaking, no aliases): the six ingest-shaped tools `memory_ingest`, `memory_add`, `memory_distill`, `memory_ingest_conversation`, `memory_ingest_structured` and `memory_ingest_document` are removed. `memory_save` (FREE: free text through the pipeline, `memory_type="pending"` stored raw, or a registered-schema record in `data`), `memory_save_document` and `memory_save_conversation` (PRO; one user/assistant turn pair is the old distill, anything else a transcript) replace them, with no extraction knob. Each returns a one-line summary plus structured content (`status` saved|held|queued|failed, the id, the save `receipt` or None, its `summary`), reading both the legacy id/dict and the save receipt. `backends/receipts.py` reads a service receipt per `SM2-SAVE-1/save-receipt-contract.json` (any 1.x, an unsupported major or an `admission_error` is a typed `WireSaveRefused`/`WireSaveRejected` with one WARNING); the service 2.0 receipt route is not live, so it is tested against contract fixtures and a fake. `*_with_receipt` doorways on the local and remote backends carry the save tools. Hosted advertises `memory_save` and `memory_save_conversation` (25 tools); `memory_save_document` stays local (path-taking). `links` on `memory_save` and `text` on `memory_save_document` are not accepted yet (no backend path).

- U49.A2 (tests only): `tests/public_wait.py` prepares explicit public waits for the local and remote backends' save-then-read suites (`await_public` with `observe_get`, reading the G43 `read_status` carrier through `backend.get`): a queued save still pending at the deadline is a visible `client_timed_out`, a failed or dead run is reported at the first read with no retry, a held run is `refused`, and a malformed carrier or an outage is a typed `PublicWaitRefused` with one WARNING. `scripts/save_then_read_census.py` derives the save vocabulary from the backend classes and the registered tools, and `tests/data/save_then_read_census.json` holds one disposition per saving test: mocked, local-inline, explicit-sync, or deferred (the three real-core hold tests, owned by C49-RW-HARNESS-1). A carrier with a missing or null `processing_stage` is refused, never read as `stored`. The 13 rows U4.42 will add are owned by U4.42 until its test file lands. The census refuses a deferral owner unless the whole string is a graph step id or a filed unit id (a prefix such as `NOT` in `NOT AN OWNER` no longer passes).

- U4.32: the local backend reads a core save return as a receipt or a legacy shape, with no switch. `LocalBackend.add` still returns the id string (or the held dict) and the `memory_add` text (`Item ID: <id>`, `Memory held: ...`) is unchanged. A save receipt (object or wire dict) reads as its subject id through the wrapper's `smartmemory_app.save_return`, never `str(receipt)`, and `add_with_receipt` returns a `SaveReturn(value, receipt, summary)` that keeps it. A rejected receipt is a typed `SaveRejected` and an unreadable one a `SaveReturnRefused`, each with one WARNING. A receipt with no wrapper reader installed is one WARNING and a `RuntimeError`, never an id. Legacy returns never import the wrapper, so a core without receipts is unchanged.

- U4.32 review fix: a save receipt that the installed wrapper cannot read now raises `SaveReturnUnavailable` (still a `RuntimeError`) with one WARNING, so callers can tell it from an unrelated runtime failure. No change on the legacy path.

- U4.32 review fix 2 (tests only): the census of `smartmemory_app.storage.ingest` callers resolves module aliases, fully dotted calls, package attribute chains and relative imports, and fails naming file:line on a reference it cannot follow (getattr, a dynamic import, the module passed on as a value).

- U4.32 review fix 3 (tests only, rule 4): the `storage.ingest` consumer census fails closed on the dynamic-import and module-registry surface. `importlib.import_module` and `__import__` are followed under any alias (`from x import y as z`, `import x as y`, `builtins`, as a call receiver) when the module name is a constant, and anything else raises AssertionError naming file:line: a name read at run time, a relative name, the loader or `importlib`/`sys`/`builtins` held as a value, `sys.modules` used other than by a constant key, a star import of those modules, or the module name as a bare string. The two packages keep copies of the helper, pinned to one hash.

- U5.22: `normalize_item`, the local `get`/`search`/`list_memories` paths, `get_working_context` items and the model-facing item allowlist keep core's response-only `read_status` (extraction state, stage, execution state, outcome, revision) when present, so a pending/receipt hit is not shown as stored. Absent on the legacy arm, so its output is unchanged. A present but unreadable `read_status` (not a dict, or no string `extraction_state`) is now a typed `MalformedReadStatusError` with one WARNING instead of being dropped; `normalize_item` checks it before core's `to_dict()` copies it, so a non-dict never escapes as a raw ValueError/TypeError. An absent carrier is unchanged.
- PREPUSH-SUBMODULE-1: the pre-push cross-repo import check no longer refuses `from pkg import name` when `name` is a submodule (`pkg/name.py`) or subpackage (`pkg/name/__init__.py`) that exists on the sibling's origin/main but is not bound in `pkg/__init__.py`, and a namespace package (no `__init__.py`) resolves the same way. A name found only in the sibling's working tree is still refused. Tests: `tests/test_pre_push_hook_submodule_imports.py` (12 rows, run through the hook's `main()` against a throwaway repo and a sibling clone).

- MCP-CAPTURE-ROUTING-1: both servers now send routing instructions at initialize (`smartmemory_mcp/instructions.py`): facts, events and dated statements go to `memory_ingest`. Locally, stated decisions go to `decision_create`. The hosted variant names no decision tool, because the hosted allowlist has none. `memory_add`'s description no longer says it skips the pipeline (false on remote/hosted, where `POST /memory/add` runs extraction). `memory_ingest` and `decision_create` now say when to use them. A `memory_add` on a backend whose `add()` skips extraction (`BackendCapabilities.add_runs_extraction`, False for `LocalBackend`, True for `RemoteBackend`) logs a WARNING naming what is lost (entities, dates, relations, Decision nodes). Tests: `tests/test_capture_routing.py`.

- PREPUSH-SUBMODULE-1: the pre-push cross-repo import check no longer refuses `from pkg import name` when `name` is a submodule (`pkg/name.py`), a subpackage (`pkg/name/__init__.py`) or a namespace directory that exists on the sibling's origin/main but is not bound in `pkg/__init__.py`. Resolution follows Python's import precedence per dotted component (regular package, then module file, then namespace directory): a file module never has children, so `pkg/plain.py` shadows `pkg/plain/child.py` and `from pkg.plain import child` is still refused (review round 1). A name found only in the sibling's working tree, index or local commits is still refused. Tests: `tests/test_pre_push_hook_submodule_imports.py` (20 rows, run through the hook's `main()` against a throwaway repo and a sibling clone).

- MCP-MEMGET-META-1 follow-up: the default `memory_get` output shows one `Created: <iso>` line (after `Type:`, before `Content:`) from the item's `created_at`, falling back to `metadata.created_at`. The value must parse as an ISO-8601 date or datetime (max 40 chars) and the parsed value is re-emitted, so free text cannot reach the output. An invalid value drops the line and logs a WARNING without the raw value. `include_metadata=True` is unchanged.

- MCP-MEMGET-META-1 (owner shape): `memory_get` now returns id, type and content plus at most one line counting conflicts (pointing at `memory_explain` and `memory_get(include_metadata=True)`); `include_metadata=True` is unchanged. The structured read tools (`memory_search`/`memory_recall` with `cite=True`, `get_working_context`, `read_around` items, hosted `code_effects`) return item fields with `metadata` emptied (`code_effects` keeps `effects_bundle`), and the catalog snippet no longer copies the metadata title. A malformed conflict count or non-dict recall metadata logs a WARNING instead of raising. The real A3 note drops from 34,163 to 5,358 characters. REST and SDKs are unchanged.
- MCP-LOCAL-BROKEN-TOOLS-1: local mode no longer advertises 14 tools that only error there (`memory_search_advanced`, `memory_policy_bundle`, `pattern_list`, `pattern_query`, `memory_plan_active`, `memory_plan_create`, `memory_log_failure`, `memory_anchor_list`, `memory_anchor_check_drift`, `dev_load_context`, `zettel_clusters`, `zettel_backlinks`, `zettel_connections`, `zettel_discover`). They use the existing capability gate (`TOOL_CAPABILITIES`, one capability per tool, declared unsupported by `LocalBackend`), so a direct call returns the usual "not supported by the active backend" error. The first listing logs one WARNING naming each hidden tool and its bug (`LOCAL_BROKEN_TOOLS` in `backends/interface.py`). Remote and hosted backends are unchanged. No tool code was removed: delete a tool's row to bring it back once its bug is fixed. `agent_evaluation_get` stays (its empty result is the documented cold-start `None`).
- Default `SMARTMEMORY_WEB_URL` is now https://www.smartmemory.ai (app.smartmemory.ai has no DNS). Filed as PLAT-APP-SUBDOMAIN-1.
- CODE-INDEXER-HARDEN-1 U6: `code_dead_code` takes opt-in `include_exported` and `production_only` (default false; a default call sends the same request and prints the same text). Remote and hosted send them to `GET /memory/code/dead-code` only when set, the local backend passes them to core `local_dead_code`. Release ordering: the options need a smartmemory-core and smart-memory-service release containing U6.
- `code_index` no longer turns the default into an explicit `["python", "typescript"]` request: it passes `languages=None`, so a TS/JS file that fails to extract is skipped with a warning instead of refusing the index (CODE-INDEXER-HARDEN-1 F32). Needs a smartmemory-core release that contains CODE-INDEXER-HARDEN-1 U3; against an older core the default indexes Python only.
- Release ordering: requires a smartmemory-core release containing CODE-INDEXER-HARDEN-1 U3. Against an older core it falls back to Python-only indexing. Do not release MCP before that core.
- CODE-INDEXER-HARDEN-1 F28: local `code_index` without `repo_name` keys the repo by the checkout's normalized git remote URL (or a path hash without a remote) instead of the directory basename, so two checkouts named `api` no longer replace each other. A name owned by another checkout is refused by core. Without smartmemory-core the tool asks for an explicit `repo_name`.
- CODE-INDEXER-HARDEN-1 U5 fix round 1: `RemoteBackend.ingest_code` (core installed) sends the dirty-aware `commit_hash` and `repo_identity` in its upload.
- U5 post-review fix round: Expose and forward file_path and item_id from code_dependencies to REST and local scoped graph resolution. Refuse exact-name/dotted-suffix ambiguity until a selector chooses the root. Real SQLite and FalkorDB tests cover each selector and both backend paths. The hosted filesystem parameter blacklist stays intact. An exact tool/parameter declaration identifies code_dependencies.file_path as a stored-field selector, with live-schema and hosted wire tests proving literal REST forwarding.

- U4.42: `LocalBackend` gains a dormant receipt save, with no switch and no tool calling it. `save_call(...)` builds one frozen call with a stable retry key (a supplied `idempotency_key` verbatim, or a minted `mcp:<uuid4 hex>`), an explicit `wait` and the registered `mcp.local` producer namespace; `submit_save(call)` sends it through `SmartMemory.save` and returns a `SaveReturn`, and `save(...)` does both. Re-submitting a call retries with the same key. It never falls back to `add` or `ingest`, and `supports("save")` is true for Local only (Remote and Hosted are unchanged). `ingest_structured` now reads a receipt as its subject id and `ingest_structured_with_receipt` keeps it; every legacy return is handed back as it was, and a rejected receipt is a typed error with one WARNING. A wrapper without the caller seam is one `SaveReturnUnavailable` with one WARNING.

- U4.42 review fix (round 2): `LocalBackend` tells a receipt from a legacy save return by the wrapper's one rule (subclasses, lookalikes and any mapping with `schema_version`, not just a `dict` or an exact class name), and a return that cannot be inspected is handed to the wrapper's reader, which refuses it. Still dormant: no tool calls the receipt save.
- U4.42 review fix: a wrapper `save_caller` module that lacks one of the names the local receipt save needs is now `SaveReturnUnavailable` with one WARNING (it was a bare `AttributeError`). Still dormant: no tool calls the receipt save, and the `memory_save*` tool names are owned by the follow-up unit U4.42-FU-TOOLS.
- U4.42 review fix (round 3): `LocalBackend` recognises a receipt before its shape (a receipt that also is a string or a mapping is still read as a receipt), and a save return that cannot be inspected is refused on the first failure with one WARNING instead of being inspected again and read as a legacy id. A failure core already logged adds no second WARNING. Still dormant.
- U4.42 land (tests only): the save-then-read census now holds dispositions for the 13 units in `tests/test_local_save_call.py` (8 `save_only` against a real Lite core with a recording manager, 5 `mocked_seam`), drops `OWNED_BY_U4_42`, names the `save_call` getattr probe in `DYNAMIC`, re-points three `NON_MEMORY` lines in `test_local_save_return_compat.py`, and adds `ingest_structured_with_receipt`, `save`, `save_call` and `submit_save` to the pinned save vocabulary.

## 1.5.21 — 2026-10-08

- Format three files that drifted from `ruff format` after 1.5.20 (no behavior change).

- CORE-HOLD-RECEIPT-1 round 2: Preserve held receipts in hosted adds and report memory, conversation, decision, pattern, session and friction holds with the policy reason.

- Carry shared code source-site counts through remote ingestion and show extraction status separately from resolved edges in code-index summaries.

- Format the integrated framework regression file to satisfy the Wave B lint gate.

- Integrated FRAMEWORK with Wave B, preserving scoped liveness and diagnostic evidence.

- FalkorDB dead-code test pins the R7 rule: members of a live class are not dead, members of an unreferenced class are (CODE-FRAMEWORK-SEMANTICS-1, R7-fw).

- FalkorDB local dead-code test pins that a function used only by value (REFERENCES) is not dead (CODE-FRAMEWORK-SEMANTICS-1, R6-fw).

- Framework test keeps its unknown registration in a separate file: since R5 an unknown registration excludes every function in its file from dead code (CODE-FRAMEWORK-SEMANTICS-1, R5-fw).

- Verify unknown framework callbacks remain excluded from local Lite and FalkorDB dead-code tools.

- Show only server-computed hosted code counts and outcomes after upload. Missing server diagnostics and lost transport report unknown (CODE-PARSE-DIAGNOSTICS-1).

- Preserve structured code-upload HTTP failure bodies, including server staging and cleanup failure outcomes. Report transport loss as unconfirmed (CODE-PARSE-DIAGNOSTICS-1).

- Code indexing and hosted uploads carry grammar diagnostics, file counts and explicit publication outcomes. Recoverable partials are visible and hard failures retain the prior index (CODE-PARSE-DIAGNOSTICS-1).
### Fixed

- CODE-EFFECTS-ENGINE-1 R-fx: Align uploaded-effects lookup JSON hashing with core for non-ASCII repository identifiers. Verify declared record conversion through scoped SQLite and HTTP.

- Add local Python code_effects scanning and an explicit hosted allowlisted reader for scoped uploaded effects snapshots (CODE-EFFECTS-ENGINE-1).
- Verify local MCP dead-code analysis against real scoped FalkorDB code publication, including component, hook and test reachability, with fixture graph cleanup (CODE-FRAMEWORK-SEMANTICS-1, R-fw-4).

- Analyze component and hook dead-code candidates in local Lite stores. Remote and hosted tools preserve callable kinds and use the service framework evidence (CODE-FRAMEWORK-SEMANTICS-1).

- Code indexing now delegates to ingest_code on every backend. Remote backends retain the existing bundle upload, limits and Python-only fallback warning.

- Code search output includes stored identity, source ranges and call resolution evidence for local and hosted backends (CODE-EDGE-CONFIDENCE-1).

- Retain the prior code index when bundled source collection or decoding fails, reporting the affected path before upload.

- CODE-INGEST-SURFACES-1 review fixes: retain prior local symbols on parse failure and support standalone remote indexing with the bundled Python parser. Warn when TS/JS indexing requires smartmemory-core.

- CODE-INGEST-SURFACES-1: local code_index delegates to core for Python, JavaScript, TypeScript, JSX and TSX. Hosted code_upload accepts bounded parsed bundles without reading server checkout paths.

- MAYA-WORK-1 team build (not deployed or accepted): decode hosted Team membership from the service team_id field, preserving per-session selection and local behavior.

## 1.5.20 — 2026-10-04

- Standalone Windows MCP selects the canonical credential file and lock without importing the wrapper. Legacy credentials are migration sources only, and a newer legacy write is preserved through protected migration.
- Windows fallback credentials use one shared implementation with wrapper setup and standalone MCP. Writers lock updates, verify a protected current-SID-only temporary file before secret bytes, and preserve the previous key on ACL failure. Wrapper installations migrate and remove the legacy MCP file once.

## 1.5.19 — 2026-10-03

### Fixed

- OKF archive imports and expanded remote exports reject Windows path escapes, device names, alternate data streams and case collisions before writing any member.
- Automatic lifecycle overrides replace existing session files on Windows, retry bounded sharing violations, preserve UTF-8 state and report persistence failures instead of claiming success.
- Local backend construction and PRO-tier tool listing defer memory and model initialization until the first storage operation, with locked, retryable initialization.
- `whoami` reports missing local models with the core `sm setup` instructions and logs a warning, instead of describing the failure as a missing login.

## 1.5.0 — 2026-09-27

### Fixed (2026-09-27) — release test harness drift

- Isolate tier registration checks from stored credentials and local database setup while listing through the real remote capability filter. Keep the documented FREE/PRO/PRO+ tool counts and membership assertions.
- Update the hosted search smoke input to the accepted semantic channel and allow the portability test registrar to accept tool metadata.

### Fixed (2026-09-17) — list and statistics failures no longer look like empty memory

- Remote statistics failures now raise through the MCP tool instead of letting a service error
  dictionary render as `Total memories: 0`; malformed statistics responses fail explicitly too.
- Remote list responses preserve the service-reported corpus total. The tool now distinguishes
  returned page size from total corpus size, labels totals unavailable for legacy bare-list
  responses, and does not call an empty page an empty corpus.
- Touched degradation and contract-error paths log warnings naming the data lost and the reason.

### Added (2026-09-12) — tool annotations on every MCP tool (directory submission blocker)

- All 106 `@mcp.tool` registrations now carry a `title` and an explicit `ToolAnnotations`
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). Previously 26 did.
- Required by both the Anthropic Claude Connectors Directory and the OpenAI ChatGPT Apps
  directory: a reviewer rejects a submission before accepting it when tools lack annotations.
  See `smart-memory-docs/docs/features/PLAT-MCP-HOSTED-1/directory-submissions.md`.
- Classified from each tool's implementation, not its name. Notable calls: alias resolution and
  clustering merge existing graph entities, and decision supersede/retract change lifecycle
  state, so all are `destructiveHint=True`; export writes to a caller-selected path, so it is
  not read-only; `peer_chat` is read-only but `openWorldHint=True` because it synthesizes
  through a model.
- The two hosted wrapper registrations now declare annotations explicitly instead of forwarding
  an annotation object indirectly.
- Behaviour unchanged: only `title=`, `annotations=` and the `ToolAnnotations` imports were
  added. No tool was renamed and no signature changed.
- Flagged for human review as conservatively classified destructive: `memory_anchor_graduate`,
  `reasoning_resolve_conflict`, `evolution_trigger`.

### Changed: CORE-LEXICAL-INDEX-1 consumer cutover
- R-E4: Pin lexical HTTP contract v2 to exact 400/503 detail envelopes and verify consumer error fidelity.

- Use one `lexical` channel with default weight 0.8. Removed channel names fail validation, explicit zero is preserved, and unavailable required lexical search fails without partial success.
- Coordinated service, common, Python, JS, MCP and lite contracts cover migration and recovery. See [migration guidance](https://github.com/smartmemory/smart-memory-docs/blob/main/docs/features/CORE-LEXICAL-INDEX-1/migration.md) and the [canonical contract](https://github.com/smartmemory/smart-memory-docs/blob/main/docs/features/CORE-LEXICAL-INDEX-1/lexical-contract.json).
- Release remains pending maintainer review of measured write cost and final verification. No version bump.

### Fixed (2026-09-10) — decision supersession context and direction

- `decision_supersede` accepts optional replacement `rejected_alternatives`, `rationale`, and
  `constraints` in local and remote modes. The remote backend sends only fields explicitly supplied.
- Decision provenance renders the new directional `superseded_by` successor list while retaining
  the existing predecessor summary.

### Added (2026-09-10) — MCP-REMOTE-DECISIONS-1: decision tools work in remote mode

- All 13 decision tools (`decision_create`, `_get`, `_list`, `_search`, `_supersede`, `_retract`,
  `_reinforce`, `_contradict`, `_provenance`, `_find_conflicts`, `_create_pending`,
  `_resolve_requirement`, `_try_activate`) now forward to the service decision routes when the
  server runs against a hosted backend. They previously answered every remote caller with
  "remote decision support is not yet implemented", so an agent on the hosted product — the
  primary configuration — could not record a decision at all.
- Decision access is now a backend capability rather than a branch inside the tool:
  `LocalBackend` wraps the core managers over the real `SmartMemory`, `RemoteBackend` calls
  `/memory/decisions/*`, and both return the same `Decision.to_dict()` dict. The tools render
  once, so the text an agent sees is byte-identical in both modes (pinned by
  `tests/test_decision_tools_mode_parity.py`, which fails if a new decision tool arrives without
  a parity case).
- Failures surface loudly: an API error raises a `RuntimeError` naming the status and the route.
  A 500 on a list can never render as "No active decisions found", and a failed create can never
  render as a success. Only a genuine 404 on an addressed decision reads as absence.
- The scope context the create/supersede/retract routes echo back (`user_id`, `tenant_id`,
  `workspace_id`, `team_id`, `isolation_level`) is stripped in the backend. Server-only identity
  must not reach an MCP client.

#### Known upstream defect (not worked around here)

`GET /memory/decisions/search` and `GET /memory/decisions/{id}/provenance` return the stored
`confidence` as the `Decision` default 0.8. Verified live 2026-09-10: a decision created at 0.33
reads 0.33 from `GET /memory/decisions/{id}` and `GET /memory/decisions`, and 0.8 from the other
two. Root cause is core, not the transport — `DecisionQueries._hydrate` rebuilds the Decision from
`MemoryItem.metadata`, which does not persist `confidence` (nor `rationale`/`domain`), so
`Decision.from_dict` falls back to its default. The MCP forwards what the route returns rather than
masking it. Note this is NOT the supersede defect the feature plan predicted: `new_confidence` is
stored correctly and reads back correctly.

### Changed (2026-09-10) — CORE-DECISION-BELIEF-SURFACE-1: decision uncertainty is legible

- `decision_get` now carries `belief_hold`, `plausibility_hold` and `ignorance`, inherited from core's
  `Decision.to_dict()` with no code change here and pinned by test so a refactor cannot drop them.
- `decision_list` and `decision_search` summary lines append `⟨not enough evidence⟩` when `ignorance`
  exceeds 0.7. The marker is words, not a float: an agent scanning the list must not read an unevidenced
  decision as a settled assertion just because its prior `confidence` is high. The belief reads are
  evidence-only, so a 0.9-confidence decision nothing has reinforced or contradicted reads `ignorance = 1.0`.
- The remote-backend refusal was unchanged by that feature; MCP-REMOTE-DECISIONS-1 (above)
  removed it, and the uncertainty marker now renders in remote mode too.

### Changed (2026-09-09)

- Document-ingestion results now distinguish resumed partial ingests, reporting
  only the chunks created during the resumed attempt.

### CI (2026-09-08) — enforce tool-to-backend contracts

- Run backend conformance on pull requests and pushes to main, including
  unsupported-tool filtering and local/remote document-ingestion delegation.

### Added (2026-09-06) — creation windows and planner choice on both backends

- Both memory search tools resolve and echo ISO or relative creation windows
  (SEARCH-TIME-RANGE-1), and expose `consensus`/`relevance`/`semantic` hop strategies through
  the local and remote backends, labelling an inert selection (SEARCH-HOP-STRATEGY-SURFACE-1).
- Local metadata search now goes through the public metadata facade so predicates run before
  the limit rather than after a scan.
- Context origin is preserved through local and remote ingestion and remote conversation
  ingestion (CORE-ORIGIN-PROPAGATION-1).


### Added

- Hosted MCP tool listings now provide human-readable titles and explicit safety
  annotations for every exposed tool.

### Fixed

- `memory_ingest_document` now delegates to the core facade locally and the
  document ingestion service route remotely, preserving ingestion/dedup results
  and surfacing API failures. Document ingestion and local-only provenance/peer
  tools are advertised and callable only when the active backend supports them.

- `--http` now serves an unauthenticated `GET /health` liveness probe for the
  single-identity MCP container healthcheck.

## 1.4.87 — 2026-09-04

### Fixed — hosted mode (PLAT-MCP-HOSTED-1 listing readiness)

- `switch_team` selection now survives across tool calls: FastMCP 4.0.2 context
  state is request-local, so the selected workspace was lost on the next call.
  The hosted middleware keys it by MCP session id.
- A forwarded `403 nda_required` surfaces as one tool error naming the web app
  (`SMARTMEMORY_WEB_URL`, default `https://app.smartmemory.ai`).
- Every hosted search/recall sends `exclude_speculative=true`, so the service
  drops origin tier-3 items server-side; the client-side gate stays as defence
  in depth. Local/stdio requests are unchanged.

### Added

- `scripts/oauth_static_client_probe.py` (PKCE client for the static client_id
  path, prints the authorize URL, exchanges, calls tools/list, tests refresh)
  and `scripts/hosted_abuse_probe.py` (unauthenticated `/mcp` + bad
  `/authorize` hammer with a 5xx gate). Both verified against production.

## 1.4.86 — 2026-09-04

- Upgraded to FastMCP 4, migrated test tool lookup helpers to its public API,
  and fixed `RemoteBackend.update` to use `PATCH /memory/{id}`
  (PLAT-MCP-HOSTED-1 T1).

### Added — hosted OAuth-protected MCP endpoint (PLAT-MCP-HOSTED-1 T2-T6)

`--hosted` (or `SMARTMEMORY_MCP_MODE=hosted`) serves a multi-tenant server where
every request carries its own credential. Two are accepted: a Clerk OAuth token,
exchanged once server-side for a SmartMemory JWT and cached until shortly before
it expires, or a SmartMemory API key (including legacy `sk_` keys), verified
against `/auth/me` and cached for a minute. A middleware resolves the caller
before every tool call and binds a backend built for that caller alone, so tools
stay synchronous and never see another tenant's credentials. Requires the
`hosted` extra. See the README for the environment and client setup.

Notable details:

- The tool surface is an explicit 25-name allowlist, not a tier. Anything taking
  a filesystem path, needing a local SmartMemory instance, or destructive in bulk
  is excluded, and hidden is the default for anything new.
- `memory_search(cite=True)` and `memory_recall(session_id=...)` / `(cite=True)`
  are refused with an explicit message rather than silently downgraded. Both
  need the `smartmemory` core package, which the hosted wheel does not ship.
- `switch_team` is per MCP session and validates membership against the API.
- OAuth state is encrypted at rest with Fernet, size-capped, and given a 30-day
  default TTL for the records that arrive without one. Explicit TTLs are never
  shortened, in either direction.
- The public surface is rate limited per client address: registration 10/min
  plus a 200/hour global ceiling, `/authorize` and `/token` 30/min each,
  unauthenticated `/mcp` 60/min.

### Changed

- `RemoteBackend` gains an `_on_unauthorized` hook, called from both `_request`
  and `search` on a 401. Base behaviour is unchanged; the hosted subclass uses it
  to drop its cached identity so the next call re-exchanges.
- `RemoteBackend.search` now forwards `channel_weights`, which it had been
  dropping (CORE-SEARCH-2a).
- `--http` refuses to bind a non-loopback host unless
  `SMARTMEMORY_MCP_ALLOW_UNAUTH_HTTP=true`, and logs what was narrowed and why.
  That mode has no per-request authentication, so binding it to `0.0.0.0`
  published one tenant's memory to the network.
- `memory_search` now logs once per process when the CORE-ORIGIN-1 tier filter
  is unavailable. It used to be swallowed, and it is the only thing hiding
  speculative derived items from a search, since the service's search request
  carries no origin field.


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

## Historical lockstep version entries
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
