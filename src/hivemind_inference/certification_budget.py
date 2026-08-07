"""Strict cross-process provider budget used only by protected certification.

The normal inference runtime is unchanged when the certification environment is
absent.  When it is present, every allowed OpenAI-compatible HTTP attempt must
reserve its request and conservative token ceilings in one shared SQLite
ledger *before* entering the transport.  Reservations are never refunded.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_SCHEMA_VERSION = "hivemind.provider-certification-budget.v1"
_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}\.[1-9][0-9]{0,19}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ENV_PATH = "HIVEMIND_CERTIFICATION_BUDGET_PATH"
_ENV_RUN_ID = "HIVEMIND_CERTIFICATION_RUN_ID"
_ENV_SOURCE_SHA = "HIVEMIND_CERTIFICATION_SOURCE_SHA"
_ENV_PROFILE_ID = "HIVEMIND_CERTIFICATION_PROFILE_ID"

# Protected-live timing authorities.  They are intentionally activated only
# by the complete strict-budget context below: ordinary runtime probes retain
# their adapter-owned defaults, while every process participating in one
# protected certification resolves the same exact bounds.
PROTECTED_CERTIFICATION_DISCOVERY_TIMEOUT_SECONDS = 60.0
PROTECTED_CERTIFICATION_GRAPH_HEALTH_TIMEOUT_SECONDS = 180.0
_METADATA_COLUMNS = frozenset(
    {
        "singleton",
        "schema_version",
        "run_id",
        "source_sha",
        "profile_id",
        "chat_provider_id",
        "chat_model",
        "embedding_provider_id",
        "embedding_model",
        "sealed",
        "violated",
    }
)
_TOTAL_COLUMNS = frozenset(
    {
        "role",
        "request_limit",
        "input_token_limit",
        "output_token_limit",
        "request_count",
        "input_tokens_upper_bound",
        "output_tokens_upper_bound",
        "reported_input_tokens",
        "reported_output_tokens",
        "usage_reported_requests",
    }
)
_ATTEMPT_COLUMNS = frozenset(
    {
        "attempt_id",
        "role",
        "state",
        "input_tokens_upper_bound",
        "output_token_reservation",
        "reported_input_tokens",
        "reported_output_tokens",
    }
)

CHAT_REQUEST_LIMIT = 12
CHAT_INPUT_TOKEN_LIMIT = 125_000
CHAT_OUTPUT_TOKEN_LIMIT = 4_096
EMBEDDING_REQUEST_LIMIT = 20
EMBEDDING_INPUT_TOKEN_LIMIT = 50_000

# UTF-8 JSON bytes already conservatively dominate normal tokenizer counts.
# Reserve additional fixed framing room for provider-side message wrappers.
_TOKEN_FRAMING_OVERHEAD = 512


class CertificationBudgetError(RuntimeError):
    """Value-free refusal raised before provider egress or promotion."""


@dataclass(frozen=True)
class BudgetConfiguration:
    path: Path
    run_id: str
    source_sha: str
    profile_id: str


@dataclass(frozen=True)
class BudgetReservation:
    path: Path
    attempt_id: int
    role: str
    input_token_upper_bound: int
    output_token_reservation: int
    run_id: str
    source_sha: str
    profile_id: str
    provider_id: str
    configured_model: str


@dataclass(frozen=True)
class RoleBudgetSnapshot:
    role: str
    request_limit: int
    input_token_limit: int
    output_token_limit: int | None
    request_count: int
    input_tokens_upper_bound: int
    output_tokens_upper_bound: int | None
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    usage_reported_requests: int

    def __post_init__(self) -> None:
        if self.role not in ("chat", "embedding"):
            raise ValueError("invalid budget-snapshot role")
        integer_values = (
            self.request_limit,
            self.input_token_limit,
            self.request_count,
            self.input_tokens_upper_bound,
            self.usage_reported_requests,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integer_values
        ):
            raise ValueError("invalid budget-snapshot integer")
        optional_values = (
            self.output_token_limit,
            self.output_tokens_upper_bound,
            self.reported_input_tokens,
            self.reported_output_tokens,
        )
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            )
            for value in optional_values
        ):
            raise ValueError("invalid optional budget-snapshot integer")
        if (
            self.request_count > self.request_limit
            or self.input_tokens_upper_bound > self.input_token_limit
            or self.usage_reported_requests > self.request_count
            or (
                self.reported_input_tokens is not None
                and self.reported_input_tokens > self.input_tokens_upper_bound
            )
        ):
            raise ValueError("budget snapshot exceeds its ceiling")
        if self.role == "chat":
            if (
                self.output_token_limit is None
                or self.output_tokens_upper_bound is None
                or self.output_tokens_upper_bound > self.output_token_limit
                or (
                    self.reported_output_tokens is not None
                    and self.reported_output_tokens
                    > self.output_tokens_upper_bound
                )
            ):
                raise ValueError("chat budget snapshot is invalid")
        elif any(
            value is not None
            for value in (
                self.output_token_limit,
                self.output_tokens_upper_bound,
                self.reported_output_tokens,
            )
        ):
            raise ValueError("embedding budget cannot carry output tokens")


@dataclass(frozen=True)
class CertificationBudgetSnapshot:
    schema_version: str
    run_id: str
    source_sha: str
    profile_id: str
    chat: RoleBudgetSnapshot
    embedding: RoleBudgetSnapshot
    sealed: bool

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("invalid certification-budget schema")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("invalid certification-budget run id")
        if not _SHA_RE.fullmatch(self.source_sha):
            raise ValueError("invalid certification-budget source sha")
        if not _PROFILE_RE.fullmatch(self.profile_id):
            raise ValueError("invalid certification-budget profile")
        if self.chat.role != "chat" or self.embedding.role != "embedding":
            raise ValueError("certification-budget roles are invalid")
        if type(self.sealed) is not bool:
            raise ValueError("certification-budget sealed must be boolean")


def _safe_identity(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise CertificationBudgetError(f"invalid {name}")
    return value


def _validate_regular_ledger(path: Path) -> None:
    if not path.is_absolute():
        raise CertificationBudgetError("budget path must be absolute")
    try:
        parent = path.parent.lstat()
        current = path.lstat()
    except OSError:
        raise CertificationBudgetError("budget ledger unavailable") from None
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise CertificationBudgetError("budget parent must be a directory")
    if stat.S_IMODE(parent.st_mode) != 0o1777:
        raise CertificationBudgetError("budget parent mode is invalid")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise CertificationBudgetError("budget ledger must be a regular file")
    if current.st_nlink != 1:
        raise CertificationBudgetError("budget ledger link count is invalid")
    if stat.S_IMODE(current.st_mode) != 0o666:
        # Both protected containers run as uid/gid 10001. World-write remains
        # deliberate so the dedicated certification runner can create the
        # ledger before those containers enter the exclusive marker-owned
        # directory. Sticky
        # parent semantics prevent either container replacing another entry.
        raise CertificationBudgetError("budget ledger mode is invalid")


def _connect(path: Path) -> sqlite3.Connection:
    _validate_regular_ledger(path)
    try:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error:
        raise CertificationBudgetError("budget ledger unavailable") from None


def _configuration_from_environment() -> BudgetConfiguration | None:
    values = {
        _ENV_PATH: os.environ.get(_ENV_PATH),
        _ENV_RUN_ID: os.environ.get(_ENV_RUN_ID),
        _ENV_SOURCE_SHA: os.environ.get(_ENV_SOURCE_SHA),
        _ENV_PROFILE_ID: os.environ.get(_ENV_PROFILE_ID),
    }
    if all(value in (None, "") for value in values.values()):
        return None
    if any(value in (None, "") for value in values.values()):
        raise CertificationBudgetError("incomplete certification budget environment")
    return BudgetConfiguration(
        path=Path(values[_ENV_PATH]),  # type: ignore[arg-type]
        run_id=_safe_identity("run id", values[_ENV_RUN_ID], _RUN_ID_RE),
        source_sha=_safe_identity("source sha", values[_ENV_SOURCE_SHA], _SHA_RE),
        profile_id=_safe_identity("profile id", values[_ENV_PROFILE_ID], _PROFILE_RE),
    )


def protected_certification_context_active() -> bool:
    """Whether the complete strict-budget environment is active.

    Partial or malformed certification identity still raises the same
    fail-closed ``CertificationBudgetError`` as every budgeted operation.  No
    environment value is returned or exposed to callers.
    """

    return _configuration_from_environment() is not None


def protected_certification_discovery_timeout_seconds() -> float | None:
    """Return the strict discovery bound, or no override in ordinary runtime.

    A partially configured certification environment remains a fail-closed
    error through ``_configuration_from_environment``; it must never silently
    fall back to the ordinary provider-probe deadline.
    """

    if _configuration_from_environment() is None:
        return None
    return PROTECTED_CERTIFICATION_DISCOVERY_TIMEOUT_SECONDS


def protected_certification_graph_health_timeout_seconds() -> float | None:
    """Return the strict outer Graph-health bound only during certification."""

    if _configuration_from_environment() is None:
        return None
    return PROTECTED_CERTIFICATION_GRAPH_HEALTH_TIMEOUT_SECONDS


def protected_certification_model_discovery(
    *,
    role: str,
    provider_id: str,
    endpoint: str,
    configured_model: str,
) -> str | None:
    """Resolve one identity-bound named-profile discovery contract.

    Ordinary runtime returns ``None`` and keeps the adapter-owned probe path.
    Strict certification validates the complete ledger identity and the exact
    frozen endpoint before returning ``available`` or ``unsupported``.  This
    selector never turns a runtime timeout into unsupported discovery.
    """

    configuration = _configuration_from_environment()
    if configuration is None:
        return None
    if role not in ("chat", "embedding"):
        raise CertificationBudgetError("invalid budget role")
    try:
        # Lazy import avoids the adapter -> budget -> registry import cycle.
        from .reference_profiles import reference_profile

        definition = reference_profile(configuration.profile_id)
        if role == "chat":
            expected = (
                definition.chat_provider_id,
                definition.chat_endpoint,
                definition.chat_model,
                definition.chat_model_discovery,
            )
        else:
            expected = (
                definition.embedding_provider_id,
                definition.embedding_endpoint,
                definition.embedding_model,
                definition.embedding_model_discovery,
            )
    except (ImportError, LookupError, TypeError, ValueError):
        raise CertificationBudgetError(
            "certification discovery profile is invalid"
        ) from None
    if (
        type(provider_id) is not str
        or type(endpoint) is not str
        or type(configured_model) is not str
        or (provider_id, endpoint, configured_model) != expected[:3]
    ):
        raise CertificationBudgetError(
            "certification discovery identity mismatch"
        )

    connection = _connect(configuration.path)
    try:
        _load_and_validate_metadata(
            connection,
            configuration,
            role=role,
            provider_id=provider_id,
            configured_model=configured_model,
            require_open=True,
        )
    finally:
        connection.close()
    discovery = expected[3]
    if discovery not in ("available", "unsupported"):
        raise CertificationBudgetError(
            "certification discovery profile is invalid"
        )
    return discovery


def initialize_budget_ledger(
    path: Path,
    *,
    run_id: str,
    source_sha: str,
    profile_id: str,
    chat_provider_id: str,
    chat_model: str,
    embedding_provider_id: str,
    embedding_model: str,
) -> BudgetConfiguration:
    """Exclusively create one empty marker-owned budget ledger."""

    run_id = _safe_identity("run id", run_id, _RUN_ID_RE)
    source_sha = _safe_identity("source sha", source_sha, _SHA_RE)
    profile_id = _safe_identity("profile id", profile_id, _PROFILE_RE)
    identities = {
        "chat_provider_id": _safe_identity(
            "chat provider", chat_provider_id, _IDENTITY_RE
        ),
        "chat_model": _safe_identity("chat model", chat_model, _IDENTITY_RE),
        "embedding_provider_id": _safe_identity(
            "embedding provider", embedding_provider_id, _IDENTITY_RE
        ),
        "embedding_model": _safe_identity(
            "embedding model", embedding_model, _IDENTITY_RE
        ),
    }
    if not path.is_absolute() or path.name != "budget.sqlite3":
        raise CertificationBudgetError("budget path is not canonical")
    try:
        path.parent.mkdir(mode=0o1777, parents=False, exist_ok=False)
        os.chmod(path.parent, 0o1777)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
        os.close(descriptor)
        os.chmod(path, 0o666)
    except OSError:
        raise CertificationBudgetError("budget ledger create failed") from None

    connection = _connect(path)
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_sha TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                chat_provider_id TEXT NOT NULL,
                chat_model TEXT NOT NULL,
                embedding_provider_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                sealed INTEGER NOT NULL CHECK (sealed IN (0, 1)),
                violated INTEGER NOT NULL CHECK (violated IN (0, 1))
            );
            CREATE TABLE totals (
                role TEXT PRIMARY KEY CHECK (role IN ('chat', 'embedding')),
                request_limit INTEGER NOT NULL,
                input_token_limit INTEGER NOT NULL,
                output_token_limit INTEGER,
                request_count INTEGER NOT NULL,
                input_tokens_upper_bound INTEGER NOT NULL,
                output_tokens_upper_bound INTEGER NOT NULL,
                reported_input_tokens INTEGER NOT NULL,
                reported_output_tokens INTEGER NOT NULL,
                usage_reported_requests INTEGER NOT NULL
            );
            CREATE TABLE attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL REFERENCES totals(role),
                state TEXT NOT NULL CHECK (state IN ('reserved', 'completed')),
                input_tokens_upper_bound INTEGER NOT NULL,
                output_token_reservation INTEGER NOT NULL,
                reported_input_tokens INTEGER,
                reported_output_tokens INTEGER
            );
            """
        )
        connection.execute(
            """
            INSERT INTO metadata VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                _SCHEMA_VERSION,
                run_id,
                source_sha,
                profile_id,
                identities["chat_provider_id"],
                identities["chat_model"],
                identities["embedding_provider_id"],
                identities["embedding_model"],
            ),
        )
        connection.executemany(
            "INSERT INTO totals VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0)",
            (
                (
                    "chat",
                    CHAT_REQUEST_LIMIT,
                    CHAT_INPUT_TOKEN_LIMIT,
                    CHAT_OUTPUT_TOKEN_LIMIT,
                ),
                (
                    "embedding",
                    EMBEDDING_REQUEST_LIMIT,
                    EMBEDDING_INPUT_TOKEN_LIMIT,
                    None,
                ),
            ),
        )
        connection.execute("COMMIT")
    except sqlite3.Error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise CertificationBudgetError("budget ledger initialization failed") from None
    finally:
        connection.close()
    return BudgetConfiguration(path, run_id, source_sha, profile_id)


def activate_budget_environment(configuration: BudgetConfiguration) -> None:
    """Activate strict accounting for direct runner adapters."""

    os.environ[_ENV_PATH] = str(configuration.path)
    os.environ[_ENV_RUN_ID] = configuration.run_id
    os.environ[_ENV_SOURCE_SHA] = configuration.source_sha
    os.environ[_ENV_PROFILE_ID] = configuration.profile_id


def clear_budget_environment() -> None:
    for name in (_ENV_PATH, _ENV_RUN_ID, _ENV_SOURCE_SHA, _ENV_PROFILE_ID):
        os.environ.pop(name, None)


def certification_retry_policy(requested: str) -> str:
    """Disable adapter retries whenever the strict certification ledger is active.

    The protected journey has a fixed request/output inventory. A retry remains
    safe in ordinary runtime, but certification accounts one deliberately
    zero-retry attempt per consumer operation and fails the run on ambiguity.
    Partial strict-mode environment is itself a value-free refusal.
    """

    if requested not in ("bounded", "none"):
        raise CertificationBudgetError("invalid certification retry policy")
    return "none" if _configuration_from_environment() is not None else requested


def budget_environment_values(
    configuration: BudgetConfiguration, *, container_path: str
) -> dict[str, str]:
    if not container_path.startswith("/run/hivemind-provider-certification/"):
        raise CertificationBudgetError("container budget path is not canonical")
    return {
        _ENV_PATH: container_path,
        _ENV_RUN_ID: configuration.run_id,
        _ENV_SOURCE_SHA: configuration.source_sha,
        _ENV_PROFILE_ID: configuration.profile_id,
    }


def _validated_role_totals(
    row: sqlite3.Row, *, allow_reported_overrun: bool = False
) -> dict[str, object]:
    try:
        if set(row.keys()) != _TOTAL_COLUMNS:
            raise ValueError
        role = row["role"]
        if role not in ("chat", "embedding"):
            raise ValueError
        integer_fields = (
            "request_limit",
            "input_token_limit",
            "request_count",
            "input_tokens_upper_bound",
            "output_tokens_upper_bound",
            "reported_input_tokens",
            "reported_output_tokens",
            "usage_reported_requests",
        )
        if any(
            type(row[field]) is not int or row[field] < 0 for field in integer_fields
        ):
            raise ValueError
        expected_limits = {
            "chat": (
                CHAT_REQUEST_LIMIT,
                CHAT_INPUT_TOKEN_LIMIT,
                CHAT_OUTPUT_TOKEN_LIMIT,
            ),
            "embedding": (
                EMBEDDING_REQUEST_LIMIT,
                EMBEDDING_INPUT_TOKEN_LIMIT,
                None,
            ),
        }
        if (
            (
                row["request_limit"],
                row["input_token_limit"],
                row["output_token_limit"],
            )
            != expected_limits[role]
            or row["request_count"] > row["request_limit"]
            or row["input_tokens_upper_bound"] > row["input_token_limit"]
            or row["usage_reported_requests"] > row["request_count"]
            or (
                row["usage_reported_requests"] == 0
                and (
                    row["reported_input_tokens"] != 0
                    or row["reported_output_tokens"] != 0
                )
            )
            or (
                role == "embedding"
                and (
                    row["output_tokens_upper_bound"] != 0
                    or row["reported_output_tokens"] != 0
                )
            )
            or (
                role == "chat"
                and row["output_tokens_upper_bound"] > row["output_token_limit"]
            )
        ):
            raise ValueError
        reported_slack = (
            row["usage_reported_requests"] if allow_reported_overrun else 0
        )
        if (
            row["reported_input_tokens"]
            > row["input_tokens_upper_bound"] + reported_slack
            or (
                role == "chat"
                and row["reported_output_tokens"]
                > row["output_tokens_upper_bound"] + reported_slack
            )
        ):
            raise ValueError
        return {field: row[field] for field in _TOTAL_COLUMNS}
    except (LookupError, TypeError, ValueError, OverflowError):
        raise CertificationBudgetError("budget role totals are invalid") from None


def _role_snapshot(row: sqlite3.Row) -> RoleBudgetSnapshot:
    values = _validated_role_totals(row)
    try:
        role = values["role"]
        if role not in ("chat", "embedding"):
            raise ValueError
        usage_reported = row["usage_reported_requests"] > 0
        return RoleBudgetSnapshot(
            role=role,
            request_limit=values["request_limit"],
            input_token_limit=values["input_token_limit"],
            output_token_limit=values["output_token_limit"],
            request_count=values["request_count"],
            input_tokens_upper_bound=values["input_tokens_upper_bound"],
            output_tokens_upper_bound=(
                values["output_tokens_upper_bound"] if role == "chat" else None
            ),
            reported_input_tokens=(
                values["reported_input_tokens"] if usage_reported else None
            ),
            reported_output_tokens=(
                values["reported_output_tokens"]
                if role == "chat" and usage_reported
                else None
            ),
            usage_reported_requests=values["usage_reported_requests"],
        )
    except (LookupError, TypeError, ValueError, OverflowError):
        raise CertificationBudgetError("budget role totals are invalid") from None


def _load_and_validate_metadata(
    connection: sqlite3.Connection,
    configuration: BudgetConfiguration,
    *,
    role: str,
    provider_id: str,
    configured_model: str,
    require_open: bool,
    allow_violated: bool = False,
) -> sqlite3.Row:
    if role not in ("chat", "embedding"):
        raise CertificationBudgetError("invalid budget role")
    provider_id = _safe_identity("provider", provider_id, _IDENTITY_RE)
    configured_model = _safe_identity("model", configured_model, _IDENTITY_RE)
    metadata = connection.execute("SELECT * FROM metadata WHERE singleton = 1").fetchone()
    if metadata is None or set(metadata.keys()) != _METADATA_COLUMNS:
        raise CertificationBudgetError("budget metadata missing")
    try:
        identity_fields = (
            "chat_provider_id",
            "chat_model",
            "embedding_provider_id",
            "embedding_model",
        )
        invalid = (
            type(metadata["singleton"]) is not int
            or metadata["singleton"] != 1
            or type(metadata["sealed"]) is not int
            or type(metadata["violated"]) is not int
            or metadata["sealed"] not in (0, 1)
            or metadata["violated"] not in (0, 1)
            or metadata["schema_version"] != _SCHEMA_VERSION
            or metadata["run_id"] != configuration.run_id
            or metadata["source_sha"] != configuration.source_sha
            or metadata["profile_id"] != configuration.profile_id
            or any(
                type(metadata[field]) is not str
                or _IDENTITY_RE.fullmatch(metadata[field]) is None
                for field in identity_fields
            )
            or metadata[f"{role}_provider_id"] != provider_id
            or metadata[f"{role}_model"] != configured_model
            or (not allow_violated and metadata["violated"] != 0)
            or (require_open and metadata["sealed"] != 0)
        )
    except (LookupError, TypeError, ValueError, OverflowError):
        raise CertificationBudgetError("budget metadata mismatch") from None
    if invalid:
        raise CertificationBudgetError("budget metadata mismatch")
    return metadata


def reserve_request_from_environment(
    *,
    role: str,
    provider_id: str,
    configured_model: str,
    method: str,
    path: str,
    json_body: Mapping[str, object] | None,
    output_token_reservation: int = 0,
) -> BudgetReservation | None:
    """Reserve one exact attempt, or return ``None`` outside strict mode."""

    configuration = _configuration_from_environment()
    if configuration is None:
        return None
    if role not in ("chat", "embedding"):
        raise CertificationBudgetError("invalid budget role")
    allowed = {
        ("GET", "/models"),
        ("POST", "/chat/completions"),
        ("POST", "/embeddings"),
    }
    if (method, path) not in allowed:
        raise CertificationBudgetError("request path is outside certification inventory")
    if path == "/chat/completions" and role != "chat":
        raise CertificationBudgetError("chat path requires chat role")
    if path == "/embeddings" and role != "embedding":
        raise CertificationBudgetError("embedding path requires embedding role")
    if method == "GET":
        if json_body is not None or output_token_reservation != 0:
            raise CertificationBudgetError("discovery budget shape is invalid")
        input_upper = 0
    else:
        if not isinstance(json_body, Mapping):
            raise CertificationBudgetError("inference budget body is missing")
        try:
            encoded = json.dumps(
                dict(json_body),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise CertificationBudgetError("inference budget body is invalid") from None
        input_upper = len(encoded) + _TOKEN_FRAMING_OVERHEAD
    if (
        isinstance(output_token_reservation, bool)
        or not isinstance(output_token_reservation, int)
        or output_token_reservation < 0
        or (role == "embedding" and output_token_reservation != 0)
        or (path == "/chat/completions" and output_token_reservation < 1)
    ):
        raise CertificationBudgetError("output token reservation is invalid")

    connection = _connect(configuration.path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _load_and_validate_metadata(
            connection,
            configuration,
            role=role,
            provider_id=provider_id,
            configured_model=configured_model,
            require_open=True,
        )
        totals = connection.execute(
            "SELECT * FROM totals WHERE role = ?", (role,)
        ).fetchone()
        if totals is None:
            raise CertificationBudgetError("budget role totals missing")
        snapshot = _role_snapshot(totals)
        if snapshot.role != role:
            raise CertificationBudgetError("budget role totals mismatch")
        next_requests = snapshot.request_count + 1
        next_input = snapshot.input_tokens_upper_bound + input_upper
        next_output = (snapshot.output_tokens_upper_bound or 0) + output_token_reservation
        if (
            next_requests > snapshot.request_limit
            or next_input > snapshot.input_token_limit
            or (
                snapshot.output_token_limit is not None
                and next_output > snapshot.output_token_limit
            )
        ):
            # A refused attempt must remain visible even when a consumer catches
            # the exception. Poison and commit inside the reservation lock before
            # raising; all later reservations and sealing reject ``violated``.
            cursor = connection.execute(
                "UPDATE metadata SET violated = 1 WHERE singleton = 1"
            )
            if cursor.rowcount != 1:
                raise CertificationBudgetError("budget metadata update failed")
            connection.execute("COMMIT")
            raise CertificationBudgetError("certification technical ceiling exceeded")
        cursor = connection.execute(
            """
            INSERT INTO attempts (
                role, state, input_tokens_upper_bound, output_token_reservation,
                reported_input_tokens, reported_output_tokens
            ) VALUES (?, 'reserved', ?, ?, NULL, NULL)
            """,
            (role, input_upper, output_token_reservation),
        )
        update = connection.execute(
            """
            UPDATE totals
            SET request_count = ?, input_tokens_upper_bound = ?,
                output_tokens_upper_bound = ?
            WHERE role = ?
            """,
            (next_requests, next_input, next_output, role),
        )
        if update.rowcount != 1:
            raise CertificationBudgetError("budget totals update failed")
        connection.execute("COMMIT")
        return BudgetReservation(
            path=configuration.path,
            attempt_id=int(cursor.lastrowid),
            role=role,
            input_token_upper_bound=input_upper,
            output_token_reservation=output_token_reservation,
            run_id=configuration.run_id,
            source_sha=configuration.source_sha,
            profile_id=configuration.profile_id,
            provider_id=_safe_identity("provider", provider_id, _IDENTITY_RE),
            configured_model=_safe_identity("model", configured_model, _IDENTITY_RE),
        )
    except CertificationBudgetError:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (sqlite3.Error, LookupError, TypeError, ValueError, OverflowError):
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise CertificationBudgetError("budget reservation failed") from None
    finally:
        connection.close()


def complete_budget_reservation(
    reservation: BudgetReservation | None,
    *,
    reported_input_tokens: int | None,
    reported_output_tokens: int | None,
) -> None:
    """Settle one reservation while retaining its conservative debit."""

    if reservation is None:
        return
    if (
        type(reservation) is not BudgetReservation
        or not isinstance(reservation.path, Path)
        or not reservation.path.is_absolute()
        or reservation.role not in ("chat", "embedding")
        or type(reservation.attempt_id) is not int
        or reservation.attempt_id < 1
        or type(reservation.input_token_upper_bound) is not int
        or reservation.input_token_upper_bound < 0
        or type(reservation.output_token_reservation) is not int
        or reservation.output_token_reservation < 0
        or (
            reservation.role == "embedding"
            and reservation.output_token_reservation != 0
        )
    ):
        raise CertificationBudgetError("budget reservation identity is invalid")
    values = (reported_input_tokens, reported_output_tokens)
    if any(
        value is not None
        and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        )
        for value in values
    ):
        raise CertificationBudgetError("reported usage is invalid")
    if reservation.role == "embedding" and reported_output_tokens is not None:
        raise CertificationBudgetError("embedding usage cannot report output tokens")
    configuration = BudgetConfiguration(
        path=reservation.path,
        run_id=_safe_identity("run id", reservation.run_id, _RUN_ID_RE),
        source_sha=_safe_identity("source sha", reservation.source_sha, _SHA_RE),
        profile_id=_safe_identity("profile id", reservation.profile_id, _PROFILE_RE),
    )
    connection = _connect(reservation.path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        metadata = _load_and_validate_metadata(
            connection,
            configuration,
            role=reservation.role,
            provider_id=reservation.provider_id,
            configured_model=reservation.configured_model,
            require_open=True,
            allow_violated=True,
        )
        already_violated = metadata["violated"] == 1
        attempt = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (reservation.attempt_id,)
        ).fetchone()
        if (
            attempt is None
            or set(attempt.keys()) != _ATTEMPT_COLUMNS
            or type(attempt["attempt_id"]) is not int
            or attempt["attempt_id"] != reservation.attempt_id
            or attempt["state"] != "reserved"
            or attempt["role"] != reservation.role
            or type(attempt["input_tokens_upper_bound"]) is not int
            or type(attempt["output_token_reservation"]) is not int
            or attempt["input_tokens_upper_bound"]
            != reservation.input_token_upper_bound
            or attempt["output_token_reservation"]
            != reservation.output_token_reservation
            or attempt["reported_input_tokens"] is not None
            or attempt["reported_output_tokens"] is not None
        ):
            raise CertificationBudgetError("budget reservation state mismatch")
        totals = connection.execute(
            "SELECT * FROM totals WHERE role = ?", (reservation.role,)
        ).fetchone()
        if totals is None:
            raise CertificationBudgetError("budget role totals mismatch")
        totals_values = _validated_role_totals(
            totals, allow_reported_overrun=already_violated
        )
        if totals_values["role"] != reservation.role:
            raise CertificationBudgetError("budget role totals mismatch")
        usage_reported = (
            reported_input_tokens is not None or reported_output_tokens is not None
        )
        violated = (
            reported_input_tokens is not None
            and reported_input_tokens > reservation.input_token_upper_bound
        ) or (
            reported_output_tokens is not None
            and reported_output_tokens > reservation.output_token_reservation
        )
        # A poisoned ledger is never certifiable, so retain only a bounded
        # overrun sentinel. This preserves the durable violation while keeping
        # SQLite arithmetic bounded and allowing every already-issued attempt to
        # settle in any completion order.
        stored_input = (
            None
            if reported_input_tokens is None
            else min(
                reported_input_tokens,
                reservation.input_token_upper_bound + 1,
            )
        )
        stored_output = (
            None
            if reported_output_tokens is None
            else min(
                reported_output_tokens,
                reservation.output_token_reservation + 1,
            )
        )
        attempt_update = connection.execute(
            """
            UPDATE attempts SET state = 'completed', reported_input_tokens = ?,
                reported_output_tokens = ? WHERE attempt_id = ?
            """,
            (stored_input, stored_output, reservation.attempt_id),
        )
        totals_update = connection.execute(
            """
            UPDATE totals SET
                reported_input_tokens = reported_input_tokens + ?,
                reported_output_tokens = reported_output_tokens + ?,
                usage_reported_requests = usage_reported_requests + ?
            WHERE role = ?
            """,
            (
                stored_input or 0,
                stored_output or 0,
                1 if usage_reported else 0,
                reservation.role,
            ),
        )
        if attempt_update.rowcount != 1 or totals_update.rowcount != 1:
            raise CertificationBudgetError("budget settlement update failed")
        if violated:
            poison = connection.execute(
                "UPDATE metadata SET violated = 1 WHERE singleton = 1"
            )
            if poison.rowcount != 1:
                raise CertificationBudgetError("budget metadata update failed")
        settled_totals = connection.execute(
            "SELECT * FROM totals WHERE role = ?", (reservation.role,)
        ).fetchone()
        if settled_totals is None:
            raise CertificationBudgetError("settled budget totals are invalid")
        settled_values = _validated_role_totals(
            settled_totals,
            allow_reported_overrun=already_violated or violated,
        )
        if settled_values["role"] != reservation.role:
            raise CertificationBudgetError("settled budget totals are invalid")
        connection.execute("COMMIT")
        if violated:
            raise CertificationBudgetError("reported usage exceeds reservation")
    except CertificationBudgetError:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (sqlite3.Error, LookupError, TypeError, ValueError, OverflowError):
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise CertificationBudgetError("budget settlement failed") from None
    finally:
        connection.close()


def seal_budget_ledger(
    configuration: BudgetConfiguration,
    *,
    chat_provider_id: str,
    chat_model: str,
    embedding_provider_id: str,
    embedding_model: str,
) -> CertificationBudgetSnapshot:
    """Atomically prohibit later egress and return the exact safe totals."""

    connection = _connect(configuration.path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        metadata = _load_and_validate_metadata(
            connection,
            configuration,
            role="chat",
            provider_id=chat_provider_id,
            configured_model=chat_model,
            require_open=True,
        )
        _load_and_validate_metadata(
            connection,
            configuration,
            role="embedding",
            provider_id=embedding_provider_id,
            configured_model=embedding_model,
            require_open=True,
        )
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE state != 'completed'"
        ).fetchone()
        rows = connection.execute("SELECT * FROM totals ORDER BY role").fetchall()
        if (
            pending is None
            or set(pending.keys()) != {"count"}
            or type(pending["count"]) is not int
            or pending["count"] != 0
            or len(rows) != 2
        ):
            raise CertificationBudgetError("budget ledger has unsettled attempts")
        role_snapshots = tuple(_role_snapshot(row) for row in rows)
        by_role = {snapshot.role: snapshot for snapshot in role_snapshots}
        if set(by_role) != {"chat", "embedding"}:
            raise CertificationBudgetError("budget role totals are incomplete")
        for role_snapshot in role_snapshots:
            if (
                role_snapshot.request_count < 1
                or role_snapshot.input_tokens_upper_bound < 1
            ):
                raise CertificationBudgetError("budget totals violate technical ceilings")
        try:
            snapshot = CertificationBudgetSnapshot(
                schema_version=metadata["schema_version"],
                run_id=metadata["run_id"],
                source_sha=metadata["source_sha"],
                profile_id=metadata["profile_id"],
                chat=by_role["chat"],
                embedding=by_role["embedding"],
                sealed=True,
            )
        except (LookupError, TypeError, ValueError, OverflowError):
            raise CertificationBudgetError("budget snapshot is invalid") from None
        update = connection.execute(
            "UPDATE metadata SET sealed = 1 WHERE singleton = 1 AND sealed = 0"
        )
        if update.rowcount != 1:
            raise CertificationBudgetError("budget seal update failed")
        connection.execute("COMMIT")
        return snapshot
    except CertificationBudgetError:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (sqlite3.Error, LookupError, TypeError, ValueError, OverflowError):
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise CertificationBudgetError("budget seal failed") from None
    finally:
        connection.close()
