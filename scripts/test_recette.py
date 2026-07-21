#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de recette global — Hivemind

Script unifié avec sélection de suites par ligne de commande.

Suites disponibles :
    - recette    : Pipeline complet (agent → notes → consolidation → bank)
    - isolation  : Tests d'allowlist inter-spaces (pas une frontière tenant)
    - qualite    : Tests de qualité des principaux outils MCP

Usage :
    uv run python scripts/test_recette.py                         # TOUTES les suites
    uv run python scripts/test_recette.py --suite isolation       # Juste l'isolation
    uv run python scripts/test_recette.py --suite recette         # Juste la recette
    uv run python scripts/test_recette.py --suite isolation,recette # Plusieurs
    uv run python scripts/test_recette.py --list                  # Lister les suites
    uv run python scripts/test_recette.py --suite isolation --step -v  # Step + verbose

Prérequis : docker compose up -d
"""

import os
import sys
import time
import asyncio
import argparse
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from cli.client import MCPClient

# ═══════════════════════════════════════════════════════════════
# Configuration globale
# ═══════════════════════════════════════════════════════════════

VERBOSE = False
STEP_MODE = False
PAUSE_SECONDS = 0  # Pause entre étapes clés (secondes) — permet d'observer sur /live
CALL_DELAY = 0.3  # Délai entre appels MCP (secondes) — réduit grâce aux rate limits WAF augmentés
CONSOLIDATION_WAIT_TIMEOUT_SECONDS = 900.0
CONSOLIDATION_STATUS_INTERVAL_SECONDS = 2.0
_VERSION_PATH = os.path.join(os.path.dirname(__file__), "..", "VERSION")
try:
    with open(_VERSION_PATH, encoding="utf-8") as _version_file:
        PRODUCT_VERSION = _version_file.read().strip()
except OSError:
    PRODUCT_VERSION = "dev"

# ═══════════════════════════════════════════════════════════════
# Affichage (partagé entre toutes les suites)
# ═══════════════════════════════════════════════════════════════

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
C = "\033[96m"
M = "\033[95m"
B = "\033[1m"
D = "\033[2m"
Z = "\033[0m"

passed = 0
failed = 0
skipped = 0
results = []


def pause(msg=""):
    """Pause between key steps: --step (interactive) or --pause N (timed)."""
    if STEP_MODE:
        print(f"\n  {D}⏸  {msg or 'Press Enter to continue...'}{Z}", end="", flush=True)
        input()
    elif PAUSE_SECONDS > 0:
        label = msg or "Next step"
        print(f"\n  {Y}⏳ {label} — waiting {PAUSE_SECONDS}s (observe on /live)…{Z}", flush=True)
        time.sleep(PAUSE_SECONDS)


def header(t):
    w = 70
    print(f"\n{B}{C}{'═' * w}{Z}")
    print(f"{B}{C}  {t}{Z}")
    print(f"{B}{C}{'═' * w}{Z}")


def section(t):
    print(f"\n{B}{M}── {t} ──{Z}")


def test_pass(name, detail=""):
    global passed
    passed += 1
    results.append(("PASS", name))
    d = f" — {detail}" if detail else ""
    print(f"  {G}✅ PASS{Z}  {name}{D}{d}{Z}")


def test_fail(name, detail=""):
    global failed
    failed += 1
    results.append(("FAIL", name))
    d = f" — {detail}" if detail else ""
    print(f"  {R}❌ FAIL{Z}  {name}{d}")


def test_skip(name, detail=""):
    global skipped
    skipped += 1
    results.append(("SKIP", name))
    d = f" — {detail}" if detail else ""
    print(f"  {Y}⏭  SKIP{Z}  {name}{D}{d}{Z}")


def vprint(msg):
    if VERBOSE:
        print(f"  {D}    → {msg}{Z}")


async def _wait_for_consolidation(
    client: MCPClient,
    acknowledgement: Mapping[str, Any],
    *,
    timeout_seconds: float = CONSOLIDATION_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = CONSOLIDATION_STATUS_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Wait for one explicitly tracked operator-run consolidation job.

    Normal agents must return after the async acknowledgement. This recipe is
    an explicit E2E runner, so it deliberately follows the returned ``job_id``
    until a terminal state, with a hard deadline and no automatic re-enqueue.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")

    ack_status = acknowledgement.get("status")
    job_id = acknowledgement.get("job_id")
    if ack_status not in {"running", "queued"}:
        return {
            "status": "invalid_ack",
            "message": (
                "bank_consolidate must return status running|queued; "
                f"received {ack_status!r}"
            ),
            "acknowledgement": dict(acknowledgement),
        }
    if not isinstance(job_id, str) or not job_id.strip():
        return {
            "status": "invalid_ack",
            "message": "bank_consolidate acknowledgement is missing a non-empty job_id",
            "acknowledgement": dict(acknowledgement),
        }

    deadline = monotonic_fn() + timeout_seconds
    last_status = ack_status
    while True:
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            return {
                "status": "timeout",
                "job_id": job_id,
                "last_status": last_status,
                "message": (
                    f"consolidation job {job_id} did not reach a terminal state "
                    f"within {timeout_seconds:g}s (last status: {last_status})"
                ),
            }

        await sleep_fn(min(poll_interval_seconds, remaining))
        if monotonic_fn() >= deadline:
            return {
                "status": "timeout",
                "job_id": job_id,
                "last_status": last_status,
                "message": (
                    f"consolidation job {job_id} did not reach a terminal state "
                    f"within {timeout_seconds:g}s (last status: {last_status})"
                ),
            }

        status_timeout = deadline - monotonic_fn()
        if status_timeout <= 0:
            return {
                "status": "timeout",
                "job_id": job_id,
                "last_status": last_status,
                "message": (
                    f"consolidation job {job_id} did not reach a terminal state "
                    f"within {timeout_seconds:g}s (last status: {last_status})"
                ),
            }
        try:
            response = await asyncio.wait_for(
                client.call_tool(
                    "bank_consolidation_status", {"job_id": job_id}
                ),
                timeout=status_timeout,
            )
        except TimeoutError:
            return {
                "status": "timeout",
                "job_id": job_id,
                "last_status": last_status,
                "message": (
                    f"consolidation job {job_id} did not reach a terminal state "
                    f"within {timeout_seconds:g}s (last status: {last_status})"
                ),
            }
        if not isinstance(response, dict):
            return {
                "status": "protocol_error",
                "job_id": job_id,
                "message": "bank_consolidation_status returned a non-object response",
            }

        status = response.get("status")
        if status == "succeeded":
            result = response.get("result")
            if not isinstance(result, dict) or result.get("status") != "ok":
                return {
                    "status": "protocol_error",
                    "job_id": job_id,
                    "message": (
                        "terminal succeeded response is missing result.status=ok"
                    ),
                    "response": response,
                }
            return response
        if status == "failed":
            return response
        if status not in {"running", "queued"}:
            return {
                "status": "protocol_error",
                "job_id": job_id,
                "message": (
                    "bank_consolidation_status returned unexpected status "
                    f"{status!r}"
                ),
                "response": response,
            }
        last_status = status


async def _consolidate_and_wait(
    client: MCPClient,
    space_id: str,
    *,
    timeout_seconds: float = CONSOLIDATION_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = CONSOLIDATION_STATUS_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    acknowledgement = await client.call_tool(
        "bank_consolidate", {"space_id": space_id}
    )
    if not isinstance(acknowledgement, dict):
        return {
            "status": "invalid_ack",
            "message": "bank_consolidate returned a non-object response",
        }
    return await _wait_for_consolidation(
        client,
        acknowledgement,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )


def _consolidation_failure_detail(outcome: Mapping[str, Any]) -> str:
    result = outcome.get("result")
    nested_message = result.get("message") if isinstance(result, dict) else None
    return str(
        outcome.get("error")
        or nested_message
        or outcome.get("message")
        or f"status={outcome.get('status')!r}"
    )


# ═══════════════════════════════════════════════════════════════
#
#  SUITE : RECETTE — Pipeline agent complet
#
# ═══════════════════════════════════════════════════════════════

RECETTE_SPACE = "recette-ubuntu-update"
RECETTE_TOKEN_NAME = "agent-sysadmin"

RECETTE_RULES = """# Rules — Memory Bank Standard
La bank doit contenir 3 fichiers :
### summary.md
Résumé général du contexte.
### decisions.md
Décisions prises et leur justification.
### progress.md
Avancement, ce qui reste à faire.
"""

RECETTE_NOTES = [
    ("observation", "Audit serveur : Ubuntu 22.04, 16 Go RAM, nginx+postgresql+php."),
    ("decision", "Stratégie : do-release-upgrade. Rollback : snapshot VM."),
    ("todo", "Checklist : backup /etc, dump PostgreSQL, snapshot VM, notifier users."),
    ("progress", "Backups pré-migration terminés. /etc: 12Mo, PostgreSQL: 340Mo."),
    ("issue", "php8.1-fpm supprimé pendant l'upgrade → installer php8.3-fpm."),
    ("progress", "TERMINÉ. Durée: 1h45. 2 issues résolues. Tous services OK."),
]


async def suite_recette(admin: MCPClient, url: str, do_cleanup: bool):
    """Suite RECETTE : pipeline agent → notes → consolidation → bank."""
    header("🧪 SUITE : RECETTE — Pipeline agent complet")

    manager_token = ""
    agent_token = ""
    agent_hash = ""

    # 1. Health check
    section("Recette 1/7 — Health check")
    try:
        r = await admin.call_tool("system_health", {})
        if r.get("status") in ("healthy", "degraded"):
            test_pass("health check", f"status={r.get('status')}")
        else:
            test_fail("health check", r.get("message", str(r)))
            return
    except Exception as e:
        test_fail("health check", str(e))
        return

    # 2. Bootstrap/admin crée un manager persistant. Le nouveau token_create
    # refuse volontairement le bootstrap (pas de hash revalidable en store).
    section("Recette 2/7 — Manager de provisioning")
    try:
        r = await admin.call_tool(
            "admin_create_token",
            {
                "name": f"{RECETTE_TOKEN_NAME}-manager",
                "permissions": "read,write,manage",
                "expires_in_days": 1,
            },
        )
        if r.get("status") == "created":
            manager_token = r["token"]
            test_pass("create manager", f"{RECETTE_TOKEN_NAME}-manager")
        else:
            test_fail("create manager", r.get("message", ""))
            return
    except Exception as e:
        test_fail("create manager", str(e))
        return

    manager = MCPClient(
        base_url=url, token=manager_token, timeout=600, call_delay=CALL_DELAY
    )

    # 3. Le manager crée le space, crée un writer sans scope, puis l'invite.
    section("Recette 3/7 — Espace + rules")
    try:
        r = await manager.call_tool(
            "space_create",
            {
                "space_id": RECETTE_SPACE,
                "description": "Recette Ubuntu 22.04 → 24.04",
                "rules": RECETTE_RULES,
            },
        )
        if r.get("status") in ("created", "already_exists"):
            test_pass("space_create", RECETTE_SPACE)
        else:
            test_fail("space_create", r.get("message", ""))
            return
    except Exception as e:
        test_fail("space_create", str(e))
        return

    try:
        r = await manager.call_tool(
            "token_create",
            {
                "name": RECETTE_TOKEN_NAME,
                "permissions": "read,write",
                "expires_in_days": 1,
            },
        )
        if r.get("status") != "created":
            test_fail("token_create", r.get("message", ""))
            return
        agent_token = r["token"]
        agent_hash = r["token_hash"]
        test_pass("token_create", "writer sans scope initial")

        r = await manager.call_tool(
            "space_invite_token",
            {"space_id": RECETTE_SPACE, "token_hash": agent_hash},
        )
        if r.get("status") != "ok":
            test_fail("space_invite_token", r.get("message", ""))
            return
        test_pass("space_invite_token", RECETTE_SPACE)
    except Exception as e:
        test_fail("provision writer", str(e))
        return

    agent = MCPClient(
        base_url=url, token=agent_token, timeout=600, call_delay=CALL_DELAY
    )

    pause("Space créé → Notes")

    # 4. Écrire les notes
    section("Recette 4/7 — Notes live")
    notes_ok = 0
    for cat, content in RECETTE_NOTES:
        try:
            r = await agent.call_tool(
                "live_note",
                {
                    "space_id": RECETTE_SPACE,
                    "category": cat,
                    "content": content,
                },
            )
            if r.get("status") in ("ok", "created"):
                notes_ok += 1
        except Exception:
            pass
    if notes_ok == len(RECETTE_NOTES):
        test_pass("live_note", f"{notes_ok}/{len(RECETTE_NOTES)} notes")
    else:
        test_fail("live_note", f"{notes_ok}/{len(RECETTE_NOTES)}")

    pause("Notes written → Consolidation")

    # 5. Consolidation
    section("Recette 5/7 — Consolidation LLM")
    consolidation_ready = False
    try:
        t0 = time.monotonic()
        outcome = await _consolidate_and_wait(agent, RECETTE_SPACE)
        dur = round(time.monotonic() - t0, 1)
        result = outcome.get("result")
        notes_processed = (
            result.get("notes_processed", 0) if isinstance(result, dict) else 0
        )
        if outcome.get("status") == "succeeded" and notes_processed > 0:
            consolidation_ready = True
            test_pass(
                "consolidate",
                f"{notes_processed} notes → bank ({dur}s, terminal=succeeded)",
            )
        elif outcome.get("status") == "succeeded":
            test_fail("consolidate", "terminal=succeeded mais aucune note traitée")
        else:
            test_fail("consolidate", _consolidation_failure_detail(outcome))
    except Exception as e:
        test_fail("consolidate", str(e))

    if not consolidation_ready:
        test_skip("bank_read_all", "consolidation non terminée avec succès")
        test_skip("bank coherence", "consolidation non terminée avec succès")
    else:
        pause("Consolidation terminale OK → Bank")

        # 6. Lire la bank uniquement après le statut terminal succeeded.
        section("Recette 6/8 — Lecture bank")
        try:
            r = await agent.call_tool("bank_read_all", {"space_id": RECETTE_SPACE})
            files = r.get("files", [])
            if len(files) > 0:
                names = [f.get("filename", "?") for f in files]
                test_pass(
                    "bank_read_all",
                    f"{len(files)} fichiers : {', '.join(names[:5])}",
                )
            else:
                test_fail("bank_read_all", "aucun fichier")
        except Exception as e:
            test_fail("bank_read_all", str(e))

        # 7. Vérifier que bank_read fonctionne pour CHAQUE fichier.
        section("Recette 7/8 — Cohérence bank_read vs bank_list")
        try:
            r_list = await agent.call_tool("bank_list", {"space_id": RECETTE_SPACE})
            bank_files = r_list.get("files", [])
            readable = 0
            broken = []
            for bf in bank_files:
                fname = bf.get("filename", "")
                r_read = await agent.call_tool(
                    "bank_read",
                    {
                        "space_id": RECETTE_SPACE,
                        "filename": fname,
                    },
                )
                if r_read.get("status") == "ok":
                    readable += 1
                else:
                    broken.append(fname)

            if broken:
                test_fail(
                    "bank coherence",
                    f"{len(broken)} fichier(s) illisibles via bank_read : {broken}",
                )
            elif readable > 0:
                test_pass(
                    "bank coherence",
                    f"{readable}/{len(bank_files)} fichiers lisibles via bank_read",
                )
            else:
                test_skip("bank coherence", "aucun fichier bank")
        except Exception as e:
            test_fail("bank coherence", str(e))

    # 8. Cleanup
    if do_cleanup:
        section("Recette 7/7 — Cleanup")
        try:
            await admin.call_tool(
                "space_delete", {"space_id": RECETTE_SPACE, "confirm": True}
            )
            test_pass("cleanup recette", f"space '{RECETTE_SPACE}' supprimé")
        except Exception:
            pass
        try:
            r = await admin.call_tool("admin_list_tokens", {})
            for t in r.get("tokens", []):
                if t.get("name", "").startswith(RECETTE_TOKEN_NAME) and not t.get("revoked"):
                    await admin.call_tool(
                        "admin_revoke_token", {"token_hash": t["hash"]}
                    )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#
#  SUITE : ISOLATION — Allowlist inter-spaces (pas une frontière tenant)
#
# ═══════════════════════════════════════════════════════════════

ISO_SPACE_A = "iso-test-alpha"
ISO_SPACE_B = "iso-test-beta"
ISO_SPACE_C = "iso-test-gamma"

ISO_RULES = """# Rules — Test Isolation
### context.md
Contexte de test.
"""


async def suite_isolation(admin: MCPClient, url: str, do_cleanup: bool):
    """Suite ISOLATION : vérifie l'allowlist inter-spaces mono-tenant."""
    header("🔒 SUITE : ISOLATION — allowlist inter-spaces")

    token_a = token_b = token_ro = token_manager = ""

    # ── SETUP ──────────────────────────────────────────
    section("Isolation 1/6 — Setup tokens + spaces")

    for name, perms, sids, label in [
        ("iso-client-a", "read,write", ISO_SPACE_A, "client-A"),
        ("iso-client-b", "read,write", ISO_SPACE_B, "client-B"),
        ("iso-readonly", "read", ISO_SPACE_A, "read-only"),
        ("iso-manager", "read,write,manage", "", "manager"),
    ]:
        try:
            r = await admin.call_tool(
                "admin_create_token",
                {
                    "name": name,
                    "permissions": perms,
                    "space_ids": sids,
                },
            )
            if r.get("status") == "created":
                if label == "client-A":
                    token_a = r["token"]
                elif label == "client-B":
                    token_b = r["token"]
                elif label == "read-only":
                    token_ro = r["token"]
                else:
                    token_manager = r["token"]
                test_pass(f"token {label}", f"perms={perms}, spaces={sids}")
            else:
                test_fail(f"token {label}", r.get("message", ""))
        except Exception as e:
            test_fail(f"token {label}", str(e))

    if not (token_a and token_b and token_ro and token_manager):
        test_fail("setup", "tokens manquants, arrêt")
        return

    for sid in [ISO_SPACE_A, ISO_SPACE_B]:
        try:
            r = await admin.call_tool(
                "space_create",
                {
                    "space_id": sid,
                    "description": f"Isolation {sid}",
                    "rules": ISO_RULES,
                },
            )
            if r.get("status") in ("created", "already_exists"):
                test_pass(f"space {sid}", "créé")
            else:
                test_fail(f"space {sid}", r.get("message", ""))
        except Exception as e:
            test_fail(f"space {sid}", str(e))

    # Écrire une note + backup dans chaque space
    for sid in [ISO_SPACE_A, ISO_SPACE_B]:
        try:
            await admin.call_tool(
                "live_note",
                {
                    "space_id": sid,
                    "category": "observation",
                    "content": f"Note de test dans {sid}",
                    "agent": "admin-setup",
                },
            )
            await admin.call_tool(
                "backup_create",
                {
                    "space_id": sid,
                    "description": f"Backup test {sid}",
                },
            )
        except Exception:
            pass

    ca = MCPClient(base_url=url, token=token_a, timeout=120, call_delay=CALL_DELAY)
    cb = MCPClient(base_url=url, token=token_b, timeout=120, call_delay=CALL_DELAY)
    ro = MCPClient(base_url=url, token=token_ro, timeout=120, call_delay=CALL_DELAY)
    manager = MCPClient(
        base_url=url, token=token_manager, timeout=120, call_delay=CALL_DELAY
    )

    pause("Setup OK → Isolation")

    # ── ISOLATION ──────────────────────────────────────
    section("Isolation 2/6 — Accès inter-espaces")

    # A ne peut pas lire B
    try:
        r = await ca.call_tool("live_read", {"space_id": ISO_SPACE_B, "limit": 10})
        if r.get("status") == "error":
            test_pass("A → B REFUSÉ", "isolation OK")
        else:
            test_fail("A → B DEVRAIT ÉCHOUER", f"status={r.get('status')}")
    except Exception as e:
        test_fail("A → B", str(e))

    # B ne peut pas lire A
    try:
        r = await cb.call_tool("live_read", {"space_id": ISO_SPACE_A, "limit": 10})
        if r.get("status") == "error":
            test_pass("B → A REFUSÉ", "isolation OK")
        else:
            test_fail("B → A DEVRAIT ÉCHOUER", f"status={r.get('status')}")
    except Exception as e:
        test_fail("B → A", str(e))

    # A peut lire A
    try:
        r = await ca.call_tool("live_read", {"space_id": ISO_SPACE_A, "limit": 10})
        if r.get("status") == "ok":
            test_pass("A → A OK", f"{r.get('total', 0)} notes")
        else:
            test_fail("A → A DEVRAIT OK", r.get("message", ""))
    except Exception as e:
        test_fail("A → A", str(e))

    # A ne peut pas écrire dans B
    try:
        r = await ca.call_tool(
            "live_note",
            {
                "space_id": ISO_SPACE_B,
                "category": "observation",
                "content": "Intrusion",
            },
        )
        if r.get("status") == "error":
            test_pass("A écrire B REFUSÉ", "isolation OK")
        else:
            test_fail("A écrire B DEVRAIT ÉCHOUER", f"status={r.get('status')}")
    except Exception as e:
        test_fail("A écrire B", str(e))

    # space_list filtré
    try:
        r = await ca.call_tool("space_list", {})
        spaces = [s.get("space_id") for s in r.get("spaces", [])]
        if ISO_SPACE_A in spaces and ISO_SPACE_B not in spaces:
            test_pass("space_list A filtré", f"voit {spaces}")
        else:
            test_fail("space_list A", f"spaces={spaces}")
    except Exception as e:
        test_fail("space_list A", str(e))

    pause("Isolation OK → Backup filtering")

    # ── BACKUP FILTERING ──────────────────────────────
    section("Isolation 3/6 — Filtrage backup_list")

    try:
        r = await ca.call_tool("backup_list", {})
        backups = r.get("backups", [])
        bk_spaces = {
            b.get("space_id", b.get("backup_id", "").split("/")[0]) for b in backups
        }
        if ISO_SPACE_B not in bk_spaces:
            test_pass("backup_list A filtré", f"ne voit PAS {ISO_SPACE_B}")
        else:
            test_fail("backup_list A VOIT B", f"spaces={bk_spaces}")
    except Exception as e:
        test_fail("backup_list A", str(e))

    try:
        r = await cb.call_tool("backup_list", {})
        backups = r.get("backups", [])
        bk_spaces = {
            b.get("space_id", b.get("backup_id", "").split("/")[0]) for b in backups
        }
        if ISO_SPACE_A not in bk_spaces:
            test_pass("backup_list B filtré", f"ne voit PAS {ISO_SPACE_A}")
        else:
            test_fail("backup_list B VOIT A", f"spaces={bk_spaces}")
    except Exception as e:
        test_fail("backup_list B", str(e))

    pause("Backup OK → Read-only")

    # ── READ-ONLY ─────────────────────────────────────
    section("Isolation 4/6 — Read-only ne peut pas écrire")

    try:
        r = await ro.call_tool("live_read", {"space_id": ISO_SPACE_A, "limit": 10})
        if r.get("status") == "ok":
            test_pass("reader lire A OK", f"{r.get('total', 0)} notes")
        else:
            test_fail("reader lire A", r.get("message", ""))
    except Exception as e:
        test_fail("reader lire A", str(e))

    try:
        r = await ro.call_tool(
            "live_note",
            {
                "space_id": ISO_SPACE_A,
                "category": "observation",
                "content": "Tentative RO",
            },
        )
        if r.get("status") == "error" and "write" in r.get("message", "").lower():
            test_pass("reader écrire REFUSÉ", "write requis")
        else:
            test_fail("reader écrire DEVRAIT ÉCHOUER", f"status={r.get('status')}")
    except Exception as e:
        test_fail("reader écrire", str(e))

    try:
        r = await ro.call_tool(
            "space_create",
            {
                "space_id": "iso-unauthorized",
                "description": "Non",
                "rules": "# no",
            },
        )
        if r.get("status") == "error":
            test_pass("reader space_create REFUSÉ", "manage requis")
        else:
            test_fail(
                "reader space_create DEVRAIT ÉCHOUER", f"status={r.get('status')}"
            )
    except Exception as e:
        test_fail("reader space_create", str(e))

    # LM2-11 : write reste autorisé à muter ses spaces, mais ne provisionne
    # plus de nouveaux spaces, même s'il connaît un identifiant libre.
    try:
        r = await ca.call_tool(
            "space_create",
            {
                "space_id": "iso-write-cannot-create",
                "description": "LM2-11 deny",
                "rules": ISO_RULES,
            },
        )
        if r.get("status") == "error" and "manage" in r.get("message", "").lower():
            test_pass("writer space_create REFUSÉ", "manage requis")
        else:
            test_fail("writer space_create DEVRAIT ÉCHOUER", str(r))
    except Exception as e:
        test_fail("writer space_create", str(e))

    pause("Read-only OK → Consolidation permissions")

    # ── CONSOLIDATION PERMISSIONS ─────────────────────
    section("Isolation 5/7 — Consolidation permissions (v0.7.4)")

    # Client A écrit une note dans son space
    try:
        r = await ca.call_tool(
            "live_note",
            {
                "space_id": ISO_SPACE_A,
                "category": "observation",
                "content": "Note test consolidation permissions",
            },
        )
        vprint(f"live_note: {r.get('status')}")
    except Exception:
        pass

    # write + agent omis → caller uniquement, PAS d'erreur de permission
    try:
        r = await ca.call_tool("bank_consolidate", {"space_id": ISO_SPACE_A})
        if r.get("status") in ("ok", "running", "queued"):
            test_pass(
                "write+agent omis → caller",
                f"status={r.get('status')}",
            )
        elif (
            r.get("status") == "error" and "permission" in r.get("message", "").lower()
        ):
            test_fail("write+agent omis REFUSÉ", r.get("message", ""))
        else:
            # Peut échouer pour d'autres raisons (pas de notes, timeout...) — pas un problème de permission
            test_pass(
                "write+agent omis → pas d'erreur permission",
                f"status={r.get('status')}",
            )
    except Exception as e:
        test_fail("write+agent omis", str(e))

    # write + agent="" explicite → REFUSÉ (scope global exige manage)
    try:
        r = await ca.call_tool(
            "bank_consolidate", {"space_id": ISO_SPACE_A, "agent": ""}
        )
        if r.get("status") == "error" and "manage" in r.get("message", "").lower():
            test_pass("write+agent='' REFUSÉ", "manage requis")
        else:
            test_fail(
                "write+agent='' DEVRAIT ÉCHOUER",
                f"status={r.get('status')}, msg={r.get('message', '')}",
            )
    except Exception as e:
        test_fail("write+agent=''", str(e))

    # write + agent=autre → REFUSÉ (manage requis)
    try:
        r = await ca.call_tool(
            "bank_consolidate",
            {
                "space_id": ISO_SPACE_A,
                "agent": "admin-setup",
            },
        )
        if r.get("status") == "error" and "manage" in r.get("message", "").lower():
            test_pass("write+agent=autre REFUSÉ", "manage requis")
        else:
            test_fail(
                "write+agent=autre DEVRAIT ÉCHOUER",
                f"status={r.get('status')}, msg={r.get('message', '')}",
            )
    except Exception as e:
        test_fail("write+agent=autre", str(e))

    # read-only ne peut pas consolider
    try:
        r = await ro.call_tool("bank_consolidate", {"space_id": ISO_SPACE_A})
        if r.get("status") == "error" and "write" in r.get("message", "").lower():
            test_pass("reader consolidate REFUSÉ", "write requis")
        else:
            test_fail("reader consolidate DEVRAIT ÉCHOUER", f"status={r.get('status')}")
    except Exception as e:
        test_fail("reader consolidate", str(e))

    pause("Consolidation OK → Auto-ajout")

    # ── AUTO-AJOUT MANAGER ────────────────────────────
    section("Isolation 6/7 — Manager crée et reçoit le nouveau space (LM2-11)")

    try:
        r = await manager.call_tool(
            "space_create",
            {
                "space_id": ISO_SPACE_C,
                "description": "Test auto-ajout",
                "rules": ISO_RULES,
            },
        )
        vprint(
            f"status={r.get('status')}, token_auto_updated={r.get('token_auto_updated')}"
        )
        if r.get("status") == "created":
            if r.get("token_auto_updated"):
                test_pass("auto-ajout space → token", r.get("token_message", "OK"))
            else:
                test_pass("space_create OK", "(token non restreint ou déjà ajouté)")
        else:
            test_fail("space_create C", r.get("message", ""))
    except Exception as e:
        test_fail("space_create C", str(e))

    try:
        r = await manager.call_tool("space_info", {"space_id": ISO_SPACE_C})
        if r.get("status") == "ok":
            test_pass("A → space-C accessible", "auto-ajout fonctionnel")
        else:
            test_fail("A → space-C DEVRAIT OK", r.get("message", ""))
    except Exception as e:
        test_fail("A → space-C", str(e))

    pause("Auto-ajout OK → Cleanup")

    # ── CLEANUP ───────────────────────────────────────
    if do_cleanup:
        section("Isolation 6/6 — Cleanup")
        for sid in [ISO_SPACE_A, ISO_SPACE_B, ISO_SPACE_C]:
            try:
                await admin.call_tool(
                    "space_delete", {"space_id": sid, "confirm": True}
                )
            except Exception:
                pass
        try:
            r = await admin.call_tool("backup_list", {})
            for b in r.get("backups", []):
                bid = b.get("backup_id", "")
                if bid.startswith("iso-test-"):
                    await admin.call_tool(
                        "backup_delete", {"backup_id": bid, "confirm": True}
                    )
        except Exception:
            pass
        try:
            r = await admin.call_tool("admin_list_tokens", {})
            for t in r.get("tokens", []):
                if t.get("name", "").startswith("iso-") and not t.get("revoked"):
                    await admin.call_tool(
                        "admin_revoke_token", {"token_hash": t["hash"]}
                    )
        except Exception:
            pass
        test_pass("cleanup isolation", "OK")


