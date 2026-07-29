# -*- coding: utf-8 -*-
"""
Service Consolidator — Pipeline LLM pour la consolidation notes → bank.

C'est le cœur intelligent de Live Memory. Le pipeline :
1. Collecte : rules + synthèse précédente + notes live + bank actuelle
2. Prompt : construit le prompt LLM (system + user)
3. Appel LLM : une seule requête au modèle configuré (LLMAAS_MODEL), réponse JSON
4. Application : éditions chirurgicales sur les fichiers bank existants
5. Écriture : bank files + synthesis + suppression notes + update meta

Principes :
    - Les agents n'écrivent JAMAIS dans la bank — seul le LLM le fait
    - Les notes sont supprimées UNIQUEMENT après succès complet (atomicité)
    - Un seul consolidate à la fois par espace (asyncio.Lock)
    - Le LLM produit des OPÉRATIONS D'ÉDITION (pas des réécritures complètes)
    - Ce qui n'est pas touché reste intact byte-for-byte (zéro perte)

Voir CONSOLIDATION_LLM.md pour les détails du pipeline et des prompts.
"""

import re
import json
import time
import logging
import inspect
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx
from openai import AsyncOpenAI

from ..config import display_proxy_url, get_settings
from .storage import get_storage, bank_relpath
from .reservation_guard import assert_space_not_reserved
from .live_note_format import split_live_note_front_matter

logger = logging.getLogger("live_mem.consolidator")


# LM2-18 fix : cooldown anti-spam pour bank_consolidate.
# Sans cela, un agent `write` peut déclencher la consolidation en boucle
# (consommation budget LLM, lock permanent du space). Le lock asyncio
# existant n'est qu'un mutex — il n'empêche pas un appel toutes les 100ms.
# Le store est in-memory (par-instance) : un déploiement HA multi-instances
# ne partage pas l'état, ce qui est acceptable car le budget LLM est commun
# au tenant Cloud Temple et la limite serait alors observée globalement
# via les quotas LLMaaS upstream.
_last_consolidation_started: dict[str, float] = {}


# LM2-13 fix : seuil de défense contre un `rewrite` malveillant qui
# tente d'effacer un fichier via prompt injection. Si le LLM produit
# un contenu < ce ratio de l'ancien, on refuse l'opération.
# 0.30 = un rewrite qui réduit de >70% est suspect (un compact légitime
# vise plutôt 50-60% de réduction). Surface bénigne acceptable car les
# rewrites légitimes du LLM ne réduisent que rarement de >70%.
_REWRITE_MIN_RATIO = 0.30
_REWRITE_MIN_ABSOLUTE_BYTES = 200  # n'évalue le ratio que si l'ancien fichier > 200B


def _parse_live_note_identity(filename: str) -> tuple[str, str]:
    """Return ``(agent, category)`` from the canonical right-hand fields.

    Agent identifiers may contain underscores, so positional ``parts[1]`` /
    ``parts[2]`` parsing confuses identity with category.  The UUID and category
    are the final two underscore-delimited fields; everything between the
    timestamp and those fields belongs to the agent.
    """
    stem = filename[:-3] if filename.endswith(".md") else filename
    parts = stem.split("_")
    if len(parts) < 4:
        return "unknown", "unknown"
    agent = "_".join(parts[1:-2]) or "unknown"
    return agent, parts[-2]


def _parse_live_note_agent(raw_content: object) -> str | None:
    """Return the exact agent identity from live-note front matter.

    Filenames contain a filesystem-safe projection of ``client_name`` and are
    therefore not an authorization boundary: distinct identities such as
    ``a.b`` and ``ab`` can project to the same filename segment.  Caller-scoped
    consolidation must compare the exact identity persisted in front matter.

    Missing, empty, malformed, or duplicate ``agent`` fields fail closed for a
    targeted consolidation.  The explicit global scope (``agent == ""``) still
    processes such notes so a manager can recover them deliberately.
    """
    parsed = split_live_note_front_matter(raw_content)
    if parsed is None:
        return None
    front_matter, _body = parsed

    identities: list[str] = []
    # Split only on the physical YAML newline. JSON-escaped identity content
    # (including U+2028 or an inline "---") must remain inside the value.
    for line in front_matter.split("\n"):
        key, separator, raw_value = line.partition(":")
        if not separator or key.strip() != "agent":
            continue
        raw_value = raw_value.strip()
        try:
            value = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            # Compatibility with early/simple front matter (``agent: alice``),
            # while refusing whitespace/quote-bearing ambiguous YAML forms.
            value = raw_value if re.fullmatch(r"[^\s\"']+", raw_value) else None
        if not isinstance(value, str) or value == "":
            return None
        identities.append(value)

    if len(identities) != 1:
        return None
    return identities[0]


# ─────────────────────────────────────────────────────────────
# Issue #17 — Post-consolidation validation pass (opt-in)
# ─────────────────────────────────────────────────────────────

# Explicit marker produced by the LLM to signal an inference (SYSTEM_PROMPT
# rule #8). Any line containing this token is considered explicitly
# attributed as an inference and is NOT counted as an unsourced claim.
# New consolidations use the English `[inferred]` marker. Continue recognizing
# the legacy French marker so validation remains compatible with existing banks.
_INFERRED_MARKER_RE = re.compile(
    r"\[(?:inferred|inféré)(?:[,\s][^\]]*)?\]", re.IGNORECASE
)

# Detection of "risky" claims: lines containing at least one verifiable
# fact (metric, date, strong status). We stay deliberately conservative
# to avoid too many false positives on purely structural content.

# Numeric metrics: "171/171 tests", "27 findings", "+737 lines",
# "60%", "1.9.0", "v2.0.0", "PR #14", "issue #17", ...
# Note: we use `(?=\W|$)` rather than `\b` at the end to correctly match
# units that end with a non-\w character (e.g. `%`) followed by a space
# or end-of-string — `\b` requires a \w↔non-\w boundary that does NOT
# exist between `%` and ` `.
_METRIC_RE = re.compile(

    r"\b\d+(?:[.,/]\d+)*\s*(?:%|tests?|notes?|findings?|lignes?|files?|"
    r"fichiers?|points?|tokens?|ms|s|h|jours?|days?|bytes?|kb|mb|gb|"
    r"commits?|PRs?|issues?)(?=\W|$)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b"
)
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_PR_REF_RE = re.compile(r"#\d+\b")

# Strong status keywords: a claimed state change should be sourced.
# Includes French inflected forms (feminine singular/plural) because
# Python's `\b` on an accented stem followed by a vowel does NOT match
# the inflected form: `\b` requires a \w↔non-\w boundary at word-end,
# and "fermée" = "fermé" + "e" puts \w on both sides.
_STATUS_KEYWORDS = (
    # résoudre / to resolve
    "résolu", "résolue", "résolus", "résolues",
    "resolu", "resolue", "resolus", "resolues",
    # merger / to merge
    "mergé", "mergée", "mergés", "mergées",
    "merge", "merged",
    # publier / to publish
    "publié", "publiée", "publiés", "publiées",
    "publie", "released",
    # déployer / to deploy
    "déployé", "déployée", "déployés", "déployées",
    "deploye", "deployed",
    # fermer / to close
    "fermé", "fermée", "fermés", "fermées",
    "ferme", "closed",
    # valider / to validate
    "validé", "validée", "validés", "validées",
    "valide", "validated",
    # test / build status
    "passed", "failed", "ko", "ok",
)

_STATUS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in _STATUS_KEYWORDS) + r")\b",
    re.IGNORECASE,
)



def _extract_claim_tokens(line: str) -> set[str]:
    """
    Extract "verifiable" tokens (significant numbers, dates, versions,
    PR/issue refs) from a bank line. These tokens form the minimal
    signature of a claim — if NONE appears in the notes, the claim is
    unsourced.

    Returns an empty set if the line contains no verifiable claim
    (e.g. structural line, sub-heading, empty bullet).
    """
    tokens: set[str] = set()
    for m in _METRIC_RE.findall(line):
        tokens.add(m.lower())
    for m in _DATE_RE.findall(line):
        tokens.add(m.lower())
    for m in _VERSION_RE.findall(line):
        tokens.add(m.lower())
    for m in _PR_REF_RE.findall(line):
        tokens.add(m.lower())
    return tokens


def _has_strong_status_claim(line: str) -> bool:
    """Tell whether the line carries a strong status word (resolved/merged/published/...).

    A line can be a claim without a numeric metric if it asserts an
    important state change.
    """
    return bool(_STATUS_RE.search(line))


def _normalize_for_match(text: str) -> str:
    """Minimal normalization for claim/notes comparison.

    Keep only `[a-z0-9/.-#%]` (digits, lowercase letters, slash, dot,
    dash, hash, percent). This lets us match `v2.0.0`, `27/05`,
    `171/171`, `#14`, `60%` regardless of the surrounding punctuation.
    """

    return re.sub(r"[^a-z0-9/.\-#%]", " ", text.lower())


def _validate_unattributed_claims(
    bank_files_before: dict[str, str],
    bank_files_after: dict[str, str],
    notes: list[dict],
    max_examples: int,
) -> dict:
    """
    Count the "claims" introduced by the consolidation that are neither
    sourced in the batch notes nor explicitly marked `[inferred]`.

    Code-only approach (deterministic, zero LLM tokens):
    1. Per-file diff: only ADDED LINES are inspected (present in
       `_after` but absent from `_before`).
    2. For each added line, extract verifiable tokens (metrics, dates,
       versions, refs).
    3. If the line carries a numeric claim OR a strong status:
       - If it contains `[inferred]` (or the legacy `[inféré]`) → traced
         but not counted.
       - Otherwise, check that each verifiable token appears in the
         normalized notes corpus. If NO token is found in the notes,
         the line is unsourced.

    Args:
        bank_files_before: filename → content before the batch
        bank_files_after: filename → content after the batch
        notes: list of batch notes (each note has a `content` field)
        max_examples: max number of examples returned (bounds the payload)

    Returns:
        {
          "unattributed_claims_count": int,
          "inferred_claims_count": int,
          "examples": [{"filename": str, "line": str, "tokens": [...]}],
          "lines_scanned": int,
          "lines_added": int,
        }
    """
    # Normalized notes corpus (single blob for the `in`-check).
    # Aggregates the contents of all batch notes.
    notes_corpus = _normalize_for_match(
        " ".join(n.get("content", "") for n in notes)
    )

    unattributed = 0
    inferred = 0
    examples: list[dict] = []
    lines_scanned = 0
    lines_added_total = 0

    for filename, after_content in bank_files_after.items():
        before_content = bank_files_before.get(filename, "")
        if before_content == after_content:
            continue

        before_lines = set(before_content.splitlines())
        for raw_line in after_content.splitlines():
            line = raw_line.strip()
            if not line or line in before_lines:
                continue

            lines_added_total += 1
            tokens = _extract_claim_tokens(line)
            has_status = _has_strong_status_claim(line)

            # Non-claim line (no metric, no strong status) → skip
            if not tokens and not has_status:
                continue

            lines_scanned += 1

            # Explicit inference marker → traced but not counted as unsourced
            # (the LLM explicitly flagged the inference).
            if _INFERRED_MARKER_RE.search(line):
                inferred += 1
                continue

            # If at least ONE verifiable token appears in the notes
            # → partially sourced claim, we accept it.
            sourced = any(tok in notes_corpus for tok in tokens) if tokens else False

            # Special case: strong status with no verifiable token
            # (e.g. "Bug resolved" without date or version). We require
            # the status root to appear literally in the notes.
            if not sourced and has_status and not tokens:
                m = _STATUS_RE.search(line)
                if m:
                    status_word = _normalize_for_match(m.group(0))
                    sourced = status_word in notes_corpus

            if not sourced:
                unattributed += 1
                if len(examples) < max_examples:
                    examples.append(
                        {
                            "filename": filename,
                            "line": line[:200],
                            "tokens": sorted(tokens)[:8],
                        }
                    )

    return {
        "unattributed_claims_count": unattributed,
        "inferred_claims_count": inferred,
        "examples": examples,
        "lines_scanned": lines_scanned,
        "lines_added": lines_added_total,
    }



# ─────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You maintain structured project Memory Banks.

Your mission is to integrate work notes into structured Markdown files through
SURGICAL EDITS.

## Input
1. The RULES that define the Memory Bank structure
2. The PREVIOUS SUMMARY (context from earlier consolidations)
3. New LIVE NOTES to integrate (including agent, category, and tag metadata)
4. The current BANK FILES

## Output
Return JSON containing EDIT OPERATIONS for each file, NOT complete file contents.

## Fundamental principle: EDIT, DO NOT REWRITE

Never return a complete file unless:
- It is a new file (action "create").
- It requires major restructuring (action "rewrite"; exceptional and requires
  justification).

For existing files, return operations targeting Markdown SECTIONS. Anything not
explicitly changed must remain intact.

## Available operations

1. **replace_section** — Replace a section's content. The content below the
   heading through the next heading at the same or a higher level is replaced.
2. **append_to_section** — Append content to the end of an existing section.
3. **prepend_to_section** — Add content at the start of an existing section,
   immediately after its heading.
4. **add_section** — Add a new heading and content at the end of the file, or
   after a specified section when "after" is supplied. Never use add_section
   for a heading that already exists; use replace_section instead. An
   add_section operation with an existing heading is automatically converted
   to replace_section.
5. **delete_section** — Delete an entire section, including its heading.

## CRITICAL ANTI-HALLUCINATION RULES

These rules are mandatory and take precedence over every other consideration:

1. **Strict source attribution**: Every factual statement written to the bank
   must be derivable from at least one note in the batch. If the notes do not
   provide information for a required section, leave it empty or write
   "To be defined — not specified in the available notes." Never invent
   content merely to complete a section.
