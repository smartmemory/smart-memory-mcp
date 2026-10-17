"""Prepared explicit public waits (U49.A2, C-49.contract.mcp-suites; G11, G12, G47).

The helper in ``tests/public_wait.py`` is prepared, not yet used by a converted test: every MCP save-then-read test
is a mocked seam, a local-inline Lite read or an explicit per-call ``sync=True`` flow, and the real-worker candidates
are listed by name in ``tests/data/save_then_read_census.json``. These rows read through the real ``RemoteBackend``
and ``LocalBackend`` ``get`` (only the HTTP or core transport is stubbed) and prove the prepared calls surface every
outcome the unit requires: queued past the deadline, a fault that stays visible with no retry, the reached
milestone, and a legacy read that is unchanged.
"""

import ast
import copy
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from tests.public_wait import (
    OUTCOMES,
    Observation,
    PublicWaitArgumentError,
    PublicWaitRefused,
    await_public,
    observe_get,
    refuse,
)

# --- SHARED BEGIN (byte-identical in smart-memory-client and smart-memory-mcp tests/test_public_wait.py) ---
WARN_PREFIX = "Explicit public wait refused: "
PENDING = Observation(detail="pending")
STORED = Observation(stage="stored", detail="stored")
FAILED = Observation(stage="stored", terminal=True, outcome="failed")


def done(stage, outcome):
    return Observation(stage=stage, terminal=True, outcome=outcome)