# ═══════════════════════════════════════════════════════════════
#
#  SUITE : QUALITE — Tests des principaux outils MCP
#
# ═══════════════════════════════════════════════════════════════

QUALITE_SPACE = "test-qualite"
QUALITE_TOKEN = "test-agent-qualite"


async def suite_qualite(admin: MCPClient, url: str, do_cleanup: bool):
    """Suite QUALITE : teste les principaux outils MCP."""
    header("🧪 SUITE : QUALITE — Outils MCP")

    # System
    section("Qualité — System")
    try:
        r = await admin.call_tool("system_health", {})
        if r.get("status") in ("healthy", "degraded"):
            test_pass(
                "system_health",
                f"S3={r.get('services', {}).get('s3', {}).get('status', '?')}",
            )
        else:
            test_fail("system_health", str(r))
    except Exception as e:
        test_fail("system_health", str(e))

    try:
        r = await admin.call_tool("system_about", {})
        if r.get("status") == "ok" and r.get("tools_count", 0) >= 25:
            test_pass(
                "system_about",
                f"{r.get('tools_count')} outils v{r.get('version', '?')}",
            )
        else:
            test_fail("system_about", str(r))
    except Exception as e:
        test_fail("system_about", str(e))

    try:
        r = await admin.call_tool("system_whoami", {})
        if r.get("status") == "ok" and r.get("client_name"):
            perms = ", ".join(r.get("permissions", []))
            test_pass(
                "system_whoami",
                f"identity={r['client_name']}, type={r.get('auth_type', '?')}, perms={perms}",
            )
        else:
            test_fail("system_whoami", str(r))
    except Exception as e:
        test_fail("system_whoami", str(e))

    # Admin tokens
    section("Qualité — Admin tokens")
    agent_token = ""
    try:
        r = await admin.call_tool(
            "admin_create_token",
            {
                "name": QUALITE_TOKEN,
                "permissions": "read,write",
                "space_ids": QUALITE_SPACE,
                "expires_in_days": 1,
            },
        )
        if r.get("status") == "created":
            agent_token = r["token"]
            test_pass("admin_create_token", QUALITE_TOKEN)
        else:
            test_fail("admin_create_token", str(r))
            return
    except Exception as e:
        test_fail("admin_create_token", str(e))
        return

    try:
        r = await admin.call_tool("admin_list_tokens", {})
        found = any(t.get("name") == QUALITE_TOKEN for t in r.get("tokens", []))
        if found:
            test_pass("admin_list_tokens", f"{QUALITE_TOKEN} trouvé")
        else:
            test_fail("admin_list_tokens", "token non trouvé")
    except Exception as e:
        test_fail("admin_list_tokens", str(e))

    agent = MCPClient(
        base_url=url, token=agent_token, timeout=600, call_delay=CALL_DELAY
    )

    # Space
    section("Qualité — Space")
    try:
        r = await admin.call_tool(
            "space_create",
            {
                "space_id": QUALITE_SPACE,
                "description": "Test qualité",
                "rules": "# Rules\n### context.md\nContexte test.",
            },
        )
        test_pass("space_create", QUALITE_SPACE) if r.get("status") in (
            "created",
            "already_exists",
        ) else test_fail("space_create", str(r))
    except Exception as e:
        test_fail("space_create", str(e))

    for tool, args in [
        ("space_list", {}),
        (
            "space_update",
            {"space_id": QUALITE_SPACE, "description": "Test qualité v0.7.7"},
        ),
        ("space_info", {"space_id": QUALITE_SPACE}),
        ("space_rules", {"space_id": QUALITE_SPACE}),
    ]:
        try:
            r = await agent.call_tool(tool, args)
            test_pass(tool, "OK") if r.get("status") == "ok" else test_fail(
                tool, str(r)
            )
        except Exception as e:
            test_fail(tool, str(e))

    # space_update_rules (admin only, v1.2.0)
    try:
        r = await admin.call_tool(
            "space_update_rules",
            {
                "space_id": QUALITE_SPACE,
                "rules": "# Test Rules v1.2.0\n\nRules de test pour space_update_rules.",
            },
        )
        test_pass("space_update_rules", f"OK ({r.get('rules_size', '?')} o)") if r.get(
            "status"
        ) == "ok" else test_fail("space_update_rules", str(r))
    except Exception as e:
        test_fail("space_update_rules", str(e))

    # Live
    section("Qualité — Live")
    try:
        r = await agent.call_tool(
            "live_note",
            {
                "space_id": QUALITE_SPACE,
                "category": "observation",
                "content": "Test qualité note",
            },
        )
        test_pass("live_note", "OK") if r.get("status") in (
            "ok",
            "created",
        ) else test_fail("live_note", str(r))
    except Exception as e:
        test_fail("live_note", str(e))

    for tool, args in [
        ("live_read", {"space_id": QUALITE_SPACE, "limit": 10}),
        ("live_search", {"space_id": QUALITE_SPACE, "query": "qualité"}),
    ]:
        try:
            r = await agent.call_tool(tool, args)
            test_pass(tool, "OK") if r.get("status") == "ok" else test_fail(
                tool, str(r)
            )
        except Exception as e:
            test_fail(tool, str(e))

    # Bank
    section("Qualité — Bank")
    consolidation_ready = False
    try:
        t0 = time.monotonic()
        outcome = await _consolidate_and_wait(agent, QUALITE_SPACE)
        dur = round(time.monotonic() - t0, 1)
        result = outcome.get("result")
        notes_processed = (
            result.get("notes_processed", 0) if isinstance(result, dict) else 0
        )
        if outcome.get("status") == "succeeded" and notes_processed > 0:
            consolidation_ready = True
            test_pass(
                "bank_consolidate",
                f"{notes_processed} notes ({dur}s, terminal=succeeded)",
            )
        elif outcome.get("status") == "succeeded":
            test_fail(
                "bank_consolidate", "terminal=succeeded mais aucune note traitée"
            )
        else:
            test_fail("bank_consolidate", _consolidation_failure_detail(outcome))
    except Exception as e:
        test_fail("bank_consolidate", str(e))

    if consolidation_ready:
        for tool, args in [
            ("bank_list", {"space_id": QUALITE_SPACE}),
            ("bank_read_all", {"space_id": QUALITE_SPACE}),
        ]:
            try:
                r = await agent.call_tool(tool, args)
                test_pass(tool, "OK") if r.get("status") == "ok" else test_fail(
                    tool, str(r)
                )
            except Exception as e:
                test_fail(tool, str(e))
    else:
        test_skip("bank_list", "consolidation non terminée avec succès")
        test_skip("bank_read_all", "consolidation non terminée avec succès")

    # Bank admin tools — tests sous-dossiers v0.9.0
    section("Qualité — Bank sous-dossiers (v0.9.0)")

    # 1. Écrire un fichier dans un sous-dossier
    try:
        r = await admin.call_tool(
            "bank_write",
            {
                "space_id": QUALITE_SPACE,
                "filename": "subdir/test_file.md",
                "content": "# Test\n\nFichier de test dans un sous-dossier.",
            },
        )
        if r.get("status") == "ok":
            test_pass(
                "bank_write (subdir)", f"subdir/test_file.md ({r.get('size', 0)} o)"
            )
        else:
            test_fail("bank_write (subdir)", str(r))
    except Exception as e:
        test_fail("bank_write (subdir)", str(e))

    # 2. Vérifier que bank_list retourne le chemin relatif complet (pas le basename)
    try:
        r = await agent.call_tool("bank_list", {"space_id": QUALITE_SPACE})
        filenames = [f.get("filename", "") for f in r.get("files", [])]
        if "subdir/test_file.md" in filenames:
            test_pass(
                "bank_list (relpath)", f"'subdir/test_file.md' trouvé dans {filenames}"
            )
        elif "test_file.md" in filenames:
            test_fail(
                "bank_list (relpath)",
                f"retourne basename au lieu du chemin relatif: {filenames}",
            )
        else:
            test_fail("bank_list (relpath)", f"fichier non trouvé dans: {filenames}")
    except Exception as e:
        test_fail("bank_list (relpath)", str(e))

    # 3. Vérifier que bank_read_all retourne le chemin relatif complet
    try:
        r = await agent.call_tool("bank_read_all", {"space_id": QUALITE_SPACE})
        filenames = [f.get("filename", "") for f in r.get("files", [])]
        if "subdir/test_file.md" in filenames:
            test_pass("bank_read_all (relpath)", "'subdir/test_file.md' trouvé")
        elif "test_file.md" in filenames:
            test_fail("bank_read_all (relpath)", f"retourne basename: {filenames}")
        else:
            test_fail(
                "bank_read_all (relpath)", f"fichier non trouvé dans: {filenames}"
            )
    except Exception as e:
        test_fail("bank_read_all (relpath)", str(e))

    # 4. Lire le fichier par chemin relatif
    try:
        r = await agent.call_tool(
            "bank_read",
            {
                "space_id": QUALITE_SPACE,
                "filename": "subdir/test_file.md",
            },
        )
        if r.get("status") == "ok" and "Test" in r.get("content", ""):
            test_pass("bank_read (subdir)", f"OK ({r.get('size', 0)} o)")
        else:
            test_fail("bank_read (subdir)", str(r))
    except Exception as e:
        test_fail("bank_read (subdir)", str(e))

    # 5. Supprimer par chemin relatif
    try:
        r = await admin.call_tool(
            "bank_delete",
            {
                "space_id": QUALITE_SPACE,
                "filename": "subdir/test_file.md",
                "confirm": True,
            },
        )
        if r.get("status") == "deleted":
            test_pass("bank_delete (subdir)", f"{r.get('files_deleted', 0)} supprimés")
        else:
            test_fail("bank_delete (subdir)", str(r))
    except Exception as e:
        test_fail("bank_delete (subdir)", str(e))

    # 6. bank_repair dry-run (doit être propre après cleanup)
    try:
        r = await admin.call_tool(
            "bank_repair",
            {
                "space_id": QUALITE_SPACE,
                "dry_run": True,
            },
        )
        if r.get("status") == "ok":
            test_pass(
                "bank_repair",
                f"scan: {r.get('files_ok', 0)} OK, {r.get('files_to_repair', 0)} à réparer, {r.get('duplicates_found', 0)} doublons",
            )
        else:
            test_fail("bank_repair", str(r))
    except Exception as e:
        test_fail("bank_repair", str(e))

    # Backup
    section("Qualité — Backup")
    backup_id = ""
    try:
        r = await admin.call_tool(
            "backup_create", {"space_id": QUALITE_SPACE, "description": "test"}
        )
        if r.get("status") in ("ok", "created"):
            backup_id = r.get("backup_id", "")
            test_pass("backup_create", backup_id)
        else:
            test_fail("backup_create", str(r))
    except Exception as e:
        test_fail("backup_create", str(e))

    try:
        r = await admin.call_tool("backup_list", {"space_id": QUALITE_SPACE})
        test_pass("backup_list", f"{len(r.get('backups', []))} backups") if r.get(
            "status"
        ) == "ok" else test_fail("backup_list", str(r))
    except Exception as e:
        test_fail("backup_list", str(e))

    if backup_id:
        try:
            r = await admin.call_tool(
                "backup_delete", {"backup_id": backup_id, "confirm": True}
            )
            test_pass("backup_delete", "OK") if r.get("status") in (
                "ok",
                "deleted",
            ) else test_fail("backup_delete", str(r))
        except Exception as e:
            test_fail("backup_delete", str(e))

    # GC
    section("Qualité — GC")
    try:
        r = await admin.call_tool(
            "admin_gc_notes",
            {
                "space_id": QUALITE_SPACE,
                "max_age_days": 30,
                "confirm": False,
            },
        )
        test_pass("admin_gc_notes", "dry-run OK") if r.get("status") in (
            "ok",
            "dry_run",
        ) else test_fail("admin_gc_notes", str(r))
    except Exception as e:
        test_fail("admin_gc_notes", str(e))

    # Cleanup
    if do_cleanup:
        section("Qualité — Cleanup")
        try:
            await admin.call_tool(
                "space_delete", {"space_id": QUALITE_SPACE, "confirm": True}
            )
            test_pass("cleanup qualité", "OK")
        except Exception:
            pass
        try:
            r = await admin.call_tool("admin_list_tokens", {})
            for t in r.get("tokens", []):
                if t.get("name") == QUALITE_TOKEN and not t.get("revoked"):
                    await admin.call_tool(
                        "admin_revoke_token", {"token_hash": t["hash"]}
                    )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#