2. **Preserve domain vocabulary**: When a note defines a project-specific
   concept, entity, or role, use the note's exact definition. Never reinterpret
   project terminology using general knowledge.
3. **Gate metrics and numbers**: Code-line counts, test counts, percentages,
   durations, scores, and other numbers may appear in the bank only when a note
   explicitly provides them. Never invent even approximate metrics. Put
   sourced metrics in the appropriate file and section.
4. **Do not invent structure**: Do not generate a file tree unless the notes
   describe it. A mentioned stack may be recorded, but its conventional file
   layout must not be invented.
5. **Isolate agents and tasks**: When notes come from multiple agents or
   independent tasks, never combine facts from different sources in one
   sentence or paragraph. Keep separate agent/task paragraphs and do not
   manufacture connections between independent notes.

## Inference and replacement rules

6. **Remove replaced material**: When a `decision` note explicitly replaces an
   earlier plan, scope, or sequence, remove the old scope from the backlog or
   roadmap. Do not retain it silently. When uncertain, mark it
   "DEPRECATED — verify".
7. **Transitive status inference**: If a `progress` note completes step N while
   the bank still shows step N-1 in progress, mark N-1 complete by inference.
   Likewise, if phase N+1 is in progress, phase N is complete.
8. **`[inferred]` traceability markers**: Any statement that is not literally
   present in a batch note and is produced through transitive inference or
   logical deduction must end with `[inferred]`, optionally followed by a
   short explanation inside the brackets. Examples:
     - "Phase 3 started on 12/03 [inferred, follows completion of Phase 2]"
     - "Migration complete [inferred]"
   Directly sourced statements must never carry the marker. This traceability
   lets operators distinguish source facts from deductions and supports
   post-consolidation validation.

## General rules

- Follow the structure in the supplied RULES exactly.
- Integrate the new live-note information.
- Prefer append_to_section and replace_section.
- For CURRENT CONTEXT files, replace the focus and append recent items.
  Actively clean them: move completed items to tracking/history, remove details
  from old sessions (more than two sessions ago), and retain only the current
  focus, recent work, next steps, and active decisions. Keep these files small.
- For HISTORY/PROGRESS files, append new entries and never delete history.
  Summarize entries older than 30 days as one line per milestone.
- Before adding a history section, check for a semantically equivalent
  milestone covering the same date and work, even if its heading or format
  differs. If one exists, enrich it with replace_section while retaining its
  heading instead of adding a duplicate. This is especially important after
  compaction has shortened existing sections.
