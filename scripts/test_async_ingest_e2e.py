#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Démonstrateur local des réponses MCP d'ingestion asynchrone.

Utiliser uniquement une stack Docker Compose jetable et un espace inutilisé.
Le script vérifie les résultats de soumission, suivi, listing, remplacement,
annulation et suppression ; il ne prouve pas l'atomicité entre datastores.
Une course d'annulation ou une réponse ambiguë échoue et conserve l'espace.
L'exécution peut déclencher de l'inférence facturée. Ce n'est pas un gate release.

Usage :
    python3 scripts/test_async_ingest_e2e.py
    python3 scripts/test_async_ingest_e2e.py --url http://localhost:8080 --space mon-espace
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ajouter le répertoire scripts pour importer MCPClient
sys.path.insert(0, os.path.dirname(__file__))
from cli.client import MCPClient


def print_banner(title: str) -> None:
    print("\n" + "=" * 74)
    print(f"  🚀 {title}")
    print("=" * 74)


def print_step(step_num: int, title: str) -> None:
    print(f"\n👉 [Étape {step_num}] {title}")


def print_success(msg: str) -> None:
    print(f"   ✅ {msg}")


def print_info(key: str, val: Any) -> None:
    print(f"   ℹ️  {key:25} : {val}")


def print_warning(msg: str) -> None:
    print(f"   ⚠️  {msg}")


def print_error(msg: str) -> None:
    print(f"   ❌ {msg}")


def load_env_token(root_dir: Path) -> str:
    """Charge le token admin depuis le fichier .env si présent."""
    env_file = root_dir / ".env"
    if not env_file.is_file():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ADMIN_BOOTSTRAP_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def check_docker_containers() -> tuple[bool, list[str]]:
    """Vérifie l'état des conteneurs Docker nécessaires."""
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        running = [name.strip().lower() for name in res.stdout.splitlines() if name.strip()]
    except Exception:
        return False, ["Docker n'est pas accessible"]

    missing = []
    # Vérifier WAF
    if not any("waf" in name for name in running):
        missing.append("waf (port 8080)")
    # Vérifier Hivemind
    if not any(name == "hivemind" or "hivemind-hivemind" in name or name.endswith("_hivemind_1") for name in running):
        missing.append("hivemind (serveur principal)")
    # Vérifier Graph Memory
    if not any("graph-memory" in name or "graph_memory" in name for name in running):
        missing.append("hivemind-graph-memory (moteur ontologique)")
    # Vérifier MinIO (obligatoire pour S3 local)
    if not any("minio" in name and "init" not in name for name in running):
        missing.append("minio (S3 local — profil dev requis)")
    # Vérifier Neo4j
    if not any("neo4j" in name for name in running):
        missing.append("neo4j (base graphe)")
    # Vérifier Qdrant
    if not any("qdrant" in name for name in running):
        missing.append("qdrant (base vectorielle)")

    return len(missing) == 0, missing