class Clock:
    """A fake clock whose sleep advances it. Binary-exact steps keep every elapsed value exact."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Script:
    """An observer that replays ``steps`` (the last one repeats) and counts its reads."""

    def __init__(self, steps):
        self.steps, self.reads = list(steps), 0

    def __call__(self):
        step = self.steps[min(self.reads, len(self.steps) - 1)]
        self.reads += 1
        if isinstance(step, BaseException):
            raise step
        return step


def warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "public_wait" and r.levelno >= logging.WARNING
    ]


def drive(observe, **kwargs):
    clock = Clock()
    kwargs = {"poll_s": 0.125, **kwargs}
    return await_public(observe, clock=clock.clock, sleep=clock.sleep, **kwargs), clock


HELD = Observation(stage="stored", held="paused")
SPAN = [0.125, 0.25, 0.5, 0.125]

# id, wait, deadline_s, steps, outcome, attempts, sleeps, warnings
CORE_ROWS = [
    ("stored on the first read", "stored", 1.0, [STORED], "reached", 1, [], []),
    (
        "queued then stored",
        "stored",
        1.0,
        [PENDING, PENDING, STORED],
        "reached",
        3,
        [0.125, 0.25],
        [],
    ),
    (
        "queued past the deadline",
        "stored",
        1.0,
        [PENDING],
        "client_timed_out",
        5,
        SPAN,
        [],
    ),
    (
        "deadline 0 reads once and times out",
        "stored",
        0,
        [PENDING, STORED],
        "client_timed_out",
        1,
        [],
        [],
    ),
    ("deadline 0 reached on the one read", "stored", 0, [STORED], "reached", 1, [], []),
    (
        "failed on the first read is not retried",
        "stored",
        1.0,
        [FAILED, STORED],
        "failed",
        1,
        [],
        [],
    ),
    (
        "failed after pending is returned when it appears",
        "stored",
        1.0,
        [PENDING, FAILED, STORED],
        "failed",
        2,
        [0.125],
        [],
    ),
    (
        "dead stays dead",
        "extracted",
        1.0,
        [done("stored", "dead"), STORED],
        "dead",
        1,
        [],
        [],
    ),
    (
        "skipped stays skipped",
        "extracted",
        1.0,
        [done("extracted", "skipped"), STORED],
        "skipped",
        1,
        [],
        [],
    ),
    (
        "partial stays partial",
        "terminal",
        1.0,
        [done("extracted", "partial"), STORED],
        "partial",
        1,
        [],
        [],
    ),
    (
        "completed at stored does not reach extracted",
        "extracted",
        1.0,
        [done("stored", "completed")],
        "unreached",
        1,
        [],
        [],
    ),
    (
        "completed no_op reaches terminal",
        "terminal",
        1.0,
        [done("stored", "no_op")],
        "reached",
        1,
        [],
        [],
    ),
    (
        "completed at extracted reaches extracted",
        "extracted",
        1.0,
        [done("extracted", "completed")],
        "reached",
        1,
        [],
        [],
    ),
    (
        "stored only, extracted wanted, times out",
        "extracted",
        1.0,
        [STORED],
        "client_timed_out",
        5,
        SPAN,
        [],
    ),
    (
        "still running, terminal wanted, times out",
        "terminal",
        1.0,
        [Observation(stage="extracted")],
        "client_timed_out",
        5,
        SPAN,
        [],
    ),
    (
        "a held run is refused once and not polled",
        "stored",
        1.0,
        [HELD, STORED],
        "refused",
        1,
        [],
        [
            "Explicit public wait: the run is held ('paused'); reported refused, not polled further"
        ],
    ),
]


@pytest.mark.parametrize("row", CORE_ROWS, ids=[r[0] for r in CORE_ROWS])
def test_each_scripted_read_sequence_has_its_outcome(row, caplog):
    _, wait, deadline_s, steps, outcome, attempts, sleeps, warned = row
    observe = Script(steps)
    result, clock = drive(observe, wait=wait, deadline_s=deadline_s)
    assert (result.outcome, result.attempts, observe.reads) == (
        outcome,
        attempts,
        attempts,
    )
    assert result.outcome in OUTCOMES and result.wait == wait
    assert (clock.sleeps, result.elapsed_s) == (sleeps, clock.now)
    assert result.last is observe.steps[min(attempts - 1, len(steps) - 1)]
    assert warnings(caplog) == warned


class Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("no text")


# id, wait, steps, message
REFUSAL_ROWS = [
    (
        "the read raises",
        "stored",
        [RuntimeError("boom"), STORED],
        "the read through the public seam failed (RuntimeError: boom)",
    ),
    (
        "the error message cannot be formatted",
        "stored",
        [Unprintable(), STORED],
        "the read through the public seam failed (Unprintable: <unprintable>)",
    ),
    (
        "the read is not an Observation",
        "stored",
        ["stored"],
        "the read returned str, not an Observation",
    ),
    (
        "an unknown terminal outcome",
        "stored",
        [done("stored", "weird")],
        "a terminal observation carries outcome 'weird'",
    ),
    (
        "a terminal observation with no outcome",
        "stored",
        [done("stored", None)],
        "a terminal observation carries outcome None",
    ),
    (
        "extracted wanted from a seam with no status",
        "extracted",
        [Observation(stage="stored", status_carried=False, detail="d")],
        "this public seam carries no extraction or terminal status, so wait='extracted' is unobservable (only 'stored' is); last read: d",
    ),
    (
        "terminal wanted from a seam with no status",
        "terminal",
        [Observation(status_carried=False)],
        "this public seam carries no extraction or terminal status, so wait='terminal' is unobservable (only 'stored' is); last read: none",
    ),
]


@pytest.mark.parametrize("row", REFUSAL_ROWS, ids=[r[0] for r in REFUSAL_ROWS])
def test_an_unanswerable_read_is_one_typed_refusal_and_never_retried(row, caplog):
    _, wait, steps, message = row
    observe = Script(steps)
    with pytest.raises(PublicWaitRefused) as caught:
        drive(observe, wait=wait, deadline_s=1.0)
    assert type(caught.value) is PublicWaitRefused and str(caught.value) == message
    assert observe.reads == 1
    assert warnings(caplog) == [WARN_PREFIX + message]


def test_a_refusal_an_adapter_raised_is_logged_once_not_twice(caplog):
    observe = Script([refuse("seam said no")])
    with pytest.raises(PublicWaitRefused, match="seam said no"):
        drive(observe, wait="stored", deadline_s=1.0)
    assert observe.reads == 1 and warnings(caplog) == [WARN_PREFIX + "seam said no"]


BAD_WAIT = "wait must be one of ('stored', 'extracted', 'terminal'), got "
# id, kwargs, message
ARGUMENT_ROWS = [
    ("wait None", {"wait": None, "deadline_s": 1.0}, BAD_WAIT + "None"),
    ("wait unknown", {"wait": "bogus", "deadline_s": 1.0}, BAD_WAIT + "'bogus'"),
    ("wait a number", {"wait": 1, "deadline_s": 1.0}, BAD_WAIT + "1"),
    (
        "deadline negative",
        {"wait": "stored", "deadline_s": -1},
        "deadline_s must be a finite number >= 0, got -1",
    ),
    (
        "deadline nan",
        {"wait": "stored", "deadline_s": float("nan")},
        "deadline_s must be a finite number >= 0, got nan",
    ),
    (
        "deadline inf",
        {"wait": "stored", "deadline_s": float("inf")},
        "deadline_s must be a finite number >= 0, got inf",
    ),
    (
        "deadline a bool",
        {"wait": "stored", "deadline_s": True},
        "deadline_s must be a finite number >= 0, got True",
    ),
    (
        "deadline a string",
        {"wait": "stored", "deadline_s": "1"},
        "deadline_s must be a finite number >= 0, got '1'",
    ),
    (
        "deadline None",
        {"wait": "stored", "deadline_s": None},
        "deadline_s must be a finite number >= 0, got None",
    ),
    (
        "poll zero",
        {"wait": "stored", "deadline_s": 1.0, "poll_s": 0},
        "poll_s must be a positive finite number, got 0",
    ),
    (
        "poll negative",
        {"wait": "stored", "deadline_s": 1.0, "poll_s": -1},
        "poll_s must be a positive finite number, got -1",
    ),
    (
        "poll nan",
        {"wait": "stored", "deadline_s": 1.0, "poll_s": float("nan")},
        "poll_s must be a positive finite number, got nan",
    ),
    (
        "poll a bool",
        {"wait": "stored", "deadline_s": 1.0, "poll_s": True},
        "poll_s must be a positive finite number, got True",
    ),
    (
        "deadline too large for a float",
        {"wait": "stored", "deadline_s": 10**400},
        "deadline_s must be a finite number >= 0, got " + repr(10**400)[:48],
    ),
    (
        "deadline whose text cannot be formed",
        {"wait": "stored", "deadline_s": 10**5000},
        "deadline_s must be a finite number >= 0, got <unprintable>",
    ),
    (
        "first bad argument wins",
        {"wait": "bogus", "deadline_s": -1},
        BAD_WAIT + "'bogus'",
    ),
]


@pytest.mark.parametrize("row", ARGUMENT_ROWS, ids=[r[0] for r in ARGUMENT_ROWS])
def test_a_bad_argument_is_refused_before_any_read(row, caplog):
    _, kwargs, message = row
    observe = Script([STORED])
    with pytest.raises(PublicWaitArgumentError) as caught:
        drive(observe, **kwargs)
    assert str(caught.value) == message and isinstance(caught.value, ValueError)
    assert observe.reads == 0
    assert warnings(caplog) == [WARN_PREFIX + message]


@pytest.mark.parametrize(
    "observe, shown", [("x", "'x'"), (None, "None")], ids=["a string", "None"]
)
def test_an_observer_that_is_not_callable_is_refused(observe, shown, caplog):
    with pytest.raises(PublicWaitArgumentError):
        await_public(observe, wait="stored", deadline_s=1.0)
    assert warnings(caplog) == [WARN_PREFIX + f"observe must be callable, got {shown}"]


def test_the_helper_does_not_import_the_core_wait_module():
    tree = ast.parse((Path(__file__).parent / "public_wait.py").read_text())
    names = [
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    ]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert [
        n
        for n in names
        if n == "smartmemory.testing" or n.startswith("smartmemory.testing.")
    ] == []


# --- SHARED END ---


class Feed:
    """Replays transport replies (the last repeats) at a backend's HTTP or core read and counts the calls."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def __call__(self, *args, **kwargs):
        step = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step