- Infer each bank file's role from the supplied RULES, not its filename.
- Headings must exactly match those in the file, including `#` characters.
- Omit files that do not need changes.
- Keep the summary concise while covering the key points from processed notes.
- Every consolidation must remove obsolete material, not merely append. If a
  file exceeds its size limit and continues growing, compact older sections to
  make room."""


class ConsolidatorService:
    """
    Service de consolidation LLM : transforme les notes live en bank.

    Utilise AsyncOpenAI pour communiquer avec le LLMaaS Cloud Temple.
    Mode "édition chirurgicale" : le LLM produit des opérations d'édition
    par section Markdown, pas des réécritures complètes.
    """

    def __init__(self):
        settings = get_settings()

        # ── Proxy HTTP sortant (optionnel) ────────────────────
        # Utilise PROXY_URL (variable custom) plutôt que HTTP_PROXY/HTTPS_PROXY
        # pour éviter d'affecter toutes les libs Python qui lisent les vars d'env OS.
        # AsyncOpenAI utilise httpx en interne — on passe un client httpx pré-configuré.
        # Quand http_client est fourni, AsyncOpenAI n'en prend pas ownership :
        # c'est ConsolidatorService qui gère son cycle de vie (voir close()).
        proxy_url = settings.proxy_url
        self._http_client: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                proxy=httpx.Proxy(url=proxy_url),
                timeout=settings.consolidation_timeout,
            )
            if proxy_url
            else None
        )
        if self._http_client:
            # P12-3 (#268) : PROXY_URL est potentiellement porteuse de
            # credentials (http://user:pass@host:port) — ne logguer que
            # l'origine scheme://host:port, jamais la valeur brute.
            logger.info(
                "ConsolidatorService: LLM requests via proxy %s",
                display_proxy_url(proxy_url),
            )

        self._client = AsyncOpenAI(
            base_url=settings.llmaas_api_url,
            api_key=settings.llmaas_api_key,
            timeout=settings.consolidation_timeout,
            http_client=self._http_client,
        )
        self._model = settings.llmaas_model
        self._context_window = settings.llmaas_context_window
        self._max_tokens = settings.llmaas_max_tokens
        self._temperature = settings.llmaas_temperature
        self._max_notes = settings.consolidation_max_notes
        self._batch_size = settings.consolidation_batch_size
        # LM2-18 fix : cooldown anti-spam (voir _last_consolidation_started)
        self._cooldown_seconds = settings.consolidation_cooldown_seconds
        # Bank compaction settings
        self._compact_threshold = settings.compact_threshold
        self._bank_file_max_size = settings.bank_file_max_size
        # Issue #17 — Pass de validation post-consolidation (opt-in)
        self._validation_enabled = settings.consolidation_validation_enabled
        self._validation_max_examples = settings.consolidation_validation_max_examples


    async def consolidate(
        self,
        space_id: str,
        agent: str = "",
        enforce_cooldown: bool = True,
        progress_callback: Callable[[dict], Awaitable[None] | None] | None = None,
        note_keys: Iterable[str] | None = None,
    ) -> dict:
        """
        Pipeline complet de consolidation pour un espace, par lots.

        Les notes sont traitées par lots de `batch_size` (défaut 10) pour :
        - Garder les réponses JSON du LLM courtes (évite le drift Unicode)
        - Permettre une meilleure intégration incrémentale
        - Rendre le pipeline plus résilient (lots précédents déjà intégrés)

        Chaque lot relit la bank à jour depuis S3, ce qui permet au LLM
        de voir les modifications des lots précédents.

        IMPORTANT : Seules les notes de l'agent appelant sont consolidées.
        Les notes des autres agents restent dans live/ en attente.

        Args:
            space_id: Identifiant de l'espace à consolider
            agent: Nom de l'agent appelant (filtre les notes à consolider)
            enforce_cooldown: Si False, contourne le cooldown LM2-18.
                Utilisé par la file FIFO issue #20 pour éviter qu'un job
                légitime échoue juste après le job précédent.
            progress_callback: Callback best-effort appelé à chaque changement
                de progression batch pour alimenter l'observabilité async.
            note_keys: Allowlist optionnelle de clés live pleinement qualifiées.
                Quand elle est fournie, seules ces clés peuvent entrer dans le
                prompt et être supprimées. Utilisée par le GC pour ne jamais
                élargir un scan ancien aux notes fraîches du même agent.

        Returns:
            Métriques de consolidation avec un statut honnête (P12-1) :

            - ``status="ok"`` : chaque opération sélectionnée a réussi ;
            - ``status="error"`` : un lot a échoué AVANT que toute mutation
              durable ait pu commencer et zéro lot a été appliqué ;
            - ``status="partial"`` : du travail a été appliqué, une écriture
              durable a commencé ou a pu commencer, ou l'état durable est
              ambigu (inclut tout échec levé depuis ``_write_results``, même
              au premier lot, et toute compaction déjà appliquée).

            Champs additionnels : ``failed_batch`` (index 1-based, présent
            uniquement pour un échec de lot identifiable), ``failure_reason``
            (raison structurée stable), message client générique. La phase de
            progression terminale est ``done`` pour ``ok`` uniquement,
            ``failed`` pour ``error`` et ``partial``.
        """
        await assert_space_not_reserved(space_id)
        t0 = time.monotonic()
        storage = get_storage()
        agent_label = agent or "(all)"

        async def emit_progress(payload: dict) -> None:
            if progress_callback is None:
                return
            try:
                maybe_awaitable = progress_callback(payload)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as e:
                logger.warning("Consolidation progress callback failed — %s", e)

        # LM2-18 fix : cooldown anti-spam avant TOUTE collecte/appel LLM.
        # On enregistre le timestamp d'enregistrement EN PREMIER (avant
        # même la lecture S3) pour fail-fast en cas de spam. Si la conso
        # échoue ensuite, le compteur reste — c'est volontaire pour
        # éviter le retry intempestif suite à un échec transitoire.
        if enforce_cooldown and self._cooldown_seconds > 0:
            last_started = _last_consolidation_started.get(space_id)
            if last_started is not None:
                elapsed = time.monotonic() - last_started
                if elapsed < self._cooldown_seconds:
                    remaining = round(self._cooldown_seconds - elapsed, 1)
                    logger.warning(
                        "Consolidation throttled — space=%s, %.1fs remaining "
                        "(cooldown=%ds)",
                        space_id,
                        remaining,
                        self._cooldown_seconds,
                    )
                    return {
                        "status": "error",
                        "message": (
                            f"Consolidation cooldown is active for '{space_id}': "
                            f"retry in {remaining:.0f}s. The "
                            f"{self._cooldown_seconds}s cooldown protects the "
                            "LLM budget and prevents lock saturation."
                        ),
                    }
            _last_consolidation_started[space_id] = time.monotonic()

        logger.info("Consolidation start — space=%s agent=%s", space_id, agent_label)

        # ── Étape 1 : Collecter les inputs ────────────────
        inputs = await self._collect_inputs(
            space_id,
            agent=agent,
            note_keys=note_keys,
        )
        if inputs.get("status") in {"error", "conflict"}:
            return inputs

        all_notes = inputs["notes"]
        all_notes_keys = inputs["notes_keys"]

        # Pas de notes → rien à faire
        if not all_notes:
            await emit_progress(
                {
                    "phase": "done",
                    "batch_size": self._batch_size,
                    "notes_total": 0,
                    "notes_done": 0,
                    "batches_total": 0,
                    "batches_done": 0,
                    "current_batch": 0,
                }
            )
            return {
                "status": "ok",
                "notes_processed": 0,
                "message": "No new notes to consolidate",
            }

        # P12-1 : suivi d'issue honnête à trois états (ok/error/partial).
        # `failed_batch` n'est renseigné que pour un échec de LOT identifiable
        # (1-based). `durable_write_may_have_started` interdit le statut
        # `error` dès qu'une mutation durable a pu commencer : compaction
        # appliquée, ou entrée dans _write_results (même sur exception).
        runtime_failure_reason: str | None = None
        failed_batch: int | None = None
        durable_write_may_have_started = False
        compaction_failed = False

        # ── Étape 1b : Auto-compact de la bank si trop grosse ──
        try:
            compact_result = await self._compact_bank_if_needed(
                space_id, inputs["bank_files"], inputs["rules"]
            )
            if compact_result["compacted"]:
                # La compaction a réécrit des fichiers bank : une écriture
                # durable a déjà eu lieu avant le premier lot.
                durable_write_may_have_started = True
                # Relire la bank compactée depuis S3
                inputs["bank_files"] = await storage.list_and_get(
                    f"{space_id}/bank/"
                )
                logger.info(
                    "Bank auto-compacted — %d files, %d→%d bytes",
                    compact_result["files_compacted"],
                    compact_result["size_before"],
                    compact_result["size_after"],
                )
        except Exception:
            # Des écritures de compaction ont pu commencer : l'état durable
            # est ambigu → issue `partial` fail-closed, jamais `error`, et
            # aucun lot n'est tenté sur une bank potentiellement incohérente.
            compaction_failed = True
            runtime_failure_reason = "bank_compact_failed"
            durable_write_may_have_started = True
            logger.exception(
                "Bank auto-compaction failed — space=%s, no batch attempted",
                space_id,
            )

        # ── Étape 2 : Découper en lots ────────────────────
        batch_size = self._batch_size
        batches = []
        if not compaction_failed:
            for i in range(0, len(all_notes), batch_size):
                batch_notes = all_notes[i : i + batch_size]
                batch_keys = all_notes_keys[i : i + batch_size]
                batches.append((batch_notes, batch_keys))

        batch_count = len(batches)
        rules = inputs["rules"]

        # Métriques accumulées
        total_notes = 0
        total_created = 0
        total_updated = 0
        total_ops_applied = 0
        total_ops_failed = 0
        total_tokens = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_notes_deleted = 0
        total_notes_delete_failed = 0
        batches_completed = 0
        last_synthesis_size = 0
        metadata_update_failed = False
        # Issue #17 — post-pass validation, accumulated over all batches
        validation_unattributed = 0
        validation_inferred = 0
        validation_lines_scanned = 0
        validation_lines_added = 0
        validation_examples: list[dict] = []


        # Bank et synthèse courantes (relues entre les lots)
        current_bank = inputs["bank_files"]
        current_synthesis = inputs["synthesis"]
        total_bank = len(
            [
                bank_file
                for bank_file in current_bank
                if not bank_file.get("key", "").endswith(".keep")
            ]
        )

        if not compaction_failed:
            logger.info(
                "Consolidation plan — %d notes in %d batch(es) of %d",
                len(all_notes),
                batch_count,
                batch_size,
            )
            await emit_progress(
                {
                    "phase": "planned",
                    "batch_size": batch_size,
                    "notes_total": len(all_notes),
                    "notes_done": 0,
                    "batches_total": batch_count,
                    "batches_done": 0,
                    "current_batch": 0,
                }
            )

        # ── Étape 3 : Traiter chaque lot ──────────────────
        for batch_idx, (batch_notes, batch_keys) in enumerate(batches, 1):
            logger.info(
                "Batch %d/%d — %d notes",
                batch_idx,
                batch_count,
                len(batch_notes),
            )
            await emit_progress(
                {
                    "phase": "batch_running",
                    "batch_size": batch_size,
                    "notes_total": len(all_notes),
                    "notes_done": total_notes,
                    "batches_total": batch_count,
                    "batches_done": batches_completed,
                    "current_batch": batch_idx,
                    "current_batch_notes": len(batch_notes),
                }
            )

            # Relire la bank et la synthèse pour les lots suivants
            # (le lot précédent a pu modifier les fichiers bank)
            if batch_idx > 1:
                try:
                    current_bank = await storage.list_and_get(f"{space_id}/bank/")
                    current_synthesis = await storage.get(
                        f"{space_id}/_synthesis.md"
                    )
                except Exception:
                    runtime_failure_reason = "batch_refresh_failed"
                    failed_batch = batch_idx
                    logger.exception(
                        "Batch %d/%d refresh failed after %d completed batch(es)",
                        batch_idx,
                        batch_count,
                        batches_completed,
                    )
                    break

            # Issue #17 — Snapshot bank before the batch (for validation pass).
            # Captures filename → content so we can diff after the writes.
            # No extra S3 read: we reuse the already-loaded `current_bank`.
            bank_before_batch: dict[str, str] = {}
            if self._validation_enabled:
                for bf in current_bank:
                    raw_relpath = bank_relpath(bf["key"], space_id)
                    fname = _sanitize_filename(raw_relpath)
                    bank_before_batch[fname] = bf.get("content", "")

            # Construire le prompt pour ce lot
            try:
                messages = self._build_prompt(
                    space_id=space_id,
                    rules=rules,
                    synthesis=current_synthesis,
                    notes=batch_notes,
                    bank_files=current_bank,
                )
            except Exception:
                runtime_failure_reason = "batch_prompt_failed"
                failed_batch = batch_idx
                logger.exception(
                    "Batch %d/%d prompt construction failed", batch_idx, batch_count
                )
                break

            # Appeler le LLM
            try:
                llm_result = await self._call_llm(messages)
            except Exception:
                runtime_failure_reason = "batch_llm_failed"
                failed_batch = batch_idx
                logger.exception(
                    "Batch %d/%d LLM call raised unexpectedly", batch_idx, batch_count
                )
                break
            if llm_result.get("status") == "error":
                runtime_failure_reason = "batch_llm_failed"
                failed_batch = batch_idx
                logger.error(
                    "Batch %d/%d LLM failed: %s — stopping (previous batches OK)",
                    batch_idx,
                    batch_count,
                    llm_result.get("message"),
                )
                break

            # Appliquer les éditions (bank + synthesis + delete notes)
            # skip_meta=True : on mettra à jour le meta une seule fois à la fin
            # P12-1 : dès que _write_results est engagé, une écriture durable
            # a PU commencer — toute défaillance à partir d'ici est `partial`,
            # jamais `error`, même au premier lot.
            durable_write_may_have_started = True
            try:
                write_result = await self._write_results(
                    space_id=space_id,
                    llm_output=llm_result["data"],
                    bank_files=current_bank,
                    notes_keys=batch_keys,
                    notes_count=len(batch_notes),
                    usage=llm_result.get("usage", {}),
                    skip_meta=True,
                )
            except Exception:
                runtime_failure_reason = "batch_write_failed"
                failed_batch = batch_idx
                logger.exception(
                    "Batch %d/%d write failed unexpectedly", batch_idx, batch_count
                )
                break

            write_status = write_result.get("status")
            if write_status not in {"ok", "partial"}:
                runtime_failure_reason = "batch_write_failed"
                failed_batch = batch_idx
                logger.error(
                    "Batch %d/%d write failed: %s — stopping",
                    batch_idx,
                    batch_count,
                    write_result.get("message"),
                )
                break

            # P12-1 (revue Codex rondes 3+4) : classer le partial AVANT toute
            # comptabilité de complétion. Deux causes de partial dans
            # _write_results —
            # - operations_failed > 0 : l'intégration bank elle-même a échoué
            #   ou été refusée (les notes sources sont TOUTES retenues,
            #   never-drop). C'est un échec de LOT identifiable
            #   (batch_write_failed + failed_batch), jamais un
            #   note_delete_failed : ce token laisserait croire que la bank
            #   est à jour et que supprimer les notes retenues est sûr. Un tel
            #   lot n'est PAS complété : pas d'incrément batches_completed,
            #   pas d'émission batch_done — sinon le résultat final pourrait
            #   annoncer batches_completed == batches_total tout en portant
            #   failed_batch, une contradiction pour la récupération/UI.
            # - sinon : intégration complète, seule la suppression des notes
            #   sources a échoué → lot complété, classé note_delete_failed
            #   sans failed_batch par la chaîne d'agrégation finale.
            write_partial = write_status == "partial"
            write_integration_failed = (
                write_partial and write_result.get("operations_failed", 0) > 0
            )
            if write_integration_failed:
                runtime_failure_reason = "batch_write_failed"
                failed_batch = batch_idx
                logger.error(
                    "Batch %d/%d bank integration incomplete "
                    "(%d operation(s) failed) — sources retained",
                    batch_idx,
                    batch_count,
                    write_result.get("operations_failed", 0),
                )

            # Accumuler les métriques (toujours, même pour un lot refusé :
            # les compteurs reflètent les mutations réellement effectuées)
            total_notes += write_result.get("notes_processed", 0)
            total_created += write_result.get("bank_files_created", 0)
            total_updated += write_result.get("bank_files_updated", 0)
            total_ops_applied += write_result.get("operations_applied", 0)
            total_ops_failed += write_result.get("operations_failed", 0)
            total_tokens += write_result.get("llm_tokens_used", 0)
            total_prompt_tokens += write_result.get("llm_prompt_tokens", 0)
            total_completion_tokens += write_result.get("llm_completion_tokens", 0)
            total_notes_deleted += write_result.get("notes_deleted", 0)
            total_notes_delete_failed += write_result.get("notes_delete_failed", 0)
            last_synthesis_size = write_result.get("synthesis_size", 0)
            reported_total_bank = write_result.get("bank_files_total")
            if isinstance(reported_total_bank, int) and reported_total_bank >= 0:
                total_bank = reported_total_bank

            if not write_integration_failed:
                batches_completed += 1
                await emit_progress(
                    {
                        "phase": "batch_done",
                        "batch_size": batch_size,
                        "notes_total": len(all_notes),
                        "notes_done": total_notes,
                        "batches_total": batch_count,
                        "batches_done": batches_completed,
                        "current_batch": batch_idx,
                        "current_batch_notes": len(batch_notes),
                    }
                )

                logger.info(
                    "Batch %d/%d done — %d notes, %d created, %d updated, "
                    "%d tokens",
                    batch_idx,
                    batch_count,
                    len(batch_notes),
                    write_result.get("bank_files_created", 0),
                    write_result.get("bank_files_updated", 0),
                    write_result.get("llm_tokens_used", 0),
                )

            # Issue #17 — Post-batch validation pass (opt-in).
            # We re-read the current bank (state after _write_results) and
            # diff it against the snapshot taken before the batch. No LLM
            # call: deterministic, cheap, idempotent. The result is purely
            # informative (does NOT block the consolidation). Skipped for a
            # batch whose bank integration failed (P12-1 ronde 4) : le lot
            # n'est pas complété et le diff serait trompeur.
            if self._validation_enabled and not write_integration_failed:
                try:
                    bank_after_raw = await storage.list_and_get(
                        f"{space_id}/bank/"
                    )
                    bank_after_batch: dict[str, str] = {}
                    for bf in bank_after_raw:
                        raw_relpath = bank_relpath(bf["key"], space_id)
                        fname = _sanitize_filename(raw_relpath)
                        bank_after_batch[fname] = bf.get("content", "")

                    val = _validate_unattributed_claims(
                        bank_files_before=bank_before_batch,
                        bank_files_after=bank_after_batch,
                        notes=batch_notes,
                        max_examples=self._validation_max_examples,
                    )
                    validation_unattributed += val["unattributed_claims_count"]
                    validation_inferred += val["inferred_claims_count"]
                    validation_lines_scanned += val["lines_scanned"]
                    validation_lines_added += val["lines_added"]
                    # Keep only the first `_validation_max_examples` examples
                    # across all batches, to bound the response payload size.
                    remaining_slots = (
                        self._validation_max_examples - len(validation_examples)
                    )
                    if remaining_slots > 0:
                        validation_examples.extend(
                            val["examples"][:remaining_slots]
                        )
                    if val["unattributed_claims_count"] > 0:
                        logger.warning(
                            "Batch %d/%d validation — %d unsourced claim(s) "
                            "detected (over %d scanned lines, %d marked "
                            "[inferred] or legacy [inféré]). See `examples` "
                            "in the MCP response.",
                            batch_idx,
                            batch_count,
                            val["unattributed_claims_count"],
                            val["lines_scanned"],
                            val["inferred_claims_count"],
                        )
                except Exception as e:
                    # Validation is best-effort — it must NOT fail the
                    # consolidation itself if it errors out.
                    logger.error(
                        "Validation pass error (batch %d/%d) — %s",
                        batch_idx,
                        batch_count,
                        e,
                    )

            # Stop before later batches on any partial write and surface an
            # honest result; continuing would compound duplicate-reprocessing
            # risk. La classification (batch_write_failed vs note_delete_failed)
            # a déjà eu lieu AVANT la comptabilité de complétion ci-dessus.
            if write_partial:
                break

        # ── Étape 4 : Mettre à jour le meta (une seule fois) ─

        if total_notes > 0:
            try:
                now = datetime.now(timezone.utc).isoformat()
                meta = await storage.get_json(f"{space_id}/_meta.json") or {}
                meta["last_consolidation"] = now
                meta["consolidation_count"] = meta.get("consolidation_count", 0) + 1
                meta["total_notes_processed"] = (
                    meta.get("total_notes_processed", 0) + total_notes
                )
                await storage.put_json(f"{space_id}/_meta.json", meta)
            except Exception:
                # At least one batch may already have deleted its live notes.
                # Preserve the exact mutation metrics instead of raising and
                # making the caller report a false zero-work failure.
                metadata_update_failed = True
                logger.exception(
                    "Consolidation metadata update failed after %d processed note(s)",
                    total_notes,
                )

        duration = round(time.monotonic() - t0, 1)
        logger.info(
            "Consolidation done — space=%s agent=%s notes=%d batches=%d/%d "
            "created=%d updated=%d tokens=%d duration=%.1fs",
            space_id,
            agent_label,
            total_notes,
            batches_completed,
            batch_count,
            total_created,
            total_updated,
            total_tokens,
            duration,
        )

        # ``notes_remaining`` is the exact number of selected live notes still
        # durable after this run: the capped-out selection plus every loaded
        # source that was not actually deleted.  Deriving it from deletions
        # avoids double-counting a batch whose integration and cleanup failed.
        notes_remaining = (
            inputs.get("notes_remaining", 0)
            + max(0, len(all_notes) - total_notes_deleted)
        )
        # Hitting the configured max-notes cap is the historical, successful
        # behavior for ordinary queued consolidation (the remainder is exposed
        # through ``notes_remaining`` for a later job).  For an exact GC
        # allowlist, however, the caller requested one frozen set: truncating it
        # must be surfaced as partial rather than silently claiming completion.
        exact_selection_truncated = (
            note_keys is not None and inputs.get("notes_remaining", 0) > 0
        )
        # P12-1 : statut honnête à trois états.
        # `error` garantit qu'un lot a échoué AVANT que toute mutation durable
        # ait pu commencer et que zéro lot a été appliqué. Dès qu'un travail a
        # été appliqué, qu'une écriture durable a commencé ou a pu commencer,
        # ou que l'état durable est ambigu, l'issue est `partial`.
        is_error = (
            runtime_failure_reason is not None
            and batches_completed == 0
            and not durable_write_may_have_started
        )
        is_partial = not is_error and (
            batches_completed < batch_count
            or exact_selection_truncated
            or total_notes_delete_failed > 0
            or runtime_failure_reason is not None
            or metadata_update_failed
        )
        if is_error:
            status = "error"
        elif is_partial:
            status = "partial"
        else:
            status = "ok"
        # Raison structurée STABLE de la défaillance (priorité : échec de lot
        # identifiable, puis suppression de notes, troncature de sélection
        # exacte, métadonnées). Les causes non-lot ne fabriquent jamais de
        # `failed_batch`.
        failure_reason: str | None = None
        if status != "ok":
            if runtime_failure_reason is not None:
                failure_reason = runtime_failure_reason
            elif total_notes_delete_failed > 0:
                failure_reason = "note_delete_failed"
            elif exact_selection_truncated:
                failure_reason = "exact_selection_truncated"
            elif metadata_update_failed:
                failure_reason = "metadata_update_failed"
        result = {
            "status": status,
            "space_id": space_id,
            "notes_processed": total_notes,
            "notes_deleted": total_notes_deleted,
            "notes_delete_failed": total_notes_delete_failed,
            "notes_remaining": notes_remaining,
            "bank_files_updated": total_updated,
            "bank_files_created": total_created,
            "bank_files_unchanged": max(0, total_bank - total_created - total_updated),
            "operations_applied": total_ops_applied,
            "operations_failed": total_ops_failed,
            "synthesis_size": last_synthesis_size,
            "llm_tokens_used": total_tokens,
            "llm_prompt_tokens": total_prompt_tokens,
            "llm_completion_tokens": total_completion_tokens,
            "batches_total": batch_count,
            "batches_completed": batches_completed,
            "batch_size": batch_size,
            "duration_seconds": duration,
        }
        if failure_reason is not None:
            result["failure_reason"] = failure_reason
        if failed_batch is not None:
            result["failed_batch"] = failed_batch
        if metadata_update_failed:
            result["metadata_update_failed"] = True
        if status == "error":
            # Message client générique : le détail provider/exception reste
            # dans les journaux serveur (LM2-24).
            result["reason"] = "consolidation_failed"
            result["message"] = (
                "Consolidation failed before any durable write: no bank file, "
                "note, or metadata was changed. The notes remain eligible for "
                "a retry; consult server logs for details."
            )
        elif status == "partial":
            result["reason"] = "partial_consolidation"
            if notes_remaining > 0:
                result["message"] = (
                    "Partial consolidation: some notes were not integrated or "
                    "deleted. They remain eligible for a controlled retry."
                )
            elif metadata_update_failed:
                result["message"] = (
                    "The notes were integrated and deleted, but consolidation "
                    "metadata could not be updated. No source notes remain to retry."
                )
            else:
                result["message"] = (
                    "Consolidation completed with a partial outcome; inspect "
                    "the counters and failure reason."
                )
        # P12-1 : la phase terminale de progression est honnête — `done`
        # UNIQUEMENT pour un succès complet, `failed` pour `error`/`partial`.
        await emit_progress(
            {
                "phase": "done" if status == "ok" else "failed",
                "batch_size": batch_size,
                "notes_total": len(all_notes),
                "notes_done": total_notes,
                "batches_total": batch_count,
                "batches_done": batches_completed,
                "current_batch": batches_completed,
            }
        )

        # Issue #17 — Validation metrics (opt-in)
        if self._validation_enabled:
            result["validation"] = {
                "enabled": True,
                "unattributed_claims_count": validation_unattributed,
                "inferred_claims_count": validation_inferred,
                "lines_added": validation_lines_added,
                "lines_scanned": validation_lines_scanned,
                "examples": validation_examples,
            }

        return result

    async def _collect_inputs(
        self,
        space_id: str,
        agent: str = "",
        note_keys: Iterable[str] | None = None,
    ) -> dict:

        """
        Étape 1 : Lire les rules, synthèse, notes de l'agent et bank depuis S3.

        Si agent est fourni, seules les notes de cet agent sont collectées.
        Les notes des autres agents restent dans live/.

        Returns:
            Dict avec rules, synthesis, notes, notes_keys, bank_files
        """
        storage = get_storage()

        # Vérifier l'existence de l'espace
        meta = await storage.get_json(f"{space_id}/_meta.json")
        if meta is None:
            return {"status": "error", "message": f"Space '{space_id}' not found"}

        # Lire les rules (immuables)
        rules = await storage.get(f"{space_id}/_rules.md") or ""

        # Lire la synthèse précédente (peut ne pas exister)
        synthesis = await storage.get(f"{space_id}/_synthesis.md")

        # Lire les notes live
        notes_raw = await storage.list_and_get(f"{space_id}/live/")

        # P5-7 fix : exclure les sidecars de provenance live/_origin/{note_id}.json.
        # ``list_and_get(.../live/)`` ramène TOUT le sous-arbre, sidecars inclus —
        # sans ce skip, un sidecar serait traité comme une note (prompt LLM +
        # ajouté à notes_keys -> SUPPRIMÉ en fin de conso, perte de provenance).
        # On miroite read_notes/search_notes : le skip n'est légitime QUE sur un
        # space Hivemind CONFIRMÉ (fail-closed : la corruption critique propage
        # CorruptedStateError). Sur un space NON-Hivemind, live/_origin/ n'est pas
        # un sidecar P5-7 mais un objet legacy ordinaire — ne pas le sauter
        # préserve le comportement byte-for-byte d'avant P5-7 (no-op : un space
        # non-Hivemind n'a aucun sidecar _origin/).
        from .hivemind.layout import origin_prefix
        from .hivemind.lifecycle import is_hivemind_space

        if await is_hivemind_space(storage, space_id):
            _origin = origin_prefix(space_id)
            notes_raw = [n for n in notes_raw if not n["key"].startswith(_origin)]

        # Exact-key allowlist (GC): filter BEFORE the agent predicate and
        # max-notes cap.  The default ``None`` preserves every historical
        # consolidation caller byte-for-byte; an explicit empty set selects
        # nothing (fail closed, never interpreted as "all").
        if note_keys is not None:
            requested_keys = list(note_keys)
            live_prefix = f"{space_id}/live/"
            invalid_keys = [
                key
                for key in requested_keys
                if not isinstance(key, str)
                or not key.startswith(live_prefix)
                or "/" in key[len(live_prefix) :]
                or not key.endswith(".md")
                or key.endswith(".keep")
            ]
            if invalid_keys:
                return {
                    "status": "error",
                    "reason": "invalid_selected_note_key",
                    "message": "The GC selection contains an invalid live-note key.",
                }
            # Stable de-duplication preserves the caller's exact processing
            # order.  GC deliberately places its synthetic notice first so a
            # configured max-notes cap can never strand that notice while
            # processing only older sources.
            selected_order = list(dict.fromkeys(requested_keys))
            selected_keys = set(selected_order)
            present_keys = {n["key"] for n in notes_raw}
            if not selected_keys.issubset(present_keys):
                return {
                    "status": "conflict",
                    "reason": "selected_note_set_changed",
                    "message": (
                        "The exact selected-note set changed before consolidation. "
                        "Run the GC scan again."
                    ),
                }
            notes_by_key = {n["key"]: n for n in notes_raw}
            notes_raw = [notes_by_key[key] for key in selected_order]

        # Historical callers remain chronological.  An exact GC selection
        # keeps the explicit order above (notice first, then frozen old keys).
        if note_keys is None:
            notes_raw.sort(key=lambda n: n["key"])

        # Filtrer par l'identité exacte du front-matter : le segment agent du
        # filename est une projection normalisée et peut collisionner (a.b/ab).
        # Une note sans identité exacte exploitable est ignorée en scope ciblé
        # et reste récupérable uniquement via le scope global manage explicite.
        if agent and note_keys is None:
            notes_raw = [
                n
                for n in notes_raw
                if _parse_live_note_agent(n.get("content")) == agent
            ]

        # Limiter au max_notes (les plus anciennes d'abord)
        notes_remaining = 0
        if len(notes_raw) > self._max_notes:
            notes_remaining = len(notes_raw) - self._max_notes
            notes_raw = notes_raw[: self._max_notes]

        # Garder les clés pour la suppression ultérieure
        notes_keys = [n["key"] for n in notes_raw]

        # Lire les fichiers bank actuels
        bank_raw = await storage.list_and_get(f"{space_id}/bank/")

        return {
            "rules": rules,
            "synthesis": synthesis,
            "notes": notes_raw,
            "notes_keys": notes_keys,
            "notes_remaining": notes_remaining,
            "bank_files": bank_raw,
            "meta": meta,
        }

    def _build_prompt(
        self,
        space_id: str,
        rules: str,
        synthesis: Optional[str],
        notes: list[dict],
        bank_files: list[dict],
    ) -> list[dict]:
        """
        Étape 2 : Construire les messages pour l'appel LLM.

        Le prompt demande des OPÉRATIONS D'ÉDITION, pas des réécritures.

        Returns:
            Liste de messages [{"role": "system", ...}, {"role": "user", ...}]
        """
        # Construire la section notes avec métadonnées (agent, catégorie, tags)
        # Issue #17 : les métadonnées permettent au LLM d'isoler les notes
        # par agent/tâche et de mieux respecter les catégories sémantiques.
        notes_section = ""
        for i, note in enumerate(notes, 1):
            content = note["content"]
            # Extraire les métadonnées du nom de fichier S3
            # Format: {ts}_{agent}_{category}_{uuid}.md
            note_key = note.get("key", "")
            note_filename = note_key.split("/")[-1] if note_key else ""
            agent_name, category = _parse_live_note_identity(note_filename)
            # Les tags ne sont pas dans le filename, mais dans le contenu YAML front-matter
            # On les extrait si présents au début du contenu
            tags = ""
            content_clean = content
            exact_agent_name = _parse_live_note_agent(content)
            parsed_front_matter = split_live_note_front_matter(content)
            if parsed_front_matter is not None:
                front_matter, content_clean = parsed_front_matter
                for line in front_matter.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("agent:"):
                        if exact_agent_name is not None:
                            agent_name = exact_agent_name
                    elif stripped.startswith("category:"):
                        category = stripped.split(":", 1)[1].strip().strip('"')
                    elif stripped.startswith("tags:"):
                        tags = stripped.split(":", 1)[1].strip()

            notes_section += (
                f"\n--- Note {i}/{len(notes)} "
                f"[agent={agent_name}, category={category}"
                f"{', tags=' + tags if tags else ''}] ---\n"
                f"{content_clean}\n"
            )

        # Construire la section bank (fichiers existants avec leur contenu)
        # On sanitise les filenames pour que le LLM voie des noms propres
        # (pas contaminés par des caractères Unicode invisibles).
        if bank_files:
            bank_section = ""
            for bf in bank_files:
                # Extraire le chemin relatif complet (supporte les sous-dossiers)
                raw_relpath = bank_relpath(bf["key"], space_id)
                filename = _sanitize_filename(raw_relpath)
                bank_section += (
                    f"\n--- File: {filename} ---\n"
                    f"{bf['content']}\n"
                    f"--- End file: {filename} ---\n"
                )
        else:
            bank_section = (
                "No bank files exist; this is the first consolidation. "
                "Use the 'create' action to create files according to the rules."
            )

        # Construire le prompt utilisateur
        user_prompt = f"""=== RULES FOR SPACE "{space_id}" ===
{rules}