async def run_live_docker_demo(
    base_url: str,
    token: str,
    space_id: str,
    cleanup: bool = True,
    poll_interval: float = 1.0,
    max_wait_seconds: float = 90.0,
) -> bool:
    """Exercise MCP responses on one newly created, disposable local space.

    Success covers the observed API statuses only, not cross-store atomicity,
    graph contents, or orphan absence. Ambiguity prevents cleanup; a failed
    deletion has an uncertain outcome requiring operator inspection.
    """
    print_banner("DÉMONSTRATEUR MCP — INGESTION ASYNCHRONE")
    print_info("Espace de test", space_id)
    print_info("Token d'accès", "[masqué]")
    if (
        not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0
        or not math.isfinite(poll_interval) or poll_interval < 0
    ):
        print_error("Délais invalides : maximum positif et intervalle non négatif requis.")
        return False

    ok_docker, _ = check_docker_containers()
    if not ok_docker:
        print_error("Préflight Docker incomplet ; aucune mutation effectuée.")
        print_info("Configuration requise", "Docker Compose jetable avec profil dev et MinIO")
        return False

    client = MCPClient(base_url=base_url, token=token, timeout=max_wait_seconds)
    creation_attempted = False

    async def call(name: str, arguments: dict, *, timeout: float | None = None) -> dict:
        # Never print server responses or exception text: they may contain secrets.
        result = await asyncio.wait_for(
            client.call_tool(name, arguments),
            timeout=max_wait_seconds if timeout is None else timeout,
        )
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError(f"Invalid response for {name}")
        return result

    def require(result: dict, statuses: set[str]) -> None:
        if result.get("status") not in statuses:
            raise ValueError("Unexpected response status")

    async def submit(text: str, source_path: str, *, replace: bool = False) -> str:
        content = text.encode("utf-8")
        result = await call("long_ingest_async", {
            "space_id": space_id,
            "documents": [{
                "source_path": source_path,
                "filename": Path(source_path).name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }],
            "options": {"replace_existing": replace},
        })
        require(result, {"ok"})
        if result.get("errors") != [] or result.get("total") != 1 or not result.get("batch_id"):
            raise ValueError("Incomplete or failed batch submission")
        items = result.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Missing submission item")
        item = items[0]
        require(item, {"queued", "running", "succeeded"})
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip() or item.get("error"):
            raise ValueError("Missing job identity")
        return job_id

    async def terminal(job_id: str) -> dict:
        deadline = time.monotonic() + max_wait_seconds
        terminal_states = {"succeeded", "failed", "cancelled", "skipped", "changed_skipped"}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Job deadline exceeded")
            result = await call("long_ingest_status", {
                "space_id": space_id, "job_id": job_id,
            }, timeout=remaining)
            if result.get("job_id") != job_id:
                raise ValueError("Mismatched job identity")
            status = result.get("status")
            if status not in terminal_states | {"queued", "running"}:
                raise ValueError("Unknown job status")
            print_info("État du job", status)
            if status in terminal_states:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Job deadline exceeded")
            await asyncio.sleep(min(poll_interval, remaining))

    try:
        print_step(1, "Sonde S3 et identité MCP")
        health = await call("system_health", {})
        require(health, {"ok", "healthy"})
        services = health.get("services")
        if not isinstance(services, dict) or not isinstance(services.get("s3"), dict):
            raise ValueError("Missing S3 health")
        require(services["s3"], {"ok"})
        whoami = await call("system_whoami", {})
        require(whoami, {"ok"})
        permissions = whoami.get("permissions")
        if not isinstance(permissions, list) or not {"manage", "admin", "*"}.intersection(permissions):
            raise ValueError("Manage permission required")

        print_step(2, "Création exclusive d'un nouvel espace de test")
        # No pre-delete, no fallback to an existing space, even after a timeout.
        creation_attempted = True
        created = await call("space_create", {
            "space_id": space_id,
            "description": "Disposable async-ingestion demonstrator",
            "rules": "# Test memory bank\nKeep this disposable test space separate.",
        })
        require(created, {"created"})
        if created.get("space_id") != space_id:
            raise ValueError("Mismatched space identity")

        print_step(3, "Soumission et résultat terminal V1")
        path = "docs/demo-architecture.md"
        job_v1 = await submit(
            "# Example architecture V1\nThe sample service stores project documents.\n", path,
        )
        require(await terminal(job_v1), {"succeeded"})

        print_step(4, "Présence du job V1 dans le listing")
        listing = await call("long_ingest_list", {
            "space_id": space_id, "limit": 10, "offset": 0,
        })
        require(listing, {"ok"})
        jobs = listing.get("jobs")
        if not isinstance(jobs, list) or not any(
            isinstance(job, dict) and job.get("job_id") == job_v1 for job in jobs
        ):
            raise ValueError("V1 job missing from listing")

        print_step(5, "Remplacement V2 et résultat terminal")
        job_v2 = await submit(
            "# Example architecture V2\nThe sample service also indexes document tags.\n",
            path, replace=True,
        )
        if job_v2 == job_v1:
            raise ValueError("Replacement did not create a distinct job")
        require(await terminal(job_v2), {"succeeded"})

        print_step(6, "Demande d'annulation et vérification du résultat terminal")
        job_v3 = await submit("# Temporary draft\nDisposable cancellation sample.\n", "docs/demo-draft.md")
        if job_v3 in {job_v1, job_v2}:
            raise ValueError("Cancellation job is not distinct")
        cancelled = await call("long_ingest_cancel", {"space_id": space_id, "job_id": job_v3})
        require(cancelled, {"cancelling", "cancelled"})
        if cancelled.get("job_id") != job_v3:
            raise ValueError("Mismatched cancellation identity")
        # A completion race (noop/succeeded) is inconclusive, not a cancellation PASS.
        require(await terminal(job_v3), {"cancelled"})

        if cleanup:
            print_step(7, "Suppression confirmée après les contrôles réussis")
            deleted = await call("space_delete", {"space_id": space_id, "confirm": True})
            require(deleted, {"deleted"})
            if deleted.get("space_id") != space_id:
                raise ValueError("Mismatched deletion identity")
            print_success("Suppression confirmée par l'API.")
        else:
            print_info("Espace conservé", space_id)

    except Exception as exc:
        print_error(f"Échec ou résultat inconclusif ({type(exc).__name__}) ; aucun succès revendiqué.")
        if creation_attempted:
            print_warning(
                f"Espace '{space_id}' conservé ou état de suppression incertain. "
                "Action opérateur : inspecter les jobs et les données ; attendre "
                "l'arrêt des writers avant toute suppression explicite."
            )
        return False
    except asyncio.CancelledError:
        print_warning(
            f"Démonstrateur interrompu ; espace '{space_id}' potentiellement conservé. "
            "Inspecter les jobs avant toute action opérateur."
        )
        raise

    print_success("Contrôles des réponses MCP terminés ; pas une validation indépendante des datastores.")
    return True


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    default_token = os.environ.get("MCP_TOKEN") or os.environ.get("ADMIN_BOOTSTRAP_KEY") or load_env_token(repo_root)
    default_url = os.environ.get("MCP_URL", "http://localhost:8080")

    parser = argparse.ArgumentParser(description="Démonstrateur MCP local d'ingestion asynchrone, sans preuve d'atomicité")
    parser.add_argument("--url", default=default_url, help=f"URL de base Hivemind (défaut: {default_url})")
    parser.add_argument("--token", default=default_token, help="Token d'authentification admin (défaut: lu depuis .env ou env)")
    parser.add_argument("--space", default=f"test-e2e-async-{time.time_ns()}", help="Nouvel identifiant de space inutilisé")
    parser.add_argument("--no-cleanup", action="store_true", help="Conserver l'espace de test après l'exécution")
    parser.add_argument("--interval", type=float, default=1.0, help="Intervalle de polling en secondes")
    parser.add_argument("--max-wait", type=float, default=90.0, help="Délai maximal par appel et par suivi de job (secondes)")

    args = parser.parse_args()

    if not args.token:
        print_error("Aucun token d'authentification trouvé.")
        print_info("Solution", "Générez un .env avec 'python3 scripts/configure_dev_env.py' ou spécifiez --token VOTRE_TOKEN")
        sys.exit(1)

    success = asyncio.run(run_live_docker_demo(
        base_url=args.url,
        token=args.token,
        space_id=args.space,
        cleanup=not args.no_cleanup,
        poll_interval=args.interval,
        max_wait_seconds=args.max_wait,
    ))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
