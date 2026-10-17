"""Prepared explicit public waits for save-then-read suites (U49.A2, C-49.contract.mcp-suites; G11, G12, G47).

A test that saves through a public seam and then reads names what it waits for: the ``stored`` or ``extracted``
milestone, or the ``terminal`` outcome, within ``deadline_s`` seconds. ``await_public(observe, wait=, deadline_s=)``
polls ``observe`` (a zero-argument read through the public seam) and answers with a ``PublicWaitResult`` whose
``outcome`` is one of ``OUTCOMES``:

- ``reached``: the milestone holds. ``unreached``: the run completed without it.
- ``client_timed_out``: still pending at the deadline (the result carries attempts, elapsed seconds and the last
  observation; nothing is cancelled).
- ``failed``/``skipped``/``dead``/``partial``: the run's explicit fault, returned on the first read that shows it.
  A fault is never retried, so no poll loop can hide it.
- ``refused``: the run is held (paused), reported with one WARNING.

A read that cannot be answered (the seam raised, returned an unreadable observation or carrier, a bad argument, or
the seam carries no status for the requested milestone) is the typed ``PublicWaitRefused`` with exactly one WARNING.
It is never read as "absent" or as a timeout.

This mirrors the vocabulary of core's local wait helper (core's own testing package) and deliberately does not import it:
outer-repo suites must not (core ``tests/contract/test_no_hidden_sync_pin.py``). No product API is added: the adapter
below reads through ``backend.get`` (``smartmemory_mcp.backends.interface.MemoryBackend.get``), whose result already
carries the G43 ``read_status`` (``extraction_state``, ``processing_stage``, ``execution_state``, ``outcome``,
``revision``) on the gate-ON arm. A result with no carrier is the legacy arm: readable means ``stored`` and nothing
more is observable.

Real-worker candidates and the rows owned by U4.42 are listed by name in ``tests/data/save_then_read_census.json``.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

# --- CORE BEGIN (byte-identical in smart-memory-client and smart-memory-mcp tests/public_wait.py) ---
logger = logging.getLogger("public_wait")

WAITS: tuple[str, ...] = ("stored", "extracted", "terminal")
OUTCOMES: tuple[str, ...] = (
    "reached",
    "client_timed_out",
    "failed",
    "skipped",
    "dead",
    "partial",
    "unreached",
    "refused",
)
FAULTS: tuple[str, ...] = ("failed", "skipped", "dead", "partial")
_STAGES = {
    "stored": ("stored", "extracted"),
    "extracted": ("extracted",),
    "terminal": (),
}
POLL_S = 0.05
MAX_POLL_S = 0.5


class PublicWaitRefused(RuntimeError):
    """The wait could not be answered. Always logged once, where it is raised."""


class PublicWaitArgumentError(PublicWaitRefused, ValueError):
    """A caller argument no wait could honour; refused before any read."""


@dataclass(frozen=True)
class Observation:
    """One read through a public seam. ``stage`` None: not readable yet (queued). ``status_carried`` False: the seam
    cannot say anything about extraction or the terminal outcome, so only ``stored`` is answerable."""

    stage: str | None = None
    terminal: bool = False
    outcome: str | None = None
    held: str | None = None
    status_carried: bool = True
    detail: str = ""


@dataclass(frozen=True)
class PublicWaitResult:
    outcome: str
    wait: str
    attempts: int
    elapsed_s: float
    last: Observation | None
    reason: str = ""


def refuse(
    reason: str, error: type[PublicWaitRefused] = PublicWaitRefused, *, log: bool = True
) -> PublicWaitRefused:
    """The typed refusal with its one WARNING. Adapters raise what this returns; ``await_public`` never logs it again.

    ``log=False`` only when the product already logged this exact refusal once (a typed error it raised); the
    refusal still carries the reason, and the caller's test pins that exactly one WARNING exists.
    """
    if log:
        logger.warning("Explicit public wait refused: %s", reason)
    return error(reason)


def describe(error: BaseException) -> str:
    """``Type: message`` for a refusal reason. Formatting a message can itself raise, so it is guarded."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 - a diagnostic never stops the refusal from being raised
        text = "<unprintable>"
    return f"{type(error).__name__}: {text[:200]}"


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def shown(value: object) -> str:
    try:
        return (
            repr(value)[:48]
            if type(value) in (str, int, float, bool, type(None))
            else type(value).__name__
        )
    except Exception:  # noqa: BLE001 - a diagnostic never stops the refusal
        return "<unprintable>"


def _check(observe: object, wait: object, deadline_s: object, poll_s: object) -> None:
    if not callable(observe):
        raise refuse(
            f"observe must be callable, got {shown(observe)}", PublicWaitArgumentError
        )
    if type(wait) is not str or wait not in WAITS:
        raise refuse(
            f"wait must be one of {WAITS}, got {shown(wait)}", PublicWaitArgumentError
        )
    if not _finite(deadline_s) or deadline_s < 0:
        raise refuse(
            f"deadline_s must be a finite number >= 0, got {shown(deadline_s)}",
            PublicWaitArgumentError,
        )
    if not _finite(poll_s) or poll_s <= 0:
        raise refuse(
            f"poll_s must be a positive finite number, got {shown(poll_s)}",
            PublicWaitArgumentError,
        )