def carrier(stage, state, outcome=None):
    return {
        "extraction_state": "extraction_pending",
        "processing_stage": stage,
        "execution_state": state,
        "outcome": outcome,
        "revision": 1,
    }


def row(read_status=None, **extra):
    base = {"item_id": "m-1", "content": "hello", "memory_type": "semantic"}
    return (
        {**base, **extra}
        if read_status is None
        else {**base, "read_status": read_status, **extra}
    )


def remote(replies):
    backend = RemoteBackend(
        api_url="https://api.test", api_key="sk_test", team_id="ws-1"
    )
    feed = Feed(replies)
    backend._request = feed
    return backend, feed


def local(replies):
    backend = LocalBackend.__new__(LocalBackend)
    feed = Feed(replies)
    backend._mem = SimpleNamespace(get=feed)
    return backend, feed


def detail(c):
    return f"get('m-1'): stage={c['processing_stage']}, execution_state={c['execution_state']}, outcome={c['outcome']}"


ABSENT = {"error": "API error 404: not found"}
RECEIVED, QUEUED, RUNNING = (
    carrier("received", "queued"),
    carrier("stored", "queued"),
    carrier("stored", "running"),
)
WAITING, EXTRACTING = carrier("stored", "waiting"), carrier("extracted", "running")
CARRY = "this public seam carries no extraction or terminal status, so wait={!r} is unobservable (only 'stored' is); last read: "

