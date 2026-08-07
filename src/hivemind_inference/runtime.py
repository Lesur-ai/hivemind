# -*- coding: utf-8 -*-
"""Per-process inference runtime holder (P13-1C / #276, ADR-0027).

Each consuming service (the Hivemind core, the embedded Graph Memory runtime)
keeps ONE holder per process. It snapshots the resolved configuration exactly
once, owns the adapter instances and their outbound transports, and closes them
on ASGI shutdown.

Two properties this module exists to guarantee:

- **Immutable profiles.** Environment changes after resolution do not mutate a
  running provider; a new process (or, under test, an explicit reset) is
  required. Both consumers therefore observe the SAME role profiles for the
  whole life of the process instead of each re-reading a drifting view.
- **One transport owner.** Adapters build the owned ``httpx`` transport
  (``PROXY_URL`` honored, ambient proxy variables never trusted — the P12-3
  egress contract is unchanged). Only :meth:`InferenceRuntime.aclose` closes
  them, so an in-flight cancellation can never tear down the shared transport.

Import discipline: this module stays import-light. ``registry`` (and through it
the adapters, ``httpx``, and any provider SDK) is imported lazily inside each
factory, so importing it from an auth/storage module costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping

from .config import InferenceConfig, merged_environment, resolve_inference_config
from .egress import display_proxy_url

_logger = logging.getLogger("hivemind_inference.runtime")

_ROLES = ("chat", "embedding")

# The four owned adapter slots, in close order.
_ADAPTER_SLOTS = (
    "_chat_provider",
    "_embedding_provider",
    "_chat_probe",
    "_embedding_probe",
)

# Strong references to close tasks started by :meth:`InferenceRuntime.aclose`.
# A close that has BEGUN must run to completion even if the coroutine awaiting
# it is cancelled, and without a strong reference the loop may collect the task
# before it finishes. Retrieving the result in the done callback also keeps a
# failed close from surfacing as an "exception was never retrieved" warning —
# the failure is reported through ``aclose`` itself.
_PENDING_CLOSES: set = set()

# Wall-clock budget for ONE slot's close. A provider transport that never
# returns from ``aclose()`` used to wedge shutdown forever AND swallow the
# caller's own ``wait_for`` timeout, because the drain absorbed that
# cancellation and then blocked again with nothing further arriving. Neither
# ASGI hook bounds the lifespan, so the container could only be ended by
# SIGKILL. The budget makes shutdown TERMINATE; the wedged slot is retained and
# named in :class:`InferenceShutdownIncomplete` rather than abandoned.
_CLOSE_BUDGET_SECONDS = 10.0


def _release_close_task(task: "asyncio.Task") -> None:
    _PENDING_CLOSES.discard(task)
    if not task.cancelled():
        task.exception()


class InferenceRoleUnavailable(RuntimeError):
    """A configured-role operation was requested for an ABSENT role.

    This is deliberately NOT an :class:`~hivemind_inference.InferenceError`:
    that envelope binds to a registered provider/adapter pair, and an absent
    role has neither. It is also not an
    :class:`~hivemind_inference.InferenceConfigError`: the configuration is
    valid — the role simply is not configured, which ADR-0027 makes a valid
    startup whose OPERATIONS fail closed at call time.

    Callers normally check ``runtime.config.<role> is None`` first and return
    their own operator-facing error; this exception is the fail-closed backstop
    for a caller that does not. Its message carries only the role name.
    """

    __slots__ = ("role",)

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"inference role '{role}' is not configured — set the "
            f"INFERENCE_{role.upper()}_* family (or the legacy complete "
            "LLMAAS_API_URL + LLMAAS_API_KEY pair)"
        )


class InferenceRuntimeClosed(RuntimeError):
    """The runtime has begun (or finished) shutting down.

    Shutdown is TERMINAL. Without this state a background task still running
    when the ASGI close hook fires would simply build a fresh adapter — and its
    transport would be owned by nobody, because the only close hook has already
    run. Failing closed converts that silent leak into an explicit, honest
    operation failure.
    """


class InferenceShutdownIncomplete(RuntimeError):
    """:meth:`InferenceRuntime.aclose` drained without confirming every close.

    Raised when a slot's close did not settle inside its budget, or settled as
    CANCELLED — neither of which is evidence that the transport is closed. The
    named slots keep their adapter and their in-flight task, so the owner may
    retry; what this exception forbids is the owner concluding "closed" and
    dropping its last reference.
    """

    __slots__ = ("slots",)

    def __init__(self, slots) -> None:
        self.slots = tuple(slots)
        super().__init__(
            "inference shutdown incomplete — transport(s) not confirmed "
            "closed: " + ", ".join(self.slots)
        )


class InferenceRuntime:
    """Immutable-profile runtime: one resolved config + lazily built adapters."""

    __slots__ = (
        "_config",
        "_proxy_url",
        "_closing",
        "_close_tasks",
        "_unconfirmed",
        "_chat_provider",
        "_embedding_provider",
        "_chat_probe",
        "_embedding_probe",
    )

    def __init__(self, config: InferenceConfig, *, proxy_url: str | None = None) -> None:
        self._config = config
        self._proxy_url = proxy_url
        self._closing = False
        # slot name -> the single in-flight close task for that slot.
        self._close_tasks: dict[str, "asyncio.Task"] = {}
        # Slots whose close RAISED or was CANCELLED. Terminal: an adapter's
        # close is not idempotent evidence. `httpx.AsyncClient.aclose()` sets
        # its state to CLOSED *before* awaiting the transport, so once a close
        # has failed or been cancelled part-way, a second call returns
        # instantly without touching the connection pool. Retrying would
        # therefore report a clean success over a transport that is still
        # open, and the owner would drop its last reference to it. The honest
        # answer is that this slot can no longer be confirmed by anyone.
        self._unconfirmed: set[str] = set()
        if proxy_url and config.configured_roles:
            # P12-3 operator signal preserved through the shared boundary. The
            # proxy endpoint is sensitive configuration even without embedded
            # credentials (ADR-0027), so only the redacted rendering is logged.
            _logger.info("inference egress via proxy %s", display_proxy_url(proxy_url))
        self._chat_provider = None
        self._embedding_provider = None
        self._chat_probe = None
        self._embedding_probe = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: str | None = ".env",
        proxy_url: str | None = None,
    ) -> "InferenceRuntime":
        """Resolve from an explicit mapping (tests) or the merged process
        environment (services).

        Raises :class:`~hivemind_inference.InferenceConfigError` fail-closed —
        before any network access — on a partial role, a mixed
        ``LLMAAS_*``/``INFERENCE_*`` family, an unknown provider, or an invalid
        value.
        """
        if environ is None:
            environ = merged_environment(env_file)
        return cls(resolve_inference_config(environ), proxy_url=proxy_url)

    @property
    def config(self) -> InferenceConfig:
        return self._config

    @property
    def is_closing(self) -> bool:
        """True once :meth:`aclose` has begun (terminal — never resets)."""
        return self._closing

    @property
    def is_fully_closed(self) -> bool:
        """Every owned transport is CONFIRMED released — the owner may now drop
        its last reference to this runtime.

        This is the single authority on that question, deliberately DERIVED
        from the slots rather than recorded in a flag. A flag has to be set on
        every path, and the previous design decided the outcome from three
        independent locals whose branches each fell through to "closed": that
        shape is what turned one ownership hole into four separate review
        findings. A predicate cannot forget a branch.

        Note it is about transport ownership, NOT about whether :meth:`aclose`
        was called: a runtime that never built an adapter owns nothing and is
        therefore already fully closed. Conversely a slot whose close raised or
        was cancelled stays unconfirmed FOREVER — no later call can produce the
        evidence, so none may claim it.
        """
        return (
            not self._close_tasks
            and not self._unconfirmed
            and all(getattr(self, slot) is None for slot in _ADAPTER_SLOTS)
        )

    def _assert_open(self) -> None:
        """Refuse to build an adapter once shutdown has begun.

        Checked BEFORE the role check so a shutting-down runtime never
        constructs a transport nothing will close.
        """
        if self._closing:
            raise InferenceRuntimeClosed(
                "the inference runtime is shutting down; no new provider "
                "transport may be built"
            )

    # -- role adapters -------------------------------------------------------

    def chat_provider(self):
        """The chat adapter (built once), or ``InferenceRoleUnavailable``."""
        self._assert_open()
        if self._config.chat is None:
            raise InferenceRoleUnavailable("chat")
        if self._chat_provider is None:
            from .registry import build_chat_provider

            self._chat_provider = build_chat_provider(
                self._config.chat, proxy_url=self._proxy_url
            )
        return self._chat_provider

    def embedding_provider(self):
        """The embedding adapter (built once), or ``InferenceRoleUnavailable``."""
        self._assert_open()
        if self._config.embedding is None:
            raise InferenceRoleUnavailable("embedding")
        if self._embedding_provider is None:
            from .registry import build_embedding_provider

            self._embedding_provider = build_embedding_provider(
                self._config.embedding, proxy_url=self._proxy_url
            )
        return self._embedding_provider

    def chat_probe(self):
        """The chat discovery probe (built once), or ``InferenceRoleUnavailable``."""
        self._assert_open()
        if self._config.chat is None:
            raise InferenceRoleUnavailable("chat")
        if self._chat_probe is None:
            from .registry import build_chat_probe

            self._chat_probe = build_chat_probe(
                self._config.chat, proxy_url=self._proxy_url
            )
        return self._chat_probe

    def embedding_probe(self):
        """The embedding discovery probe (built once), or ``InferenceRoleUnavailable``."""
        self._assert_open()
        if self._config.embedding is None:
            raise InferenceRoleUnavailable("embedding")
        if self._embedding_probe is None:
            from .registry import build_embedding_probe

            self._embedding_probe = build_embedding_probe(
                self._config.embedding, proxy_url=self._proxy_url
            )
        return self._embedding_probe

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close EVERY owned adapter transport — idempotent and
        cancellation-safe.

        Shutdown is finalization, so each slot's close is modelled as a small
        LIFECYCLE rather than as one pass of a loop. Treating it as a loop is
        what produced two rounds of leaks: every branch fixed in isolation left
        the ownership question ("who may clear this slot, and when?")
        implicit. The invariants are now explicit:

        - **At most one close is ever in flight per slot.** The task is stored
          against its slot, so a retry that arrives while a close is still
          running AWAITS THAT TASK instead of invoking ``adapter.aclose()`` a
          second time concurrently.
        - **Every started close is awaited to completion before this returns.**
          A cancellation is recorded and the drain continues, so the caller
          cannot return into an event-loop teardown that would kill a close
          still in flight. Only once every task has settled is the cancellation
          re-raised.
        - **A slot is cleared only on CONFIRMED success**, and a close that
          RAISED or was CANCELLED makes its slot permanently unconfirmed. It is
          tempting to retain the adapter "so a later call can finish the job",
          and that is what this method used to promise — but the promise was
          empty: ``httpx.AsyncClient.aclose()`` marks itself closed before it
          awaits the transport, so the second call is a no-op that would report
          success over a still-open connection pool. A close that never settled
          is different: that task is still running, so it is resumed rather
          than restarted.
        - **The outcome is read from** :attr:`is_fully_closed`, **never from
          how this returns.** Anything unconfirmed does raise — a failure, a
          cancellation, or :class:`InferenceShutdownIncomplete` naming the
          slots — but the converse is deliberately NOT claimed, in either
          direction. A cancellation delivered mid-drain is re-raised even when
          every close then completed cleanly (swallowing it because the outcome
          happened to be clean is the exact shape the earlier leaks came from),
          and with two concurrent callers the one arriving after a sibling has
          already drained a slot can return normally while another slot is
          retained. Neither is a defect, and neither can mislead the owner,
          because the owner keys its decision off the predicate.
        - **The drain terminates.** Each slot gets a wall-clock budget, so an
          adapter whose ``aclose()`` never returns leaves shutdown with a named
          incomplete result instead of wedging the process. The bound is a
          wall-clock DEADLINE per slot, so a cancel scope that re-delivers on
          every loop iteration cannot burn the allowance without any waiting
          actually happening.

        Idempotent: a slot already cleared is skipped, so a second call closes
        nothing twice.
        """
        # Terminal from the FIRST instruction: a concurrent caller must not be
        # able to build a new transport while this one is tearing them down.
        self._closing = True

        # Phase 1 — ensure exactly one close task per populated slot, reusing
        # any close already in flight from an earlier (cancelled) call. A slot
        # already known UNCONFIRMED is never re-attempted: see the class
        # docstring of :class:`InferenceShutdownIncomplete` and the comment on
        # ``_unconfirmed`` below.
        for attribute in _ADAPTER_SLOTS:
            adapter = getattr(self, attribute)
            if adapter is None or attribute in self._close_tasks:
                continue
            if attribute in self._unconfirmed:
                continue
            task = asyncio.ensure_future(adapter.aclose())
            _PENDING_CLOSES.add(task)
            task.add_done_callback(_release_close_task)
            self._close_tasks[attribute] = task

        # Phase 2 — drain every started close.
        first_error: BaseException | None = None
        cancelled: BaseException | None = None
        unfinished: list[str] = []
        for attribute in _ADAPTER_SLOTS:
            task = self._close_tasks.get(attribute)
            if task is None:
                continue
            # ``asyncio.wait`` is what makes this both cancellation-safe and
            # bounded. It never cancels the future it waits on, so a
            # cancellation delivered here is recorded rather than obeyed and
            # the close keeps running; and its timeout expires WITHOUT
            # injecting a cancellation, unlike ``asyncio.timeout``, whose
            # injected CancelledError this loop would have to swallow — and
            # swallowing it is precisely what defeats that construct.
            # The bound is a wall-clock DEADLINE, not a round count. A round
            # count is defeated by a cancel scope that re-delivers on every
            # loop iteration: each delivery would consume a round instantly,
            # burning the whole allowance in milliseconds and abandoning a
            # close that had barely started.
            deadline = asyncio.get_running_loop().time() + _CLOSE_BUDGET_SECONDS
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({task}, timeout=remaining)
                except asyncio.CancelledError as exc:
                    # Recorded, not obeyed: the task is still running and
                    # abandoning it here is the leak. Keep draining until the
                    # deadline, then retain rather than abandon.
                    if cancelled is None:
                        cancelled = exc
            if not task.done():
                # Never settled: keep BOTH the task and the adapter so the next
                # call resumes this exact close instead of starting a new one.
                unfinished.append(attribute)
                continue
            self._close_tasks.pop(attribute, None)
            if task.cancelled():
                self._unconfirmed.add(attribute)
                unfinished.append(attribute)
                continue
            failure = task.exception()
            if failure is not None:
                if first_error is None:
                    first_error = failure
                self._unconfirmed.add(attribute)
                continue
            setattr(self, attribute, None)

        # Slots already known unconfirmed keep being reported, so a later call
        # cannot present a quiet success over them.
        for attribute in _ADAPTER_SLOTS:
            if attribute in self._unconfirmed and attribute not in unfinished:
                unfinished.append(attribute)

        if cancelled is not None:
            raise cancelled
        if first_error is not None:
            raise first_error
        if unfinished:
            raise InferenceShutdownIncomplete(unfinished)