#  SUITE : GRAPH — Test du pont Graph Memory (optionnel)
#
# ═══════════════════════════════════════════════════════════════

GRAPH_SPACE = "test-graph-bridge"
GRAPH_TOKEN_NAME = "agent-graph-test"
GRAPH_MEMORY_ID = "LIVE-MEM-TEST"

GRAPH_RULES = """# Rules — Test Graph Bridge
### context.md
Résumé du contexte.
### progress.md
Avancement.
"""

GRAPH_NOTES = [
    (
        "observation",
        "Architecture micro-services : API Gateway (Kong), Auth (Keycloak), Data (PostgreSQL).",
    ),
    ("decision", "HAProxy retenu comme load balancer L4/L7 pour les 3 backends."),
    ("progress", "Phase 1 terminée : 12 endpoints REST validés, JWT flow OK."),
]


async def _cleanup_graph_suite(admin: MCPClient) -> None:
    section("Graph 6/6 — Cleanup")
    try:
        await admin.call_tool(
            "space_delete", {"space_id": GRAPH_SPACE, "confirm": True}
        )
        test_pass("cleanup graph", "OK")
    except Exception:
        pass
    try:
        response = await admin.call_tool("admin_list_tokens", {})
        for token in response.get("tokens", []):
            if token.get("name") == GRAPH_TOKEN_NAME and not token.get("revoked"):
                await admin.call_tool(
                    "admin_revoke_token", {"token_hash": token["hash"]}
                )
    except Exception:
        pass