# id, wait, steps, outcome, attempts
CARRIER_ROWS = [
    ("received then stored", "stored", [RECEIVED, QUEUED], "reached", 2),
    (
        "stored and queued, extracted wanted, times out",
        "extracted",
        [QUEUED],
        "client_timed_out",
        5,
    ),
    (
        "waiting for allowance times out visibly",
        "extracted",
        [WAITING],
        "client_timed_out",
        5,
    ),
    ("running then extracted", "extracted", [RUNNING, EXTRACTING], "reached", 2),
    (
        "completed at extracted reaches terminal",
        "terminal",
        [RUNNING, carrier("extracted", "completed", "completed")],
        "reached",
        2,
    ),
    (
        "completed no_op at stored does not reach extracted",
        "extracted",
        [carrier("stored", "completed", "no_op")],
        "unreached",
        1,
    ),
    (
        "failed is returned on the first read",
        "stored",
        [carrier("stored", "failed", "failed"), EXTRACTING],
        "failed",
        1,
    ),
    (
        "failed after queued is returned when it appears",
        "extracted",
        [QUEUED, carrier("stored", "failed", "failed")],
        "failed",
        2,
    ),
    (
        "dead stays dead",
        "terminal",
        [carrier("stored", "dead", "dead"), EXTRACTING],
        "dead",
        1,
    ),
    (
        "skipped stays skipped",
        "extracted",
        [carrier("stored", "completed", "skipped")],
        "skipped",
        1,
    ),
    (
        "partial stays partial",
        "terminal",
        [carrier("extracted", "completed", "partial")],
        "partial",
        1,
    ),
    (
        "a held run is refused",
        "stored",
        [carrier("stored", "paused"), EXTRACTING],
        "refused",
        1,
    ),
]


@pytest.mark.parametrize("row_", CARRIER_ROWS, ids=[r[0] for r in CARRIER_ROWS])
def test_remote_get_carrier_outcomes(row_, caplog):
    _, wait, steps, outcome, attempts = row_
    backend, feed = remote([row(c) for c in steps])
    result, _ = drive(observe_get(backend, "m-1"), wait=wait, deadline_s=1.0)
    assert (result.outcome, result.attempts, feed.calls) == (
        outcome,
        attempts,
        attempts,
    )
    assert result.last.detail == detail(steps[min(attempts - 1, len(steps) - 1)])
    held = [
        "Explicit public wait: the run is held ('paused'); reported refused, not polled further"
    ]
    assert warnings(caplog) == (held if outcome == "refused" else [])


