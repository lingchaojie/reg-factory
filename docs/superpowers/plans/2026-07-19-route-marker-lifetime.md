# Route Marker Lifetime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope `NETWORK_ROUTE_MODE` to one launched WebUI task or one loop account while preserving no-reprobe behavior inside that attempt.

**Architecture:** Keep `prepare_clash_or_direct()` as the descendant inheritance mechanism. Reset the marker only at explicit new-attempt boundaries: WebUI child environment construction and each accepted `register_three_platforms --loop` account. WebUI startup may apply a route to the server process but must remove the ephemeral marker afterward.

**Tech Stack:** Python 3, `unittest`, `unittest.mock`, asyncio.

## Global Constraints

- Preserve IPMart scope: Outlook to Graph to mailbox to Claude only; do not extend IPMart to ChatGPT or Grok.
- Preserve BitBrowser and AdsPower behavior.
- Keep `NETWORK_ROUTE_MODE` mode-only and never log or persist proxy credentials.
- Standalone entry points without a marker still probe; descendants inside one attempt reuse the marker without probing.
- Use TDD and create a new commit without rewriting history.

---

### Task 1: WebUI task boundary

**Files:**
- Modify: `webui/server.py`
- Test: `tests/test_webui_env_reload.py`

**Interfaces:**
- Consumes: `common.network_route.RESOLVED_ROUTE_ENV_KEY`, `prepare_clash_or_direct(env)`
- Produces: `_ensure_proxy_env()` with no retained marker and `_child_env()` with one fresh stamped route per call

- [x] **Step 1: Write failing symmetric WebUI task tests**

Add tests that call `_child_env()` twice with connector outcomes refused then reachable, and reachable then refused. Seed a stale process marker and assert two connector calls, changed task modes, and matching proxy variables. Add a startup assertion that `_ensure_proxy_env()` leaves no process marker.

- [x] **Step 2: Run WebUI tests and verify RED**

Run: `python -m unittest tests.test_webui_env_reload -v`

Expected: transition tests fail because the stale process marker suppresses both probes, and startup retains the marker.

- [x] **Step 3: Implement the WebUI boundary reset**

Import `RESOLVED_ROUTE_ENV_KEY`. In `_ensure_proxy_env()`, apply the route and remove the marker in `finally`. In `_child_env()`, remove the marker after process and dotenv values are merged, then probe/stamp the returned child environment when IPMart is disabled.

- [x] **Step 4: Run WebUI tests and verify GREEN**

Run: `python -m unittest tests.test_webui_env_reload -v`

Expected: all WebUI environment tests pass.

### Task 2: Loop account boundary

**Files:**
- Modify: `register_three_platforms.py`
- Test: `tests/test_platform_proxy_env.py`

**Interfaces:**
- Consumes: `child_env_for(args, fresh_route=False)`, `process_account(account, args, child_env)`
- Produces: `process_loop_account(account, args)` which fresh-resolves once and shares its stamped environment within that account

- [x] **Step 1: Write failing symmetric loop-account tests**

Add async tests that process two accepted accounts with connector outcomes refused then reachable, and reachable then refused. Capture the real stamped environments passed into `process_account`, construct multiple real platform child environments inside the capture, and assert exactly one probe per account with no within-account re-probe.

- [x] **Step 2: Run loop-account tests and verify RED**

Run: `python -m unittest tests.test_platform_proxy_env -v`

Expected: tests fail because no per-account fresh route boundary exists.

- [x] **Step 3: Implement the loop boundary reset**

Add a `fresh_route` keyword to `child_env_for()` that removes only `NETWORK_ROUTE_MODE` from its copied environment before route preparation. Add `process_loop_account()` and make loop `guarded()` call it. Move the ordinary `child_env_for()` call into the non-loop branch so inherited standalone/descendant semantics remain unchanged.

- [x] **Step 4: Run platform tests and verify GREEN**

Run: `python -m unittest tests.test_platform_proxy_env -v`

Expected: all platform environment and launch tests pass.

### Task 3: Regression, security, and commit

**Files:**
- Modify: `.superpowers/sdd/task-2-report.md` (ignored report only)

**Interfaces:**
- Consumes: completed WebUI and loop boundary fixes
- Produces: verified commit and appended evidence report

- [x] **Step 1: Run targeted regression**

Run route, WebUI, platform, IPMart, and standalone entrypoint test modules, including the existing within-attempt transition tests.

- [x] **Step 2: Run full verification**

Run `python -m unittest discover -s tests -v`, `python -m py_compile` on changed Python files, `git diff --check`, and scan the staged diff for credential-bearing marker/log additions.

- [ ] **Step 3: Commit exact source and test files**

Commit with message `fix: scope network route to each attempt`.

- [ ] **Step 4: Append final report and verify clean state**

Append RED/GREEN commands, counts, commit hash, security review, and concerns to `.superpowers/sdd/task-2-report.md`; verify `git status --short` is clean.