async def suite_graph(
    admin: MCPClient,
    url: str,
    do_cleanup: bool,
    graph_url: str = "",
    graph_token: str = "",
):
    """Suite GRAPH : test de l'override Hivemind → Graph Memory."""
    header("🌉 SUITE : GRAPH — Pont vers Graph Memory")

    if not graph_url or not graph_token:
        test_skip("graph (toute la suite)", "pas de --graph-url ou --graph-token")
        return

    agent_token = ""

    # Setup : token + space + notes + consolidation
    section("Graph 1/6 — Setup (token + space + notes + consolidation)")
    try:
        r = await admin.call_tool(
            "admin_create_token",
            {
                "name": GRAPH_TOKEN_NAME,
                "permissions": "read,write",
                "space_ids": GRAPH_SPACE,
                "expires_in_days": 1,
            },
        )
        if r.get("status") == "created":
            agent_token = r["token"]
            test_pass("token graph", GRAPH_TOKEN_NAME)
        else:
            test_fail("token graph", r.get("message", ""))
            return
    except Exception as e:
        test_fail("token graph", str(e))
        return

    agent = MCPClient(base_url=url, token=agent_token, timeout=600, call_delay=1.0)

    try:
        r = await admin.call_tool(
            "space_create",
            {
                "space_id": GRAPH_SPACE,
                "description": "Test Graph Bridge",
                "rules": GRAPH_RULES,
            },
        )
        test_pass("space graph", GRAPH_SPACE) if r.get("status") in (
            "created",
            "already_exists",
        ) else test_fail("space graph", str(r))
    except Exception as e:
        test_fail("space graph", str(e))
        return

    for cat, content in GRAPH_NOTES:
        try:
            await agent.call_tool(
                "live_note",
                {
                    "space_id": GRAPH_SPACE,
                    "category": cat,
                    "content": content,
                },
            )
        except Exception:
            pass

    consolidation_ready = False
    try:
        t0 = time.monotonic()
        outcome = await _consolidate_and_wait(agent, GRAPH_SPACE)
        dur = round(time.monotonic() - t0, 1)
        result = outcome.get("result")
        notes_processed = (
            result.get("notes_processed", 0) if isinstance(result, dict) else 0
        )
        if outcome.get("status") == "succeeded" and notes_processed > 0:
            consolidation_ready = True
            test_pass(
                "consolidate graph",
                f"{notes_processed} notes ({dur}s, terminal=succeeded)",
            )
        elif outcome.get("status") == "succeeded":
            test_fail(
                "consolidate graph", "terminal=succeeded mais aucune note traitée"
            )
        else:
            test_fail("consolidate graph", _consolidation_failure_detail(outcome))
    except Exception as e:
        test_fail("consolidate graph", str(e))

    if not consolidation_ready:
        test_skip(
            "graph connect/push",
            "consolidation non terminée avec succès; aucun push long exécuté",
        )
        if do_cleanup:
            await _cleanup_graph_suite(admin)
        return

    pause("Consolidation terminale OK → Connect")

    # Connect
    section("Graph 2/6 — graph_connect")
    try:
        r = await agent.call_tool(
            "graph_connect",
            {
                "space_id": GRAPH_SPACE,
                "url": graph_url,
                "token": graph_token,
                "memory_id": GRAPH_MEMORY_ID,
            },
        )
        if r.get("status") == "connected":
            test_pass("graph_connect", f"memory={GRAPH_MEMORY_ID}")
        else:
            test_fail("graph_connect", r.get("message", str(r)))
            return
    except Exception as e:
        test_fail("graph_connect", str(e))
        return

    # Push
    section("Graph 3/6 — graph_push")
    try:
        t0 = time.monotonic()
        r = await agent.call_tool("graph_push", {"space_id": GRAPH_SPACE})
        dur = round(time.monotonic() - t0, 1)
        if r.get("status") == "ok":
            test_pass("graph_push", f"{r.get('pushed', 0)} fichiers ({dur}s)")
        else:
            test_fail("graph_push", r.get("message", str(r)))
    except Exception as e:
        test_fail("graph_push", str(e))

    # Status
    section("Graph 4/6 — graph_status")
    try:
        r = await agent.call_tool("graph_status", {"space_id": GRAPH_SPACE})
        if r.get("status") == "ok" and r.get("reachable"):
            stats = r.get("graph_stats", {})
            test_pass(
                "graph_status",
                f"entities={stats.get('entity_count', '?')}, relations={stats.get('relation_count', '?')}",
            )
        else:
            test_fail("graph_status", r.get("message", str(r)))
    except Exception as e:
        test_fail("graph_status", str(e))

    # Disconnect
    section("Graph 5/6 — graph_disconnect")
    try:
        r = await agent.call_tool("graph_disconnect", {"space_id": GRAPH_SPACE})
        if r.get("status") in ("ok", "disconnected"):
            test_pass("graph_disconnect", "déconnecté")
        else:
            test_fail("graph_disconnect", r.get("message", str(r)))
    except Exception as e:
        test_fail("graph_disconnect", str(e))

    # Cleanup
    if do_cleanup:
        await _cleanup_graph_suite(admin)