=== PREVIOUS SUMMARY ===
{synthesis or "None; this is the first consolidation"}

=== LIVE NOTES TO INTEGRATE ({len(notes)} notes) ===
{notes_section}

=== CURRENT BANK FILES ===
{bank_section}

=== RESPONSE FORMAT ===
Return JSON with this exact structure:
{{
  "file_edits": [
    {{
      "filename": "activeContext.md",
      "action": "edit",
      "operations": [
        {{
          "type": "replace_section",
          "heading": "## Current Focus",
          "content": "New section content..."
        }},
        {{
          "type": "append_to_section",
          "heading": "## Recent Work",
          "content": "- Newly added item\\n- Another item"
        }},
        {{
          "type": "add_section",
          "heading": "## New Section",
          "content": "Content of the new section",
          "after": "## Existing Section"
        }},
        {{
          "type": "delete_section",
          "heading": "## Obsolete Section"
        }}
      ]
    }},
    {{
      "filename": "new_file.md",
      "action": "create",
      "content": "# Title\\n\\nComplete content of the new file..."
    }},
    {{
      "filename": "restructured_file.md",
      "action": "rewrite",
      "content": "# Title\\n\\nComplete rewritten content...",
      "reason": "Major restructuring is necessary because..."
    }}
  ],
  "synthesis": "Concise summary of the processed notes..."
}}