# id, carrier, message
INVALID_ROWS = [
    (
        "an unknown stage",
        carrier("bogus", "running"),
        "read_status.processing_stage is 'bogus', not one of ('received', 'stored', 'extracted')",
    ),
    (
        "a null stage on an active run",
        carrier(None, "queued"),
        "read_status.processing_stage is None, not one of ('received', 'stored', 'extracted')",
    ),
    (
        "a missing stage on an active run",
        {
            k: v
            for k, v in carrier("stored", "queued").items()
            if k != "processing_stage"
        },
        "read_status.processing_stage is None, not one of ('received', 'stored', 'extracted')",
    ),
    (
        "a missing stage on a completed run",
        {
            k: v
            for k, v in carrier("stored", "completed", "completed").items()
            if k != "processing_stage"
        },
        "read_status.processing_stage is None, not one of ('received', 'stored', 'extracted')",
    ),
    (
        "an unknown execution state",
        carrier("stored", "bogus"),
        "read_status.execution_state is 'bogus', not one of ('queued', 'waiting', 'running', 'paused', 'completed', 'failed', 'dead')",
    ),
    (
        "no execution state",
        carrier("stored", None),
        "read_status.execution_state is None, not one of ('queued', 'waiting', 'running', 'paused', 'completed', 'failed', 'dead')",
    ),
    (
        "an outcome while running",
        carrier("stored", "running", "completed"),
        "read_status carries outcome 'completed' while execution_state is running",
    ),
    (
        "failed with no outcome",
        carrier("stored", "failed"),
        "read_status.execution_state is failed but outcome is None",
    ),
    (
        "dead with the failed outcome",
        carrier("stored", "dead", "failed"),
        "read_status.execution_state is dead but outcome is 'failed'",
    ),
    (
        "completed with a fault outcome",
        carrier("stored", "completed", "failed"),
        "read_status.execution_state is completed but outcome is 'failed'",
    ),
    (
        "completed with no outcome",
        carrier("stored", "completed"),
        "read_status.execution_state is completed but outcome is None",
    ),
]


@pytest.mark.parametrize("row_", INVALID_ROWS, ids=[r[0] for r in INVALID_ROWS])
def test_an_inconsistent_carrier_is_one_typed_refusal_never_a_guess(row_, caplog):
    _, bad, message = row_
    backend, feed = remote(
        [row(bad), row(carrier("extracted", "completed", "completed"))]
    )
    with pytest.raises(PublicWaitRefused) as caught:
        drive(observe_get(backend, "m-1"), wait="stored", deadline_s=1.0)
    assert type(caught.value) is PublicWaitRefused and str(caught.value) == message
    assert feed.calls == 1 and warnings(caplog) == [WARN_PREFIX + message]


# id, arm, replies, wait, deadline_s, outcome, attempts, calls
ABSENCE_ROWS = [
    (
        "remote 404 twice then readable",
        "remote",
        [ABSENT, ABSENT, row(QUEUED)],
        "stored",
        1.0,
        "reached",
        3,
        3,
    ),
    (
        "remote 404 past the deadline is a visible timeout",
        "remote",
        [ABSENT],
        "stored",
        1.0,
        "client_timed_out",
        5,
        5,
    ),
    (
        "local absent twice then readable",
        "local",
        [None, None, row(QUEUED)],
        "stored",
        1.0,
        "reached",
        3,
        3,
    ),
    (
        "local absent past the deadline is a visible timeout",
        "local",
        [None],
        "stored",
        1.0,
        "client_timed_out",
        5,
        5,
    ),
    (
        "deadline 0 reads once",
        "remote",
        [ABSENT, row(QUEUED)],
        "stored",
        0,
        "client_timed_out",
        1,
        1,
    ),
]