# ═══════════════════════════════════════════════════════════════
# Registre des suites
# ═══════════════════════════════════════════════════════════════

SUITES = {
    "recette": (
        "🧪 Pipeline agent complet (notes → consolidation → bank)",
        suite_recette,
    ),
    "isolation": ("🔒 Allowlist inter-spaces (mono-tenant)", suite_isolation),
    "qualite": ("🧪 Tests de qualité des outils MCP", suite_qualite),
    "graph": (
        "🌉 Pont vers Graph Memory (nécessite --graph-url et --graph-token)",
        suite_graph,
    ),
}


# ═══════════════════════════════════════════════════════════════
# Orchestrateur
# ═══════════════════════════════════════════════════════════════


async def run_all(
    url: str,
    bootstrap_key: str,
    suites_to_run: list,
    do_cleanup: bool,
    graph_url: str = "",
    graph_token: str = "",
):
    admin = MCPClient(
        base_url=url, token=bootstrap_key, timeout=600, call_delay=CALL_DELAY
    )
    t0 = time.monotonic()

    header(f"🏗️  RECETTE GLOBALE — Hivemind {PRODUCT_VERSION}")
    print(f"  {C}Serveur :{Z} {url}")
    print(f"  {C}Suites  :{Z} {', '.join(suites_to_run)}")

    for name in suites_to_run:
        if name not in SUITES:
            print(f"\n  {R}❌ Suite inconnue : '{name}'{Z}")
            print(f"  Suites disponibles : {', '.join(SUITES.keys())}")
            continue
        desc, func = SUITES[name]
        if name == "graph":
            await func(
                admin, url, do_cleanup, graph_url=graph_url, graph_token=graph_token
            )
        else:
            await func(admin, url, do_cleanup)

    # Résumé final
    duration = round(time.monotonic() - t0, 1)
    total = passed + failed

    header("📊 RÉSUMÉ GLOBAL")
    print()
    for status, name in results:
        icon = {"PASS": f"{G}✅", "FAIL": f"{R}❌", "SKIP": f"{Y}⏭ "}[status]
        print(f"  {icon} {status:4s}{Z}  {name}")

    print(f"\n  {B}Total    :{Z} {total} tests")
    print(f"  {G}Passed   :{Z} {passed}")
    print(f"  {R}Failed   :{Z} {failed}")
    print(f"  {Y}Skipped  :{Z} {skipped}")
    print(f"  {C}Suites   :{Z} {', '.join(suites_to_run)}")
    print(f"  {C}Duration :{Z} {duration}s")

    if failed == 0:
        print(f"\n  {G}{B}🎉 RECETTE OK — {passed} PASS, 0 FAIL{Z}")
    else:
        print(f"\n  {R}{B}💥 RECETTE KO — {failed} test(s) en erreur{Z}")

    return failed


