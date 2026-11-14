"""Terminal-message table of the ``/ws/studio`` protocol.

Every message is an envelope ``{"type": ..., "payload": {...}, "request_id": ...}``.
A command is answered, in wire order, by a fixed sequence of events
(``backend/app/schemas/messages.py`` docstrings, ``backend/app/api/ws_stream.py``).
:class:`CommandSpec` records that sequence; the client waits until it has seen
every step in order, skipping anything else (intermediate frames, ``PROGRESS``,
unknown events).

Since Phase D the client sends a unique ``request_id`` with every command and the
server echoes it on every message that command causes, so stale messages of
earlier commands are recognised by id. Servers that do not echo it (pre-Phase D)
are handled by the ordering heuristics alone (``ERROR.command`` /
``STREAM_STOPPED.stopped`` matching, "a stream that applied frames pushes a
trailing ``HISTORY_STATE``").

Adding a command = one entry in :data:`COMMANDS` (and, optionally, a convenience
method on :class:`latticeflow_client.Studio`); :meth:`Studio.send_command` works
for any entry.

The REST project library and job queue (``docs/protocol.md`` §6-§7) have their
constants and the :class:`JobStep` type at the end of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, TypedDict

Predicate = Callable[[Dict[str, Any]], bool]

# ---- server events --------------------------------------------------------------
SINGLE_POINT_UPDATE = "SINGLE_POINT_UPDATE"
HISTORY_STATE = "HISTORY_STATE"
CRYSTAL_STATE_FINAL = "CRYSTAL_STATE_FINAL"
FLOW_TRAJECTORY_FRAME = "FLOW_TRAJECTORY_FRAME"
RELAXATION_FRAME = "RELAXATION_FRAME"
PHONON_ANALYSIS_COMPLETE = "PHONON_ANALYSIS_COMPLETE"
EIGENMODE_DATA = "EIGENMODE_DATA"
STREAM_STOPPED = "STREAM_STOPPED"
ERROR = "ERROR"
BULK_MODULUS_RESULT = "BULK_MODULUS_RESULT"
PHONON_DOS_DATA = "PHONON_DOS_DATA"
PROGRESS = "PROGRESS"
PHONON_CONVERGENCE_RESULT = "PHONON_CONVERGENCE_RESULT"
ELASTIC_TENSOR_RESULT = "ELASTIC_TENSOR_RESULT"
PHONON_EXPORT = "PHONON_EXPORT"
MODEL_CHANGED = "MODEL_CHANGED"
LOCAL_ANALYSIS_DATA = "LOCAL_ANALYSIS_DATA"
FORMATION_ENERGY_RESULT = "FORMATION_ENERGY_RESULT"
SUBSTITUTION_RESULT = "SUBSTITUTION_RESULT"
MD_FRAME = "MD_FRAME"
MD_COMPLETE = "MD_COMPLETE"
COMMITTEE_RESULT = "COMMITTEE_RESULT"
GENERATION_CANDIDATES = "GENERATION_CANDIDATES"
PROTOTYPE_CANDIDATES = "PROTOTYPE_CANDIDATES"
HISTORY_ENTRY = "HISTORY_ENTRY"

#: events that carry a ``crystal`` the session just made active
CRYSTAL_EVENTS = (SINGLE_POINT_UPDATE, CRYSTAL_STATE_FINAL, RELAXATION_FRAME, FLOW_TRAJECTORY_FRAME, MD_FRAME)


@dataclass(frozen=True)
class Step:
    event: str
    when: Optional[Predicate] = None   # extra condition on the payload

    def matches(self, event: str, payload: Dict[str, Any]) -> bool:
        return event == self.event and (self.when is None or bool(self.when(payload)))


def _final(payload: Dict[str, Any]) -> bool:
    return bool(payload.get("final"))


@dataclass(frozen=True)
class CommandSpec:
    steps: Tuple[Step, ...]                        # terminal sequence, in wire order
    streaming: bool = False                        # can be cancelled -> STREAM_STOPPED
    frames: Tuple[str, ...] = ()                   # intermediate events passed to on_frame
    collect: Tuple[str, ...] = ()                  # optional events kept in the reply if they occur
    extra: Dict[str, Any] = field(default_factory=dict)


def _spec(*steps: Any, **kw: Any) -> CommandSpec:
    return CommandSpec(tuple(s if isinstance(s, Step) else Step(s) for s in steps), **kw)


_COMMIT = (SINGLE_POINT_UPDATE, HISTORY_STATE)   # state-changing instant commands

COMMANDS: Dict[str, CommandSpec] = {
    # ---- v1.1
    "LOAD_STRUCTURE": _spec(*_COMMIT),
    "PERTURB_OR_TRANSMUTE_ATOM": _spec(*_COMMIT),           # auto_relax: see AUTO_RELAX_SPEC
    "DELETE_ATOM": _spec(*_COMMIT),
    "REQUEST_EIGENMODE": _spec(EIGENMODE_DATA),
    "START_RELAXATION": _spec(Step(RELAXATION_FRAME, _final), HISTORY_STATE, streaming=True,
                              frames=(RELAXATION_FRAME,)),
    "FOLLOW_INSTABILITY": _spec(Step(RELAXATION_FRAME, _final), HISTORY_STATE, streaming=True,
                                frames=(RELAXATION_FRAME,)),
    "START_FLOW_GENERATION": _spec(CRYSTAL_STATE_FINAL, HISTORY_STATE, streaming=True,
                                   frames=(FLOW_TRAJECTORY_FRAME,), collect=(GENERATION_CANDIDATES,)),
    "RUN_PHONON_SHAKE_TEST": _spec(PHONON_ANALYSIS_COMPLETE, SINGLE_POINT_UPDATE, streaming=True),
    # ---- Phase A
    "COMPUTE_BULK_MODULUS": _spec(BULK_MODULUS_RESULT),
    "REFINE_STRUCTURE": _spec(*_COMMIT),
    "UNDO": _spec(*_COMMIT),
    "REDO": _spec(*_COMMIT),
    "REQUEST_PHONON_DOS": _spec(PHONON_DOS_DATA),
    # ---- Phase B
    "CHECK_PHONON_CONVERGENCE": _spec(PHONON_CONVERGENCE_RESULT, PHONON_ANALYSIS_COMPLETE, SINGLE_POINT_UPDATE,
                                      streaming=True),
    "COMPUTE_ELASTIC_TENSOR": _spec(ELASTIC_TENSOR_RESULT, streaming=True),
    "EXPORT_PHONON_DATA": _spec(PHONON_EXPORT),
    "SET_MODEL": _spec(MODEL_CHANGED, SINGLE_POINT_UPDATE, HISTORY_STATE, streaming=True),
    # ---- Phase C1
    "REQUEST_LOCAL_ANALYSIS": _spec(LOCAL_ANALYSIS_DATA),
    "MAKE_SUPERCELL": _spec(*_COMMIT),
    "ADD_ATOM": _spec(*_COMMIT),
    "COMPUTE_FORMATION_ENERGY": _spec(FORMATION_ENERGY_RESULT, streaming=True),
    "SUBSTITUTE_FRACTION": _spec(SUBSTITUTION_RESULT, SINGLE_POINT_UPDATE, HISTORY_STATE, streaming=True),
    "START_MD": _spec(MD_COMPLETE, HISTORY_STATE, streaming=True, frames=(MD_FRAME,)),
    "COMMITTEE_EVALUATE": _spec(COMMITTEE_RESULT, streaming=True),
    # ---- Phase C2
    "PROTOTYPE_SEARCH": _spec(PROTOTYPE_CANDIDATES, streaming=True),
    # ---- Phase D
    "REQUEST_HISTORY_ENTRY": _spec(HISTORY_ENTRY),
}

#: PERTURB_OR_TRANSMUTE_ATOM with auto_relax=true: the commit, then a relaxation stream
AUTO_RELAX_SPEC = _spec(SINGLE_POINT_UPDATE, HISTORY_STATE, Step(RELAXATION_FRAME, _final), HISTORY_STATE,
                        streaming=True, frames=(RELAXATION_FRAME,))

#: what the server sends on connect (default preset loaded)
CONNECT_SPEC = _spec(*_COMMIT)


# ---- project library + job queue (REST, docs/protocol.md §6-§7) -----------------
#: ``ResultCreate.kind`` / ``ResultRow.kind``
RESULT_KINDS = ("relax", "phonons", "elastic", "eos", "hull", "md", "diffraction", "superconductivity")
#: ``JobStep.kind``
JOB_STEP_KINDS = ("relax", "phonons", "elastic", "eos", "hull", "follow", "superconductivity")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled", "skipped")
#: a job in one of these statuses will not change any more
JOB_FINISHED = frozenset({"done", "failed", "cancelled", "skipped"})


class JobStep(TypedDict, total=False):
    """One step of a ``POST /api/jobs`` chain; omitted ``settings`` take the interactive UI defaults."""

    kind: str                     # one of JOB_STEP_KINDS
    settings: Dict[str, Any]      # e.g. {"fmax": 0.01} for relax, {"supercell_dim": [2, 2, 2]} for phonons
    only_if_unstable: bool        # follow only: skip (instead of fail) when the phonons are stable


def job_step(kind: str, *, only_if_unstable: bool = False, **settings: Any) -> JobStep:
    """``job_step("relax", fmax=0.01)`` -> ``{"kind": "relax", "settings": {"fmax": 0.01}}``."""
    step: JobStep = {"kind": kind, "settings": settings}
    if only_if_unstable:
        step["only_if_unstable"] = True
    return step