@pytest.mark.parametrize("row_", ABSENCE_ROWS, ids=[r[0] for r in ABSENCE_ROWS])
def test_absence_is_queued_and_a_visible_timeout_at_the_deadline(row_, caplog):
    _, arm, replies, wait, deadline_s, outcome, attempts, calls = row_
    backend, feed = (remote if arm == "remote" else local)(replies)
    result, _ = drive(observe_get(backend, "m-1"), wait=wait, deadline_s=deadline_s)
    assert (result.outcome, result.attempts, feed.calls) == (outcome, attempts, calls)
    if outcome == "client_timed_out":
        assert result.last.detail == "get('m-1') returned None, not readable yet"
    assert warnings(caplog) == []


def test_the_local_arm_reads_the_carrier_through_the_core_item_shape():
    core_item = SimpleNamespace(
        item_id="m-1", read_status=EXTRACTING, to_dict=lambda: row(EXTRACTING)
    )
    backend, feed = local([core_item])
    result, _ = drive(observe_get(backend, "m-1"), wait="extracted", deadline_s=1.0)
    assert (result.outcome, result.attempts) == (
        "reached",
        1,
    ) and result.last.detail == detail(EXTRACTING)


def test_a_legacy_row_with_no_carrier_is_readable_and_extraction_is_unobservable(
    caplog,
):
    backend, feed = remote([row(), row()])
    result, _ = drive(observe_get(backend, "m-1"), wait="stored", deadline_s=1.0)
    assert (result.outcome, result.attempts) == ("reached", 1) and warnings(
        caplog
    ) == []
    with pytest.raises(PublicWaitRefused):
        drive(observe_get(backend, "m-1"), wait="extracted", deadline_s=1.0)
    assert feed.calls == 2
    assert warnings(caplog) == [
        WARN_PREFIX + CARRY.format("extracted") + "get('m-1') carries no read_status"
    ]


def test_an_outage_is_refused_never_read_as_absence(caplog):
    backend, feed = remote([{"error": "API error 500: boom"}, row(QUEUED)])
    with pytest.raises(PublicWaitRefused):
        drive(observe_get(backend, "m-1"), wait="stored", deadline_s=1.0)
    assert feed.calls == 1
    assert warnings(caplog) == [
        WARN_PREFIX
        + "the read through the public seam failed (RuntimeError: API error 500: boom)"
    ]


def test_a_carrier_the_product_refused_is_one_warning_in_total(caplog):
    backend, feed = remote([row("bogus"), row(QUEUED)])
    with caplog.at_level(logging.WARNING), pytest.raises(PublicWaitRefused) as caught:
        drive(observe_get(backend, "m-1"), wait="stored", deadline_s=1.0)
    assert (
        str(caught.value)
        == "MalformedReadStatusError: read_status on m-1 is a str, not a dict"
    )
    assert feed.calls == 1
    assert [
        (r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING
    ] == [
        (
            "smartmemory_mcp.backends.models",
            "read_status carrier on m-1 is a str, not a dict: refusing to normalize the row without its extraction status",
        )
    ]


def test_a_row_that_is_not_a_dict_is_refused(caplog):
    with pytest.raises(PublicWaitRefused):
        drive(
            observe_get(SimpleNamespace(get=lambda item_id: ["x"]), "m-1"),
            wait="stored",
            deadline_s=1.0,
        )
    assert warnings(caplog) == [
        WARN_PREFIX + "get('m-1') returned list, not a MemoryResult dict"
    ]


def test_the_legacy_read_is_unchanged_by_the_wait():
    legacy, with_carrier = row(), row(QUEUED)
    before = copy.deepcopy([legacy, with_carrier])
    backend, feed = remote([legacy, with_carrier])
    direct = backend.get("m-1")
    assert "read_status" not in direct and direct["item_id"] == "m-1"
    assert backend.get("m-1")["read_status"] == QUEUED
    drive(observe_get(backend, "m-1"), wait="stored", deadline_s=0)
    assert [legacy, with_carrier] == before