# ═══════════════════════════════════════════════════════════════
# Point d'entrée
# ═══════════════════════════════════════════════════════════════


def _read_key():
    p = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(p):
        for l in open(p):
            if l.strip().startswith("ADMIN_BOOTSTRAP_KEY="):
                return l.strip().split("=", 1)[1].strip()
    return ""


def main():
    global VERBOSE, STEP_MODE, PAUSE_SECONDS

    ap = argparse.ArgumentParser(
        description=f"Recette globale — Hivemind {PRODUCT_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  uv run python scripts/test_recette.py                          # Toutes les suites
  uv run python scripts/test_recette.py --suite isolation       # Juste isolation
  uv run python scripts/test_recette.py --suite recette,qualite # Plusieurs suites
  uv run python scripts/test_recette.py --list                  # Lister les suites
  uv run python scripts/test_recette.py --suite isolation -v --step
""",
    )
    ap.add_argument(
        "--url",
        default=os.environ.get("MCP_URL", "http://localhost:8080"),
        help="URL du serveur Hivemind (défaut: $MCP_URL ou localhost:8080)",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("ADMIN_BOOTSTRAP_KEY", ""),
        help="Bootstrap key admin (défaut: $ADMIN_BOOTSTRAP_KEY ou .env)",
    )
    ap.add_argument(
        "--suite",
        default="",
        help=f"Suites à exécuter, séparées par virgules (défaut: toutes). "
        f"Disponibles: {', '.join(SUITES.keys())}",
    )
    ap.add_argument(
        "--list", action="store_true", help="Lister les suites disponibles et quitter"
    )
    ap.add_argument(
        "--no-cleanup", action="store_true", help="Conserver les données de test"
    )
    ap.add_argument("--step", action="store_true", help="Mode pas-à-pas")
    ap.add_argument(
        "--pause",
        type=int,
        default=0,
        help="Pause N secondes entre étapes clés (permet d'observer sur /live)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Affichage détaillé")
    ap.add_argument(
        "--graph-url",
        default=os.environ.get("GRAPH_MEM_URL", ""),
        help="URL de Graph Memory (pour --suite graph)",
    )
    ap.add_argument(
        "--graph-token",
        default=os.environ.get("GRAPH_MEM_TOKEN", ""),
        help="Token Graph Memory (pour --suite graph)",
    )
    a = ap.parse_args()

    # --list : afficher les suites et quitter
    if a.list:
        print(f"\n{B}Suites disponibles :{Z}\n")
        for name, (desc, _) in SUITES.items():
            print(f"  {C}{name:12s}{Z}  {desc}")
        print(
            f"\n  Utilisation : uv run python scripts/test_recette.py --suite {','.join(SUITES.keys())}"
        )
        sys.exit(0)

    VERBOSE = a.verbose
    STEP_MODE = a.step
    PAUSE_SECONDS = a.pause

    if not a.token:
        a.token = _read_key()
    if not a.token:
        print("\033[91m❌ ADMIN_BOOTSTRAP_KEY requis (--token ou .env)\033[0m")
        sys.exit(1)

    # Déterminer les suites à exécuter
    if a.suite:
        suites = [s.strip() for s in a.suite.split(",") if s.strip()]
    else:
        suites = list(SUITES.keys())

    errors = asyncio.run(
        run_all(
            a.url,
            a.token,
            suites,
            not a.no_cleanup,
            graph_url=a.graph_url,
            graph_token=a.graph_token,
        )
    )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