def _decide(obs: Observation, wait: str) -> tuple[str | None, str]:
    """The outcome one observation already decides (None: pending) and its reason."""
    if type(obs) is not Observation:
        raise refuse(f"the read returned {type(obs).__name__}, not an Observation")
    if obs.held is not None:
        logger.warning(
            "Explicit public wait: the run is held (%s); reported refused, not polled further",
            shown(obs.held),
        )
        return "refused", f"held: {obs.held}"
    if wait != "stored" and not obs.status_carried:
        raise refuse(
            f"this public seam carries no extraction or terminal status, so wait={wait!r} is unobservable "
            f"(only 'stored' is); last read: {obs.detail or 'none'}"
        )
    if obs.terminal:
        if obs.outcome in FAULTS:
            return obs.outcome, f"terminal outcome {obs.outcome}"
        if obs.outcome not in ("completed", "no_op"):
            raise refuse(f"a terminal observation carries outcome {shown(obs.outcome)}")
        if wait == "terminal" or obs.stage in _STAGES[wait]:
            return "reached", obs.detail
        return (
            "unreached",
            f"completed at stage {shown(obs.stage)} without reaching {wait}",
        )
    if obs.stage in _STAGES[wait]:
        return "reached", obs.detail
    return None, obs.detail


def await_public(
    observe: Callable[[], Observation],
    *,
    wait: str,
    deadline_s: float,
    poll_s: float = POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PublicWaitResult:
    """Wait until ``observe()`` shows ``wait`` or the explicit fault, for at most ``deadline_s`` seconds.

    ``deadline_s`` 0 reads once. A read that raises is not retried: it is the typed refusal with one WARNING
    (a ``PublicWaitRefused`` an adapter raised already logged itself and is re-raised as is).
    """
    _check(observe, wait, deadline_s, poll_s)
    start = clock()
    delay = poll_s
    attempts = 0
    while True:
        attempts += 1
        try:
            obs = observe()
        except PublicWaitRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable seam is a typed refusal, never "still pending"
            raise refuse(
                f"the read through the public seam failed ({describe(exc)})"
            ) from exc
        outcome, reason = _decide(obs, wait)
        if outcome is not None:
            return PublicWaitResult(
                outcome, wait, attempts, clock() - start, obs, reason
            )
        remaining = deadline_s - (clock() - start)
        if remaining <= 0:
            return PublicWaitResult(
                "client_timed_out", wait, attempts, clock() - start, obs, obs.detail
            )
        sleep(min(delay, remaining))
        delay = min(delay * 2, MAX_POLL_S)


# --- CORE END ---


_TERMINAL = ("completed", "failed", "dead")
_ACTIVE = ("queued", "waiting", "running", "paused")
_STAGE_VALUES = ("received", "stored", "extracted")
_SUCCESS_OUTCOMES = ("completed", "no_op", "skipped", "partial")


def observation_from_row(row: object, item_id: str) -> Observation:
    """One ``backend.get`` result as an ``Observation``, mirroring core's fate rules for the G43 carrier.

    ``None`` is genuine absence (queued: not readable yet). A row with no carrier is the legacy arm: readable
    means ``stored``, and no more is observable. Active execution carries no outcome, failed and dead carry the
    matching outcome, completed carries a success outcome; any other combination is refused, never guessed.
    """
    from smartmemory_mcp.backends.models import validated_carrier

    if row is None:
        return Observation(
            detail=f"get({shown(item_id)}) returned None, not readable yet"
        )
    if not isinstance(row, dict):
        raise refuse(
            f"get({shown(item_id)}) returned {type(row).__name__}, not a MemoryResult dict"
        )
    carrier = validated_carrier(row.get("read_status"), item_id)
    if carrier is None:
        return Observation(
            stage="stored",
            status_carried=False,
            detail=f"get({shown(item_id)}) carries no read_status",
        )
    stage, state, outcome = (
        carrier.get(k) for k in ("processing_stage", "execution_state", "outcome")
    )
    if (
        stage not in _STAGE_VALUES
    ):  # a missing or null stage is as unreadable as an unknown one
        raise refuse(
            f"read_status.processing_stage is {shown(stage)}, not one of {_STAGE_VALUES}"
        )
    if state not in _TERMINAL + _ACTIVE:
        raise refuse(
            f"read_status.execution_state is {shown(state)}, not one of {_ACTIVE + _TERMINAL}"
        )
    if state in _ACTIVE and outcome is not None:
        raise refuse(
            f"read_status carries outcome {shown(outcome)} while execution_state is {state}"
        )
    if state in ("failed", "dead") and outcome != state:
        raise refuse(
            f"read_status.execution_state is {state} but outcome is {shown(outcome)}"
        )
    if state == "completed" and outcome not in _SUCCESS_OUTCOMES:
        raise refuse(
            f"read_status.execution_state is completed but outcome is {shown(outcome)}"
        )
    detail = f"get({shown(item_id)}): stage={stage}, execution_state={state}, outcome={outcome}"
    return Observation(
        stage=None if stage == "received" else stage,
        terminal=state in _TERMINAL,
        outcome=outcome,
        held="paused" if state == "paused" else None,
        detail=detail,
    )


def observe_get(backend, item_id: str) -> Callable[[], Observation]:
    """Read ``item_id`` through ``backend.get``, the MCP public read seam (local or remote backend).

    A carrier the product refused to normalize (``MalformedReadStatusError``) was logged once where it was raised, so
    the typed wait refusal carries its reason without a second WARNING. Any other error from the read is the generic
    refusal ``await_public`` raises, with its one WARNING.
    """
    from smartmemory_mcp.backends.models import MalformedReadStatusError

    def observe() -> Observation:
        try:
            return observation_from_row(backend.get(item_id), item_id)
        except MalformedReadStatusError as exc:
            raise refuse(describe(exc), log=False) from exc

    return observe
