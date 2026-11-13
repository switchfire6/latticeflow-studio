"""``/ws/studio`` WebSocket event multiplexer (PROJECT_SPEC.md §5.2, §7 Phase 3).

One :class:`StudioSession` per connection. It owns the active ``Atoms``, the last
``CrystalStatePayload`` sent for it, a structure version counter, the cached
phonon analysis (tied to the version it was computed for), its uMLIP engine
and at most one in-flight streaming job.

Per-session engine (Phase B, IMPROVEMENT_SUGGESTIONS.md §6.1)
--------------------------------------------------------------
A session starts on the process default (``UMLIPEngine.get_instance()``) and
switches with ``SET_MODEL`` (``umlip_engine.get_engine``: a shared, LRU-bounded
registry, so two sessions on the same model share one engine; each engine's
RLock serialises its GPU use across sessions). Every engine use of the session
- evaluate, relax, flow, phonons, convergence, bulk modulus, elastic tensor -
uses the session engine; a job captures the engine it was started with.

Concurrency model (all session state is touched only on the event-loop thread)
-----------------------------------------------------------------------------
* **Receive loop** - parses and validates every message. ``STOP_STREAM`` is
  handled right there (unless a streaming command is still queued, see below);
  everything else, including parse / validation errors, goes into a FIFO so
  replies come back in command order.
* **Command processor** - one task consuming the FIFO serially. Instant
  commands run their engine call with ``asyncio.to_thread``; streaming
  commands start a dedicated worker thread and the processor moves on at once,
  so a later command can cancel the stream.
* **Sender** - one task draining an ``asyncio.Queue`` into ``send_json``; the
  queue order is the wire order.
* **Workers** - one daemon thread per stream running the synchronous,
  cancellable engine cores (``iter_relaxation``, ``RiemannianFlowGenerator
  .iter_frames``, ``run_phonon_analysis``, ``stabilize_mode``). Each output is
  posted to the loop with ``call_soon_threadsafe`` together with a private
  snapshot of the structure it describes; the loop applies it (active
  structure := snapshot, version += 1) and queues the message only if the job
  has not been terminated. A job is terminated, on the loop thread and
  atomically with queueing its terminal message, either by its own terminal
  output or by a cancel (which queues ``STREAM_STOPPED``). Hence exactly one
  terminal message per stream, nothing from the job after it, and after a stop
  the active structure is the structure of the last frame that was sent.
* ``STREAM_STOPPED`` is queued the moment the cancel is decided; the worker
  exits at its next cancellation checkpoint (≤ one engine call / one phonon
  force chunk later). Any later command that needs the engine or the session
  state first waits (bounded) for that worker to finish.
* A worker can run at most ``FRAME_WINDOW`` frames ahead of the socket
  (back-pressure), so a slow client does not make the server buffer an
  unbounded trajectory.
* ``STOP_STREAM`` while a streaming command is still queued (sent but not yet
  started) is queued behind it instead of being handled immediately, so
  ``START_RELAXATION`` immediately followed by ``STOP_STREAM`` stops that
  relaxation rather than answering ``{stopped: null}`` and letting it run.
* Consecutive queued coordinate-only ``PERTURB_OR_TRANSMUTE_ATOM`` commands for
  the same atom (drag frames) are coalesced: only the latest is applied and
  answered.
* Instant commands that only *read* the structure (``REQUEST_EIGENMODE``,
  ``REQUEST_PHONON_DOS``, ``COMPUTE_BULK_MODULUS``, ``EXPORT_PHONON_DATA``) cancel nothing; they work
  on a snapshot. ``COMPUTE_BULK_MODULUS`` also stores ``k_gpa`` in the active
  payload's ``bulk_modulus_gpa`` (no extra ``SINGLE_POINT_UPDATE``), unless the
  structure changed while it ran. ``REFINE_STRUCTURE``, ``UNDO`` and ``REDO``
  are state-changing: they cancel a live stream first, like ``DELETE_ATOM``.

Undo / redo history (IMPROVEMENT_SUGGESTIONS.md §4.4)
------------------------------------------------------
Each session keeps up to ``HISTORY_LIMIT`` entries (atoms snapshot, payload,
label) and a current position; it is mutated only on the loop thread, in the
same callback that commits the structure, so the history always describes the
wire order. An entry is pushed for every committed structure: the initial
preset, ``LOAD_STRUCTURE``, ``PERTURB_OR_TRANSMUTE_ATOM``, ``DELETE_ATOM``,
``REFINE_STRUCTURE`` and every stream that changed the structure - at its
terminal output (relaxation / follow final frame, ``CRYSTAL_STATE_FINAL``, or
an ``ERROR`` after frames were applied: label "... (failed)") or at its
cancellation once at least one frame was applied (label "... (stopped)"; the
entry is the last frame sent). Intermediate frames and the phonon flagging
``SINGLE_POINT_UPDATE`` never push. A coordinate-only drag commit of the same
atom as the previous push *replaces* the current entry (one drag gesture = one
undo step); any other push, a stream start, ``UNDO`` and ``REDO`` close the
gesture. A push after an ``UNDO`` drops the redo branch; beyond the limit the
oldest entry is dropped. Every push is followed immediately by
``HISTORY_STATE`` (after the ``SINGLE_POINT_UPDATE`` / terminal message(s) /
``STREAM_STOPPED`` it belongs to). ``UNDO`` / ``REDO`` cancel a live stream
(whose own push happens first, so ``UNDO`` then returns to the structure
before that stream), restore the neighbouring entry (version += 1, which
invalidates the phonon cache) and send ``SINGLE_POINT_UPDATE`` then
``HISTORY_STATE``; with nothing to undo / redo they answer
``ERROR INVALID_OPERATION``.

History entries are *structures*: the force field is not part of the history.
Each entry remembers the ``backend_name`` that produced its payload; ``UNDO`` /
``REDO`` to an entry evaluated with another model (i.e. from before a
``SET_MODEL``) re-evaluate the restored structure with the session's *current*
engine (the entry keeps that payload). Undoing a model switch therefore does
not switch the model back; it only steps back to the previous structure.

Phase B commands (terminal messages; every stream still ends with exactly one
terminal group, ``STREAM_STOPPED`` or ``ERROR``)
-----------------------------------------------------------------------------
* ``RUN_PHONON_SHAKE_TEST`` gains ``cell_mode`` / ``min_supercell_length``
  (passed to ``run_phonon_analysis``). The ``phonon_max_atoms()`` limit applies to
  the displayed cell; band indices are validated against ``n_branches``
  (3 N of the phonon cell); eigenvectors stay displayed-atom indexed, so the
  unstable-actor flags work in both modes. A ``ValueError`` of the phonon
  engine (e.g. supercell budget) is ``ERROR INVALID_OPERATION``.
* ``CHECK_PHONON_CONVERGENCE`` (streaming, needs a valid phonon analysis else
  ``NO_PHONON_ANALYSIS``): reruns the cached analysis' structure with the same
  ``cell_mode`` / displacement in ``supercell_dim`` or
  ``next_supercell_dim(current)`` (none fits / over budget:
  ``STRUCTURE_TOO_LARGE``). ``PROGRESS`` events: one at the start (total 0),
  then one per force chunk (``done`` = supercells evaluated so far, total 0 =
  unknown). Terminal: ``PHONON_CONVERGENCE_RESULT``, ``PHONON_ANALYSIS_COMPLETE``
  of the larger supercell (it replaces the phonon cache), flagging
  ``SINGLE_POINT_UPDATE``. No history push; a cancel keeps the old cache.
* ``COMPUTE_ELASTIC_TENSOR`` (streaming, reads the structure only, no history
  push): ``PROGRESS`` events from ``elastic.compute_elastic_tensor``'s callback,
  terminal ``ELASTIC_TENSOR_RESULT``. Non-periodic structure (or a ``ValueError``
  of the fit): ``ERROR INVALID_OPERATION``.
* ``EXPORT_PHONON_DATA`` (instant, cancels nothing, needs a valid phonon
  analysis): ``PHONON_EXPORT {filename: "<formula>_phonopy_params.yaml", phonopy_yaml}``.
* ``SET_MODEL`` (streaming, state-changing): cancels a live stream first (usual
  ``STREAM_STOPPED`` + history rules), loads the model in the worker
  (``PROGRESS``: "Loading <id>" with total 0, download bytes if a checkpoint is
  fetched, "Loaded ..." 1/1), then terminal ``MODEL_CHANGED``, the re-evaluated
  ``SINGLE_POINT_UPDATE`` of the active structure, ``HISTORY_STATE`` (pushed
  entry, label "Switch model to <id>" or "Switch model to <id> + D3"). The
  phonon cache is invalidated. Failure: ``ERROR MODEL_UNAVAILABLE`` (unknown
  id, not cached with downloads disabled, D3 without torch-dftd, load error;
  ``COMPUTE_FAILED`` if only the evaluation fails) and the session keeps its
  previous engine. A cancelled ``SET_MODEL`` is not joined: its load may finish
  in the background (into the shared registry) but is never applied.

Phase C1 commands (IMPROVEMENT_SUGGESTIONS.md §5.7-5.10, §6.2)
--------------------------------------------------------------
* ``HISTORY_STATE.entries`` is always the full history (oldest first):
  position, label, formula, n_atoms, energy_per_atom (of the stored payload,
  i.e. the model that produced it), spacegroup_symbol.
* ``REQUEST_LOCAL_ANALYSIS`` (instant, cancels nothing): ``LOCAL_ANALYSIS_DATA``
  of a snapshot, its ``bond_ideal_lengths`` aligned with the active payload's bonds.
* ``MAKE_SUPERCELL`` / ``ADD_ATOM`` (instant, state-changing like ``DELETE_ATOM``):
  ``SINGLE_POINT_UPDATE`` + ``HISTORY_STATE`` (labels "Make supercell <label>"
  with ``defects.supercell_label``, "Add <El> at (u, v, w)" with 3 decimals).
  Over ``max_atoms()``: ``STRUCTURE_TOO_LARGE``; det <= 0, an atom too close or
  any other ``ValueError``: ``INVALID_OPERATION``; unknown / Z > 89 element:
  ``INVALID_ELEMENT``.
* ``COMPUTE_FORMATION_ENERGY`` (streaming, reads only, no history push):
  ``bulk_position`` outside the history: ``INDEX_OUT_OF_RANGE``. ``PROGRESS``
  from ``defects.formation_energy``; terminal ``FORMATION_ENERGY_RESULT``. No
  ``MP_API_KEY`` (and nothing cached): ``ERROR INVALID_OPERATION`` explaining it.
* ``SUBSTITUTE_FRACTION`` (streaming, state-changing): ``PROGRESS`` from
  ``ordering.substitute_fraction``; terminal ``SUBSTITUTION_RESULT``, the
  evaluated ``SINGLE_POINT_UPDATE`` of the chosen configuration, ``HISTORY_STATE``
  ("Substitute <n> <A> -> <B>"). Element absent, A == B, zero atoms to replace
  or a ``ValueError``: ``INVALID_OPERATION``; unknown / Z > 89: ``INVALID_ELEMENT``.
* ``START_MD`` (streaming, state-changing, frame window like a relaxation):
  ``MD_FRAME`` at step 0 and every ``frame_interval`` steps; the terminal group
  is the last ``MD_FRAME`` (step ``n_steps``), ``MD_COMPLETE``, ``HISTORY_STATE``
  ("MD <T> K <ensemble> (<t> ps)"). Every ``MD_FRAME`` replaces the active
  structure (wrapped, no velocities), so a stop leaves the last sent frame
  active (``STREAM_STOPPED`` + "MD <T> K <ensemble> (stopped)"). Divergence:
  ``ERROR COMPUTE_FAILED``, the last sent frame stays active ("... (failed)").
* ``COMMITTEE_EVALUATE`` (streaming, reads only, no history push): ``PROGRESS``
  before each model (done = models finished, total = number of models) and at
  the end; terminal ``COMMITTEE_RESULT``. Unknown / unloadable model:
  ``ERROR MODEL_UNAVAILABLE``.

Phase C2 commands (IMPROVEMENT_SUGGESTIONS.md §7.5)
---------------------------------------------------
* ``START_FLOW_GENERATION`` with ``top_k`` > 1: after the last frame the worker
  also runs ``RiemannianFlowGenerator.top_candidates`` (extra symmetric
  relaxations of the search's other structures, <= 9 engine calls per tried
  alternate, at most top_k + 1 alternates; pre-stream budget and per-frame cost
  unchanged). The terminal group is then ``GENERATION_CANDIDATES``,
  ``CRYSTAL_STATE_FINAL``, ``HISTORY_STATE`` (one ``_apply_job_output``, so it is
  atomic like every terminal group). Rank 1 is the streamed endpoint, i.e. the
  ``CRYSTAL_STATE_FINAL`` crystal that becomes active; the other candidates are not
  applied (the client loads one with ``LOAD_STRUCTURE``). A stop during the extra
  relaxations is an ordinary stop after the last frame (``STREAM_STOPPED``, history
  "Flow generation (stopped)", the endpoint stays active). top_k = 1 sends exactly
  the Phase A sequence.
* ``PROTOTYPE_SEARCH`` (streaming, reads nothing, no history push, needs no active
  structure): ``prototypes.search`` with the session engine and the MP client of
  ``prototype_client(MP_API_KEY)``; ``PROGRESS`` per source formula, hull reference
  and candidate relaxation; terminal ``PROTOTYPE_CANDIDATES``. No / malformed key:
  ``ERROR INVALID_OPERATION`` explaining ``MP_API_KEY``; MP / hull fetch failure:
  ``COMPUTE_FAILED``; non-ionic composition, no prototypes found or another
  ``ValueError``: ``INVALID_OPERATION``. Cancellable at every MP call, relaxation
  step and hull reference.

Phase D (protocol ergonomics, IMPROVEMENT_SUGGESTIONS.md §7.7)
--------------------------------------------------------------
* ``request_id``: an optional envelope field (<= 64 chars), also read from an
  otherwise invalid envelope. Every message queued by ``_send`` is stamped with
  ``self._rid`` (top-level ``"request_id"``; absent when None). The processor
  sets ``_rid`` to the command it runs (replies, its ``ERROR``, the
  ``HISTORY_STATE`` it pushes); a job captures the id of the command that started
  it, and every loop-thread section acting for a job - ``_apply_job_output``
  (frames, ``PROGRESS``, terminal group, its ``HISTORY_STATE``, a stream ``ERROR``)
  and ``_cancel_live`` (``STREAM_STOPPED`` and its push) - runs under
  ``_as_request(job.request_id)``. These sections are synchronous, so the
  override/restore cannot interleave with another coroutine; the ordering and
  concurrency guarantees above are unchanged. Hence a stream's ``STREAM_STOPPED``
  carries the stream's id even when a later command (with another id) cancelled
  it; ``STOP_STREAM``'s own id appears only on ``{stopped: null}``. Coalesced
  drag frames are never answered, so their ids never appear. The connect-time
  preset load carries no id.
* ``STREAM_STOPPED.history_pushed`` / ``ERROR.history_pushed``: true iff a
  ``HISTORY_STATE`` for that stream follows (frames had been applied, or the
  terminal group has its own label, e.g. "Relaxation (diverged, restored)").
  ``ERROR.after_terminal``: true for ``RELAXATION_DIVERGED`` sent after the final
  ``RELAXATION_FRAME`` (relaxation and ``FOLLOW_INSTABILITY``).
* ``REQUEST_HISTORY_ENTRY {position}`` (instant, cancels nothing, changes nothing):
  ``HISTORY_ENTRY {position, label, crystal}`` with the stored payload; unknown
  position: ``INDEX_OUT_OF_RANGE``.
* Size limits are read at call time from ``config.get_settings()``:
  ``structure_io.max_atoms()`` (``LATTICEFLOW_MAX_ATOMS``, default 128) and
  :func:`phonon_max_atoms` (``LATTICEFLOW_PHONON_MAX_ATOMS``, default 64).
  ``PHONON_MAX_ATOMS`` / ``structure_io.MAX_ATOMS`` remain as default / import-time aliases.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.constraints import FixCom
from ase.data import atomic_masses, atomic_numbers
from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.config import get_settings
from app.core import md as md_core
from app.core import phonon_engine, relaxer, structure_io
from app.core.flow_generator import FlowCancelled, RiemannianFlowGenerator
from app.core.phonon_engine import PhononAnalysis, PhononCancelled
from app.core.relaxer import RelaxationDivergedError
from app.core.structure_io import StructureIOError, atoms_from_cif, max_atoms
from app.core.umlip_engine import ModelUnavailableError, UMLIPEngine, get_engine, wrap_in_place
from app.presets import DEFAULT_PRESET_ID, load_preset
from app.schemas.crystal import CrystalStatePayload
from app.schemas.messages import (
    F_TOL_THZ,
    CheckPhononConvergencePayload,
    ClientCommand,
    ComputeBulkModulusPayload,
    ComputeElasticTensorPayload,
    CrystalStateFinal,
    DeleteAtomPayload,
    EigenmodeData,
    Envelope,
    ErrorCode,
    ErrorPayload,
    ExportPhononDataPayload,
    FollowInstabilityPayload,
    HistoryState,
    LoadStructurePayload,
    ModelChanged,
    PerturbOrTransmuteAtomPayload,
    PhononExport,
    Progress,
    RedoPayload,
    RefineStructurePayload,
    RelaxationFrame,
    RequestEigenmodePayload,
    RequestPhononDosPayload,
    RunPhononShakeTestPayload,
    ServerEvent,
    SetModelPayload,
    SinglePointUpdate,
    StartFlowGenerationPayload,
    StartRelaxationPayload,
    StopStreamPayload,
    StreamStopped,
    UndoPayload,
    # Phase C1
    AddAtomPayload,
    CommitteeEvaluatePayload,
    ComputeFormationEnergyPayload,
    HistoryEntryInfo,
    MakeSupercellPayload,
    MDComplete,
    MDFrame,
    RequestLocalAnalysisPayload,
    StartMDPayload,
    SubstituteFractionPayload,
    # Phase C2
    PrototypeSearchPayload,
    # Phase D
    HistoryEntry,
    RequestHistoryEntryPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router", "StudioSession", "PHONON_MAX_ATOMS", "HISTORY_LIMIT", "phonon_max_atoms"]

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
#: Default phonon shake-test limit (the studio-wide limit is ``structure_io.max_atoms()``);
#: configurable with ``LATTICEFLOW_PHONON_MAX_ATOMS`` - read it with :func:`phonon_max_atoms`.
PHONON_MAX_ATOMS = 64
#: Relative participation |e_k|^2 / max_k |e_k|^2 in the softest mode that marks an unstable actor.
ACTOR_PARTICIPATION_THRESHOLD = 0.5
#: spglib tolerance for ``apply_to_equivalent_sites``.
EQUIVALENT_SITES_SYMPREC = 1e-2
#: Highest atomic number covered by MACE-MP-0 (H .. Ac).
MAX_Z = 89
#: Frames a worker may produce ahead of the socket.
FRAME_WINDOW = 4
#: Max wait for a cancelled worker to reach its next checkpoint before moving on.
JOB_JOIN_TIMEOUT_S = 30.0
#: Poll interval of a worker blocked on the frame window (cancellation latency).
_SLOT_POLL_S = 0.05
_MAX_ERROR_DETAILS = 3
#: Max undo-history entries per session (the oldest is dropped).
HISTORY_LIMIT = 50

_C = ClientCommand
_PAYLOAD_MODELS: Dict[ClientCommand, type] = {
    _C.START_FLOW_GENERATION: StartFlowGenerationPayload,
    _C.PERTURB_OR_TRANSMUTE_ATOM: PerturbOrTransmuteAtomPayload,
    _C.START_RELAXATION: StartRelaxationPayload,
    _C.RUN_PHONON_SHAKE_TEST: RunPhononShakeTestPayload,
    _C.LOAD_STRUCTURE: LoadStructurePayload,
    _C.DELETE_ATOM: DeleteAtomPayload,
    _C.REQUEST_EIGENMODE: RequestEigenmodePayload,
    _C.FOLLOW_INSTABILITY: FollowInstabilityPayload,
    _C.STOP_STREAM: StopStreamPayload,
    _C.COMPUTE_BULK_MODULUS: ComputeBulkModulusPayload,
    _C.REFINE_STRUCTURE: RefineStructurePayload,
    _C.UNDO: UndoPayload,
    _C.REDO: RedoPayload,
    _C.REQUEST_PHONON_DOS: RequestPhononDosPayload,
    _C.CHECK_PHONON_CONVERGENCE: CheckPhononConvergencePayload,
    _C.COMPUTE_ELASTIC_TENSOR: ComputeElasticTensorPayload,
    _C.EXPORT_PHONON_DATA: ExportPhononDataPayload,
    _C.SET_MODEL: SetModelPayload,
    _C.REQUEST_LOCAL_ANALYSIS: RequestLocalAnalysisPayload,
    _C.MAKE_SUPERCELL: MakeSupercellPayload,
    _C.ADD_ATOM: AddAtomPayload,
    _C.COMPUTE_FORMATION_ENERGY: ComputeFormationEnergyPayload,
    _C.SUBSTITUTE_FRACTION: SubstituteFractionPayload,
    _C.START_MD: StartMDPayload,
    _C.COMMITTEE_EVALUATE: CommitteeEvaluatePayload,
    _C.PROTOTYPE_SEARCH: PrototypeSearchPayload,
    _C.REQUEST_HISTORY_ENTRY: RequestHistoryEntryPayload,
}
_STREAMING = frozenset(
    {
        _C.START_FLOW_GENERATION, _C.START_RELAXATION, _C.RUN_PHONON_SHAKE_TEST, _C.FOLLOW_INSTABILITY,
        _C.CHECK_PHONON_CONVERGENCE, _C.COMPUTE_ELASTIC_TENSOR, _C.SET_MODEL,
        _C.COMPUTE_FORMATION_ENERGY, _C.SUBSTITUTE_FRACTION, _C.START_MD, _C.COMMITTEE_EVALUATE,
        _C.PROTOTYPE_SEARCH,
    }
)
_INIT = "__INIT__"  # internal first FIFO item: load the default preset


def phonon_max_atoms() -> int:
    """Phonon shake-test atoms-per-cell limit (``Settings.phonon_max_atoms``, default 64)."""
    try:
        return int(get_settings().phonon_max_atoms)
    except ValueError:  # malformed environment: fall back to the default
        return PHONON_MAX_ATOMS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class CommandError(Exception):
    """A command failed with a protocol ``ErrorCode`` (reported as an ``ERROR`` event)."""

    def __init__(self, code: ErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message


class _JobCancelled(Exception):
    """Raised inside a worker when its cancel flag is set."""


def _message(event: ServerEvent, model: BaseModel) -> Dict[str, Any]:
    return {"type": event.value, "payload": model.model_dump(mode="json")}


def _error_message(code: ErrorCode | str, message: str, command: Optional[str]) -> Dict[str, Any]:
    code_value = code.value if isinstance(code, ErrorCode) else str(code)
    return _message(ServerEvent.ERROR, ErrorPayload(code=code_value, message=message, command=command))


def _summarize_validation(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    parts = []
    for err in errors[:_MAX_ERROR_DETAILS]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "payload"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    if len(errors) > _MAX_ERROR_DETAILS:
        parts.append(f"(+{len(errors) - _MAX_ERROR_DETAILS} more)")
    return "invalid payload: " + "; ".join(parts)


def canonical_element(symbol: str) -> Tuple[str, int]:
    """Normalise a chemical symbol ("na" -> "Na"); raise ``CommandError(INVALID_ELEMENT)``."""
    s = symbol.strip() if isinstance(symbol, str) else ""
    if s:
        s = s[0].upper() + s[1:].lower()
    z = atomic_numbers.get(s)
    if not s or z is None or z < 1 or s == "X":
        raise CommandError(ErrorCode.INVALID_ELEMENT, f"unknown chemical element {symbol!r}")
    if z > MAX_Z:
        raise CommandError(
            ErrorCode.INVALID_ELEMENT,
            f"element {s} (Z={z}) is outside the force-field coverage (Z <= {MAX_Z})",
        )
    return s, int(z)


def _wrap_in_place(atoms: Atoms) -> None:
    """Wrap fractional coordinates into [0, 1) (same rule as the engine / relaxer)."""
    wrap_in_place(atoms)


def equivalent_sites(atoms: Atoms, index: int, symprec: float = EQUIVALENT_SITES_SYMPREC) -> List[int]:
    """Indices in the spglib ``equivalent_atoms`` orbit of ``index`` (just ``[index]`` on failure)."""
    try:
        import spglib

        cell = (
            np.array(atoms.cell.array, dtype=float),
            np.mod(atoms.get_scaled_positions(wrap=False), 1.0),
            np.array(atoms.numbers, dtype=int),
        )
        dataset = spglib.get_symmetry_dataset(cell, symprec=symprec)
        if dataset is None:
            return [index]
        eq = dataset["equivalent_atoms"] if isinstance(dataset, dict) else dataset.equivalent_atoms
        eq = np.asarray(eq, dtype=int)
        return [int(i) for i in np.flatnonzero(eq == eq[index])]
    except Exception:  # noqa: BLE001 - symmetry is best effort
        logger.debug("spglib failed; transmuting only atom %d", index, exc_info=True)
        return [index]


def unstable_actor_flags(analysis: PhononAnalysis) -> np.ndarray:
    """Per-atom flag: participation in the softest mode >= threshold, if that mode is imaginary.

    The softest mode is band 0 at the q-point of ``min_frequency_thz``;
    participation is |e_k|^2 divided by its maximum over atoms k.
    """
    freqs = analysis.payload_frequencies
    q_index = int(np.argmin(freqs[:, 0]))
    flags = np.zeros(analysis.n_atoms, dtype=bool)
    if not freqs[q_index, 0] < -F_TOL_THZ:
        return flags
    _, modes = analysis.eigenvectors_at(q_index)
    participation = np.sum(np.abs(modes[0]) ** 2, axis=1)
    peak = float(participation.max())
    if peak > 0:
        flags = participation / peak >= ACTOR_PARTICIPATION_THRESHOLD
    return flags


def _n_branches(analysis: PhononAnalysis) -> int:
    """Phonon branches (3 N of the phonon cell: displayed cell, or primitive cell in primitive mode)."""
    n = getattr(analysis, "n_branches", None)
    return int(n) if n is not None else 3 * int(analysis.n_atoms)


class _ForceProgressEngine:
    """Engine proxy that reports every ``forces_batch`` chunk of a phonon run as PROGRESS.

    Everything else is delegated to the wrapped engine (same lock, same model).
    ``total`` is unknown up front (0); ``done`` counts the supercells evaluated.
    """

    def __init__(self, engine: UMLIPEngine, report: Callable[[int, str], None]) -> None:
        self._engine = engine
        self._report = report
        self._done = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    def forces_batch(self, atoms_list, **kwargs):
        out = self._engine.forces_batch(atoms_list, **kwargs)
        self._done += len(out)
        self._report(self._done, f"{self._done} supercell force evaluations")
        return out


def _flag_payload(payload: CrystalStatePayload, flags: Sequence[bool]) -> CrystalStatePayload:
    out = payload.model_copy(deep=True)
    for site in out.atoms:
        site.is_unstable_actor = bool(flags[site.index]) if site.index < len(flags) else False
    for bond in out.bonds:
        # Either end: soft modes are often carried by one sublattice (e.g. the O
        # cage in the SrTiO3 R-point rotation), whose atoms never bond each other.
        bond.is_unstable = bool(
            (bond.source_index < len(flags) and flags[bond.source_index])
            or (bond.target_index < len(flags) and flags[bond.target_index])
        )
    return out


def _snapshot(atoms: Atoms) -> Atoms:
    snap = atoms.copy()
    snap.calc = None
    return snap


@dataclass
class _Command:
    type: str                              # ClientCommand value, or _INIT
    payload: Optional[BaseModel] = None
    error: Optional[Dict[str, Any]] = None  # a parse/validation ERROR message to emit in order
    streams: bool = False                  # starts a stream (counts towards _pending_streams)
    request_id: Optional[str] = None       # Phase D: echoed on every message the command causes


@dataclass
class _HistoryEntry:
    atoms: Atoms                           # private snapshot (never handed out; copied on restore)
    payload: CrystalStatePayload
    label: str
    drag_atom: Optional[int] = None        # set for a coordinate-only drag commit (coalescing key)
    backend: Optional[str] = None          # backend_name of the engine that produced ``payload``


class _Job:
    """One in-flight streaming job (worker thread + cancel flag)."""

    def __init__(self, command: str, loop: asyncio.AbstractEventLoop, label: str = "",
                 join_on_cancel: bool = True, request_id: Optional[str] = None) -> None:
        self.command = command
        self.request_id = request_id        # of the command that started the stream
        self.label = label                  # history label prefix, e.g. "Relaxation"
        # False: after a cancel the worker touches no session state and no engine the
        # session uses (SET_MODEL: a model load / download), so nobody waits for it to exit.
        self.join_on_cancel = join_on_cancel
        self.frames_applied = 0             # loop thread only: outputs that replaced the active structure
        self.loop = loop
        self.cancel = threading.Event()
        self.done: asyncio.Future = loop.create_future()
        self.terminated = False             # loop thread only
        self.slots = threading.Semaphore(FRAME_WINDOW)
        self.thread: Optional[threading.Thread] = None

    # ---- worker-thread side
    def check(self) -> None:
        if self.cancel.is_set():
            raise _JobCancelled()

    def post(self, fn: Callable, *args) -> bool:
        try:
            self.loop.call_soon_threadsafe(fn, *args)
            return True
        except RuntimeError:  # event loop closed: the connection is gone
            self.cancel.set()
            return False

    def acquire_slot(self) -> None:
        while not self.slots.acquire(timeout=_SLOT_POLL_S):
            self.check()
        if self.cancel.is_set():
            self.slots.release()
            raise _JobCancelled()

    # ---- loop side
    def mark_done(self) -> None:
        if not self.done.done():
            self.done.set_result(None)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
class StudioSession:
    """Per-connection state machine for ``/ws/studio``."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine: Optional[UMLIPEngine] = None
        self.atoms: Optional[Atoms] = None
        self.payload: Optional[CrystalStatePayload] = None
        self.version = 0
        self._phonon: Optional[Tuple[PhononAnalysis, int]] = None
        self._job: Optional[_Job] = None
        self._fifo: Deque[_Command] = collections.deque()
        self._fifo_event = asyncio.Event()
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._pending_streams = 0
        self._closing = False
        self.coalesced_perturbs = 0
        # Phase D: request_id stamped on every message queued by ``_send`` (loop thread only).
        # The processor sets it for the command it runs; synchronous loop-thread sections that
        # act for another request (job outputs, a stream's cancellation) override it with
        # ``_as_request`` and restore it, so interleaving at an ``await`` cannot mislabel.
        self._rid: Optional[str] = None
        # Undo history (loop thread only).
        self._history: List[_HistoryEntry] = []
        self._history_pos = -1
        self._drag_open: Optional[int] = None   # atom of the open drag gesture (top entry), if any
        self._entry_version = -1                # self.version when the current entry became active

    # ================================================================ lifecycle
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        sender = asyncio.create_task(self._sender(), name="ws-studio-sender")
        processor = asyncio.create_task(self._processor(), name="ws-studio-processor")
        self._enqueue(_Command(_INIT))
        try:
            await self._receive_loop()
        finally:
            await self._shutdown(sender, processor)

    async def _shutdown(self, sender: asyncio.Task, processor: asyncio.Task) -> None:
        # Synchronous part first: it must happen even if this coroutine is cancelled.
        self._closing = True
        job = self._job
        if job is not None:
            job.cancel.set()
            job.terminated = True
        processor.cancel()
        sender.cancel()
        try:
            await asyncio.gather(processor, sender, return_exceptions=True)
            if job is not None and not job.done.done() and job.join_on_cancel:
                try:
                    await asyncio.wait_for(asyncio.shield(job.done), JOB_JOIN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning("%s worker still running %.0f s after disconnect", job.command, JOB_JOIN_TIMEOUT_S)
        except asyncio.CancelledError:
            # The server is tearing the connection down; workers exit on their own (cancel flag set).
            raise

    async def _receive_loop(self) -> None:
        while True:
            try:
                message = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                raw = message.get("bytes")
                try:
                    text = raw.decode("utf-8") if raw is not None else None
                except UnicodeDecodeError:
                    text = None
            command = self._parse(text)
            if command.type == _C.STOP_STREAM.value and command.error is None:
                self._handle_stop_from_receiver(command)
            else:
                self._enqueue(command)

    # ================================================================ parsing
    def _parse(self, text: Optional[str]) -> _Command:
        rid: Optional[str] = None

        def bad(code: ErrorCode, msg: str, cmd: Optional[str]) -> _Command:
            return _Command(cmd or "", error=_error_message(code, msg, cmd), request_id=rid)

        if text is None:
            return bad(ErrorCode.INVALID_MESSAGE, "message is not UTF-8 text", None)
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):
            return bad(ErrorCode.INVALID_MESSAGE, "message is not valid JSON", None)
        if isinstance(raw, dict) and raw.get("payload", {}) is None:
            raw = {**raw, "payload": {}}
        if isinstance(raw, dict) and isinstance(raw.get("request_id"), str) and len(raw["request_id"]) <= 64:
            rid = raw["request_id"]  # echoed even if the rest of the envelope is invalid
        try:
            env = Envelope.model_validate(raw)
        except ValidationError:
            return bad(
                ErrorCode.INVALID_MESSAGE,
                'message must be an envelope {"type": <string>, "payload": <object>}',
                None,
            )
        try:
            ctype = ClientCommand(env.type)
        except ValueError:
            return bad(ErrorCode.UNKNOWN_COMMAND, f"unknown command type {env.type!r}", env.type)
        try:
            payload = _PAYLOAD_MODELS[ctype].model_validate(env.payload)
        except ValidationError as exc:
            return bad(ErrorCode.INVALID_PAYLOAD, _summarize_validation(exc), ctype.value)
        streams = ctype in _STREAMING or (
            ctype is _C.PERTURB_OR_TRANSMUTE_ATOM and bool(payload.auto_relax)
        )
        return _Command(ctype.value, payload=payload, streams=streams, request_id=env.request_id)

    # ================================================================ queues
    def _enqueue(self, command: _Command) -> None:
        if command.streams:
            self._pending_streams += 1
        self._fifo.append(command)
        self._fifo_event.set()

    async def _next_command(self) -> _Command:
        while not self._fifo:
            self._fifo_event.clear()
            await self._fifo_event.wait()
        return self._fifo.popleft()

    def _send(self, message: Dict[str, Any], release: Optional[Callable[[], None]] = None) -> None:
        if self._closing:
            if release is not None:
                release()
            return
        if self._rid is not None:
            message = {**message, "request_id": self._rid}
        self._outbox.put_nowait((message, release))

    @contextlib.contextmanager
    def _as_request(self, request_id: Optional[str]) -> Iterator[None]:
        """Loop thread, synchronous sections only: stamp sends with ``request_id``, then restore."""
        previous, self._rid = self._rid, request_id
        try:
            yield
        finally:
            self._rid = previous

    def _send_error(self, code: ErrorCode | str, message: str, command: Optional[str]) -> None:
        self._send(_error_message(code, message, command))

    async def _sender(self) -> None:
        while True:
            message, release = await self._outbox.get()
            try:
                if self.ws.client_state is not WebSocketState.CONNECTED:
                    raise RuntimeError("websocket is not connected")
                await self.ws.send_json(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - peer went away; the receive loop sees the disconnect
                logger.debug("send failed; dropping outgoing messages", exc_info=True)
                self._closing = True
                job = self._job
                if job is not None:
                    job.cancel.set()
            finally:
                if release is not None:
                    release()

    # ================================================================ processor
    @staticmethod
    def _is_drag(command: _Command) -> bool:
        p = command.payload
        return (
            command.type == _C.PERTURB_OR_TRANSMUTE_ATOM.value
            and command.error is None
            and p.new_cart_coords is not None
            and p.new_element is None
            and not p.auto_relax
        )

    def _coalesce(self, command: _Command) -> _Command:
        while (
            self._is_drag(command)
            and self._fifo
            and self._is_drag(self._fifo[0])
            and self._fifo[0].payload.atom_index == command.payload.atom_index
        ):
            command = self._fifo.popleft()
            self.coalesced_perturbs += 1
        return command

    async def _processor(self) -> None:
        while True:
            command = self._coalesce(await self._next_command())
            self._rid = command.request_id
            try:
                if command.error is not None:
                    self._send(command.error)
                else:
                    await self._dispatch(command)
            except asyncio.CancelledError:
                raise
            except CommandError as exc:
                self._send_error(exc.code, exc.message, command.type if command.type != _INIT else None)
            except Exception as exc:  # noqa: BLE001 - never let one command kill the session
                logger.exception("command %s failed", command.type)
                self._send_error(
                    ErrorCode.COMPUTE_FAILED,
                    f"{type(exc).__name__}: {exc}",
                    command.type if command.type != _INIT else None,
                )
            finally:
                self._rid = None
                if command.streams:
                    self._pending_streams -= 1

    async def _dispatch(self, command: _Command) -> None:
        handler = {
            _INIT: self._cmd_init,
            _C.START_FLOW_GENERATION.value: self._cmd_flow,
            _C.PERTURB_OR_TRANSMUTE_ATOM.value: self._cmd_perturb,
            _C.START_RELAXATION.value: self._cmd_relax,
            _C.RUN_PHONON_SHAKE_TEST.value: self._cmd_phonon,
            _C.LOAD_STRUCTURE.value: self._cmd_load,
            _C.DELETE_ATOM.value: self._cmd_delete,
            _C.REQUEST_EIGENMODE.value: self._cmd_eigenmode,
            _C.FOLLOW_INSTABILITY.value: self._cmd_follow,
            _C.STOP_STREAM.value: self._cmd_stop,
            _C.COMPUTE_BULK_MODULUS.value: self._cmd_bulk_modulus,
            _C.REFINE_STRUCTURE.value: self._cmd_refine,
            _C.UNDO.value: self._cmd_undo,
            _C.REDO.value: self._cmd_redo,
            _C.REQUEST_PHONON_DOS.value: self._cmd_phonon_dos,
            _C.CHECK_PHONON_CONVERGENCE.value: self._cmd_phonon_convergence,
            _C.COMPUTE_ELASTIC_TENSOR.value: self._cmd_elastic,
            _C.EXPORT_PHONON_DATA.value: self._cmd_export_phonon,
            _C.SET_MODEL.value: self._cmd_set_model,
            _C.REQUEST_LOCAL_ANALYSIS.value: self._cmd_local_analysis,
            _C.MAKE_SUPERCELL.value: self._cmd_make_supercell,
            _C.ADD_ATOM.value: self._cmd_add_atom,
            _C.COMPUTE_FORMATION_ENERGY.value: self._cmd_formation_energy,
            _C.SUBSTITUTE_FRACTION.value: self._cmd_substitute,
            _C.START_MD.value: self._cmd_md,
            _C.COMMITTEE_EVALUATE.value: self._cmd_committee,
            _C.PROTOTYPE_SEARCH.value: self._cmd_prototype_search,
            _C.REQUEST_HISTORY_ENTRY.value: self._cmd_history_entry,
        }[command.type]
        await handler(command.payload)

    # ================================================================ job control
    def _job_live(self) -> bool:
        return self._job is not None and not self._job.terminated

    def _stop_now(self) -> None:
        """Cancel the live job (queues STREAM_STOPPED for it), or report that nothing was running."""
        job = self._job
        if job is not None and not job.terminated:
            self._cancel_live(job)
        else:
            self._send(_message(ServerEvent.STREAM_STOPPED, StreamStopped(stopped=None)))

    def _cancel_live(self, job: _Job) -> None:
        """Loop thread: terminate a live job, queue its STREAM_STOPPED and record what it left behind."""
        job.cancel.set()
        job.terminated = True
        pushed = job.frames_applied > 0 and self.atoms is not None and self.payload is not None
        with self._as_request(job.request_id):  # the stream's id, whoever triggered the cancel
            self._send(_message(ServerEvent.STREAM_STOPPED, StreamStopped(stopped=job.command, history_pushed=pushed)))
            if pushed:
                self._history_push(f"{job.label} (stopped)")

    def _handle_stop_from_receiver(self, command: _Command) -> None:
        if self._pending_streams > 0:
            # A stream is queued or being started: stop it in order, not whatever ran before it.
            self._enqueue(command)
        else:
            with self._as_request(command.request_id):  # used only by {stopped: null}
                self._stop_now()

    async def _cmd_stop(self, _payload: StopStreamPayload) -> None:
        self._stop_now()

    async def _end_job(self) -> None:
        """Implicitly cancel the live job (if any) and wait (bounded) for its worker to exit."""
        job = self._job
        if job is None:
            return
        if not job.terminated:
            self._cancel_live(job)
        if not job.done.done() and job.join_on_cancel:
            try:
                await asyncio.wait_for(asyncio.shield(job.done), JOB_JOIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s worker did not reach a cancellation point within %.0f s; continuing",
                    job.command, JOB_JOIN_TIMEOUT_S,
                )
        if self._job is job:
            self._job = None

    def _launch(self, command: str, label: str, target: Callable[..., None], *args,
                join_on_cancel: bool = True) -> _Job:
        assert self.loop is not None
        job = _Job(command, self.loop, label, join_on_cancel, self._rid)
        self._job = job
        self._drag_open = None  # a stream ends the current drag gesture

        def run() -> None:
            try:
                target(job, *args)
            except (_JobCancelled, FlowCancelled, PhononCancelled):
                pass
            except CommandError as exc:
                job.post(self._apply_job_output, job, [_error_message(exc.code, exc.message, command)], None, None, True)
            except Exception as exc:  # noqa: BLE001
                if job.cancel.is_set():
                    logger.debug("%s worker raised after cancellation", command, exc_info=True)
                else:
                    logger.exception("%s stream failed", command)
                    job.post(
                        self._apply_job_output, job,
                        [_error_message(ErrorCode.COMPUTE_FAILED, f"{type(exc).__name__}: {exc}", command)],
                        None, None, True,
                    )
            finally:
                job.post(job.mark_done)

        job.thread = threading.Thread(target=run, name=f"ws-job-{command.lower()}", daemon=True)
        job.thread.start()
        return job

    # ---- worker -> loop
    def _apply_job_output(
        self,
        job: _Job,
        messages: List[Dict[str, Any]],
        atoms: Optional[Atoms],
        payload: Optional[CrystalStatePayload],
        terminal: bool,
        on_apply: Optional[Callable[[], None]] = None,
        release: Optional[Callable[[], None]] = None,
        history_label: Optional[str] = None,
    ) -> None:
        """Loop thread: apply one worker output unless the job was already terminated.

        A terminal output pushes a history entry: ``history_label`` if given,
        else "<label> (failed)" when the job had already changed the structure.
        """
        if job.terminated or self._closing:
            if release is not None:
                release()
            return
        with self._as_request(job.request_id):
            if atoms is not None:
                self.atoms = atoms
                self.version += 1
                job.frames_applied += 1
            if payload is not None:
                self.payload = payload
            if on_apply is not None:
                on_apply()
            if terminal:
                if history_label is None and job.frames_applied > 0:
                    history_label = f"{job.label} (failed)"
                if history_label is not None and (self.atoms is None or self.payload is None):
                    history_label = None  # _history_push would be a no-op
                if history_label is not None:
                    for message in messages:  # the HISTORY_STATE pushed below follows this ERROR
                        if message.get("type") == ServerEvent.ERROR.value:
                            message["payload"]["history_pushed"] = True
            for i, message in enumerate(messages):
                self._send(message, release if i == len(messages) - 1 else None)
            if terminal:
                job.terminated = True
                if history_label is not None:
                    self._history_push(history_label)

    def _emit_frame(self, job: _Job, event: ServerEvent, frame: BaseModel, atoms: Atoms,
                    payload: CrystalStatePayload) -> None:
        """Worker thread: send one non-terminal frame (blocks on the frame window)."""
        message = _message(event, frame)
        job.acquire_slot()
        if not job.post(self._apply_job_output, job, [message], atoms, payload, False, None, job.slots.release):
            raise _JobCancelled()

    def _emit_terminal(self, job: _Job, messages: List[Dict[str, Any]], atoms: Optional[Atoms],
                       payload: Optional[CrystalStatePayload], on_apply: Optional[Callable[[], None]] = None,
                       history_label: Optional[str] = None) -> None:
        job.post(self._apply_job_output, job, messages, atoms, payload, True, on_apply, None, history_label)

    # ================================================================ helpers
    def _require_atoms(self) -> Atoms:
        if self.atoms is None or self.payload is None:
            raise CommandError(ErrorCode.NO_ACTIVE_STRUCTURE, "no active structure; send LOAD_STRUCTURE first")
        return self.atoms

    def _check_atom_index(self, index: int) -> None:
        n = len(self._require_atoms())
        if not 0 <= index < n:
            raise CommandError(ErrorCode.INDEX_OUT_OF_RANGE, f"atom_index {index} out of range [0, {n})")

    def _valid_phonon(self) -> PhononAnalysis:
        if self._phonon is None or self._phonon[1] != self.version:
            raise CommandError(
                ErrorCode.NO_PHONON_ANALYSIS,
                "no phonon analysis for the current structure; run RUN_PHONON_SHAKE_TEST first",
            )
        return self._phonon[0]

    @staticmethod
    def _check_mode_index(analysis: PhononAnalysis, q_index: int, band_index: int) -> None:
        if not 0 <= q_index < analysis.n_q:
            raise CommandError(ErrorCode.INDEX_OUT_OF_RANGE, f"q_index {q_index} out of range [0, {analysis.n_q})")
        nb = _n_branches(analysis)
        if not 0 <= band_index < nb:
            raise CommandError(ErrorCode.INDEX_OUT_OF_RANGE, f"band_index {band_index} out of range [0, {nb})")

    async def _get_engine(self) -> UMLIPEngine:
        if self.engine is None:
            self.engine = await asyncio.to_thread(UMLIPEngine.get_instance)
        return self.engine

    async def _evaluate(self, atoms: Atoms, bond_mode: str = "fast") -> CrystalStatePayload:
        engine = await self._get_engine()
        return await asyncio.to_thread(engine.evaluate, atoms, bond_mode)

    def _commit(self, atoms: Atoms, payload: CrystalStatePayload, label: str,
                drag_atom: Optional[int] = None) -> None:
        self.atoms = atoms
        self.payload = payload
        self.version += 1
        self._send(_message(ServerEvent.SINGLE_POINT_UPDATE, SinglePointUpdate(crystal=payload)))
        self._history_push(label, drag_atom)

    # ================================================================ history
    def _send_history(self) -> None:
        entry = self._history[self._history_pos]
        state = HistoryState(
            can_undo=self._history_pos > 0,
            can_redo=self._history_pos < len(self._history) - 1,
            position=self._history_pos,
            length=len(self._history),
            label=entry.label,
            entries=[
                HistoryEntryInfo(
                    position=i, label=e.label, formula=e.payload.formula, n_atoms=len(e.payload.atoms),
                    energy_per_atom=e.payload.energy_per_atom, spacegroup_symbol=e.payload.spacegroup_symbol,
                )
                for i, e in enumerate(self._history)
            ],
        )
        self._send(_message(ServerEvent.HISTORY_STATE, state))

    def _history_push(self, label: str, drag_atom: Optional[int] = None) -> None:
        """Loop thread: record the active structure as the new current entry, then send HISTORY_STATE.

        A drag commit (``drag_atom`` set) of the atom of the open drag gesture
        replaces the current (top) entry instead of pushing.
        """
        if self.atoms is None or self.payload is None:
            return
        backend = self.engine.backend_name if self.engine is not None else None
        entry = _HistoryEntry(_snapshot(self.atoms), self.payload, label, drag_atom, backend)
        h = self._history
        if (
            drag_atom is not None
            and self._drag_open == drag_atom
            and h
            and self._history_pos == len(h) - 1
            and h[-1].drag_atom == drag_atom
        ):
            h[-1] = entry
        else:
            del h[self._history_pos + 1:]          # a new branch drops the redo entries
            h.append(entry)
            if len(h) > HISTORY_LIMIT:
                del h[: len(h) - HISTORY_LIMIT]
            self._history_pos = len(h) - 1
        self._drag_open = drag_atom
        self._entry_version = self.version
        self._send_history()

    async def _history_move(self, delta: int) -> None:
        await self._end_job()  # a live stream is stopped (and recorded) first
        target = self._history_pos + delta
        if not self._history or not 0 <= target < len(self._history):
            what = "undo" if delta < 0 else "redo"
            raise CommandError(ErrorCode.INVALID_OPERATION, f"nothing to {what}")
        entry = self._history[target]
        engine = await self._get_engine()
        if entry.backend != engine.backend_name:
            # The entry was evaluated with another force field (before a SET_MODEL): history
            # entries are structures, so re-evaluate with the session's current model. The
            # entry keeps the new payload (until the model changes again).
            entry.payload = await self._evaluate(entry.atoms)
            entry.backend = engine.backend_name
        self._history_pos = target
        self.atoms = _snapshot(entry.atoms)
        self.payload = entry.payload
        self.version += 1  # invalidates the phonon cache
        self._drag_open = None
        self._entry_version = self.version
        self._send(_message(ServerEvent.SINGLE_POINT_UPDATE, SinglePointUpdate(crystal=entry.payload)))
        self._send_history()

    async def _cmd_undo(self, _payload: UndoPayload) -> None:
        await self._history_move(-1)

    async def _cmd_redo(self, _payload: RedoPayload) -> None:
        await self._history_move(+1)

    async def _cmd_history_entry(self, p: RequestHistoryEntryPayload) -> None:
        """Phase D: the stored structure of one entry (instant, cancels nothing, changes nothing)."""
        n = len(self._history)
        if not 0 <= p.position < n:
            raise CommandError(ErrorCode.INDEX_OUT_OF_RANGE, f"position {p.position} out of range [0, {n})")
        entry = self._history[p.position]
        self._send(_message(ServerEvent.HISTORY_ENTRY,
                            HistoryEntry(position=p.position, label=entry.label, crystal=entry.payload)))

    # ================================================================ instant commands
    async def _cmd_init(self, _payload: Any) -> None:
        await self._get_engine()
        atoms = load_preset(DEFAULT_PRESET_ID)
        payload = await self._evaluate(atoms)
        self._commit(atoms, payload, f"Load preset {DEFAULT_PRESET_ID}")

    @staticmethod
    def _check_elements(atoms: Atoms) -> None:
        bad = sorted({int(z) for z in atoms.numbers if not 1 <= int(z) <= MAX_Z})
        if bad:
            raise CommandError(
                ErrorCode.INVALID_ELEMENT,
                f"atomic number(s) {bad} outside the force-field coverage (1 <= Z <= {MAX_Z})",
            )

    async def _cmd_load(self, p: LoadStructurePayload) -> None:
        if p.preset_id is not None:
            try:
                atoms = load_preset(p.preset_id)
            except KeyError:
                raise CommandError(ErrorCode.UNKNOWN_PRESET, f"unknown preset id {p.preset_id!r}") from None
            label = f"Load preset {p.preset_id}"
        else:
            if p.cif is not None:
                build, label = (lambda: atoms_from_cif(p.cif)), "Load CIF"
            elif p.text is not None:
                build, label = (lambda: structure_io.atoms_from_text(p.text, p.text_format)), "Load file"
            else:
                build, label = (lambda: structure_io.atoms_from_payload(p.crystal)), "Load structure"
            try:
                atoms = await asyncio.to_thread(build)
            except StructureIOError as exc:
                raise CommandError(exc.code, str(exc)) from None
            except Exception as exc:  # noqa: BLE001 - any other construction failure is a bad structure
                if p.cif is not None:
                    raise
                raise CommandError(ErrorCode.INVALID_STRUCTURE, f"{type(exc).__name__}: {exc}") from None
            self._check_elements(atoms)
        await self._end_job()
        payload = await self._evaluate(atoms)
        self._commit(atoms, payload, label)

    async def _cmd_perturb(self, p: PerturbOrTransmuteAtomPayload) -> None:
        self._check_atom_index(p.atom_index)
        if p.new_cart_coords is None and p.new_element is None:
            raise CommandError(ErrorCode.INVALID_PAYLOAD, "nothing to change: give new_cart_coords and/or new_element")
        element = canonical_element(p.new_element) if p.new_element is not None else None
        coords = None
        if p.new_cart_coords is not None:
            coords = np.asarray(p.new_cart_coords, dtype=float)
            if not np.all(np.isfinite(coords)):
                raise CommandError(ErrorCode.INVALID_PAYLOAD, "new_cart_coords must be finite")
        await self._end_job()
        atoms = _snapshot(self._require_atoms())
        drag_atom: Optional[int] = None
        if element is not None:
            symbol, z = element
            targets = equivalent_sites(atoms, p.atom_index) if p.apply_to_equivalent_sites else [p.atom_index]
            if len(targets) > 1:
                label = f"Transmute {len(targets)} sites to {symbol}"
            else:
                label = f"Transmute atom {p.atom_index} to {symbol}"
            numbers = atoms.get_atomic_numbers().copy()
            numbers[targets] = z
            had_masses = atoms.has("masses")
            masses = atoms.get_masses().copy() if had_masses else None
            atoms.set_atomic_numbers(numbers)
            if had_masses:
                masses[targets] = atomic_masses[z]
                atoms.set_masses(masses)
        else:
            label = f"Move atom {p.atom_index}"
            if not p.auto_relax:
                drag_atom = p.atom_index  # one drag gesture = one undo step
        if coords is not None:
            positions = atoms.positions.copy()
            positions[p.atom_index] = coords
            atoms.positions = positions
            _wrap_in_place(atoms)
        payload = await self._evaluate(atoms)
        self._commit(atoms, payload, label, drag_atom)
        if p.auto_relax:
            self._start_relaxation(StartRelaxationPayload(), _C.PERTURB_OR_TRANSMUTE_ATOM.value)

    async def _cmd_delete(self, p: DeleteAtomPayload) -> None:
        self._check_atom_index(p.atom_index)
        if len(self.atoms) <= 1:
            raise CommandError(ErrorCode.INVALID_OPERATION, "cannot delete the last atom of the structure")
        await self._end_job()
        atoms = _snapshot(self._require_atoms())
        symbol = atoms[p.atom_index].symbol
        del atoms[p.atom_index]
        payload = await self._evaluate(atoms)
        self._commit(atoms, payload, f"Delete atom {p.atom_index} ({symbol})")

    async def _cmd_refine(self, p: RefineStructurePayload) -> None:
        self._require_atoms()
        await self._end_job()
        atoms = _snapshot(self._require_atoms())
        try:
            refined = await asyncio.to_thread(structure_io.refine_atoms, atoms, cell=p.cell, symprec=p.symprec)
        except StructureIOError as exc:
            raise CommandError(exc.code, str(exc)) from None
        payload = await self._evaluate(refined)
        self._commit(refined, payload, f"Refine symmetry ({payload.spacegroup_symbol}, {p.cell})")

    async def _cmd_bulk_modulus(self, p: ComputeBulkModulusPayload) -> None:
        atoms = _snapshot(self._require_atoms())
        version = self.version
        engine = await self._get_engine()
        try:
            result = await asyncio.to_thread(
                relaxer.bulk_modulus_result, atoms, engine, strain_window=p.strain_window, n_points=p.n_points,
            )
        except ValueError as exc:
            raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
        if self.version == version and self.payload is not None:
            # Same structure as when we started: remember K on it (and on its history entry).
            self.payload.bulk_modulus_gpa = result.k_gpa
            if self._entry_version == version and self._history:
                self._history[self._history_pos].payload.bulk_modulus_gpa = result.k_gpa
        self._send(_message(ServerEvent.BULK_MODULUS_RESULT, result))

    async def _cmd_phonon_dos(self, p: RequestPhononDosPayload) -> None:
        analysis = self._valid_phonon()
        if not p.t_max > p.t_min:
            raise CommandError(ErrorCode.INVALID_PAYLOAD, f"t_max ({p.t_max}) must exceed t_min ({p.t_min})")
        if p.mesh is not None and any(m < 1 for m in p.mesh):
            raise CommandError(ErrorCode.INVALID_PAYLOAD, f"mesh entries must be >= 1, got {p.mesh}")
        data = await asyncio.to_thread(
            lambda: analysis.dos_and_thermal(mesh=p.mesh, t_min=p.t_min, t_max=p.t_max, t_step=p.t_step)
        )
        self._send(_message(ServerEvent.PHONON_DOS_DATA, data))

    async def _cmd_eigenmode(self, p: RequestEigenmodePayload) -> None:
        analysis = self._valid_phonon()
        self._check_mode_index(analysis, p.q_index, p.band_index)
        try:
            mode = await asyncio.to_thread(analysis.eigenmode, p.q_index, p.band_index)
        except IndexError as exc:
            raise CommandError(ErrorCode.INDEX_OUT_OF_RANGE, str(exc)) from None
        self._send(_message(ServerEvent.EIGENMODE_DATA, EigenmodeData(eigenmode=mode)))

    # ================================================================ streaming commands
    async def _cmd_relax(self, p: StartRelaxationPayload) -> None:
        self._require_atoms()
        await self._end_job()
        self._start_relaxation(p, _C.START_RELAXATION.value)

    def _start_relaxation(self, p: StartRelaxationPayload, command: str) -> None:
        work = _snapshot(self._require_atoms())
        engine = self.engine

        def frames(job: _Job) -> Iterator[RelaxationFrame]:
            return relaxer.iter_relaxation(
                work, engine, fmax=p.fmax, max_steps=p.max_steps, relax_cell=p.relax_cell,
                should_cancel=job.cancel.is_set, fix_symmetry=p.fix_symmetry, pressure_gpa=p.pressure_gpa,
                stress_tol_gpa=p.stress_tol_gpa, optimizer=p.optimizer,
            )

        self._launch(command, "Relaxation", lambda job: self._relaxation_worker(job, work, frames(job)))

    def _relaxation_worker(self, job: _Job, atoms: Atoms, frames: Iterator[RelaxationFrame]) -> None:
        """Worker: stream RELAXATION_FRAMEs; ``atoms`` is relaxed in place by ``frames``."""
        try:
            while True:
                job.check()
                try:
                    frame = next(frames)
                except StopIteration:
                    job.check()  # the relaxer stops silently when cancelled
                    raise RuntimeError("relaxation ended without a final frame")
                except RelaxationDivergedError as exc:  # the starting structure itself is bad
                    raise CommandError(ErrorCode.RELAXATION_DIVERGED, str(exc)) from None
                snap = _snapshot(atoms)  # the generator is suspended: atoms *is* this frame
                if not frame.final:
                    self._emit_frame(job, ServerEvent.RELAXATION_FRAME, frame, snap, frame.crystal)
                    continue
                messages = [_message(ServerEvent.RELAXATION_FRAME, frame)]
                outcome = "converged" if frame.converged else "max steps"
                try:
                    next(frames)  # a diverged run raises here, after its final frame
                except StopIteration:
                    pass
                except RelaxationDivergedError as exc:
                    error = _error_message(ErrorCode.RELAXATION_DIVERGED, str(exc), job.command)
                    error["payload"]["after_terminal"] = True  # follows the final RELAXATION_FRAME
                    messages.append(error)
                    outcome = "diverged, restored"
                self._emit_terminal(job, messages, snap, frame.crystal, history_label=f"{job.label} ({outcome})")
                return
        finally:
            close = getattr(frames, "close", None)
            if close is not None:
                close()

    async def _cmd_flow(self, p: StartFlowGenerationPayload) -> None:
        engine = await self._get_engine()
        await self._end_job()

        def worker(job: _Job) -> None:
            generator = RiemannianFlowGenerator(engine)
            try:
                frames = generator.iter_frames(p, should_cancel=job.cancel.is_set)
            except ValueError as exc:
                raise CommandError(ErrorCode.GENERATION_FAILED, str(exc)) from None
            try:
                for frame in frames:
                    job.check()
                    self._emit_frame(job, ServerEvent.FLOW_TRAJECTORY_FRAME, frame,
                                     _snapshot(generator.current_atoms), frame.crystal)
            except ValueError as exc:
                raise CommandError(ErrorCode.GENERATION_FAILED, str(exc)) from None
            finally:
                frames.close()
            final = generator.final_atoms
            if final is None:
                job.check()
                raise CommandError(ErrorCode.GENERATION_FAILED, "flow ended without a final frame")
            final = _snapshot(final)
            job.check()
            crystal = engine.evaluate(final, bond_mode="crystalnn")
            job.check()
            crystal.bulk_modulus_gpa = relaxer.estimate_bulk_modulus(final, engine)
            job.check()
            messages = [_message(ServerEvent.CRYSTAL_STATE_FINAL, CrystalStateFinal(crystal=crystal))]
            if p.top_k > 1:
                candidates = generator.top_candidates(p.top_k, final, crystal, should_cancel=job.cancel.is_set)
                job.check()
                messages.insert(0, _message(ServerEvent.GENERATION_CANDIDATES, candidates))
            self._emit_terminal(job, messages, final, crystal, history_label=job.label)

        self._launch(_C.START_FLOW_GENERATION.value, "Flow generation", worker)

    async def _cmd_phonon(self, p: RunPhononShakeTestPayload) -> None:
        atoms = self._require_atoms()
        limit = phonon_max_atoms()
        if len(atoms) > limit:
            raise CommandError(
                ErrorCode.STRUCTURE_TOO_LARGE,
                f"phonon shake-test supports at most {limit} atoms per cell (structure has {len(atoms)})",
            )
        if any(n < 1 for n in p.supercell_dim):
            raise CommandError(ErrorCode.INVALID_PAYLOAD, f"supercell_dim entries must be >= 1, got {p.supercell_dim}")
        await self._end_job()
        work = _snapshot(self.atoms)
        base_payload = self.payload
        version = self.version
        engine = self.engine

        def worker(job: _Job) -> None:
            try:
                analysis = phonon_engine.run_phonon_analysis(
                    work, engine, supercell_dim=p.supercell_dim, displacement_distance=p.displacement_distance,
                    cell_mode=p.cell_mode, min_supercell_length=p.min_supercell_length,
                    should_cancel=job.cancel.is_set,
                )
            except ValueError as exc:  # e.g. no primitive cell / supercell over the atom budget
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            self._emit_phonon_terminal(job, analysis, base_payload, version)

        self._launch(_C.RUN_PHONON_SHAKE_TEST.value, "Phonon shake-test", worker)

    def _emit_phonon_terminal(self, job: _Job, analysis: PhononAnalysis, base_payload: CrystalStatePayload,
                              version: int, lead: Sequence[Dict[str, Any]] = ()) -> None:
        """Worker: terminal ``[*lead, PHONON_ANALYSIS_COMPLETE, flagging SINGLE_POINT_UPDATE]``.

        The analysis becomes the session's phonon cache if the structure is unchanged.
        """
        flagged = _flag_payload(base_payload, unstable_actor_flags(analysis))
        messages = [
            *lead,
            _message(ServerEvent.PHONON_ANALYSIS_COMPLETE, analysis.result),
            _message(ServerEvent.SINGLE_POINT_UPDATE, SinglePointUpdate(crystal=flagged)),
        ]

        def on_apply() -> None:
            if self.version == version:
                self._phonon = (analysis, version)

        job.check()
        self._emit_terminal(job, messages, None, flagged, on_apply)

    def _post_progress(self, job: _Job, done: int, total: int, message: str = "") -> None:
        """Any thread: queue a non-terminal PROGRESS event of ``job`` (dropped once it is terminated)."""
        msg = _message(ServerEvent.PROGRESS, Progress(command=job.command, done=int(done), total=int(total),
                                                       message=str(message)))
        job.post(self._apply_job_output, job, [msg], None, None, False)

    async def _cmd_phonon_convergence(self, p: CheckPhononConvergencePayload) -> None:
        analysis = self._valid_phonon()
        n_cell = _n_branches(analysis) // 3          # atoms of the phonon cell (primitive in primitive mode)
        current = [int(n) for n in (getattr(analysis, "supercell_dim", None) or analysis.result.supercell_dim)]
        limit = int(getattr(phonon_engine, "PHONON_SUPERCELL_MAX_ATOMS", 600))
        if p.supercell_dim is not None:
            dim = [int(n) for n in p.supercell_dim]
            if any(n < 1 for n in dim):
                raise CommandError(ErrorCode.INVALID_PAYLOAD, f"supercell_dim entries must be >= 1, got {dim}")
            if n_cell * int(np.prod(dim)) > limit:
                raise CommandError(
                    ErrorCode.STRUCTURE_TOO_LARGE,
                    f"supercell {dim} of the {n_cell}-atom phonon cell has {n_cell * int(np.prod(dim))} atoms "
                    f"(limit {limit})",
                )
        else:
            try:
                dim = [int(n) for n in phonon_engine.next_supercell_dim(current, n_cell, max_atoms=limit)]
            except ValueError as exc:
                raise CommandError(ErrorCode.STRUCTURE_TOO_LARGE, str(exc)) from None
        await self._end_job()
        work = _snapshot(self._require_atoms())
        base_payload = self.payload
        version = self.version
        engine = self.engine
        cell_mode = getattr(analysis, "cell_mode", "as_displayed")
        displacement = float(getattr(analysis, "displacement_distance", 0.01))

        def worker(job: _Job) -> None:
            self._post_progress(job, 0, 0, f"Phonons in a {'x'.join(map(str, dim))} supercell")
            counting = _ForceProgressEngine(engine, lambda done, msg: self._post_progress(job, done, 0, msg))
            try:
                larger = phonon_engine.run_phonon_analysis(
                    work, counting, supercell_dim=dim, displacement_distance=displacement,
                    cell_mode=cell_mode, min_supercell_length=None, should_cancel=job.cancel.is_set,
                )
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.STRUCTURE_TOO_LARGE, str(exc)) from None
            job.check()
            comparison = phonon_engine.compare_phonon_analyses(analysis, larger)
            job.check()
            lead = [_message(ServerEvent.PHONON_CONVERGENCE_RESULT, comparison)]
            self._emit_phonon_terminal(job, larger, base_payload, version, lead)

        self._launch(_C.CHECK_PHONON_CONVERGENCE.value, "Phonon convergence", worker)

    async def _cmd_elastic(self, p: ComputeElasticTensorPayload) -> None:
        atoms = self._require_atoms()
        if not (bool(np.all(atoms.pbc)) and atoms.cell.rank == 3):
            raise CommandError(ErrorCode.INVALID_OPERATION, "the elastic tensor needs a fully periodic 3D cell")
        await self._end_job()
        work = _snapshot(atoms)
        engine = self.engine

        def worker(job: _Job) -> None:
            from app.core import elastic

            def progress(done: int, total: int, message: str = "") -> None:
                self._post_progress(job, done, total, message)

            try:
                result = elastic.compute_elastic_tensor(
                    work, engine, max_strain=p.max_strain, n_strains=p.n_strains, relax_ions=p.relax_ions,
                    fmax=p.fmax, should_cancel=job.cancel.is_set, progress=progress,
                )
            except elastic.ElasticCancelled:
                raise _JobCancelled() from None
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            self._emit_terminal(job, [_message(ServerEvent.ELASTIC_TENSOR_RESULT, result)], None, None)

        self._launch(_C.COMPUTE_ELASTIC_TENSOR.value, "Elastic tensor", worker)

    async def _cmd_export_phonon(self, _payload: ExportPhononDataPayload) -> None:
        analysis = self._valid_phonon()
        formula = (self.payload.formula if self.payload is not None else "") or "structure"
        text = await asyncio.to_thread(analysis.export_phonopy_yaml)
        self._send(_message(ServerEvent.PHONON_EXPORT,
                            PhononExport(filename=f"{formula}_phonopy_params.yaml", phonopy_yaml=text)))

    async def _cmd_set_model(self, p: SetModelPayload) -> None:
        self._require_atoms()
        await self._get_engine()
        await self._end_job()
        work = _snapshot(self._require_atoms())

        def worker(job: _Job) -> None:
            def progress(done: int, total: int, message: str) -> None:
                self._post_progress(job, done, total, message)

            try:
                engine = get_engine(p.model_id, p.dispersion, progress=progress)
            except ModelUnavailableError as exc:
                job.check()
                raise CommandError(ErrorCode.MODEL_UNAVAILABLE, str(exc)) from None
            job.check()
            payload = engine.evaluate(work)
            job.check()
            changed = ModelChanged(model_id=engine.model_id, backend_name=engine.backend_name,
                                   dispersion=engine.dispersion, device=str(engine.device), dtype=str(engine.dtype))

            def on_apply() -> None:
                self.engine = engine
                self._phonon = None  # computed with the previous model

            messages = [
                _message(ServerEvent.MODEL_CHANGED, changed),
                _message(ServerEvent.SINGLE_POINT_UPDATE, SinglePointUpdate(crystal=payload)),
            ]
            # ``work`` is the active structure (nothing state-changing can run during the
            # job without cancelling it); applying it bumps the version like any commit.
            self._emit_terminal(job, messages, work, payload, on_apply,
                                history_label=f"Switch model to {engine.label}")

        self._launch(_C.SET_MODEL.value, "Switch model", worker, join_on_cancel=False)

    async def _cmd_follow(self, p: FollowInstabilityPayload) -> None:
        analysis = self._valid_phonon()
        self._check_mode_index(analysis, p.q_index, p.band_index)
        freq = float(analysis.payload_frequencies[p.q_index, p.band_index])
        if not freq < -F_TOL_THZ:
            raise CommandError(
                ErrorCode.MODE_NOT_IMAGINARY,
                f"mode (q_index={p.q_index}, band_index={p.band_index}) has f = {freq:.4f} THz; "
                f"only imaginary modes (f < -{F_TOL_THZ} THz) can be followed",
            )
        try:
            mult = phonon_engine.commensurate_multiples(analysis.path.qpoints[p.q_index])
        except ValueError as exc:
            raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
        n_super = int(np.prod(mult)) * analysis.n_atoms
        limit = max_atoms()
        if n_super > limit:
            raise CommandError(
                ErrorCode.STRUCTURE_TOO_LARGE,
                f"the commensurate supercell {tuple(mult)} has {n_super} atoms; the studio limit is {limit}",
            )
        await self._end_job()
        engine = self.engine

        def worker(job: _Job) -> None:
            displaced, frames = phonon_engine.stabilize_mode(
                analysis, p.q_index, p.band_index, amplitude=p.amplitude, fmax=p.fmax, max_steps=p.max_steps,
                engine=engine, should_cancel=job.cancel.is_set,
            )
            self._relaxation_worker(job, displaced, frames)

        self._launch(_C.FOLLOW_INSTABILITY.value, "Follow instability", worker)

    # ================================================================ Phase C1
    async def _cmd_local_analysis(self, p: RequestLocalAnalysisPayload) -> None:
        from app.core import local_structure

        atoms = _snapshot(self._require_atoms())
        bonds = list(self.payload.bonds)  # aligned with the active payload (bond_ideal_lengths)
        # The bond image offsets refer to the payload's wrapped coordinates; the session atoms
        # can sit a hair outside [0, 1) after a relaxation, which would pick the wrong images.
        if len(self.payload.atoms) == len(atoms):
            atoms.positions = np.array([a.cart_coords for a in self.payload.atoms], dtype=float)
        try:
            data = await asyncio.to_thread(
                lambda: local_structure.analyze(atoms, bonds, include_partial_rdf=p.include_partial_rdf)
            )
        except ValueError as exc:
            raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
        self._send(_message(ServerEvent.LOCAL_ANALYSIS_DATA, data))

    async def _cmd_make_supercell(self, p: MakeSupercellPayload) -> None:
        from app.core import defects

        atoms = self._require_atoms()
        if any(len(row) != 3 for row in p.matrix):
            raise CommandError(ErrorCode.INVALID_PAYLOAD, "matrix must be 3x3 integers")
        matrix = [[int(x) for x in row] for row in p.matrix]
        det = int(round(float(np.linalg.det(np.array(matrix, dtype=float)))))
        if det <= 0:
            raise CommandError(ErrorCode.INVALID_OPERATION, f"supercell matrix must have det > 0 (got {det})")
        limit = max_atoms()
        if len(atoms) * det > limit:
            raise CommandError(
                ErrorCode.STRUCTURE_TOO_LARGE,
                f"the supercell would have {len(atoms) * det} atoms; the studio limit is {limit}",
            )
        await self._end_job()
        work = _snapshot(self._require_atoms())
        try:
            supercell = await asyncio.to_thread(defects.make_supercell, work, matrix)
        except ValueError as exc:
            raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
        payload = await self._evaluate(supercell)
        self._commit(supercell, payload, f"Make supercell {defects.supercell_label(matrix)}")

    async def _cmd_add_atom(self, p: AddAtomPayload) -> None:
        from app.core import defects

        atoms = self._require_atoms()
        symbol, _ = canonical_element(p.element)
        frac = [float(x) for x in p.frac_coords]
        if not all(np.isfinite(frac)):
            raise CommandError(ErrorCode.INVALID_PAYLOAD, "frac_coords must be finite")
        limit = max_atoms()
        if len(atoms) + 1 > limit:
            raise CommandError(ErrorCode.STRUCTURE_TOO_LARGE, f"the studio limit is {limit} atoms")
        await self._end_job()
        work = _snapshot(self._require_atoms())
        try:
            added = await asyncio.to_thread(defects.add_atom, work, symbol, frac)
        except ValueError as exc:
            raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
        payload = await self._evaluate(added)
        self._commit(added, payload, f"Add {symbol} at ({frac[0]:.3f}, {frac[1]:.3f}, {frac[2]:.3f})")

    async def _cmd_formation_energy(self, p: ComputeFormationEnergyPayload) -> None:
        self._require_atoms()
        if not p.bulk_position < len(self._history):
            raise CommandError(
                ErrorCode.INDEX_OUT_OF_RANGE,
                f"bulk_position {p.bulk_position} out of range [0, {len(self._history)})",
            )
        bulk = _snapshot(self._history[p.bulk_position].atoms)
        await self._end_job()
        defect = _snapshot(self._require_atoms())
        engine = self.engine
        api_key = get_settings().mp_api_key

        def worker(job: _Job) -> None:
            from app.core import defects, hull_analyzer

            def progress(done: int, total: int, message: str = "") -> None:
                self._post_progress(job, done, total, message)

            try:
                result = defects.formation_energy(
                    defect, bulk, engine, api_key=api_key, should_cancel=job.cancel.is_set, progress=progress,
                )
            except (defects.DefectCancelled, hull_analyzer.HullCancelled):
                raise _JobCancelled() from None
            except hull_analyzer.HullNoApiKeyError as exc:
                job.check()
                raise CommandError(
                    ErrorCode.INVALID_OPERATION,
                    f"chemical potentials need the Materials Project reference phases: {exc}. "
                    "Set MP_API_KEY in the backend environment (or compute the hull of this chemical "
                    "system once while a key is configured, so the references are cached).",
                ) from None
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            self._emit_terminal(job, [_message(ServerEvent.FORMATION_ENERGY_RESULT, result)], None, None)

        self._launch(_C.COMPUTE_FORMATION_ENERGY.value, "Formation energy", worker)

    async def _cmd_substitute(self, p: SubstituteFractionPayload) -> None:
        atoms = self._require_atoms()
        from_symbol, _ = canonical_element(p.from_element)
        to_symbol, _ = canonical_element(p.to_element)
        if from_symbol == to_symbol:
            raise CommandError(ErrorCode.INVALID_OPERATION, f"from_element and to_element are both {from_symbol}")
        n_from = atoms.get_chemical_symbols().count(from_symbol)
        if n_from == 0:
            raise CommandError(ErrorCode.INVALID_OPERATION, f"the structure contains no {from_symbol}")
        # (fraction rounding to zero atoms: ordering's ValueError -> INVALID_OPERATION)
        await self._end_job()
        work = _snapshot(self._require_atoms())
        engine = self.engine

        def worker(job: _Job) -> None:
            from app.core import ordering

            def progress(done: int, total: int, message: str = "") -> None:
                self._post_progress(job, done, total, message)

            try:
                chosen, result = ordering.substitute_fraction(
                    work, from_symbol, to_symbol, p.fraction, engine, n_candidates=p.n_candidates, seed=p.seed,
                    should_cancel=job.cancel.is_set, progress=progress,
                )
            except ordering.OrderingCancelled:
                raise _JobCancelled() from None
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            chosen = _snapshot(chosen)
            payload = engine.evaluate(chosen)
            job.check()
            messages = [
                _message(ServerEvent.SUBSTITUTION_RESULT, result),
                _message(ServerEvent.SINGLE_POINT_UPDATE, SinglePointUpdate(crystal=payload)),
            ]
            self._emit_terminal(job, messages, chosen, payload,
                                history_label=f"Substitute {result.n_substituted} {from_symbol} -> {to_symbol}")

        self._launch(_C.SUBSTITUTE_FRACTION.value, "Substitution", worker)

    async def _cmd_md(self, p: StartMDPayload) -> None:
        atoms = self._require_atoms()
        if p.ensemble == "NPT" and not (bool(np.all(atoms.pbc)) and atoms.cell.rank == 3):
            raise CommandError(ErrorCode.INVALID_OPERATION, "NPT needs a fully periodic 3D cell")
        await self._end_job()
        work = _snapshot(self._require_atoms())
        engine = self.engine
        label = f"MD {p.temperature_k:g} K {p.ensemble}"
        periodic = bool(np.all(work.pbc)) and work.cell.rank == 3

        def frame_snapshot() -> Atoms:
            """The integrator's atoms *are* the frame just yielded: wrapped copy, no velocities."""
            snap = _snapshot(work)
            if "momenta" in snap.arrays:
                del snap.arrays["momenta"]
            snap.set_constraint([c for c in snap.constraints if not isinstance(c, FixCom)])  # NVT's own
            if periodic:
                _wrap_in_place(snap)
            return snap

        def worker(job: _Job) -> None:
            try:
                frames = md_core.iter_md(
                    work, engine, temperature_k=p.temperature_k, ensemble=p.ensemble, timestep_fs=p.timestep_fs,
                    n_steps=p.n_steps, friction_per_fs=p.friction_per_fs, pressure_gpa=p.pressure_gpa,
                    frame_interval=p.frame_interval, seed=p.seed, should_cancel=job.cancel.is_set,
                )
            except ValueError as exc:
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            summary: Optional[MDComplete] = None
            final: Optional[Tuple[MDFrame, Atoms]] = None
            try:
                while True:
                    job.check()
                    try:
                        frame = next(frames)
                    except StopIteration as stop:
                        summary = stop.value
                        break
                    except md_core.MDDivergedError as exc:
                        raise CommandError(
                            ErrorCode.COMPUTE_FAILED, f"{exc}; the last frame sent is kept as the structure",
                        ) from None
                    snap = frame_snapshot()
                    if frame.step >= p.n_steps:
                        final = (frame, snap)  # sent with MD_COMPLETE as the terminal group
                    else:
                        self._emit_frame(job, ServerEvent.MD_FRAME, frame, snap, frame.crystal)
            finally:
                frames.close()
            if summary is None or final is None:
                job.check()  # the generator stops silently when cancelled
                raise RuntimeError("MD ended without a summary")
            frame, snap = final
            messages = [_message(ServerEvent.MD_FRAME, frame), _message(ServerEvent.MD_COMPLETE, summary)]
            self._emit_terminal(job, messages, snap, frame.crystal,
                                history_label=f"{label} ({summary.time_ps:.3g} ps)")

        self._launch(_C.START_MD.value, label, worker)

    async def _cmd_committee(self, p: CommitteeEvaluatePayload) -> None:
        self._require_atoms()
        await self._end_job()
        work = _snapshot(self._require_atoms())

        def worker(job: _Job) -> None:
            from app.core import committee

            def progress(done: int, total: int, message: str = "") -> None:
                self._post_progress(job, done, total, message)

            try:
                result = committee.evaluate(
                    work, p.model_ids, relax=p.relax, dispersion=p.dispersion,
                    should_cancel=job.cancel.is_set, progress=progress,
                )
            except committee.CommitteeCancelled:
                raise _JobCancelled() from None
            except ModelUnavailableError as exc:
                job.check()
                raise CommandError(ErrorCode.MODEL_UNAVAILABLE, str(exc)) from None
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            self._emit_terminal(job, [_message(ServerEvent.COMMITTEE_RESULT, result)], None, None)

        self._launch(_C.COMMITTEE_EVALUATE.value, "Committee", worker)

    async def _cmd_prototype_search(self, p: PrototypeSearchPayload) -> None:
        engine = await self._get_engine()
        await self._end_job()
        api_key = get_settings().mp_api_key

        def worker(job: _Job) -> None:
            from app.core import hull_analyzer, mp_client, prototypes

            def progress(done: int, total: int, message: str = "") -> None:
                self._post_progress(job, done, total, message)

            try:
                client = prototype_client(api_key)
                result = prototypes.search(p, engine, client=client, api_key=api_key,
                                           should_cancel=job.cancel.is_set, progress=progress)
            except (prototypes.PrototypeCancelled, hull_analyzer.HullCancelled):
                raise _JobCancelled() from None
            except (mp_client.MPNoApiKeyError, hull_analyzer.HullNoApiKeyError) as exc:
                job.check()
                raise CommandError(
                    ErrorCode.INVALID_OPERATION,
                    f"prototype search needs Materials Project structures: {exc}. "
                    "Set MP_API_KEY (a 32-character Materials Project API key) in the backend environment.",
                ) from None
            except (mp_client.MPFetchError, hull_analyzer.HullFetchError,
                    hull_analyzer.HullMissingElementalReferenceError) as exc:
                job.check()
                raise CommandError(ErrorCode.COMPUTE_FAILED, f"Materials Project request failed: {exc}") from None
            except ValueError as exc:
                job.check()
                raise CommandError(ErrorCode.INVALID_OPERATION, str(exc)) from None
            job.check()
            self._emit_terminal(job, [_message(ServerEvent.PROTOTYPE_CANDIDATES, result)], None, None)

        self._launch(_C.PROTOTYPE_SEARCH.value, "Prototype search", worker)


def prototype_client(api_key: Optional[str]):
    """MP client of ``PROTOTYPE_SEARCH`` (test seam; raises ``MPNoApiKeyError`` without a key)."""
    from app.core import mp_client

    return mp_client.MaterialsProjectClient(api_key)


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@router.websocket("/ws/studio")
async def studio_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    session = StudioSession(websocket)
    await session.run()