=== IMPORTANT INSTRUCTIONS ===
1. For EXISTING files, use action "edit" with surgical operations.
2. For NEW files, use action "create" with the complete content.
3. Action "rewrite" means a COMPLETE rewrite; use it only for major restructuring.
4. Do not include unchanged files in file_edits.
5. Operation headings must exactly match the file's headings.
6. Prefer append_to_section to add information without losing anything.
7. Prefer replace_section to update a section whose content changed.
8. For history/progress files, always append and never delete history.
9. The residual summary must summarize the processed notes."""

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    async def _call_llm(self, messages: list[dict]) -> dict:
        """
        Étape 3 : Appeler le LLM et parser la réponse JSON.

        Calcule dynamiquement max_tokens en sortie pour éviter de dépasser
        le context window du modèle (input + output ≤ context_window).

        Heuristique : 1 token ≈ 4 caractères. On réserve au minimum
        8192 tokens pour la sortie (éditions chirurgicales JSON).

        Inclut un retry si la réponse n'est pas du JSON valide.

        Returns:
            {"status": "ok", "data": {...}, "usage": {...}} ou erreur
        """
        # ── Calcul dynamique du budget de sortie ──────────────
        # Budget de sortie :
        # - Ne doit pas dépasser max_tokens (config : max output demandé à l'API)
        # - Ne doit pas dépasser context_window - input (sinon le modèle rejette)
        # P12-1 (revue Codex PR #256) : l'ancien plancher forçait 8192 tokens
        # AU-DESSUS des deux limites — une config valide au démarrage
        # (ex. MAX_TOKENS=1024 < CONTEXT_WINDOW=4096) était alors rejetée par
        # le provider au runtime. La requête ne dépasse plus jamais ni le cap
        # configuré ni la fenêtre restante ; le plancher ne sert plus que de
        # seuil de diagnostic. Fenêtre épuisée → erreur structurée pré-écriture
        # (le pipeline la classe batch_llm_failed sans mutation durable).
        # Revue ronde 2 : le budget est recalculé sur les messages COURANTS
        # avant CHAQUE appel provider — les chemins de retry (JSON invalide,
        # structure invalide) apprennent la réponse brute + une correction au
        # prompt, et un budget figé pouvait dépasser la fenêtre exactement
        # quand le retry était nécessaire.
        _MIN_OUTPUT_TOKENS = 8192

        def _compute_output_budget() -> int | None:
            # Estimer les tokens d'input (heuristique 1 token ≈ 4 chars)
            input_chars = sum(len(m.get("content", "")) for m in messages)
            estimated_input_tokens = input_chars // 4
            remaining_in_window = self._context_window - estimated_input_tokens
            output_budget = min(self._max_tokens, remaining_in_window)

            if output_budget <= 0:
                logger.error(
                    "LLM call refused — estimated input (~%d tokens) exhausts "
                    "the context window (context_window=%d, max_tokens=%d): no "
                    "positive output budget remains. Reduce the bank size or "
                    "raise LLMAAS_CONTEXT_WINDOW.",
                    estimated_input_tokens,
                    self._context_window,
                    self._max_tokens,
                )
                return None

            if output_budget < _MIN_OUTPUT_TOKENS:
                logger.warning(
                    "LLM output budget très réduit : %d tokens "
                    "(< %d recommandés pour du JSON chirurgical ; "
                    "context_window=%d, max_tokens=%d, input ~%d tokens).",
                    output_budget,
                    _MIN_OUTPUT_TOKENS,
                    self._context_window,
                    self._max_tokens,
                    estimated_input_tokens,
                )

            if estimated_input_tokens > self._context_window * 0.8:
                logger.warning(
                    "LLM input très large : ~%d tokens estimés "
                    "(context_window=%d, max_tokens=%d). "
                    "Budget sortie réduit à %d tokens. "
                    "Considérez réduire la taille de la bank.",
                    estimated_input_tokens,
                    self._context_window,
                    self._max_tokens,
                    output_budget,
                )

            logger.info(
                "LLM call — input ~%d tokens, context_window=%d, "
                "output budget %d tokens (max_tokens=%d)",
                estimated_input_tokens,
                self._context_window,
                output_budget,
                self._max_tokens,
            )
            return output_budget

        _WINDOW_EXHAUSTED_ERROR = {
            "status": "error",
            "message": (
                "Le contexte estimé épuise la fenêtre du modèle : aucun "
                "budget de sortie positif. Réduisez la taille de la bank "
                "ou augmentez LLMAAS_CONTEXT_WINDOW."
            ),
        }

        for attempt in range(2):  # 1 essai + 1 retry
            output_budget = _compute_output_budget()
            if output_budget is None:
                return dict(_WINDOW_EXHAUSTED_ERROR)
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    max_tokens=output_budget,
                    temperature=self._temperature,
                )

                raw_content = response.choices[0].message.content or ""
                finish_reason = response.choices[0].finish_reason
                completion_tokens = (
                    response.usage.completion_tokens if response.usage else None
                )

                # Extraire le JSON de la réponse (peut être enveloppé dans ```json)
                json_str = _extract_json(raw_content)

                # Parser le JSON
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError as exc:
                    # Log la réponse brute (tronquée) pour diagnostic
                    raw_preview = raw_content[:500] if raw_content else "(empty)"
                    visible_tokens_est = len(raw_content) // 4
                    logger.warning(
                        "LLM: JSON invalide (attempt %d/%d) — "
                        "json_error=%s, finish_reason=%s, "
                        "completion_tokens=%s, visible_tokens_est=%d, "
                        "raw_len=%d, raw_preview=%s",
                        attempt + 1,
                        2,
                        str(exc)[:100],
                        finish_reason,
                        completion_tokens,
                        visible_tokens_est,
                        len(raw_content),
                        raw_preview,
                    )

                    # ── Tentative de réparation automatique ──
                    # Avant le retry coûteux (2ème appel LLM complet),
                    # essayer de réparer le JSON tronqué/malformé.
                    # Gère le cas "Unterminated string" (le plus fréquent
                    # avec qwen3.x : chaîne non fermée, finish_reason=stop).
                    repaired_data = _repair_json(json_str, exc)
                    repaired_files = (
                        len(repaired_data.get("file_edits", []))
                        if repaired_data
                        else 0
                    )
                    if repaired_data is not None and repaired_files > 0:
                        # Repair réussie avec du contenu utile
                        repaired_ops = sum(
                            len(fe.get("operations", []))
                            for fe in repaired_data.get("file_edits", [])
                            if fe.get("action") == "edit"
                        )
                        logger.warning(
                            "LLM: JSON réparé automatiquement — "
                            "%d file_edits, %d operations récupérées "
                            "(dernière opération tronquée supprimée)",
                            repaired_files,
                            repaired_ops,
                        )
                        data = repaired_data
                        # Fall through vers la validation ci-dessous
                    elif attempt == 0:
                        # Repair échouée OU repair vide (0 file_edits) → retry
                        if repaired_data is not None and repaired_files == 0:
                            logger.warning(
                                "LLM: JSON réparé mais 0 file_edits "
                                "récupérés — retry au lieu d'accepter"
                            )
                        # Retry avec un rappel plus explicite
                        messages.append({"role": "assistant", "content": raw_content})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your response is not valid JSON. Return ONLY "
                                    "a valid JSON object with file_edits and synthesis."
                                ),
                            }
                        )
                        continue
                    else:
                        return {
                            "status": "error",
                            "message": "LLM returned invalid JSON after retry",
                            "raw_preview": raw_preview,
                        }

                # Valider la structure minimale
                if "file_edits" not in data or "synthesis" not in data:
                    # Rétrocompat : accepter aussi l'ancien format "bank_files"
                    if "bank_files" in data and "synthesis" in data:
                        data = _convert_legacy_format(data)
                    elif attempt == 0:
                        logger.warning(
                            "LLM: structure invalide (attempt %d), retry...",
                            attempt + 1,
                        )
                        messages.append({"role": "assistant", "content": raw_content})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your response must contain 'file_edits' and "
                                    "'synthesis'. Return JSON in the requested format."
                                ),
                            }
                        )
                        continue
                    else:
                        return {
                            "status": "error",
                            "message": "LLM response missing file_edits or synthesis",
                        }

                # Extraire les métriques d'usage
                usage = {}
                if response.usage:
                    usage = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }

                return {"status": "ok", "data": data, "usage": usage}

            except Exception as e:
                # LM2-25 fix : ne pas exposer str(e) (peut contenir l'URL
                # LLMaaS et des détails openai). Log côté serveur, message
                # générique au client. Le caller (consolidate()) propage
                # déjà ce dict tel quel.
                logger.error("LLM call exception : %s", e)
                from ..config import get_settings as _gs
                if _gs().mcp_server_debug:
                    return {
                        "status": "error",
                        "message": f"LLM call failed: {str(e)}",
                    }
                return {"status": "error", "message": "LLM call failed"}

        return {"status": "error", "message": "LLM failed after retries"}

    async def _write_results(
        self,
        space_id: str,
        llm_output: dict,
        bank_files: list[dict],
        notes_keys: list[str],
        notes_count: int,
        usage: dict,
        skip_meta: bool = False,
    ) -> dict:
        """
        Applique les éditions LLM et écrit les résultats sur S3.

        Pour chaque file_edit :
        - action "edit" : lire le fichier existant, appliquer les opérations, écrire
        - action "create" : écrire le contenu complet (nouveau fichier)
        - action "rewrite" : écrire le contenu complet (réécriture justifiée)

        Ordre : bank files → synthesis → [meta si non skip] → delete notes.
        Les notes sont supprimées EN DERNIER (atomicité logique).

        Args:
            skip_meta: Si True, ne met pas à jour _meta.json (mode batch,
                       le meta est mis à jour une seule fois à la fin)

        Returns:
            Métriques de consolidation
        """
        storage = get_storage()

        # Construire un index des fichiers bank existants par filename SANITISÉ.
        # On sanitise les clés pour matcher avec les filenames du LLM (qui sont
        # aussi sanitisés). On garde la correspondance raw_key → sanitized pour
        # pouvoir nettoyer les anciennes clés S3 contaminées par Unicode.
        bank_index = {}  # sanitized_filename → content
        bank_raw_keys = {}  # sanitized_filename → [liste des clés S3 brutes]
        for bf in bank_files:
            raw_key = bf["key"]
            # Extraire le chemin relatif complet (supporte les sous-dossiers)
            raw_relpath = bank_relpath(raw_key, space_id)
            sanitized = _sanitize_filename(raw_relpath)
            # Si plusieurs clés S3 sanitisent vers le même nom → doublons !
            # On garde la version la plus récente (dernière dans la liste triée)
            bank_index[sanitized] = bf["content"]
            if sanitized not in bank_raw_keys:
                bank_raw_keys[sanitized] = []
            bank_raw_keys[sanitized].append(raw_key)

        files_created = 0
        files_updated = 0
        files_cleaned = 0
        operations_applied = 0
        operations_failed = 0

        async def _cleanup_unicode_duplicates(sanitized_name: str) -> None:
            """Supprime les anciennes clés S3 contaminées par Unicode
            qui sanitisent vers le même nom de fichier."""
            nonlocal files_cleaned
            canonical_key = f"{space_id}/bank/{sanitized_name}"
            raw_keys = bank_raw_keys.get(sanitized_name, [])
            for rk in raw_keys:
                if rk != canonical_key:
                    logger.info(
                        "Cleaning Unicode duplicate: %r → canonical %s",
                        rk,
                        canonical_key,
                    )
                    await storage.delete(rk)
                    files_cleaned += 1

        # 4a. Appliquer chaque édition de fichier
        for file_edit in llm_output.get("file_edits", []):
            filename = _sanitize_filename(file_edit.get("filename", ""))
            action = file_edit.get("action", "edit")

            if not filename:
                logger.warning("file_edit sans filename, ignoré")
                operations_failed += 1
                continue

            if action == "create":
                # Nouveau fichier : écriture complète
                content = file_edit.get("content", "")
                if content:
                    await storage.put(f"{space_id}/bank/{filename}", content)
                    await _cleanup_unicode_duplicates(filename)
                    files_created += 1
                    logger.info("Created bank file: %s", filename)
                else:
                    operations_failed += 1
                    logger.warning("CREATE vide pour %s, ignoré", filename)

            elif action == "rewrite":
                # Réécriture complète (fallback justifié)
                content = file_edit.get("content", "")
                reason = file_edit.get("reason", "non spécifiée")
                if content:
                    # LM2-13 fix : protection anti-effacement par prompt injection.
                    # Si le rewrite réduit le fichier de plus de (1 - _REWRITE_MIN_RATIO),
                    # c'est suspect (un compact légitime vise rarement >70%). On
                    # refuse l'opération et on logue pour audit. Le fichier original
                    # reste intact. Cette défense n'est appliquée que si l'ancien
                    # fichier dépasse _REWRITE_MIN_ABSOLUTE_BYTES (sinon le ratio
                    # est trop sensible aux petites variations).
                    old_content = bank_index.get(filename)
                    old_size = len(old_content) if old_content else 0
                    new_size = len(content)
                    if (
                        old_size >= _REWRITE_MIN_ABSOLUTE_BYTES
                        and new_size < old_size * _REWRITE_MIN_RATIO
                    ):
                        logger.error(
                            "REWRITE refused for %s — content shrinks too much "
                            "(%d → %d bytes, ratio=%.2f, threshold=%.2f). "
                            "Reason given by LLM: %s. Possible prompt injection.",
                            filename,
                            old_size,
                            new_size,
                            new_size / old_size if old_size else 0,
                            _REWRITE_MIN_RATIO,
                            reason,
                        )
                        operations_failed += 1
                        # Skip ce file_edit — le fichier original n'est pas touché
                        continue

                    # Déduplication défensive via LLM : le LLM peut produire
                    # un rewrite avec des sections déjà dupliquées
                    content, dedup_count = await self._deduplicate_content(
                        content, filename
                    )
                    await storage.put(f"{space_id}/bank/{filename}", content)
                    await _cleanup_unicode_duplicates(filename)
                    files_updated += 1
                    logger.info("Rewrote bank file: %s (reason: %s)", filename, reason)
                else:
                    operations_failed += 1
                    logger.warning("REWRITE vide pour %s, ignoré", filename)

            elif action == "edit":
                # Édition chirurgicale : appliquer les opérations
                operations = file_edit.get("operations", [])
                if not operations:
                    continue

                # Lire le contenu existant
                existing_content = bank_index.get(filename)
                if existing_content is None:
                    # Le fichier n'existe pas → le LLM aurait dû utiliser "create"
                    # On tente quand même en partant de rien
                    logger.warning(
                        "edit sur fichier inexistant '%s', traité comme create",
                        filename,
                    )
                    existing_content = ""

                # Appliquer les opérations une par une
                updated_content = existing_content
                for op in operations:
                    try:
                        updated_content = _apply_operation(updated_content, op)
                        operations_applied += 1
                    except Exception as e:
                        logger.error(
                            "Échec opération %s sur %s: %s",
                            op.get("type", "?"),
                            filename,
                            str(e),
                        )
                        operations_failed += 1

                # HM-05 fix : garde anti-effacement par prompt injection sur le
                # chemin `edit`, symétrique de LM2-13 sur le chemin `rewrite`. Sans
                # elle, une note injectée pouvait faire émettre au LLM des
                # delete_section / replace_section vides qui érodaient le bank
                # section par section EN CONTOURNANT totalement le check de ratio
                # (qui ne gardait que `rewrite`). On applique le même seuil : si
                # l'édition rétrécit un fichier au-dessus du seuil absolu sous
                # _REWRITE_MIN_RATIO de sa taille, on REFUSE l'écriture (le fichier
                # original reste intact) et on trace comme opérations échouées.
                #
                # Mesuré AVANT _deduplicate_content (comme le chemin `rewrite`) :
                # un fichier à >30% de doublons pré-existants — que le dedup est
                # justement censé nettoyer — n'est ainsi pas refusé à tort.
                old_size = len(existing_content)
                new_size = len(updated_content)
                if (
                    old_size >= _REWRITE_MIN_ABSOLUTE_BYTES
                    and new_size < old_size * _REWRITE_MIN_RATIO
                ):
                    logger.error(
                        "EDIT refused for %s — content shrinks too much "
                        "(%d → %d bytes, ratio=%.2f, threshold=%.2f). Possible "
                        "prompt injection via delete_section/replace_section.",
                        filename,
                        old_size,
                        new_size,
                        new_size / old_size if old_size else 0,
                        _REWRITE_MIN_RATIO,
                    )
                    operations_failed += 1
                    # Skip l'écriture — le fichier original n'est pas touché.
                    continue

                # Déduplication défensive post-opérations via LLM :
                # rattrape les doublons résiduels que les opérations
                # n'ont pas pu corriger (ex: doublons pré-existants)
                updated_content, dedup_count = await self._deduplicate_content(
                    updated_content, filename
                )

                # Écrire seulement si le contenu a changé
                if updated_content != existing_content:
                    await storage.put(f"{space_id}/bank/{filename}", updated_content)
                    await _cleanup_unicode_duplicates(filename)
                    files_updated += 1
                    logger.info(
                        "Updated bank file: %s (%d operations applied)",
                        filename,
                        len(operations),
                    )
            else:
                logger.warning(
                    "Action inconnue '%s' pour %s, ignorée", action, filename
                )
                operations_failed += 1

        # A rejected/invalid edit means the selected source set was not fully
        # integrated.  Keep every live note for a controlled retry instead of
        # deleting evidence after a partial bank mutation (never-drop).
        notes_processed = 0 if operations_failed else notes_count

        # 4b. Écrire la synthèse résiduelle
        synthesis_content = llm_output.get("synthesis", "")
        now = datetime.now(timezone.utc).isoformat()
        synthesis_md = (
            f"---\n"
            f'consolidated_at: "{now}"\n'
            f"notes_processed: {notes_processed}\n"
            f"mode: surgical_edit\n"
            f"operations_applied: {operations_applied}\n"
            f"operations_failed: {operations_failed}\n"
            f"---\n\n"
            f"{synthesis_content}"
        )
        await storage.put(f"{space_id}/_synthesis.md", synthesis_md)

        # 4c. Mettre à jour _meta.json (sauf en mode batch où le meta
        #     est mis à jour une seule fois à la fin par consolidate())
        if not skip_meta:
            meta = await storage.get_json(f"{space_id}/_meta.json") or {}
            meta["last_consolidation"] = now
            meta["consolidation_count"] = meta.get("consolidation_count", 0) + 1
            meta["total_notes_processed"] = (
                meta.get("total_notes_processed", 0) + notes_processed
            )
            await storage.put_json(f"{space_id}/_meta.json", meta)

        # Compter les fichiers bank inchangés
        bank_objects = await storage.list_objects(f"{space_id}/bank/")
        total_bank = len([o for o in bank_objects if not o["Key"].endswith(".keep")])
        files_unchanged = total_bank - files_created - files_updated

        # Prepare every non-destructive response field before deleting.  Once
        # delete_many succeeds, no later storage operation may erase the exact
        # mutation counts from the caller-visible result.
        result = {
            "space_id": space_id,
            "notes_processed": notes_processed,
            "bank_files_updated": files_updated,
            "bank_files_created": files_created,
            "bank_files_unchanged": max(0, files_unchanged),
            "bank_files_total": total_bank,
            "operations_applied": operations_applied,
            "operations_failed": operations_failed,
            "synthesis_size": len(synthesis_content),
            "llm_tokens_used": usage.get("total_tokens", 0),
            "llm_prompt_tokens": usage.get("prompt_tokens", 0),
            "llm_completion_tokens": usage.get("completion_tokens", 0),
        }

        # 4d. Supprimer les notes live traitées (DERNIER await non-best-effort).
        # A partial integration deliberately performs no source deletion.
        notes_deleted = (
            await storage.delete_many(notes_keys) if operations_failed == 0 else 0
        )
        if not isinstance(notes_deleted, int) or not 0 <= notes_deleted <= notes_count:
            logger.error(
                "Invalid delete_many count after consolidation: %r for %d note(s)",
                notes_deleted,
                notes_count,
            )
            notes_deleted = 0
        notes_delete_failed = notes_count - notes_deleted
        result.update(
            {
                "status": "partial" if notes_delete_failed else "ok",
                "notes_deleted": notes_deleted,
                "notes_delete_failed": notes_delete_failed,
            }
        )
        if operations_failed:
            result["reason"] = "partial_consolidation"
            result["message"] = (
                "Consolidation partielle : au moins une édition bank a échoué "
                "ou été refusée. Aucune note source n'a été supprimée."
            )
        elif notes_delete_failed:
            result["reason"] = "partial_delete"
            result["message"] = (
                "Consolidation écrite dans la bank, mais certaines notes live "
                "n'ont pas pu être supprimées. Elles restent présentes pour "
                "une reprise contrôlée."
            )
        return result

    async def _deduplicate_content(
        self, content: str, filename: str
    ) -> tuple[str, int]:
        """
        Détecte et fusionne les sections dupliquées via le LLM.

        Traite UN SEUL doublon par itération, puis re-détecte les doublons
        restants sur le contenu mis à jour. Cela évite le bug d'indices
        décalés (IndexError) qui survenait quand on utilisait les indices
        de la détection initiale après avoir modifié la liste de sections.

        Args:
            content: Contenu Markdown du fichier
            filename: Nom du fichier (pour les logs)

        Returns:
            Tuple (contenu dédupliqué, nombre de doublons fusionnés)
        """
        total_merged = 0
        max_iterations = 50  # Sécurité anti-boucle infinie

        for _ in range(max_iterations):
            # Re-détecter les doublons sur le contenu ACTUEL à chaque itération
            duplicates = _detect_duplicates(content)
            if not duplicates:
                break

            # Traiter le PREMIER doublon trouvé
            heading, indices = next(iter(duplicates.items()))
            sections = _parse_sections(content)

            # Vérifier que les indices sont valides (sécurité défensive)
            if any(i >= len(sections) for i in indices):
                logger.error(
                    "DEDUP %s: indices invalides pour '%s' (max=%d, indices=%s) — skip",
                    filename,
                    heading,
                    len(sections) - 1,
                    indices,
                )
                break

            # Extraire le contenu de chaque version dupliquée
            versions = [sections[i]["content"] for i in indices]

            logger.warning(
                "DEDUP %s: heading '%s' trouvé %d fois — fusion via LLM",
                filename,
                heading,
                len(indices),
            )

            # ── Optimisation : skip LLM si les versions sont identiques
            # ou si l'une est un sous-ensemble de l'autre ──
            stripped = [v.strip() for v in versions]
            unique = set(stripped)

            if len(unique) == 1:
                # Toutes les versions identiques → garder la dernière, pas d'appel LLM
                logger.info(
                    "DEDUP %s: '%s' — %d versions identiques, skip LLM",
                    filename, heading, len(indices),
                )
                merged = stripped[-1]
            elif len(unique) == 2:
                # Vérifier si l'une est un sous-ensemble de lignes de l'autre.
                # On compare au niveau des LIGNES (pas des sous-chaînes) pour
                # éviter les faux positifs comme "OK" in "Jalon OK terminé".
                short_v, long_v = sorted(unique, key=len)
                short_lines = {ln.strip() for ln in short_v.splitlines() if ln.strip()}
                long_lines = {ln.strip() for ln in long_v.splitlines() if ln.strip()}
                if short_lines and short_lines.issubset(long_lines):
                    merged = long_v  # Garder la version la plus complète
                    logger.info(
                        "DEDUP %s: '%s' — %d/%d lignes incluses dans la version longue, skip LLM",
                        filename, heading, len(short_lines), len(long_lines),
                    )
                else:
                    # Versions réellement différentes → appel LLM
                    logger.warning(
                        "DEDUP %s: heading '%s' trouvé %d fois — fusion via LLM",
                        filename, heading, len(indices),
                    )
                    merged = await self._merge_sections_via_llm(heading, versions)
            else:
                # 3+ versions différentes → appel LLM
                logger.warning(
                    "DEDUP %s: heading '%s' trouvé %d fois — fusion via LLM",
                    filename, heading, len(indices),
                )
                merged = await self._merge_sections_via_llm(heading, versions)

            if merged is not None:
                # Garder la DERNIÈRE occurrence, supprimer les précédentes
                last_idx = indices[-1]
                sections[last_idx]["content"] = (
                    "\n" + merged + "\n" if not merged.startswith("\n") else merged
                )

                # Supprimer les occurrences précédentes (en partant de la fin)
                for idx in reversed(indices[:-1]):
                    sections.pop(idx)
                    total_merged += 1
            else:
                # Fallback si le LLM échoue : garder la dernière occurrence
                logger.error(
                    "DEDUP %s: fusion LLM échouée pour '%s' — "
                    "fallback: conservation de la dernière occurrence",
                    filename,
                    heading,
                )
                for idx in reversed(indices[:-1]):
                    sections.pop(idx)
                    total_merged += 1

            # Reconstruire le contenu pour la prochaine itération
            content = _reconstruct_from_sections(sections)

        return content, total_merged

    async def _merge_sections_via_llm(
        self, heading: str, versions: list[str]
    ) -> str | None:
        """
        Appelle le LLM pour fusionner N versions d'une même section.

        Prompt court et ciblé : le LLM reçoit les versions et doit
        retourner une seule version fusionnée, sans perte d'information
        pertinente et sans duplication.

        Args:
            heading: Le heading Markdown de la section (ex: "### État technique V2")
            versions: Liste des contenus des différentes versions

        Returns:
            Contenu fusionné, ou None si l'appel LLM échoue
        """
        versions_text = ""
        for i, v in enumerate(versions, 1):
            versions_text += f"\n--- VERSION {i} ---\n{v.strip()}\n"

        prompt = f"""You receive {len(versions)} versions of the same Markdown section, duplicated by mistake.

