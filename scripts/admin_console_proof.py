# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.49,<2"]
# ///
"""P8-7 (#145) — admin console visual / responsive / accessibility proof harness.

This is the committed, reproducible proof tool for the final P8 integration gate.
It is **operator-run**, never collected by pytest (``testpaths = ["tests"]``) and
never wired into CI — the same posture as ``scripts/release_smoke.sh``. It stands
up nothing itself; it drives an already-running Hivemind stack (the compose
topology of ``docker-compose.yml``, entered through the WAF on
``http://localhost:8080``) with a real Chromium and proves the *integrated*
console (P8-1 shell + P8-2..P8-6 views) with programmatic assertions and
screenshots.

--------------------------------------------------------------------------------
Data-safety contract (Terra adversarial review, PR #170)
--------------------------------------------------------------------------------

The harness NEVER touches an operator's existing data:

* Every proof resource is namespaced by an unpredictable per-run id
  (``p8-proof-<random>``), so a run cannot collide with, overwrite, or delete
  anything an operator already owns.
* Before seeding it refuses to run if the generated namespace already exists.
* It records only the resources it actually created (space ids + the exact
  returned token hash) and tears down ONLY those, then re-reads to CONFIRM they
  are gone. Unconfirmed cleanup fails the run (non-zero exit) — never a swallowed
  error, never a delete by fixed name.
* The one-time token created for the secret-masking proof is always revoked and
  deleted (even on failure, via ``finally``), and its plaintext is redacted in
  the live DOM and asserted redacted BEFORE any screenshot/report capture.

--------------------------------------------------------------------------------
One-time setup
--------------------------------------------------------------------------------

    uv run --with playwright playwright install chromium

Run (against a live stack — see docs/DEPLOYMENT.md for `docker compose --profile
dev up -d --build`):

    export HIVEMIND_PROOF_BOOTSTRAP_KEY='<the stack ADMIN_BOOTSTRAP_KEY>'
    uv run scripts/admin_console_proof.py --profile degraded-llm

The bootstrap key is read from the ``HIVEMIND_PROOF_BOOTSTRAP_KEY`` environment
variable **only** — never argv (it would be visible in ``ps``), never logged,
never written to an artifact.

Self-test (no stack, no browser — proves the data-honesty checks go RED on bad
input, i.e. they are not tautological):

    uv run scripts/admin_console_proof.py --self-test

--------------------------------------------------------------------------------
Proof profiles (issue #145 acceptance + P8-0 D7)
--------------------------------------------------------------------------------

* ``full-llm``      — a real LLMaaS is configured: Dashboard healthy, a
                      ``succeeded`` consolidation, a connected long tier.
* ``degraded-llm``  — no LLMaaS: Dashboard degraded, consolidation ``failed``,
                      long push failure. These degraded states are honest,
                      first-class renderings (D7) and the ADR-0019 failure
                      framing is itself a required proof. ``degraded-llm`` +
                      healthy S3/queue states is the minimum merge bar; the
                      limitation is stated explicitly in the PR.

Exit code is non-zero if any assertion in the battery fails, a required seed or
fixture is missing, or teardown cannot confirm it removed exactly what this run
created (gate semantics).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ===========================================================================
# Pinned proof contract — asserted by
# tests/test_p8_7_integration_proof.py::test_proof_harness_pins_agreed_viewports_and_views
# (T-P87-4). Do NOT drift these without updating the agreed P8-0 proof contract.
# ===========================================================================

# Desktop 1440×900 and the agreed narrow viewport 768×1024 (P8-1 icon-rail
# breakpoint, max-width:1023px). Every shot/assertion runs at both.
VIEWPORTS: list[tuple[int, int]] = [(1440, 900), (768, 1024)]

# The eight shipped routes (admin-app.js router). '#/spaces/{space}' is the
# Space Detail deep link; '{space}' is substituted with this run's unpredictable
# proof-space id so the crawl never targets a fixed, operator-owned space.
VIEWS: list[str] = [
    "#/dashboard",
    "#/spaces",
    "#/spaces/{space}",
    "#/consolidation",
    "#/audit",
    "#/access",
    "#/operator/backups",
    "#/operator/maintenance",
]

# Env-only bootstrap key intake (never argv / logs / artifacts).
BOOTSTRAP_KEY_ENV = "HIVEMIND_PROOF_BOOTSTRAP_KEY"

# ---------------------------------------------------------------------------
# Per-run namespace (data-safety). Unpredictable → no collision with operator
# resources. hex is [0-9a-f], which satisfies the space-id charset.
# ---------------------------------------------------------------------------

RUN_ID = secrets.token_hex(5)  # 10 hex chars
PROOF_SPACE = f"p8-proof-{RUN_ID}"
# A long id (≤ 64 chars) to prove long-ID truncation has a tooltip/copy path.
PROOF_LONG_SPACE = f"p8-proof-truncation-tooltip-check-{RUN_ID}-longspaceid"[:64]
PROOF_OWNER = "p8-proof-operator"
PROOF_TAG = "p8proof"
PROOF_TOKEN_NAME = f"p8-proof-token-{RUN_ID}"

NOTE_CATEGORIES = [
    "observation", "decision", "todo", "insight",
    "question", "progress", "issue", "observation",  # +1 dup for grouping
]

# Strong fake/broken-data sentinels (A-3). Standalone domain words such as the
# real ``todo`` note category are deliberately not placeholders.
FAKE_DATA_SENTINELS = [
    "undefined", "NaN", "[object Object]", "Invalid Date", "lorem ipsum", "mock data",
]

# Per-surface focus outline colors (contract §2.8.1, asserted by A-7).
FOCUS_CYAN_SIDEBAR = "rgb(0, 167, 199)"   # #00A7C7 — on the dark sidebar/login

REQUIRED_FONTS = ["Space Grotesk", "Hanken Grotesk", "JetBrains Mono"]

# WAF `api` zone budget is 120/min/IP; stay at half so the proof never trips 429.
MAX_API_PER_MIN = 60

# Statuses accepted as "this call really succeeded" for a required seed.
_OK = ("ok", "created")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(route: str) -> str:
    return route.replace("#/", "").replace("/", "_").replace("{space}", "spacedetail") or "root"


# ===========================================================================
# Pure data-honesty predicates (factored out so --self-test can prove they
# discriminate — Terra finding 3: the cross-checks must go RED on bad input).
# ===========================================================================


def counts_match(rendered_count: int, authoritative_count: int) -> bool:
    """A rendered count is honest iff it EQUALS the authoritative count."""
    return isinstance(rendered_count, int) and rendered_count == authoritative_count


def ids_are_subset(rendered_ids, authoritative_ids) -> bool:
    """Every rendered id must be a real id; an empty render is not a pass on its
    own (the caller separately asserts the seeded id is present)."""
    return set(rendered_ids).issubset(set(authoritative_ids))


def read_ok(res) -> bool:
    """An authoritative tool read counts only if it explicitly succeeded. A
    transport/HTTP error maps to ``{status: error}`` (never an empty result), so
    absence-of-data from a FAILED read must never be treated as a real empty
    set (Terra R2 findings 1 & 3)."""
    return isinstance(res, dict) and res.get("status") in _OK


def unexpected_console_errors(errors: list[str], allowed_401_paths: tuple[str, ...]) -> list[str]:
    """Drop only deliberate auth-negative 401 resource messages.

    The caller passes only errors captured between an explicit checkpoint and
    the end of a deliberate invalid-login or cookie-wipe phase. Every other
    status, path, JavaScript error, and out-of-phase console error stays red.
    """

    unexpected = []
    for error in errors:
        expected = "401 (Unauthorized)" in error and any(
            f"{path}'" in error or f'{path}"' in error for path in allowed_401_paths
        )
        if not expected:
            unexpected.append(error)
    return unexpected


def crosscheck_ok(authoritative_read_ok: bool, rendered_count, authoritative_count) -> bool:
    """A data-honesty cross-check passes only when the authoritative read
    SUCCEEDED and the rendered count equals it. A failed read (even 0 == 0) is
    never a pass."""
    return bool(authoritative_read_ok) and counts_match(rendered_count, authoritative_count)


def teardown_is_confirmed(list_reads_ok: bool, remaining_run_resources) -> bool:
    """Cleanup is confirmed only when the confirming reads SUCCEEDED and no
    resource this run created still exists. A failed confirming read is never
    'nothing remains' (Terra R2 finding 1)."""
    return bool(list_reads_ok) and not list(remaining_run_resources)


def _self_test() -> int:
    """Prove the honesty predicates are not tautological and are fail-closed."""
    failures = []
    # counts_match: RED on mismatch, GREEN on equality.
    if not counts_match(3, 3):
        failures.append("counts_match(3,3) should be True")
    if counts_match(0, 3):
        failures.append("counts_match(0,3) MUST be False (empty render vs 3 real)")
    if counts_match(4, 3):
        failures.append("counts_match(4,3) MUST be False (fabricated extra)")
    # ids_are_subset: RED when a rendered id is not real.
    if not ids_are_subset({"a", "b"}, {"a", "b", "c"}):
        failures.append("ids_are_subset(subset) should be True")
    if ids_are_subset({"a", "ghost"}, {"a", "b"}):
        failures.append("ids_are_subset with a stray id MUST be False")
    # read_ok: distinguishes an explicit success from a failed read.
    if read_ok({"status": "error"}) or read_ok({}) or read_ok(None):
        failures.append("read_ok MUST be False for error/empty/None responses")
    if not read_ok({"status": "ok"}) or not read_ok({"status": "created"}):
        failures.append("read_ok should be True for ok/created")
    expected_auth_error = (
        "{'url': 'http://localhost/api/login'}: Failed to load resource: "
        "the server responded with a status of 401 (Unauthorized)"
    )
    unexpected_auth_error = expected_auth_error.replace("/api/login", "/api/tool")
    server_error = expected_auth_error.replace(
        "401 (Unauthorized)", "500 (Internal Server Error)"
    )
    filtered = unexpected_console_errors(
        [expected_auth_error, unexpected_auth_error, server_error], ("/api/login",)
    )
    if filtered != [unexpected_auth_error, server_error]:
        failures.append("expected-auth filter must remove only the allowed-path 401")
    # crosscheck_ok: a FAILED authoritative read must never pass, even at 0 == 0.
    if crosscheck_ok(False, 0, 0):
        failures.append("crosscheck_ok(read_failed, 0, 0) MUST be False")
    if not crosscheck_ok(True, 3, 3):
        failures.append("crosscheck_ok(read_ok, 3, 3) should be True")
    if crosscheck_ok(True, 0, 3):
        failures.append("crosscheck_ok(read_ok, 0, 3) MUST be False")
    # teardown_is_confirmed: failed confirming reads or leftovers => not confirmed.
    if teardown_is_confirmed(False, []):
        failures.append("teardown_is_confirmed(reads_failed, none) MUST be False")
    if teardown_is_confirmed(True, ["p8-proof-x"]):
        failures.append("teardown_is_confirmed(reads_ok, remaining) MUST be False")
    if not teardown_is_confirmed(True, []):
        failures.append("teardown_is_confirmed(reads_ok, none) should be True")
    # Budget: the rolling 60s window keeps recent events and prunes stale ones
    # (this is what caps the /api rate under the WAF limit).
    b = Budget(3)
    b.events = [0.0, 0.0, 0.0]
    b._prune(1.0)
    if len(b.events) != 3:
        failures.append("Budget must keep events within the 60s window")
    b._prune(61.0)
    if b.events:
        failures.append("Budget must prune events older than 60s")
    # Budget.reserve: a burst reserved near the cap must not be able to overshoot
    # (Terra R4 scenario: 59 events + a 4-request Dashboard entry).
    b2 = Budget(60)
    b2.events = [time.monotonic()] * 59
    if b2.has_room(4):
        failures.append("has_room(4) at 59/60 MUST be False (would reach 63)")
    b2.events = [time.monotonic()] * 56
    if not b2.has_room(4):
        failures.append("has_room(4) at 56/60 should be True (reaches exactly 60)")
    # namespace unpredictability sanity.
    if PROOF_SPACE == "p8-proof" or len(RUN_ID) < 8:
        failures.append("proof namespace must be per-run unpredictable")

    # Fault injection (offline): the collision preflight must fail closed, and
    # teardown must NOT delete a pre-existing namespaced resource under an
    # unproven preflight (Terra R6).
    import types
    _orig_api = globals()["_api_tool"]
    try:
        # (a) A failed preflight read -> collision_check raises; preflight_clean False.
        globals()["_api_tool"] = lambda *a, **k: {"status": "error"}
        st = RunState()
        raised = False
        try:
            collision_check("http://x", "k", st)
        except SeedError:
            raised = True
        if not raised:
            failures.append("collision_check MUST fail closed on a failed preflight read")
        if st.preflight_clean:
            failures.append("collision_check MUST NOT set preflight_clean on a failed read")

        # (b) teardown under an unproven preflight (preflight_clean False) must not
        # delete a pre-existing namespaced space it did not record creating.
        deleted: list[str] = []

        def _fake(base, key, tool, args):
            if tool == "space_list":
                return {"status": "ok", "spaces": [{"space_id": PROOF_SPACE}]}
            if tool == "admin_list_tokens":
                return {"status": "ok", "tokens": []}
            if tool == "space_delete":
                deleted.append(args.get("space_id"))
            return {"status": "ok"}

        globals()["_api_tool"] = _fake
        st2 = RunState()  # preflight_clean False, nothing recorded created
        teardown("http://x", "k", False, st2, types.SimpleNamespace(teardown={}))
        if PROOF_SPACE in deleted:
            failures.append("teardown MUST NOT delete a namespaced resource it did not "
                            "create when the preflight was not proven clean")
    finally:
        globals()["_api_tool"] = _orig_api

    if failures:
        for f in failures:
            print(f"  SELF-TEST FAIL: {f}")
        return 1
    print("  self-test OK: data-honesty predicates discriminate (RED on bad input); "
          f"namespace={PROOF_SPACE!r}")
    return 0


# ===========================================================================
# Report
# ===========================================================================


class Report:
    def __init__(self) -> None:
        self.started_at = _now_iso()
        self.assertions: list[dict] = []
        self.shots: list[str] = []
        self.request_counts: dict[str, int] = {}
        self.console_errors: list[str] = []
        self.csp_violations: list[str] = []
        self.seed: dict = {}
        self.fixtures: dict = {}
        self.teardown: dict = {}
        self.meta: dict = {}

    def record(self, name: str, view: str, viewport: str, ok: bool, detail: str = "") -> None:
        self.assertions.append({"assertion": name, "view": view, "viewport": viewport,
                                "ok": bool(ok), "detail": detail})
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {name} @ {view} {viewport}"
        if detail and not ok:
            line += f" — {detail}"
        print(line)

    @property
    def failed(self) -> list[dict]:
        return [a for a in self.assertions if not a["ok"]]

    def to_dict(self) -> dict:
        return {
            "proof": "P8-7 admin console visual/responsive/accessibility proof",
            "issue": 145, "started_at": self.started_at, "finished_at": _now_iso(),
            "meta": self.meta, "run_id": RUN_ID,
            "viewports": [f"{w}x{h}" for w, h in VIEWPORTS], "views": VIEWS,
            "seed": self.seed, "fixtures": self.fixtures, "teardown": self.teardown,
            "request_counts": self.request_counts,
            "console_errors": self.console_errors, "csp_violations": self.csp_violations,
            "assertions": self.assertions, "screenshots": self.shots,
            "summary": {"total": len(self.assertions), "failed": len(self.failed),
                        "passed": len(self.assertions) - len(self.failed)},
        }


# ===========================================================================
# Global request budget — ENFORCED so the proof never trips the WAF `api` zone
# (120/min/IP). Every /api request (Python seed/teardown/cross-check AND the
# browser's in-view fetches) counts against one rolling-60s window; navigations
# and Python calls block until the window has room. Half the WAF budget.
# ===========================================================================


class Budget:
    def __init__(self, per_min: int) -> None:
        self.per_min = max(1, per_min)
        self.events: list[float] = []

    def _prune(self, now: float) -> None:
        self.events = [t for t in self.events if now - t < 60.0]

    def has_room(self, n: int = 1) -> bool:
        now = time.monotonic()
        self._prune(now)
        return len(self.events) + n <= self.per_min

    def wait(self) -> None:
        """Block until there is room for one more request in the trailing 60s."""
        self.reserve(1)

    def reserve(self, n: int) -> None:
        """Block until the window has room for a burst of up to ``n`` requests,
        so an in-view batch (e.g. the Dashboard's concurrent /api/tool calls)
        cannot push the trailing-60s count past ``per_min`` after it fires. The
        listener notes each actual request; this only reserves headroom BEFORE a
        navigation, closing the count-after-dispatch gap (Terra R4)."""
        n = max(1, min(n, self.per_min))
        now = time.monotonic()
        self._prune(now)
        while len(self.events) + n > self.per_min:
            idx = max(0, min(len(self.events) + n - self.per_min - 1, len(self.events) - 1))
            sleep_for = 60.0 - (now - self.events[idx]) + 0.05
            time.sleep(max(0.1, sleep_for))
            now = time.monotonic()
            self._prune(now)

    def note(self) -> None:
        self.events.append(time.monotonic())


# Reset per run in main(); a module global so _api_tool and the browser request
# listener share one window.
BUDGET = Budget(MAX_API_PER_MIN)

# Upper bound on the /api/tool calls a single view fires on entry (the Dashboard
# is the heaviest at 4). Reserved before each navigation so a burst near the cap
# still stays within the budget.
_MAX_VIEW_BATCH = 8


def _nav(page, url: str, wait_until: str = "networkidle") -> None:
    """Budget-paced navigation: reserve headroom for the view's entry burst so
    it cannot exceed the cap even when several /api calls fire concurrently."""
    BUDGET.reserve(_MAX_VIEW_BATCH)
    page.goto(url, wait_until=wait_until)


def _reload(page, wait_until: str = "networkidle") -> None:
    BUDGET.reserve(_MAX_VIEW_BATCH)
    page.reload(wait_until=wait_until)


def _wait_for_view_loaded(page, selector: str = "#content", timeout: int = 15000) -> None:
    """Wait until a routed view exists and has no active loading state."""

    page.wait_for_function(
        "selector => { const el = document.querySelector(selector);"
        " return !!el && !el.querySelector('.state-loading'); }",
        arg=selector,
        timeout=timeout,
    )


# ===========================================================================
# API helpers (Bearer bootstrap key)
# ===========================================================================


def _api_tool(base_url: str, key: str, tool: str, arguments: dict) -> dict:
    BUDGET.wait()
    BUDGET.note()
    payload = json.dumps({"tool": tool, "arguments": arguments}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/tool", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"status": "error", "http": exc.code}
    except Exception as exc:
        return {"status": "error", "message": type(exc).__name__}


def api_get_json(base_url: str, key: str, path: str) -> dict:
    if path.startswith("/api"):
        BUDGET.wait()
        BUDGET.note()
    req = urllib.request.Request(f"{base_url}{path}", method="GET",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _spaces_of(resp: dict) -> list:
    raw = resp.get("spaces") or resp.get("data") or []
    out = []
    for s in raw:
        if isinstance(s, dict) and s.get("space_id"):
            out.append(s["space_id"])
        elif isinstance(s, str):
            out.append(s)
    return out


def _tokens_of(resp: dict) -> list:
    return resp.get("tokens") or resp.get("data") or []


# ===========================================================================
# Run state: only what THIS run created (data-safety)
# ===========================================================================


class RunState:
    def __init__(self) -> None:
        self.created_spaces: list[str] = []
        self.created_token_hash: str | None = None
        # Backups persist under a separate _backups/<space>/ prefix and SURVIVE
        # space deletion, so they must be deleted explicitly (Terra R9).
        self.created_backups: list[str] = []
        # Set True only when the collision preflight POSITIVELY proved (via
        # status-checked reads) that this run's namespace was free at start.
        # Teardown's discover-by-namespace sweep is enabled ONLY then; otherwise
        # teardown may delete solely the exact ids/hashes it recorded creating.
        self.preflight_clean: bool = False


class SeedError(RuntimeError):
    pass


def collision_check(base_url: str, key: str, state: RunState) -> None:
    """Prove this run's namespace is free before seeding, FAIL-CLOSED. Both the
    space and token reads must explicitly succeed (a failed read is never an
    empty result); on any read failure or a real collision we raise and never
    seed. Only a clean, positively-verified preflight sets ``preflight_clean``,
    which is what later authorises teardown to sweep by namespace (Terra R6:
    without this, an ambiguous read could let the sweep delete a pre-existing
    operator resource that merely shares the namespace)."""
    ok_s, sids = _list_spaces_strict(base_url, key)
    ok_t, toks = _list_tokens_strict(base_url, key)
    if not (ok_s and ok_t):
        raise SeedError("cannot verify the proof namespace is free (a preflight "
                        "read failed) — refusing to seed under ambiguity")
    clash = [s for s in (PROOF_SPACE, PROOF_LONG_SPACE) if s in set(sids)]
    if clash:
        raise SeedError(f"namespace collision on {clash} — aborting to avoid touching "
                        "operator data (re-run to draw a new namespace)")
    if any(_token_name(t) == PROOF_TOKEN_NAME for t in toks):
        raise SeedError(f"token name {PROOF_TOKEN_NAME!r} already exists — aborting")
    state.preflight_clean = True


def seed(base_url: str, key: str, profile: str, state: RunState, report: Report) -> None:
    """Seed honest data via real tool calls (§6). Every seed here is synchronous
    and produces state teardown can positively delete and confirm. Asynchronous
    side effects the harness cannot quiesce (bank_consolidate's worker,
    graph_push's graph-side data) are deliberately NOT seeded (Terra R9/R10) —
    otherwise a worker could write bank files / _meta.json after teardown's final
    confirm and leave orphaned proof state."""
    log: dict = {"profile": profile, "calls": []}

    def required(label: str, tool: str, args: dict) -> dict:
        res = _api_tool(base_url, key, tool, args)
        status = res.get("status", "?")
        log["calls"].append({"label": label, "tool": tool, "status": status, "required": True})
        print(f"  seed {label}: {tool} -> {status}")
        if status not in _OK:
            raise SeedError(f"required seed {label} ({tool}) returned {status!r}: "
                            f"{res.get('message', '')}")
        return res

    # S1/S2 — spaces (brand-new random ids → MUST be created).
    required("S1", "space_create", {"space_id": PROOF_SPACE,
             "description": "P8-7 visual proof space", "owner": PROOF_OWNER, "rules": ""})
    state.created_spaces.append(PROOF_SPACE)
    required("S2", "space_create", {"space_id": PROOF_LONG_SPACE,
             "description": "Truncation/tooltip proof", "owner": PROOF_OWNER, "rules": ""})
    state.created_spaces.append(PROOF_LONG_SPACE)

    # S3 — short-tier notes (required: at least the batch succeeds).
    created_notes = 0
    for i, category in enumerate(NOTE_CATEGORIES):
        res = _api_tool(base_url, key, "live_note", {
            "space_id": PROOF_SPACE, "category": category,
            "content": (f"P8-7 proof note {i + 1}: a distinct, real {category} entry "
                        "seeded through POST /api/tool for the short-tier panel."),
            "tags": PROOF_TAG})
        if res.get("status") in _OK:
            created_notes += 1
    log["calls"].append({"label": "S3", "tool": "live_note", "created": created_notes,
                         "required": True})
    print(f"  seed S3: live_note -> {created_notes}/{len(NOTE_CATEGORIES)} created")
    if created_notes < len(NOTE_CATEGORIES) - 1:
        raise SeedError(f"short-tier seeding failed: only {created_notes} notes created")

    # S4 — mid-tier bank file (required; no LLM needed).
    required("S4", "bank_write", {"space_id": PROOF_SPACE, "filename": "canonical.md",
             "content": ("# P8-7 canonical fact sheet\n\nSeeded mid-tier content for the "
                         "Space Detail mid panel. Written directly via bank_write.\n")})

    # No bank_consolidate: it enqueues an asynchronous worker that holds a
    # consolidation lock independent of space_delete's lifecycle lock, so a
    # worker that already read its inputs could write bank files / _meta.json
    # AFTER teardown's final confirm — leaving orphaned proof state despite a
    # green run (Terra R10). The harness cannot deterministically quiesce that
    # worker, so it does not trigger one. The Consolidation view still renders
    # its honest lane state (idle / no recent job) from bank_consolidation_queues.

    # S6 — backup row (required; S3 storage, not LLM). Record the backup_id so
    # teardown can delete it (backups survive space deletion — Terra R9).
    s6 = required("S6", "backup_create", {"space_id": PROOF_SPACE,
                  "description": "P8-7 proof backup"})
    bid = s6.get("backup_id")
    if bid:
        state.created_backups.append(bid)

    # No graph_push: the long tier is derived and a graph_push under a real long
    # runtime would leave graph-side artifacts that this harness cannot delete
    # and verify fail-closed (Terra R9). The Space Detail long panel still proves
    # its honest state (unbound / derived / attention) from graph_status without
    # a push — omitting the write keeps teardown provably clean.

    report.seed = log


def verify_fixtures(base_url: str, key: str, report: Report) -> None:
    """Before crawling, PROVE the seeded fixtures are actually present, so an
    empty/broken stack cannot yield a green proof (Terra finding 3)."""
    fx: dict = {}
    space_ids = _spaces_of(_api_tool(base_url, key, "space_list", {}))
    fx["proof_space_listed"] = PROOF_SPACE in space_ids
    notes = _api_tool(base_url, key, "live_read", {"space_id": PROOF_SPACE})
    n_notes = notes.get("total")
    if not isinstance(n_notes, int):
        n_notes = len(notes.get("notes") or [])
    fx["short_notes"] = n_notes
    files = _api_tool(base_url, key, "bank_list", {"space_id": PROOF_SPACE})
    filenames = [f.get("filename") for f in (files.get("files") or []) if isinstance(f, dict)]
    fx["canonical_present"] = "canonical.md" in filenames
    report.fixtures = fx
    missing = []
    if not fx["proof_space_listed"]:
        missing.append(f"{PROOF_SPACE} not in space_list")
    if n_notes < len(NOTE_CATEGORIES) - 1:
        missing.append(f"short notes {n_notes} < expected")
    if not fx["canonical_present"]:
        missing.append("canonical.md missing from bank_list")
    if missing:
        raise SeedError("fixture verification failed (stack not honestly seeded): "
                        + "; ".join(missing))
    print(f"  fixtures verified: {fx}")


def _list_spaces_strict(base_url: str, key: str) -> tuple[bool, list]:
    """(read_succeeded, space_ids). Absence-from-a-failed-read is NOT [] here —
    the caller must distinguish 'read failed' from 'really empty'."""
    res = _api_tool(base_url, key, "space_list", {})
    return (read_ok(res), _spaces_of(res) if read_ok(res) else [])


def _list_tokens_strict(base_url: str, key: str) -> tuple[bool, list]:
    res = _api_tool(base_url, key, "admin_list_tokens", {"include_revoked": True})
    return (read_ok(res), _tokens_of(res) if read_ok(res) else [])


def _token_name(t: dict) -> str:
    return (t.get("name") or t.get("client_name") or "") if isinstance(t, dict) else ""


def _token_hash(t: dict) -> str | None:
    return (t.get("token_hash") or t.get("hash")) if isinstance(t, dict) else None


def _list_backups_strict(base_url: str, key: str) -> tuple[bool, list]:
    """(read_succeeded, backup_ids). backup_list with an empty space_id lists all
    accessible backups, so it still surfaces proof backups after their space is
    deleted (backups live under a separate _backups/<space>/ prefix)."""
    res = _api_tool(base_url, key, "backup_list", {})
    ids: list[str] = []
    if read_ok(res):
        for b in (res.get("backups") or res.get("data") or []):
            if isinstance(b, dict) and b.get("backup_id"):
                ids.append(b["backup_id"])
            elif isinstance(b, str):
                ids.append(b)
    return (read_ok(res), ids)


def _backup_space(backup_id: str) -> str:
    """The <space_id> part of a 'space_id/timestamp' backup id."""
    return backup_id.split("/", 1)[0] if isinstance(backup_id, str) else ""


def teardown(base_url: str, key: str, keep: bool, state: RunState, report: Report) -> bool:
    """Destroy ONLY what this run created, discovering leftovers by the run's
    unpredictable namespace, then CONFIRM via status-checked reads. Fail-closed:
    a failed action OR a failed confirming read OR any surviving run resource
    makes this return False (which fails the whole run). The throwaway token is
    always destroyed (secret hygiene), even with --keep or after a crash.

    Discovering by namespace closes the orphan window (Terra R2 finding 2): the
    id/name is unpredictable, so ONLY this run could have created a match, even
    if the create response was lost before its id reached ``state``."""
    result: dict = {"keep": keep, "actions": [], "confirmed": False}
    run_space_ns = {PROOF_SPACE, PROOF_LONG_SPACE}
    action_errors: list[str] = []

    def act(tool: str, args: dict, label: str) -> None:
        res = _api_tool(base_url, key, tool, args)
        status = res.get("status", "?")
        result["actions"].append({"tool": tool, "target": label, "status": status})
        # Deleting an already-absent resource may legitimately not report ok;
        # only outright transport/HTTP errors count as an action failure.
        if not isinstance(res, dict) or "http" in res or status == "error":
            action_errors.append(f"{tool}({label})={status}")

    # The discover-by-namespace sweep is SAFE only when the collision preflight
    # positively proved the namespace was free at start (Terra R6). Without that
    # proof, delete solely the exact ids/hashes this run recorded creating, so an
    # ambiguous preflight can never delete a pre-existing operator resource that
    # merely shares the namespace.
    sweep = state.preflight_clean
    result["namespace_sweep"] = sweep

    # Discover current state (strict) to drive the (gated) namespace sweep.
    ok_t, toks = _list_tokens_strict(base_url, key)
    ok_s, sids = _list_spaces_strict(base_url, key)

    # Token(s): always the recorded hash; add tokens bearing this run's unique
    # name ONLY under a proven-clean preflight.
    token_hashes: set[str] = set()
    if state.created_token_hash:
        token_hashes.add(state.created_token_hash)
    if sweep and ok_t:
        for t in toks:
            if _token_name(t) == PROOF_TOKEN_NAME:
                h = _token_hash(t)
                if h:
                    token_hashes.add(h)
    for h in token_hashes:
        act("admin_revoke_token", {"token_hash": h}, f"token:{h[:20]}")
        act("admin_delete_token", {"token_hash": h}, f"token:{h[:20]}")

    # Backups: always the recorded ids; add backups in this run's namespace (by
    # the backup_id's <space_id> prefix) ONLY under a proven-clean preflight.
    # Backups persist under _backups/<space>/ and survive space deletion, so
    # they must be deleted explicitly (Terra R9). Kept with --keep, like spaces.
    ok_b, backup_ids = _list_backups_strict(base_url, key)
    if not keep:
        target_backups = set(state.created_backups)
        if sweep and ok_b:
            target_backups |= {b for b in backup_ids if _backup_space(b) in run_space_ns}
        for bid in sorted(target_backups):
            act("backup_delete", {"backup_id": bid, "confirm": True}, f"backup:{bid}")

    # Spaces: always the recorded ids; add namespaced listed spaces ONLY under a
    # proven-clean preflight (unless --keep).
    if not keep:
        targets = set(state.created_spaces)
        if sweep and ok_s:
            targets |= {s for s in sids if s in run_space_ns}
        for sid in sorted(targets):
            act("space_delete", {"space_id": sid, "confirm": True}, f"space:{sid}")

    # CONFIRM via fresh strict reads — check exactly what we tried to remove.
    ok_s2, sids2 = _list_spaces_strict(base_url, key)
    ok_t2, toks2 = _list_tokens_strict(base_url, key)
    ok_b2, backup_ids2 = _list_backups_strict(base_url, key)
    remaining: list[str] = []
    if not keep and ok_s2:
        if sweep:
            remaining += [s for s in sids2 if s in run_space_ns]
        else:
            remaining += [s for s in state.created_spaces if s in sids2]
    if not keep and ok_b2:
        if sweep:
            remaining += [b for b in backup_ids2 if _backup_space(b) in run_space_ns]
        else:
            remaining += [b for b in state.created_backups if b in backup_ids2]
    if ok_t2 and token_hashes:
        hashes2 = {_token_hash(t) for t in toks2}
        names2 = {_token_name(t) for t in toks2}
        if (token_hashes & hashes2) or (sweep and PROOF_TOKEN_NAME in names2):
            remaining.append("proof-token")

    reads_ok = ok_t and ok_s and ok_b and ok_s2 and ok_t2 and ok_b2
    result["reads_ok"] = reads_ok
    result["action_errors"] = action_errors
    if remaining:
        result["unremoved"] = remaining
    result["confirmed"] = (teardown_is_confirmed(reads_ok, remaining)
                           and not action_errors)

    report.teardown = result
    print(f"  teardown: {json.dumps(result)}")
    return result["confirmed"]


# ===========================================================================
# Browser-side assertion battery (§8.3)
# ===========================================================================

_PAGE_ASSERT_JS = r"""
(sentinels) => {
    const content = document.getElementById('content');
    const out = {};
    out.a1_nonblank = !!content && content.children.length >= 1
        && (content.innerText || '').trim().length > 0;
    out.a2_no_hoverflow = document.documentElement.scrollWidth <= window.innerWidth;
    const bodyText = (content ? content.innerText : '') || '';
    out.a3_fakes = [];
    for (const s of sentinels) {
        const escaped = s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const left = /^\w/.test(s) ? '\\b' : '';
        const right = /\w$/.test(s) ? '\\b' : '';
        const re = new RegExp(left + escaped + right, 'i');
        if (re.test(bodyText)) out.a3_fakes.push(s);
    }
    out.a6_bad = [];
    const els = content ? content.querySelectorAll('*') : [];
    for (const el of els) {
        const cs = getComputedStyle(el);
        if (cs.textOverflow !== 'ellipsis') continue;
        if (el.scrollWidth <= el.clientWidth) continue;
        const hasTitle = el.hasAttribute('title') && el.getAttribute('title').trim().length > 0;
        const hasCopy = !!el.querySelector('[data-copy],[data-action="copy-value"],.copy-btn')
            || el.closest('.mono-chip') != null;
        if (!hasTitle && !hasCopy) out.a6_bad.push((el.className || el.tagName || '').toString().slice(0, 60));
    }
    const clone = content ? content.cloneNode(true) : null;
    if (clone) clone.querySelectorAll('.server-msg, .server-msg-text').forEach(n => n.remove());
    const uiText = clone ? (clone.innerText || '') : '';
    out.a12_emoji = /\p{Extended_Pictographic}/u.test(uiText);
    return out;
}
"""


def run_view_battery(page, report: Report, view: str, viewport_label: str) -> None:
    res = page.evaluate(_PAGE_ASSERT_JS, FAKE_DATA_SENTINELS)
    report.record("A-1 non-blank", view, viewport_label, res["a1_nonblank"])
    report.record("A-2 no h-overflow", view, viewport_label, res["a2_no_hoverflow"])
    report.record("A-3 no fake data", view, viewport_label, not res["a3_fakes"], ",".join(res["a3_fakes"]))
    report.record("A-6 truncation affordance", view, viewport_label, not res["a6_bad"], ";".join(res["a6_bad"][:5]))
    report.record("A-12 no UI emoji", view, viewport_label, not res["a12_emoji"])


# ===========================================================================
# Login + crawl
# ===========================================================================


def ui_login(page, base_url: str, key: str) -> None:
    _nav(page, f"{base_url}/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginOverlay", state="visible", timeout=15000)
    page.fill("#loginToken", key)
    page.click("#loginBtn")
    page.wait_for_selector("#sidebarNav a[data-nav]", state="attached", timeout=15000)
    page.wait_for_function(
        "() => { const o = document.getElementById('loginOverlay');"
        " return !o || getComputedStyle(o).display === 'none' || !o.offsetParent; }",
        timeout=15000)


def crawl(page, context, base_url: str, key: str, out_dir: Path, report: Report) -> int:
    from playwright.sync_api import Error as PlaywrightError

    for (w, h) in VIEWPORTS:
        viewport_label = f"{w}x{h}"
        page.set_viewport_size({"width": w, "height": h})
        for route_tmpl in VIEWS:
            route = route_tmpl.replace("{space}", PROOF_SPACE)
            counter = {"n": 0}

            def _count(req, counter=counter):
                if "/api/" in req.url:
                    counter["n"] += 1

            page.on("request", _count)
            _nav(page, f"{base_url}/admin{route}", wait_until="networkidle")
            page.wait_for_timeout(400)
            page.remove_listener("request", _count)
            report.request_counts[f"{route}@{viewport_label}"] = counter["n"]

            run_view_battery(page, report, route, viewport_label)

            before = page.url
            _reload(page, wait_until="networkidle")
            report.record("A-11 reload-stable", route, viewport_label,
                          page.url == before, f"{before} -> {page.url}")

            shot = out_dir / f"{_slug(route_tmpl)}__{viewport_label}.png"
            page.screenshot(path=str(shot), full_page=True)
            report.shots.append(shot.name)

        if (w, h) == VIEWPORTS[0]:
            _nav(page, f"{base_url}/admin#/dashboard", wait_until="networkidle")
            _wait_for_view_loaded(page)
            idle = {"urls": []}

            def _idle(req, idle=idle):
                if "/api/" in req.url:
                    idle["urls"].append(req.url)

            page.on("request", _idle)
            page.wait_for_timeout(30000)
            page.remove_listener("request", _idle)
            report.record("A-10 no polling (30s idle)", "#/dashboard", viewport_label,
                          not idle["urls"],
                          f"{len(idle['urls'])} requests during idle: {idle['urls'][:4]}")

    # A-8 fonts (desktop).
    page.set_viewport_size({"width": 1440, "height": 900})
    _nav(page, f"{base_url}/admin#/dashboard", wait_until="networkidle")
    for fam in REQUIRED_FONTS:
        loaded = page.evaluate("(f) => document.fonts.check(`16px \"${f}\"`)", fam)
        report.record("A-8 font loaded", "#/dashboard", "1440x900", bool(loaded), fam)

    _cross_check_data_honesty(page, report, base_url, key)

    # A-7 focus visibility on a sidebar nav stop (dark surface → cyan #00A7C7).
    try:
        _nav(page, f"{base_url}/admin#/dashboard", wait_until="networkidle")
        page.focus("#sidebarNav a[data-nav]")
        color = page.evaluate("() => getComputedStyle(document.querySelector("
                              "'#sidebarNav a[data-nav]')).outlineColor")
        report.record("A-7 focus ring (sidebar)", "#/dashboard", "1440x900",
                      color == FOCUS_CYAN_SIDEBAR, f"outlineColor={color}")
    except PlaywrightError as exc:
        report.record("A-7 focus ring (sidebar)", "#/dashboard", "1440x900", False, str(exc))

    # A-9 session wipe: clearing the HttpOnly cookie + acting shows login and
    # wipes privileged DOM.
    auth_error_checkpoint = len(report.console_errors)
    try:
        context.clear_cookies()
        _nav(page, f"{base_url}/admin#/access", wait_until="domcontentloaded")
        page.wait_for_selector("#loginOverlay", state="visible", timeout=15000)
        wiped = page.evaluate("() => { const c = document.getElementById('content');"
                              " return !c || c.querySelectorAll('table,[data-hash]').length === 0; }")
        report.record("A-9 session wipe", "#/access", "1440x900", bool(wiped),
                      "login shown + content wiped")
    except PlaywrightError as exc:
        report.record("A-9 session wipe", "#/access", "1440x900", False, str(exc))
    # Return the checkpoint so the caller can keep this deliberate auth-negative
    # phase open until the subsequent positive login has fully rerendered. The
    # browser may emit failed-resource console entries after the login overlay
    # itself is already visible.
    return auth_error_checkpoint


def _cross_check_data_honesty(page, report: Report, base_url: str, key: str) -> None:
    """A-5: rendered counters must EQUAL authoritative reads (no tautologies).
    Each check FAILS if the authoritative read did not explicitly succeed — a
    failed read is never accepted as an honest empty set (Terra R2 finding 3)."""
    sl_ok, space_ids = _list_spaces_strict(base_url, key)

    # Dashboard Spaces tile count == authoritative space_list length.
    _nav(page, f"{base_url}/admin#/dashboard", wait_until="networkidle")
    _wait_for_view_loaded(page)
    dash_total = page.evaluate(
        "() => { const el = document.querySelector('#content a.dash-tile-link"
        "[href=\"#/spaces\"] .metric-value'); if (!el) return null;"
        " const n = parseInt((el.textContent||'').trim(), 10); return Number.isNaN(n) ? null : n; }")
    report.record("A-5 dashboard spaces == space_list", "#/dashboard", "1440x900",
                  crosscheck_ok(sl_ok, dash_total, len(space_ids)),
                  f"read_ok={sl_ok} dashboard={dash_total} space_list={len(space_ids)}")

    # Spaces rows: every rendered space id is real, and our seeded space is shown.
    _nav(page, f"{base_url}/admin#/spaces", wait_until="networkidle")
    _wait_for_view_loaded(page, "#spacesTableWrap")
    rendered_ids = page.evaluate(
        "() => Array.from(document.querySelectorAll('#content a[href^=\"#/spaces/\"]'))"
        ".map(a => decodeURIComponent(a.getAttribute('href').slice('#/spaces/'.length)))"
        ".filter(s => s && s.indexOf('/') === -1)")
    rendered_ids = [r for r in (rendered_ids or []) if r]
    report.record("A-5 spaces rows are real", "#/spaces", "1440x900",
                  sl_ok and ids_are_subset(rendered_ids, space_ids),
                  f"read_ok={sl_ok} stray={sorted(set(rendered_ids)-set(space_ids))}")
    report.record("A-5 seeded space is shown", "#/spaces", "1440x900",
                  sl_ok and PROOF_SPACE in rendered_ids,
                  f"read_ok={sl_ok} {PROOF_SPACE} in {len(rendered_ids)} rendered")

    # Access: rendered unique token-hash count == admin_list_tokens length.
    tk_ok, toks = _list_tokens_strict(base_url, key)
    token_n = len(toks) if tk_ok else -1
    _nav(page, f"{base_url}/admin#/access", wait_until="networkidle")
    _wait_for_view_loaded(page)
    rendered_token_n = page.evaluate(
        "() => new Set(Array.from(document.querySelectorAll('#content [data-hash]'))"
        ".map(e => e.getAttribute('data-hash'))).size")
    report.record("A-5 access rows == tokens", "#/access", "1440x900",
                  crosscheck_ok(tk_ok, rendered_token_n, token_n),
                  f"read_ok={tk_ok} rendered={rendered_token_n} admin_list_tokens={token_n}")


# ===========================================================================
# Interaction proofs (§8.2) — REAL create→secret→mask, resilient but honest
# ===========================================================================


def interaction_proofs(page, base_url: str, key: str, out_dir: Path, state: RunState,
                       report: Report) -> None:
    from playwright.sync_api import Error as PlaywrightError

    page.set_viewport_size({"width": 1440, "height": 900})

    # Shot 14 — Access create-token: drive the REAL form, submit, wait for the
    # shipped secret node #ctSecret, redact it and ASSERT redaction BEFORE any
    # capture. The created token is recorded (by exact hash) for teardown.
    try:
        _nav(page, f"{base_url}/admin#/access", wait_until="networkidle")
        page.click('[data-action="access-create"]')
        page.wait_for_selector("#ctName", timeout=8000)
        page.fill("#ctName", PROOF_TOKEN_NAME)
        page.fill("#ctSpaces", PROOF_SPACE)
        # Pick a read,write preset (never admin/manage) if the select exists.
        page.evaluate(
            "() => { const s = document.getElementById('ctPerms'); if (!s) return;"
            " const opt = Array.from(s.options).find(o => /read/i.test(o.value+o.text)"
            "   && /write/i.test(o.value+o.text) && !/admin|manage/i.test(o.value+o.text))"
            "   || s.options[0]; if (opt) s.value = opt.value; }")
        page.click("#modalConfirmBtn")
        # Wait for the shipped one-time-secret node.
        page.wait_for_selector("#ctSecret", timeout=10000)
        # Record the created token hash from the modal's #ctTokenHash node
        # IMMEDIATELY — before any redaction, capture, or API lookup — so a later
        # failure can never orphan it (Terra R2 finding 2). #ctTokenHash carries
        # the response's full sha256 hash (the identifier admin_revoke/delete
        # take); it is not the secret. Teardown's namespace sweep by the unique
        # token name is the fail-closed backstop if this node is absent.
        state.created_token_hash = page.evaluate(
            "() => { const n = document.getElementById('ctTokenHash');"
            " return n ? (n.textContent || '').trim() : null; }") or state.created_token_hash
        # Redact BEFORE any capture, then ASSERT the raw token is gone.
        redacted = page.evaluate(
            "() => { const n = document.getElementById('ctSecret'); if (!n) return false;"
            " n.textContent = '(redacted for proof)';"
            " return n.textContent === '(redacted for proof)'; }")
        secret_still_visible = page.evaluate(
            "() => { const n = document.getElementById('ctSecret');"
            " return n ? /lm_[A-Za-z0-9]/.test(n.textContent || '') : false; }")
        ok = bool(redacted) and not secret_still_visible
        shot = out_dir / "access_create_modal__1440x900.png"
        page.screenshot(path=str(shot))
        report.shots.append(shot.name)
        report.record("shot-14 create-token secret masked", "#/access", "1440x900", ok,
                       f"redacted={redacted} secret_still_visible={secret_still_visible}")
        # Fail-closed backstop: if #ctTokenHash was absent, resolve the hash by
        # this run's unique token name so teardown can still destroy it.
        if not state.created_token_hash:
            _ok, toks = _list_tokens_strict(base_url, key)
            if _ok:
                for t in toks:
                    if _token_name(t) == PROOF_TOKEN_NAME:
                        state.created_token_hash = _token_hash(t)
                        break
        report.record("shot-14 token created & recorded", "#/access", "1440x900",
                      state.created_token_hash is not None,
                      "recorded for teardown" if state.created_token_hash else "NOT recorded")
        page.click('[data-action="close-modal"]')
        page.wait_for_selector("#adminModal", state="hidden", timeout=8000)
    except PlaywrightError as exc:
        report.record("shot-14 create-token secret masked", "#/access", "1440x900", False, str(exc))

    # Shot 17 — typed-confirm destructive modal on the S2 long-id space (cancel;
    # actual deletion is owned by teardown).
    try:
        _nav(page, f"{base_url}/admin#/spaces/{PROOF_LONG_SPACE}", wait_until="networkidle")
        page.wait_for_selector(
            '[data-action="sd-confirm-space-delete"]', state="visible", timeout=15000
        )
        page.click('[data-action="sd-confirm-space-delete"]')
        page.wait_for_selector("#modalConfirmBtn", timeout=8000)
        disabled_before = page.evaluate(
            "() => { const b = document.getElementById('modalConfirmBtn'); return !!b && b.disabled; }")
        shot = out_dir / "typed_confirm_disabled__1440x900.png"
        page.screenshot(path=str(shot))
        report.shots.append(shot.name)
        typed = page.locator("#destructiveConfirmInput")
        typed.fill(PROOF_LONG_SPACE)
        enabled_after = page.evaluate(
            "() => { const b = document.getElementById('modalConfirmBtn'); return !!b && !b.disabled; }")
        report.record("shot-17 typed-confirm gate", f"#/spaces/{PROOF_LONG_SPACE}", "1440x900",
                      disabled_before and enabled_after,
                      f"disabled_before={disabled_before} enabled_after={enabled_after}")
        page.evaluate("() => { const b = document.querySelector('[data-action=\"close-modal\"]');"
                      " if (b) b.click(); }")
    except PlaywrightError as exc:
        report.record("shot-17 typed-confirm gate", f"#/spaces/{PROOF_LONG_SPACE}", "1440x900",
                      False, str(exc))


# ===========================================================================
# Main
# ===========================================================================


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P8-7 admin console visual proof harness")
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--out-dir", default="proof-artifacts")
    p.add_argument("--profile", choices=["full-llm", "degraded-llm"], default="degraded-llm")
    p.add_argument("--keep", action="store_true",
                   help="skip deletion of the proof spaces (the throwaway token is ALWAYS destroyed)")
    p.add_argument("--self-test", action="store_true",
                   help="run the offline data-honesty self-test and exit (no stack/browser)")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.self_test:
        return _self_test()

    key = os.environ.get(BOOTSTRAP_KEY_ENV, "").strip()
    if not key:
        sys.stderr.write(f"{BOOTSTRAP_KEY_ENV} is not set. Export the stack's "
                         "ADMIN_BOOTSTRAP_KEY into that env var (never pass it on argv).\n")
        return 2

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        sys.stderr.write("playwright is required. Run via `uv run scripts/admin_console_proof.py` "
                         "and install the browser once with "
                         "`uv run --with playwright playwright install chromium`.\n")
        return 2

    base_url = args.base_url.rstrip("/")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = Report()
    report.meta = {"profile": args.profile, "base_url": base_url, "run_id": RUN_ID}
    state = RunState()

    health = api_get_json(base_url, key, "/health")
    report.meta["health"] = {k: health.get(k) for k in ("status", "version") if k in health}
    print(f"[proof] stack health: {report.meta['health'] or 'unknown'}")

    # ONE outer try/finally covering seeding, fixture verification, AND the
    # browser work, so teardown ALWAYS runs — including on a KeyboardInterrupt
    # during the sleep or fixture verification (Terra R2/R3 medium finding).
    # Only SeedError is caught for a clean message; every other exception
    # (incl. the interrupt) runs teardown in `finally`, then propagates.
    exit_code = 1
    teardown_ok = False
    seed_failed = False
    try:
        collision_check(base_url, key, state)
        print("[proof] seeding …")
        seed(base_url, key, args.profile, state, report)
        time.sleep(2)  # let the consolidation job move off 'queued'
        verify_fixtures(base_url, key, report)

        with sync_playwright() as pw:
            report.meta["playwright_version"] = getattr(pw, "version", "?")
            browser = pw.chromium.launch(headless=True)
            report.meta["chromium_version"] = browser.version
            context = browser.new_context(device_scale_factor=2)  # tracing/video OFF
            page = context.new_page()
            # Count the browser's in-view /api fetches against the shared budget
            # so pacing before the next navigation accounts for them (Terra R3).
            page.on("request", lambda r: BUDGET.note() if "/api/" in r.url else None)
            page.on("console", lambda m: (report.console_errors.append(f"{m.location}: {m.text}")
                                          if m.type == "error" else None))
            page.on("pageerror", lambda e: report.console_errors.append(str(e)))
            page.add_init_script(
                "document.addEventListener('securitypolicyviolation', e => {"
                " (window.__cspViolations = window.__cspViolations || []).push("
                "  e.violatedDirective + ' ' + e.blockedURI); });")

            # Shot 1/2 — login screen + failure.
            page.set_viewport_size({"width": 1440, "height": 900})
            auth_error_checkpoint = len(report.console_errors)
            _nav(page, f"{base_url}/admin", wait_until="domcontentloaded")
            page.wait_for_selector("#loginOverlay", state="visible", timeout=15000)
            page.screenshot(path=str(out_dir / "login__1440x900.png"))
            report.shots.append("login__1440x900.png")
            page.fill("#loginToken", "deliberately-wrong-token")
            page.click("#loginBtn")
            page.wait_for_timeout(600)
            page.screenshot(path=str(out_dir / "login_failure__1440x900.png"))
            report.shots.append("login_failure__1440x900.png")
            ui_login(page, base_url, key)
            _wait_for_view_loaded(page)
            report.console_errors[auth_error_checkpoint:] = unexpected_console_errors(
                report.console_errors[auth_error_checkpoint:], ("/api/spaces", "/api/login")
            )
            auth_recovery_checkpoint = crawl(
                page, context, base_url, key, out_dir, report
            )

            ui_login(page, base_url, key)  # A-9 wiped the session
            _wait_for_view_loaded(page)
            report.console_errors[auth_recovery_checkpoint:] = unexpected_console_errors(
                report.console_errors[auth_recovery_checkpoint:], ("/api/spaces", "/api/tool")
            )
            interaction_proofs(page, base_url, key, out_dir, state, report)

            try:
                _nav(page, f"{base_url}/admin#/dashboard", wait_until="networkidle")
                report.csp_violations.extend(page.evaluate("() => window.__cspViolations || []") or [])
            except PlaywrightError:
                pass

            report.record("A-4 zero console errors", "ALL", "both",
                          not report.console_errors, str(report.console_errors[:3]))
            report.record("A-4 zero CSP violations", "ALL", "both",
                          not report.csp_violations, str(report.csp_violations[:3]))

            context.close()
            browser.close()
    except SeedError as exc:
        sys.stderr.write(f"[proof] seed/fixture error: {exc}\n")
        seed_failed = True
    finally:
        # Always attempt cleanup + report, even on interrupt (then it propagates).
        teardown_ok = teardown(base_url, key, args.keep, state, report)
        (out_dir / "proof-report.json").write_text(json.dumps(report.to_dict(), indent=2), "utf-8")
        summary = report.to_dict()["summary"]
        print(f"\n[proof] report written to {out_dir / 'proof-report.json'}")
        print(f"[proof] {summary['passed']}/{summary['total']} assertions passed, "
              f"{summary['failed']} failed; teardown_confirmed={teardown_ok}; "
              f"{len(report.shots)} screenshots")

    if seed_failed:
        return 1
    # Gate: fail if any assertion failed OR teardown could not confirm it removed
    # exactly what this run created.
    exit_code = 0 if (not report.failed and teardown_ok) else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