SECTION: {heading}

{versions_text}

INSTRUCTION: Merge these versions into ONE coherent version.
- Keep all RELEVANT and CURRENT information from every version.
- If one version has newer data (for example, "322 tests" versus "272 tests"), keep the newer data.
- Remove duplicate information.
- Preserve the Markdown format and style.
- Return ONLY the merged content, WITHOUT the heading, tags, or an explanation."""

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],  # type: ignore[list-item]
                max_tokens=4096,
                temperature=0.1,  # Basse température pour la fusion
            )

            merged = response.choices[0].message.content or ""

            # Nettoyer : retirer les blocs <think> et les backticks
            merged = re.sub(r"<think>.*?</think>", "", merged, flags=re.DOTALL)
            merged = re.sub(r"^```(?:markdown)?\s*", "", merged.strip())
            merged = re.sub(r"\s*```$", "", merged.strip())

            logger.info(
                "DEDUP merge OK: '%s' — %d versions → 1 (%d chars)",
                heading,
                len(versions),
                len(merged),
            )
            return merged

        except Exception as e:
            logger.error("DEDUP merge FAILED: '%s' — %s", heading, str(e))
            return None

    async def close(self) -> None:
        """
        Ferme le httpx.AsyncClient injecté, si présent.

        AsyncOpenAI ne prend pas ownership du http_client qu'on lui passe :
        c'est ConsolidatorService qui est responsable de l'appeler explicitement.
        À brancher sur le shutdown ASGI (voir close_consolidator_if_initialized).
        """
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def test_connection(self) -> dict:
        """Teste la connexion au LLMaaS avec un appel minimal."""
        try:
            t0 = time.monotonic()
            await self._client.models.list()
            latency = round((time.monotonic() - t0) * 1000, 1)
            return {
                "status": "ok",
                "model": self._model,
                "latency_ms": latency,
            }
        except Exception as e:
            # LM2-25 fix : ne pas exposer str(e) (peut contenir l'URL LLMaaS).
            logger.warning("LLMaaS test_connection failed: %s", e)
            return {"status": "error", "message": "LLMaaS unreachable"}

    # ─────────────────────────────────────────────────────────
    # Bank Compaction
    # ─────────────────────────────────────────────────────────

    def _get_max_size_for_file(self, filename: str) -> int:
        """Retourne la taille max autorisée pour un fichier bank.

        Limite universelle unique — les noms de fichiers dépendent des
        rules de chaque espace et ne sont pas contrôlés par le serveur.
        """
        return self._bank_file_max_size

    async def _compact_bank_if_needed(
        self, space_id: str, bank_files: list[dict], rules: str
    ) -> dict:
        """
        Auto-compact de la bank avant consolidation.

        Vérifie si le prompt total (bank + notes estimées) risque de
        dépasser le seuil configuré. Si oui, compacte chaque fichier
        bank dépassant sa taille max via un appel LLM dédié.

        Inspiré de l'autoCompact de Claude Code — voir CONTEXT_COMPACTION.md.

        Args:
            space_id: Identifiant de l'espace
            bank_files: Liste des fichiers bank actuels
            rules: Rules de l'espace (pour le contexte du LLM)

        Returns:
            Dict avec compacted (bool), files_compacted, size_before, size_after
        """
        storage = get_storage()

        # Estimer la taille totale de la bank
        total_bank_size = sum(len(bf.get("content", "")) for bf in bank_files)
        estimated_bank_tokens = total_bank_size // 4

        # Vérifier si la compaction est nécessaire
        # Seuil : la bank seule consomme déjà > compact_threshold du budget
        if estimated_bank_tokens <= self._max_tokens * self._compact_threshold:
            logger.debug(
                "Bank size OK — %d bytes (~%d tokens), threshold %.0f%% of %d",
                total_bank_size,
                estimated_bank_tokens,
                self._compact_threshold * 100,
                self._max_tokens,
            )
            return {
                "compacted": False,
                "files_compacted": 0,
                "size_before": total_bank_size,
                "size_after": total_bank_size,
            }

        logger.warning(
            "COMPACT — Bank trop grosse : %d bytes (~%d tokens, "
            "seuil=%.0f%% de %d). Compaction en cours...",
            total_bank_size,
            estimated_bank_tokens,
            self._compact_threshold * 100,
            self._max_tokens,
        )

        # Identifier les fichiers à compacter (ceux qui dépassent leur limite)
        files_compacted = 0
        size_before = total_bank_size
        size_after = 0

        for bf in bank_files:
            raw_key = bf["key"]
            raw_relpath = bank_relpath(raw_key, space_id)
            filename = _sanitize_filename(raw_relpath)
            content = bf.get("content", "")
            file_size = len(content)
            max_size = self._get_max_size_for_file(filename)

            if file_size <= max_size:
                size_after += file_size
                continue

            # Ce fichier doit être compacté
            logger.info(
                "COMPACT %s — %d bytes (max %d), compaction via LLM...",
                filename,
                file_size,
                max_size,
            )

            compacted = await self._compact_single_file(
                filename, content, max_size, rules
            )

            if compacted is not None and len(compacted) < file_size:
                # Écrire le fichier compacté sur S3
                await storage.put(f"{space_id}/bank/{filename}", compacted)
                files_compacted += 1
                size_after += len(compacted)
                logger.info(
                    "COMPACT %s — %d → %d bytes (-%d%%)",
                    filename,
                    file_size,
                    len(compacted),
                    round((1 - len(compacted) / file_size) * 100),
                )
            else:
                # Compaction échouée ou pas de réduction → garder l'original
                size_after += file_size
                logger.warning(
                    "COMPACT %s — échec ou pas de réduction, fichier conservé",
                    filename,
                )

        return {
            "compacted": files_compacted > 0,
            "files_compacted": files_compacted,
            "size_before": size_before,
            "size_after": size_after,
        }

    async def _compact_single_file(
        self, filename: str, content: str, max_size: int, rules: str
    ) -> str | None:
        """
        Compacte un seul fichier bank via un appel LLM dédié.

        Le LLM reçoit le contenu actuel et doit le résumer/nettoyer
        pour le ramener sous la taille cible, tout en conservant
        les informations structurantes.

        Args:
            filename: Nom du fichier (pour adapter les instructions)
            content: Contenu Markdown actuel
            max_size: Taille cible en bytes
            rules: Rules de l'espace (contexte)

        Returns:
            Contenu compacté, ou None si l'appel LLM échoue
        """
        # Instructions de compaction génériques — les rules de l'espace
        # définissent la sémantique de chaque fichier, pas le serveur.
        specific = (
            "Summarize the content while preserving the section structure.\n"
            "- Merge redundant information\n"
            "- Remove obsolete or overly granular details\n"
            "- Preserve architectural decisions and structural information\n"
            "- Summarize old entries as one line per milestone\n"
            "- Use the REFERENCE RULES above to understand this file's role"
        )

        prompt = f"""You receive a bank file named "{filename}" containing {len(content)} bytes
(limit: {max_size} bytes). COMPACT it below that limit.

=== REFERENCE RULES ===
{rules[:2000]}

=== FILE-SPECIFIC INSTRUCTIONS ===
{specific}

=== COMPACTION RULES ===
- Preserve the main heading and section structure.
- Summarize redundant or overly detailed blocks.
- Remove completed or obsolete items.
- Merge similar entries.
- Preserve important milestone dates.
- Do not lose structural information such as decisions, architecture, or stack.
- The result must be under {max_size} bytes.

=== CURRENT CONTENT ===
{content}

Return ONLY the compacted content as plain Markdown, without JSON, tags, or an explanation."""

        try:
            # Estimer le budget de sortie : on veut ~max_size bytes en sortie
            # + marge pour le formatting. Le contenu max_size / 4 ≈ tokens cibles.
            output_tokens = max(4096, max_size // 3)

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=output_tokens,
                temperature=0.2,  # Basse température pour la compaction
            )

            compacted = response.choices[0].message.content or ""

            # Nettoyer : retirer les blocs <think> et les backticks
            compacted = re.sub(r"<think>.*?</think>", "", compacted, flags=re.DOTALL)
            compacted = re.sub(r"^```(?:markdown)?\s*", "", compacted.strip())
            compacted = re.sub(r"\s*```$", "", compacted.strip())

            return compacted

        except Exception as e:
            logger.error("COMPACT %s FAILED: %s", filename, str(e))
            return None

    async def compact_bank(self, space_id: str, dry_run: bool = True) -> dict:
        """
        Compaction manuelle de la bank d'un espace (outil MCP standalone).

        En mode dry_run, rapporte les fichiers à compacter et leurs tailles
        sans modifier quoi que ce soit.

        Args:
            space_id: Identifiant de l'espace
            dry_run: True = scan seul, False = compaction effective

        Returns:
            Rapport de compaction avec détails par fichier
        """
        storage = get_storage()

        # Vérifier l'existence de l'espace
        meta = await storage.get_json(f"{space_id}/_meta.json")
        if meta is None:
            return {"status": "error", "message": f"Space '{space_id}' not found"}

        # Lire la bank et les rules
        bank_files = await storage.list_and_get(f"{space_id}/bank/")
        rules = await storage.get(f"{space_id}/_rules.md") or ""

        # Analyser chaque fichier
        file_reports = []
        total_before = 0
        total_after = 0
        files_over_limit = 0

        for bf in bank_files:
            raw_relpath = bank_relpath(bf["key"], space_id)
            filename = _sanitize_filename(raw_relpath)
            content = bf.get("content", "")
            file_size = len(content)
            max_size = self._get_max_size_for_file(filename)
            over = file_size > max_size

            total_before += file_size

            report = {
                "filename": filename,
                "size": file_size,
                "max_size": max_size,
                "over_limit": over,
                "ratio": round(file_size / max_size, 2) if max_size > 0 else 0,
            }

            if over:
                files_over_limit += 1
                if not dry_run:
                    await assert_space_not_reserved(space_id)
                    # Compacter effectivement
                    compacted = await self._compact_single_file(
                        filename, content, max_size, rules
                    )
                    if compacted is not None and len(compacted) < file_size:
                        await storage.put(f"{space_id}/bank/{filename}", compacted)
                        report["compacted_size"] = len(compacted)
                        report["reduction_pct"] = round(
                            (1 - len(compacted) / file_size) * 100
                        )
                        total_after += len(compacted)
                    else:
                        report["compacted_size"] = file_size
                        report["error"] = "compaction failed or no reduction"
                        total_after += file_size
                else:
                    total_after += file_size
            else:
                total_after += file_size

            file_reports.append(report)

        return {
            "status": "ok",
            "space_id": space_id,
            "dry_run": dry_run,
            "files_total": len(bank_files),
            "files_over_limit": files_over_limit,
            "total_size_before": total_before,
            "total_size_after": total_after if not dry_run else total_before,
            "files": file_reports,
        }


# ─────────────────────────────────────────────────────────────
# Sanitisation des noms de fichiers LLM
# ─────────────────────────────────────────────────────────────

# Caractères Unicode invisibles que les LLMs insèrent parfois dans les
# noms de fichiers (surtout dans les réponses JSON longues — "drift").
# Leur présence crée des clés S3 visuellement identiques mais techniquement
# différentes, rendant les fichiers illisibles par bank_read.
_INVISIBLE_CHARS = frozenset(
    {
        "\u200b",  # Zero Width Space
        "\u200c",  # Zero Width Non-Joiner
        "\u200d",  # Zero Width Joiner
        "\u200e",  # Left-to-Right Mark
        "\u200f",  # Right-to-Left Mark
        "\u202a",  # Left-to-Right Embedding
        "\u202b",  # Right-to-Left Embedding
        "\u202c",  # Pop Directional Formatting
        "\u202d",  # Left-to-Right Override
        "\u202e",  # Right-to-Left Override
        "\u2060",  # Word Joiner
        "\u2061",  # Function Application
        "\u2062",  # Invisible Times
        "\u2063",  # Invisible Separator
        "\u2064",  # Invisible Plus
        "\ufeff",  # Byte Order Mark (ZWNBS)
        "\u00ad",  # Soft Hyphen
        "\u034f",  # Combining Grapheme Joiner
        "\u061c",  # Arabic Letter Mark
        "\u180e",  # Mongolian Vowel Separator
    }
)

# Caractères Unicode ressemblant à des tirets mais qui ne sont pas
# le tiret ASCII standard (U+002D). Normalisés vers '-'.
_HYPHEN_LIKE = frozenset(
    {
        "\u2010",  # Hyphen
        "\u2011",  # Non-Breaking Hyphen
        "\u2012",  # Figure Dash
        "\u2013",  # En Dash
        "\u2014",  # Em Dash
        "\u2015",  # Horizontal Bar
        "\u2212",  # Minus Sign
        "\ufe58",  # Small Em Dash
        "\ufe63",  # Small Hyphen-Minus
        "\uff0d",  # Fullwidth Hyphen-Minus
    }
)


def _sanitize_filename(filename: str) -> str:
    """
    Nettoie un nom de fichier généré par le LLM.

    Supprime les caractères Unicode invisibles et normalise les tirets
    Unicode vers le tiret ASCII standard (U+002D).

    Bug découvert le 13/03/2026 : le LLM insère des
    caractères invisibles dans les noms de fichiers à partir du ~8ème
    fichier dans les réponses JSON longues. Ces caractères rendent
    les fichiers illisibles par bank_read (qui reconstruit la clé S3
    manuellement) alors que bank_read_all fonctionne (utilise les
    vraies clés S3 depuis list_objects).

    Args:
        filename: Nom de fichier brut issu du JSON LLM

    Returns:
        Nom de fichier nettoyé (ASCII + caractères courants uniquement)
    """
    chars = []
    removed = 0
    normalized = 0

    for ch in filename:
        if ch in _INVISIBLE_CHARS:
            removed += 1
            continue
        elif ch in _HYPHEN_LIKE:
            chars.append("-")
            normalized += 1
        else:
            chars.append(ch)

    sanitized = "".join(chars).strip()

    # Nettoyer les préfixes parasites que le LLM invente en lisant les rules.
    # Ex: les rules presales disent "ILS SONT DANS LE REPERTOIRE 1.MEMORY_BANK"
    # → le LLM retourne "1.MEMORY_BANK/personaProfiles/acheteur.md"
    # On retire ces préfixes connus mais on GARDE les sous-dossiers légitimes.
    _PARASITIC_PREFIXES = ("1.MEMORY_BANK/", "MEMORY_BANK/", "bank/")
    for prefix in _PARASITIC_PREFIXES:
        if sanitized.startswith(prefix):
            old = sanitized
            sanitized = sanitized[len(prefix) :]
            logger.warning(
                "Filename parasitic prefix removed: %r → %r",
                old,
                sanitized,
            )

    # Nettoyer les / en début/fin et les doubles //
    sanitized = sanitized.strip("/")
    while "//" in sanitized:
        sanitized = sanitized.replace("//", "/")

    if removed > 0 or normalized > 0:
        logger.warning(
            "Filename sanitized: %r → %r (removed %d invisible, normalized %d hyphens)",
            filename,
            sanitized,
            removed,
            normalized,
        )

    return sanitized


# ─────────────────────────────────────────────────────────────
# Moteur d'édition Markdown
# ─────────────────────────────────────────────────────────────


def _parse_sections(content: str) -> list[dict]:
    """
    Parse un fichier Markdown en sections.

    Chaque section est définie par un heading (# ## ### etc.) et contient
    tout le texte jusqu'au prochain heading de même niveau ou supérieur.

    Returns:
        Liste de dicts :
        {
            "heading": "## Section Title" (ou "" pour le préambule),
            "heading_text": "Section Title" (sans les #),
            "level": 2 (nombre de #, 0 pour le préambule),
            "content": "lignes de contenu après le heading\\n...",
            "start_line": 0  (index de ligne du heading)
        }
    """
    lines = content.split("\n")
    sections = []
    current_heading = ""
    current_heading_text = ""
    current_level = 0
    current_content_lines = []
    current_start = 0

    for i, line in enumerate(lines):
        # Détecter un heading Markdown (# à ######)
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)

        if heading_match:
            # Sauvegarder la section précédente
            sections.append(
                {
                    "heading": current_heading,
                    "heading_text": current_heading_text,
                    "level": current_level,
                    "content": "\n".join(current_content_lines),
                    "start_line": current_start,
                }
            )

            # Commencer une nouvelle section
            hashes = heading_match.group(1)
            current_heading = line
            current_heading_text = heading_match.group(2).strip()
            current_level = len(hashes)
            current_content_lines = []
            current_start = i
        else:
            current_content_lines.append(line)

    # Sauvegarder la dernière section
    sections.append(
        {
            "heading": current_heading,
            "heading_text": current_heading_text,
            "level": current_level,
            "content": "\n".join(current_content_lines),
            "start_line": current_start,
        }
    )

    return sections


def _find_section_index(sections: list[dict], heading: str) -> int:
    """
    Trouve l'index d'une section par son heading.

    Matching flexible :
    - Correspondance exacte : "## Focus Actuel"
    - Sans les # : "Focus Actuel"
    - Case-insensitive en dernier recours

    Returns:
        Index dans la liste sections, ou -1 si non trouvé
    """
    heading_stripped = heading.strip()

    # 1. Correspondance exacte
    for i, sec in enumerate(sections):
        if sec["heading"].strip() == heading_stripped:
            return i

    # 2. Sans les # (le LLM a peut-être omis les ##)
    heading_no_hash = re.sub(r"^#+\s*", "", heading_stripped)
    for i, sec in enumerate(sections):
        if sec["heading_text"] == heading_no_hash:
            return i

    # 3. Case-insensitive
    heading_lower = heading_no_hash.lower()
    for i, sec in enumerate(sections):
        if sec["heading_text"].lower() == heading_lower:
            return i

    return -1


def _reconstruct_from_sections(sections: list[dict]) -> str:
    """
    Reconstruit un fichier Markdown à partir de sections parsées.

    Returns:
        Contenu Markdown reconstruit
    """
    parts = []
    for sec in sections:
        if sec["heading"]:
            parts.append(sec["heading"])
        if sec["content"]:
            parts.append(sec["content"])
        elif sec["heading"]:
            # Section avec heading mais sans contenu : ajouter une ligne vide
            parts.append("")

    result = "\n".join(parts)

    # Nettoyer les lignes vides multiples (max 2 consécutives)
    result = re.sub(r"\n{4,}", "\n\n\n", result)

    return result


def _apply_operation(content: str, operation: dict) -> str:
    """
    Applique une seule opération d'édition sur un contenu Markdown.

    Args:
        content: Contenu Markdown du fichier
        operation: Dict avec "type", "heading", "content", etc.

    Returns:
        Contenu Markdown modifié

    Raises:
        ValueError: Si l'opération est invalide ou la section introuvable
    """
    op_type = operation.get("type", "")
    heading = operation.get("heading", "")
    new_content = operation.get("content", "")

    if op_type == "replace_section":
        return _op_replace_section(content, heading, new_content)
    elif op_type == "append_to_section":
        return _op_append_to_section(content, heading, new_content)
    elif op_type == "prepend_to_section":
        return _op_prepend_to_section(content, heading, new_content)
    elif op_type == "add_section":
        after = operation.get("after", "")
        return _op_add_section(content, heading, new_content, after)
    elif op_type == "delete_section":
        return _op_delete_section(content, heading)
    else:
        raise ValueError(f"Unknown operation type: {op_type}")


def _op_replace_section(content: str, heading: str, new_content: str) -> str:
    """
    Remplace le contenu d'une section (entre le heading et le prochain
    heading de même niveau ou supérieur).

    Le heading lui-même est conservé.
    """
    sections = _parse_sections(content)
    idx = _find_section_index(sections, heading)

    if idx == -1:
        raise ValueError(f"Section not found: {heading}")

    # Remplacer le contenu de la section
    # S'assurer que le nouveau contenu commence et finit proprement
    if new_content and not new_content.startswith("\n"):
        new_content = "\n" + new_content
    if new_content and not new_content.endswith("\n"):
        new_content = new_content + "\n"

    sections[idx]["content"] = new_content

    return _reconstruct_from_sections(sections)


def _op_append_to_section(content: str, heading: str, new_content: str) -> str:
    """
    Ajoute du contenu à la fin d'une section existante.
    Le contenu existant est intégralement préservé.
    """
    sections = _parse_sections(content)
    idx = _find_section_index(sections, heading)

    if idx == -1:
        raise ValueError(f"Section not found: {heading}")

    existing = sections[idx]["content"]

    # Ajouter le nouveau contenu après l'existant
    if existing.rstrip():
        sections[idx]["content"] = existing.rstrip("\n") + "\n" + new_content + "\n"
    else:
        sections[idx]["content"] = "\n" + new_content + "\n"

    return _reconstruct_from_sections(sections)


def _op_prepend_to_section(content: str, heading: str, new_content: str) -> str:
    """
    Ajoute du contenu au début d'une section (après le heading).
    Le contenu existant est intégralement préservé.
    """
    sections = _parse_sections(content)
    idx = _find_section_index(sections, heading)

    if idx == -1:
        raise ValueError(f"Section not found: {heading}")

    existing = sections[idx]["content"]

    # Ajouter le nouveau contenu avant l'existant
    if existing.lstrip():
        sections[idx]["content"] = "\n" + new_content + "\n" + existing.lstrip("\n")
    else:
        sections[idx]["content"] = "\n" + new_content + "\n"

    return _reconstruct_from_sections(sections)


def _op_add_section(
    content: str, heading: str, new_content: str, after: str = ""
) -> str:
    """
    Ajoute une nouvelle section au fichier.

    Si 'after' est spécifié, insère après cette section.
    Sinon, ajoute à la fin du fichier.

    GARDE-FOU ANTI-DOUBLON (v1.3.0) : si une section avec le même
    heading existe déjà, l'opération est automatiquement convertie
    en replace_section pour éviter les doublons récurrents.
    """
    sections = _parse_sections(content)

    # ── GARDE-FOU : vérifier si le heading existe déjà ────
    existing_idx = _find_section_index(sections, heading)
    if existing_idx != -1:
        logger.warning(
            "add_section '%s' AUTO-CONVERTI en replace_section "
            "(section déjà existante à l'index %d)",
            heading,
            existing_idx,
        )
        return _op_replace_section(content, heading, new_content)

    # Déterminer le niveau du heading
    heading_match = re.match(r"^(#{1,6})\s+(.+)$", heading.strip())
    if heading_match:
        level = len(heading_match.group(1))
        heading_text = heading_match.group(2).strip()
    else:
        # Pas de # → on assume ## (section de 2ème niveau)
        level = 2
        heading_text = heading.strip()
        heading = f"## {heading_text}"

    new_section = {
        "heading": heading,
        "heading_text": heading_text,
        "level": level,
        "content": "\n" + new_content + "\n",
        "start_line": -1,
    }

    if after:
        # Insérer après la section spécifiée
        idx = _find_section_index(sections, after)
        if idx != -1:
            sections.insert(idx + 1, new_section)
        else:
            # Section 'after' non trouvée → ajouter à la fin
            logger.warning(
                "Section 'after' non trouvée: %s — ajout en fin de fichier", after
            )
            sections.append(new_section)
    else:
        sections.append(new_section)

    return _reconstruct_from_sections(sections)


def _detect_duplicates(content: str) -> dict[str, list[int]]:
    """
    Détecte les sections dupliquées dans un fichier Markdown.

    Tient compte de la HIÉRARCHIE : deux headings identiques (ex: ### X)
    sous des parents différents (ex: ## A et ## B) sont des sections
    DISTINCTES, pas des doublons.

    L'identifiant complet d'une section est construit en préfixant
    le heading avec son parent hiérarchique le plus proche (heading
    de niveau strictement supérieur trouvé en remontant).

    Returns:
        Dict heading_key → [index1, index2, ...] pour les headings qui
        apparaissent plus d'une fois sous le même parent.
        Vide si pas de doublons.
    """
    sections = _parse_sections(content)

    # Compter les occurrences de chaque heading en tenant compte du chemin
    # hiérarchique COMPLET (tous les ancêtres, pas seulement le parent direct).
    # Ex: "## Parent A > ### Child > #### Grandchild"
    heading_indices: dict[str, list[int]] = {}
    for i, sec in enumerate(sections):
        h = sec["heading"].strip()
        if not h:  # Ignorer le préambule (heading vide)
            continue

        level = sec["level"]

        # Construire le chemin hiérarchique complet en remontant
        # vers tous les ancêtres (niveaux strictement décroissants)
        ancestors = []
        current_level = level
        if level > 1:
            for j in range(i - 1, -1, -1):
                jlevel = sections[j]["level"]
                if jlevel > 0 and jlevel < current_level:
                    ancestors.insert(0, sections[j]["heading"].strip())
                    current_level = jlevel
                    if current_level <= 1:
                        break

        # Identifiant hiérarchique complet :
        # "## Parent A > ### Child > #### Grandchild"
        if ancestors:
            full_key = " > ".join(ancestors) + " > " + h
        else:
            full_key = h

        if full_key not in heading_indices:
            heading_indices[full_key] = []
        heading_indices[full_key].append(i)

    # Ne garder que les headings dupliqués (même heading + même parent)
    return {h: indices for h, indices in heading_indices.items() if len(indices) > 1}


def _op_delete_section(content: str, heading: str) -> str:
    """
    Supprime une section entière (heading + contenu).
    """
    sections = _parse_sections(content)
    idx = _find_section_index(sections, heading)

    if idx == -1:
        raise ValueError(f"Section not found for deletion: {heading}")

    # Supprimer la section
    sections.pop(idx)

    return _reconstruct_from_sections(sections)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _extract_json(text: str) -> str:
    """
    Extrait le JSON d'une réponse LLM qui peut le contenir dans :
    - Un bloc ```json ... ```
    - Un bloc <think>...</think> suivi de JSON
    - Du texte brut avec un objet JSON {}

    Args:
        text: Réponse brute du LLM

    Returns:
        Chaîne JSON nettoyée prête pour json.loads()
    """
    # 1. Retirer les blocs <think>...</think> (Qwen thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # 2. Chercher un bloc ```json ... ```
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # 3. Chercher un bloc ``` ... ```
    match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 4. Chercher le premier { ... } (objet JSON brut)
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]

    # 5. Retourner le texte tel quel (json.loads() échouera)
    return text.strip()


def _repair_json(json_str: str, exc: json.JSONDecodeError) -> dict | None:
    """
    Tente de réparer un JSON tronqué/malformé provenant du LLM.

    Gère le cas "Unterminated string" (le plus fréquent avec qwen3.x) :
    le modèle génère une chaîne JSON dont une valeur string n'est
    jamais fermée (ex: guillemet ou caractère spécial non échappé).
    finish_reason=stop mais le JSON est structurellement invalide.

    Stratégie :
    1. Tronquer au point de l'erreur (avant la chaîne non terminée)
    2. Ajouter une chaîne vide "" comme placeholder
    3. Fermer toutes les structures JSON ouvertes ({, [)
    4. Parser le JSON réparé
    5. Supprimer la dernière opération (celle avec le contenu tronqué)
    6. Ajouter un champ "synthesis" par défaut s'il est absent

    Avantages vs retry :
    - Récupère ~90% des opérations instantanément (0 latence)
    - Économise 1 appel LLM complet (~100s + ~50K tokens)
    - Si la réparation échoue, le retry existant prend le relais

    Args:
        json_str: Chaîne JSON extraite par _extract_json()
        exc: L'exception JSONDecodeError avec la position de l'erreur

    Returns:
        Dict parsé si la réparation réussit, None sinon
    """
    error_msg = str(exc)

    if "Unterminated string" not in error_msg:
        return None

    pos = exc.pos
    if not pos or pos <= 0 or pos >= len(json_str):
        return None

    # ── Étape 1 : Tronquer avant la chaîne non terminée ──
    # exc.pos pointe vers le `"` ouvrant de la chaîne qui n'a pas de `"` fermant.
    # Tout ce qui précède cette position est du JSON valide (parsé sans erreur).
    # On ajoute "" comme placeholder pour la valeur tronquée.
    prefix = json_str[:pos] + '""'

    # ── Étape 2 : Fermer toutes les structures ouvertes ──
    repaired_str = _close_json_structure(prefix)
    if repaired_str is None:
        return None

    # ── Étape 3 : Parser le JSON réparé ──
    try:
        data = json.loads(repaired_str)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "file_edits" not in data:
        return None

    # ── Étape 4 : Nettoyer les opérations tronquées ──
    # La dernière opération du dernier file_edit a un content="" (notre placeholder).
    # Plutôt que d'appliquer une opération replace_section avec un contenu vide
    # (qui effacerait la section), on la supprime proprement.
    file_edits = data.get("file_edits", [])
    if file_edits:
        last_edit = file_edits[-1]
        if last_edit.get("action") == "edit":
            ops = last_edit.get("operations", [])
            if ops:
                last_op = ops[-1]
                # Supprimer l'opération si son contenu est vide (= tronquée)
                if not last_op.get("content", "").strip():
                    ops.pop()
                    logger.info(
                        "JSON repair: suppression de l'opération tronquée "
                        "(%s sur '%s')",
                        last_op.get("type", "?"),
                        last_op.get("heading", "?"),
                    )
                # Si plus aucune opération, supprimer le file_edit vide
                if not ops:
                    file_edits.pop()
        elif last_edit.get("action") in ("create", "rewrite"):
            # Pour create/rewrite, le content est le fichier entier.
            # S'il est vide, le file_edit est inutile.
            if not last_edit.get("content", "").strip():
                file_edits.pop()

    # ── Étape 5 : Ajouter synthesis par défaut si absent ──
    if "synthesis" not in data:
        data["synthesis"] = (
            "(consolidation partielle — JSON réparé automatiquement, "
            "dernière opération tronquée supprimée)"
        )

    return data


def _close_json_structure(partial_json: str) -> str | None:
    """
    Ferme toutes les structures JSON ouvertes à la fin d'un JSON partiel.

    Parcourt le JSON en suivant les guillemets (strings) et empile les
    ouvertures { et [. Puis ajoute les fermetures manquantes dans l'ordre.

    Robuste face aux strings contenant des accolades/crochets échappés.

    Args:
        partial_json: JSON partiel (potentiellement non terminé)

    Returns:
        JSON complété avec les fermetures manquantes, ou None si
        on est encore dans une string non fermée (irréparable)
    """
    stack = []
    in_string = False
    escape_next = False

    for ch in partial_json:
        if escape_next:
            escape_next = False
            continue

        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue

        # Hors d'une string
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    # Si on est encore dans une string, la réparation est impossible
    # (notre caller aurait dû fermer la string avant d'appeler)
    if in_string:
        return None

    if not stack:
        return partial_json

    # Fermer toutes les structures ouvertes dans l'ordre inverse
    closing = "".join(reversed(stack))
    return partial_json + closing


def _convert_legacy_format(data: dict) -> dict:
    """
    Convertit l'ancien format de réponse LLM (bank_files) vers le nouveau
    format (file_edits). Sert de filet de sécurité si le LLM retombe
    sur l'ancien format malgré le nouveau prompt.

    Ancien format:
        {"bank_files": [{"filename": "x.md", "content": "...", "action": "updated"}]}

    Nouveau format:
        {"file_edits": [{"filename": "x.md", "action": "rewrite", "content": "..."}]}
    """
    file_edits = []
    for bf in data.get("bank_files", []):
        old_action = bf.get("action", "updated")
        file_edits.append(
            {
                "filename": bf.get("filename", ""),
                "action": "create" if old_action == "created" else "rewrite",
                "content": bf.get("content", ""),
                "reason": "Legacy format conversion (LLM used old bank_files format)",
            }
        )

    return {
        "file_edits": file_edits,
        "synthesis": data.get("synthesis", ""),
    }


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_consolidator: ConsolidatorService | None = None


def get_consolidator() -> ConsolidatorService:
    """Retourne le singleton ConsolidatorService."""
    global _consolidator
    if _consolidator is None:
        _consolidator = ConsolidatorService()
    return _consolidator


async def close_consolidator_if_initialized() -> None:
    """
    Ferme le ConsolidatorService singleton s'il a été instancié.

    À appeler au shutdown ASGI pour libérer proprement le httpx.AsyncClient
    injecté dans AsyncOpenAI (quand PROXY_URL est défini).
    Sans appel explicite, le client resterait ouvert jusqu'à la fin du process.
    """
    global _consolidator
    if _consolidator is not None:
        await _consolidator.close()
        _consolidator = None
