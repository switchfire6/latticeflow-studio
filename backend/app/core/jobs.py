"""Background job queue over the project library (``app.core.library``).

``JobQueue.create`` turns one request into one *chain* per structure: the
steps run in order, each on the output structure of the last ``relax`` /
``follow`` step before it (else on the chain's starting structure). A failed
step fails the remaining steps of its chain, a cancelled step cancels them;
other chains are unaffected. Every finished step writes one result row (and,
for ``relax`` / ``follow``, a child structure) into the library.

Worker
------
One daemon thread (:meth:`JobQueue.start`, called from the app lifespan) runs
the eligible queued job with the lowest ``position`` (eligible: the first step
of its chain, or the previous step is ``done`` / ``skipped``). It calls the core
modules directly with the engine of ``umlip_engine.get_engine(model_id,
dispersion)``, so model calls are serialised with interactive sessions by the
engine lock.

* **Interactive priority**: the worker's cancellation checkpoint (the
  ``should_cancel`` callback handed to the core iterators, plus one before every
  step) blocks while ``ws_stream.live_streams() > 0`` (any WebSocket stream
  running) and the job's message says it is paused.
* **Cancellation** is cooperative at those checkpoints (relaxation step,
  phonon force chunk, elastic strain, MD step; EOS and hull are checked before
  they start). A queued job is cancelled at once.
* **Restart**: jobs left ``running`` go back to ``queued`` (:meth:`start`).
  :meth:`stop` (shutdown) returns the running job to ``queued`` as well.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from app.core.library import ACTIVE_STATUSES, FINISHED_STATUSES, Conflict, Library, LibraryError, now_iso
from app.schemas.crystal import CrystalStatePayload
from app.schemas.messages import (
    F_TOL_THZ,
    ComputeBulkModulusPayload,
    ComputeElasticTensorPayload,
    RunPhononShakeTestPayload,
    StartRelaxationPayload,
)

log = logging.getLogger("latticeflow.jobs")

STEP_KINDS = ("relax", "phonons", "elastic", "eos", "hull", "follow", "superconductivity")
#: Progress / message writes to the database are throttled to one per this many seconds.
PROGRESS_INTERVAL_S = 0.5
PAUSED_MESSAGE = "paused: an interactive session is computing"


class HullStepSettings(BaseModel):
    relax: bool = True                     # as POST /api/hull/evaluate


class FollowStepSettings(BaseModel):
    amplitude: float = Field(default=0.1, gt=0)            # the UI's FOLLOW_INSTABILITY defaults
    fmax: float = Field(default=0.005, gt=0)
    max_steps: int = Field(default=300, ge=1, le=2000)


class SuperconductivityStepSettings(BaseModel):
    mesh: Optional[List[int]] = Field(default=None, min_length=3, max_length=3)   # None = automatic DOS mesh
    use_mp: bool = True                    # Materials Project gap for the metallicity gate (heuristic otherwise)


def normalize_settings(kind: str, settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Full step settings with the interactive (UI) defaults filled in; raises ``LibraryError``."""
    s = dict(settings or {})
    try:
        if kind == "relax":
            model: BaseModel = StartRelaxationPayload(**{"stress_tol_gpa": 0.05, **s})
        elif kind == "phonons":
            base: Dict[str, Any] = {"cell_mode": "primitive"}
            if "supercell_dim" not in s:
                base["min_supercell_length"] = 10.0
            model = RunPhononShakeTestPayload(**{**base, **s})
        elif kind == "elastic":
            model = ComputeElasticTensorPayload(**s)
        elif kind == "eos":
            model = ComputeBulkModulusPayload(**s)
        elif kind == "hull":
            model = HullStepSettings(**s)
        elif kind == "follow":
            model = FollowStepSettings(**s)
        elif kind == "superconductivity":
            model = SuperconductivityStepSettings(**s)
            if model.mesh is not None and any(int(n) < 1 for n in model.mesh):
                raise LibraryError("superconductivity settings: mesh: must be three positive integers")
        else:
            raise LibraryError(f"unknown step kind {kind!r}; one of {', '.join(STEP_KINDS)}")
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise LibraryError(f"{kind} settings: {loc}: {first.get('msg')}") from None
    return model.model_dump(mode="json")


class JobCancelled(Exception):
    pass


class StepFailed(Exception):
    pass


class StepSkipped(Exception):
    pass


@dataclass
class HullJobContext:
    api_key: Optional[str]
    fetcher: Any = None
    cache: Any = None
    mp_client: Any = None          # superconductivity step: injected MPClient (None = from api_key)


def _default_engine(model_id: Optional[str], dispersion: bool):
    from app.core.umlip_engine import get_engine

    return get_engine(model_id, bool(dispersion))


def _default_hull_context() -> HullJobContext:
    from app.config import get_settings

    return HullJobContext(api_key=get_settings().mp_api_key)


def _default_live_streams() -> int:
    from app.api import ws_stream

    return ws_stream.live_streams()


def _pretty_q(label: Optional[str]) -> str:
    if not label:
        return "q"
    return label.replace("\\Gamma", "Γ").replace("\\", "")


class _CountingEngine:
    """Engine proxy counting phonon supercell force evaluations (progress message)."""

    def __init__(self, engine, report: Callable[[int], None]) -> None:
        self._engine, self._report, self._done = engine, report, 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    def forces_batch(self, atoms_list, **kwargs):
        out = self._engine.forces_batch(atoms_list, **kwargs)
        self._done += len(out)
        self._report(self._done)
        return out


class JobQueue:
    """Job creation / control (REST side) and the single worker thread."""

    def __init__(self, library: Library, *, engine_for: Callable[[Optional[str], bool], Any] = _default_engine,
                 hull_context: Callable[[], HullJobContext] = _default_hull_context,
                 live_streams: Callable[[], int] = _default_live_streams, idle_poll_s: float = 1.0) -> None:
        self.library = library
        self.engine_for = engine_for
        self.hull_context = hull_context
        self.live_streams = live_streams
        self.idle_poll_s = idle_poll_s
        self._state = threading.Lock()          # claim / cancel decisions
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel = threading.Event()        # cancel the running job
        self._current: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._analyses: Dict[str, Tuple[int, Any]] = {}   # chain_id -> (structure_id, PhononAnalysis)
        self._last_write = 0.0

    # ================================================================ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        n = self.library.requeue_running()
        if n:
            log.info("requeued %d job(s) left running by a previous process", n)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="latticeflow-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def paused(self) -> bool:
        return self.live_streams() > 0

    def wake(self) -> None:
        self._wake.set()

    # ================================================================ REST side
    def create(self, project_id: int, structure_ids: List[int], steps: List[Dict[str, Any]],
               model_id: Optional[str] = None, dispersion: bool = False) -> List[Dict[str, Any]]:
        lib = self.library
        lib.get_project(project_id)
        for sid in structure_ids:
            if lib.get_structure(sid, with_crystal=False)["project_id"] != project_id:
                raise LibraryError(f"structure {sid} is not in project {project_id}")
        norm = []
        phonons_ready = False
        for i, step in enumerate(steps):
            kind = step["kind"]
            settings = normalize_settings(kind, step.get("settings"))
            if step.get("only_if_unstable") and kind != "follow":
                raise LibraryError(f"steps[{i}]: only_if_unstable applies to follow steps only")
            if kind in ("follow", "superconductivity") and not phonons_ready:
                raise LibraryError(f"steps[{i}]: {kind} needs a phonons step before it (with no relax / follow "
                                   "in between)")
            if kind == "phonons":
                phonons_ready = True
            elif kind in ("relax", "follow"):
                phonons_ready = False
            norm.append({"kind": kind, "settings": settings, "only_if_unstable": bool(step.get("only_if_unstable"))})
        ids: List[int] = []
        for sid in structure_ids:
            ids += lib.insert_chain(project_id, sid, norm, model_id, dispersion)
        self.wake()
        return [lib.get_job(j) for j in ids]

    def cancel(self, jid: int) -> Dict[str, Any]:
        lib = self.library
        with self._state:
            job = lib.get_job(jid)
            if job["status"] == "queued":
                self._finish(job, "cancelled", error="cancelled", rest_status="cancelled",
                             rest_error=f"previous step (job {jid}) was cancelled")
            elif job["status"] == "running":
                if self._current == jid:
                    self._cancel.set()
                lib.update_job(jid, message="cancelling")
            else:
                raise Conflict(f"job {jid} is already {job['status']}")
        return lib.get_job(jid)

    def move(self, jid: int, position: int) -> Dict[str, Any]:
        """Move a queued job to index ``position`` (0 = first) of the queued list."""
        lib = self.library
        with self._state, lib.tx() as db:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                from app.core.library import NotFound

                raise NotFound(f"job {jid} not found")
            if row["status"] != "queued":
                raise Conflict(f"only queued jobs can be moved (job {jid} is {row['status']})")
            queued = db.execute("SELECT id, position FROM jobs WHERE status = 'queued' ORDER BY position, id").fetchall()
            slots = sorted(int(r["position"]) for r in queued)
            order = [int(r["id"]) for r in queued if int(r["id"]) != jid]
            order.insert(max(0, min(int(position), len(order))), jid)
            for job_id, pos in zip(order, slots):
                db.execute("UPDATE jobs SET position = ? WHERE id = ?", (pos, job_id))
        return lib.get_job(jid)

    def delete(self, jid: int) -> None:
        lib = self.library
        with self._state, lib.tx() as db:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                from app.core.library import NotFound

                raise NotFound(f"job {jid} not found")
            if row["status"] not in FINISHED_STATUSES:
                raise Conflict(f"job {jid} is {row['status']}; only finished jobs can be deleted")
            db.execute("DELETE FROM jobs WHERE id = ?", (jid,))

    def clear_finished(self, project_id: Optional[int] = None) -> int:
        marks = ",".join("?" * len(FINISHED_STATUSES))
        sql, args = f"DELETE FROM jobs WHERE status IN ({marks})", list(FINISHED_STATUSES)
        if project_id is not None:
            sql += " AND project_id = ?"
            args.append(project_id)
        with self._state, self.library.tx() as db:
            return db.execute(sql, args).rowcount

    # ================================================================ worker
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._claim()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("job queue: claim failed")
                job = None
            if job is None:
                self._wake.wait(self.idle_poll_s)
                self._wake.clear()
                continue
            self._run(job)

    def _claim(self) -> Optional[Dict[str, Any]]:
        lib = self.library
        if self.live_streams() > 0:
            return None  # interactive priority: do not even start a job
        with self._state:
            status: Dict[Tuple[str, int], str] = {}
            queued = lib.list_jobs(active=True)
            for j in queued:
                status[(j["chain_id"], j["step_index"])] = j["status"]
            for j in queued:
                if j["status"] != "queued":
                    continue
                if j["step_index"] > 0:
                    prev = status.get((j["chain_id"], j["step_index"] - 1))
                    if prev is None:
                        prev = next((p["status"] for p in lib.chain_jobs(j["chain_id"])
                                     if p["step_index"] == j["step_index"] - 1), "done")
                    if prev not in ("done", "skipped"):
                        continue
                lib.update_job(j["id"], status="running", started_at=now_iso(), progress=0.0, message="", error=None)
                self._current = j["id"]
                self._cancel.clear()
                return lib.get_job(j["id"])
        return None

    def _finish(self, job: Dict[str, Any], status: str, *, error: Optional[str] = None,
                rest_status: Optional[str] = None, rest_error: Optional[str] = None, **fields: Any) -> None:
        lib = self.library
        lib.update_job(job["id"], status=status, error=error, finished_at=now_iso(), **fields)
        if rest_status is not None:
            for later in lib.chain_jobs(job["chain_id"]):
                if later["step_index"] > job["step_index"] and later["status"] == "queued":
                    lib.update_job(later["id"], status=rest_status, error=rest_error, finished_at=now_iso())
        if rest_status is not None or job["step_index"] == job["n_steps"] - 1:
            self._analyses.pop(job["chain_id"], None)

    def _run(self, job: Dict[str, Any]) -> None:
        jid = job["id"]
        try:
            out = self._execute(job)
        except JobCancelled:
            with self._state:
                self._current = None
                if self._stop.is_set() and not self._cancel.is_set():
                    self.library.update_job(jid, status="queued", started_at=None, progress=0.0,
                                            message="requeued at shutdown")
                else:
                    self._finish(job, "cancelled", error="cancelled", message="cancelled", rest_status="cancelled",
                                 rest_error=f"previous step (job {jid}) was cancelled")
            return
        except StepSkipped as exc:
            with self._state:
                self._current = None
                self._finish(job, "skipped", message=str(exc), progress=1.0)
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) if isinstance(exc, (StepFailed, LibraryError)) else f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, (StepFailed, LibraryError)):
                log.exception("job %s (%s) failed", jid, job["kind"])
            with self._state:
                self._current = None
                self._finish(job, "failed", error=msg, message="failed", rest_status="failed",
                             rest_error=f"previous step (job {jid}) failed")
            return
        with self._state:
            self._current = None
            self._finish(job, "done", progress=1.0, message=out.pop("message", "done"), **out)
            target = out.get("output_structure_id")
            if target is not None:
                for later in self.library.chain_jobs(job["chain_id"]):
                    if later["step_index"] > job["step_index"] and later["status"] == "queued":
                        self.library.update_job(later["id"], structure_id=target)

    # ---------------------------------------------------------------- checkpoints
    def _should_cancel_for(self, jid: int) -> Callable[[], bool]:
        def should_cancel() -> bool:
            if self._cancel.is_set() or self._stop.is_set():
                return True
            if self.live_streams() > 0:
                self.library.update_job(jid, message=PAUSED_MESSAGE)
                while self.live_streams() > 0 and not (self._cancel.is_set() or self._stop.is_set()):
                    time.sleep(0.1)
                self.library.update_job(jid, message="resumed")
            return self._cancel.is_set() or self._stop.is_set()

        return should_cancel

    def _progress_for(self, jid: int) -> Callable[[float, str], None]:
        def progress(fraction: float, message: str = "") -> None:
            t = time.monotonic()
            if t - self._last_write < PROGRESS_INTERVAL_S:
                return
            self._last_write = t
            self.library.update_job(jid, progress=max(0.0, min(1.0, float(fraction))), message=message)

        return progress

    # ---------------------------------------------------------------- steps
    def _execute(self, job: Dict[str, Any]) -> Dict[str, Any]:
        from app.core import structure_io
        from app.core.umlip_engine import ModelUnavailableError

        lib = self.library
        jid, kind, settings = job["id"], job["kind"], job["settings"]
        check = self._should_cancel_for(jid)
        progress = self._progress_for(jid)
        if check():
            raise JobCancelled()
        if job["structure_id"] is None:
            raise StepFailed("the input structure was deleted")
        structure = lib.get_structure(job["structure_id"])
        crystal = CrystalStatePayload.model_validate(structure["crystal"])
        try:
            atoms = structure_io.atoms_from_payload(crystal)
        except Exception as exc:  # noqa: BLE001
            raise StepFailed(f"invalid stored structure: {exc}") from None
        try:
            engine = self.engine_for(job["model_id"], job["dispersion"])
        except ModelUnavailableError as exc:
            raise StepFailed(f"MODEL_UNAVAILABLE: {exc}") from None
        ctx = _StepContext(job=job, structure=structure, atoms=atoms, engine=engine, settings=settings,
                           check=check, progress=progress)
        if check():
            raise JobCancelled()
        return getattr(self, f"_step_{kind}")(ctx)

    def _save_result(self, ctx: "_StepContext", sid: int, kind: str, data: BaseModel,
                     settings: Optional[Dict[str, Any]] = None) -> int:
        row = self.library.add_result(sid, kind, data.model_dump(mode="json"), model_id=ctx.engine.model_id,
                                      dispersion=bool(ctx.engine.dispersion),
                                      settings=ctx.settings if settings is None else settings)
        return int(row["id"])

    def _relax_frames(self, ctx: "_StepContext", frames, max_steps: int):
        from app.core.relaxer import RelaxationDivergedError

        last = None
        try:
            for frame in frames:
                last = frame
                ctx.progress(frame.step / max(1, max_steps), f"step {frame.step}")
        except RelaxationDivergedError as exc:
            raise StepFailed(f"RELAXATION_DIVERGED: {exc}") from None
        finally:
            close = getattr(frames, "close", None)
            if close is not None:
                close()
        if last is None or not last.final:
            if ctx.check():
                raise JobCancelled()
            raise StepFailed("relaxation ended without a final frame")
        return last

    def _step_relax(self, ctx: "_StepContext") -> Dict[str, Any]:
        from app.core import relaxer

        s = ctx.settings
        frames = relaxer.iter_relaxation(
            ctx.atoms, ctx.engine, fmax=s["fmax"], max_steps=s["max_steps"], relax_cell=s["relax_cell"],
            should_cancel=ctx.check, fix_symmetry=s["fix_symmetry"], pressure_gpa=s["pressure_gpa"],
            stress_tol_gpa=s["stress_tol_gpa"] if s["relax_cell"] else None, optimizer=s["optimizer"],
        )
        final = self._relax_frames(ctx, frames, s["max_steps"])
        child = self.library.add_structure(ctx.job["project_id"], final.crystal.model_dump(mode="json"),
                                           parent_id=ctx.structure["id"], origin="relax")
        rid = self._save_result(ctx, child["id"], "relax", final)
        state = "converged" if final.converged else "max steps"
        return {"result_id": rid, "output_structure_id": child["id"], "message": f"{state} in {final.step} steps"}

    def _run_phonons(self, ctx: "_StepContext", s: Dict[str, Any]):
        from app.config import get_settings
        from app.core import phonon_engine

        limit = int(get_settings().phonon_max_atoms)
        if len(ctx.atoms) > limit:
            raise StepFailed(f"STRUCTURE_TOO_LARGE: phonons support at most {limit} atoms per cell "
                             f"(structure has {len(ctx.atoms)})")
        counting = _CountingEngine(ctx.engine, lambda n: ctx.progress(0.0, f"{n} supercell force evaluations"))
        try:
            analysis = phonon_engine.run_phonon_analysis(
                ctx.atoms, counting, supercell_dim=s["supercell_dim"], displacement_distance=s["displacement_distance"],
                cell_mode=s["cell_mode"], min_supercell_length=s["min_supercell_length"], should_cancel=ctx.check,
            )
        except phonon_engine.PhononCancelled:
            raise JobCancelled() from None
        except ValueError as exc:
            raise StepFailed(f"INVALID_OPERATION: {exc}") from None
        analysis.engine = ctx.engine  # not the counting proxy
        return analysis

    def _step_phonons(self, ctx: "_StepContext") -> Dict[str, Any]:
        analysis = self._run_phonons(ctx, ctx.settings)
        self._analyses[ctx.job["chain_id"]] = (ctx.structure["id"], analysis)
        rid = self._save_result(ctx, ctx.structure["id"], "phonons", analysis.result)
        r = analysis.result
        return {"result_id": rid, "message": "stable" if r.is_dynamically_stable
                else f"unstable (min {r.min_frequency_thz:.2f} THz)"}

    def _chain_analysis(self, ctx: "_StepContext"):
        """The chain's cached ``PhononAnalysis`` of this structure, else a rerun of its last phonons settings."""
        job = ctx.job
        cached = self._analyses.get(job["chain_id"])
        if cached is not None and cached[0] == ctx.structure["id"]:
            return cached[1]
        # e.g. after a restart: rerun the chain's last phonons step settings
        prior = [j for j in self.library.chain_jobs(job["chain_id"])
                 if j["kind"] == "phonons" and j["step_index"] < job["step_index"]]
        settings = prior[-1]["settings"] if prior else normalize_settings("phonons", {})
        return self._run_phonons(ctx, settings)

    def _step_follow(self, ctx: "_StepContext") -> Dict[str, Any]:
        import numpy as np

        from app.core import phonon_engine
        from app.core.structure_io import max_atoms

        job = ctx.job
        analysis = self._chain_analysis(ctx)
        r = analysis.result
        if r.is_dynamically_stable:
            if job["only_if_unstable"]:
                raise StepSkipped("skipped: the phonons are dynamically stable")
            raise StepFailed("MODE_NOT_IMAGINARY: the structure is dynamically stable; nothing to follow")
        limit = max_atoms()
        chosen = None
        for mode in sorted((m for m in r.eigenmodes if m.is_imaginary and m.frequency_thz < -F_TOL_THZ),
                           key=lambda m: m.frequency_thz):
            try:
                mult = phonon_engine.commensurate_multiples(analysis.path.qpoints[mode.q_index])
            except ValueError:
                continue
            if int(np.prod(mult)) * analysis.n_atoms <= limit:
                chosen = mode
                break
        if chosen is None:
            raise StepFailed("no imaginary mode at a labelled q-point fits a commensurate supercell within "
                             f"{limit} atoms")
        s = ctx.settings
        displaced, frames = phonon_engine.stabilize_mode(
            analysis, chosen.q_index, chosen.band_index, amplitude=s["amplitude"], fmax=s["fmax"],
            max_steps=s["max_steps"], engine=ctx.engine, should_cancel=ctx.check,
        )
        final = self._relax_frames(ctx, frames, s["max_steps"])
        label = f"follow {_pretty_q(chosen.q_label)} ν{chosen.band_index}"
        child = self.library.add_structure(job["project_id"], final.crystal.model_dump(mode="json"),
                                           parent_id=ctx.structure["id"], origin=label)
        settings = {**s, "follow": True, "q_index": chosen.q_index, "band_index": chosen.band_index,
                    "q_label": chosen.q_label, "frequency_thz": chosen.frequency_thz}
        rid = self._save_result(ctx, child["id"], "relax", final, settings)
        return {"result_id": rid, "output_structure_id": child["id"],
                "message": f"{label}: {final.crystal.spacegroup_symbol}"}

    def _step_superconductivity(self, ctx: "_StepContext") -> Dict[str, Any]:
        """Tier 0 proxies (DOS descriptors, assumed-λ Allen–Dynes grid) plus the metallicity gate."""
        from app.core import phonon_engine
        from app.core import superconductivity as sc
        from app.schemas.api import MetallicityResponse, SuperconductivityResult

        analysis = self._chain_analysis(ctx)
        self._analyses[ctx.job["chain_id"]] = (ctx.structure["id"], analysis)   # a later follow reuses it
        s = ctx.settings
        try:
            dos = analysis.dos_and_thermal(mesh=s["mesh"], should_cancel=ctx.check)
        except phonon_engine.PhononCancelled:
            raise JobCancelled() from None
        except ValueError as exc:
            raise StepFailed(f"INVALID_OPERATION: {exc}") from None
        desc = dos.eph_proxy or sc.descriptors_from_dos(dos)
        if ctx.check():
            raise JobCancelled()
        try:  # MP errors never fail the step
            client, why = None, None
            if s["use_mp"]:
                hc = self.hull_context()
                client = hc.mp_client
                if client is None:
                    client, why = sc.mp_client_or_reason(hc.api_key)
            met = sc.assess_metallicity(ctx.atoms, client, s["use_mp"], unavailable_reason=why)
        except Exception as exc:  # noqa: BLE001
            log.warning("superconductivity step: metallicity gate failed: %s", exc)
            met = MetallicityResponse(verdict="unknown", source="heuristic",
                                      reason=f"metallicity check failed ({type(exc).__name__})")
        r = analysis.result
        flags = sc.sc_flags(desc, met.verdict, dynamically_stable=r.is_dynamically_stable,
                            band_frequencies_thz=analysis.frequencies, band_qpoints=analysis.path.qpoints,
                            labelled_indices=list(analysis.path.point_names))
        data = SuperconductivityResult(descriptors=desc, metallicity=met, flags=flags, dos_mesh=list(dos.mesh),
                                       imaginary_fraction=dos.imaginary_fraction)
        rid = self._save_result(ctx, ctx.structure["id"], "superconductivity", data)
        w_log = f"ω_log {desc.omega_log_k:.0f} K" if desc.omega_log_k is not None else "ω_log n/a"
        tc = f"T_c,ref {desc.tc_ref_k:.1f} K" if desc.tc_ref_k is not None else "T_c,ref n/a (not reliable)"
        src = "MP" if met.source == "materials_project" else "heuristic"
        return {"result_id": rid, "message": f"{w_log} · {tc} · {met.verdict} ({src})"}

    def _step_elastic(self, ctx: "_StepContext") -> Dict[str, Any]:
        import numpy as np

        from app.core import elastic

        if not (bool(np.all(ctx.atoms.pbc)) and ctx.atoms.cell.rank == 3):
            raise StepFailed("INVALID_OPERATION: the elastic tensor needs a fully periodic 3D cell")
        s = ctx.settings
        try:
            result = elastic.compute_elastic_tensor(
                ctx.atoms, ctx.engine, max_strain=s["max_strain"], n_strains=s["n_strains"],
                relax_ions=s["relax_ions"], fmax=s["fmax"], should_cancel=ctx.check,
                progress=lambda done, total, msg="": ctx.progress(done / total if total else 0.0, msg),
            )
        except elastic.ElasticCancelled:
            raise JobCancelled() from None
        except ValueError as exc:
            raise StepFailed(f"INVALID_OPERATION: {exc}") from None
        return {"result_id": self._save_result(ctx, ctx.structure["id"], "elastic", result),
                "message": f"K_VRH {result.k_vrh_gpa:.1f} GPa"}

    def _step_eos(self, ctx: "_StepContext") -> Dict[str, Any]:
        from app.core import relaxer

        s = ctx.settings
        try:
            result = relaxer.bulk_modulus_result(ctx.atoms, ctx.engine, strain_window=s["strain_window"],
                                                 n_points=s["n_points"])
        except ValueError as exc:
            raise StepFailed(f"INVALID_OPERATION: {exc}") from None
        if ctx.check():
            raise JobCancelled()
        return {"result_id": self._save_result(ctx, ctx.structure["id"], "eos", result),
                "message": f"K {result.k_gpa:.1f} GPa" if result.k_gpa is not None else "fit rejected"}

    def _step_hull(self, ctx: "_StepContext") -> Dict[str, Any]:
        from app.core import hull_analyzer as ha

        hc = self.hull_context()
        try:
            result = ha.evaluate_hull(ctx.atoms, engine=ctx.engine, api_key=hc.api_key, fetcher=hc.fetcher,
                                      cache=hc.cache, relax_candidate=ctx.settings["relax"])
        except ha.HullError as exc:
            raise StepFailed(f"HULL_UNAVAILABLE: {exc}") from None
        if ctx.check():
            raise JobCancelled()
        return {"result_id": self._save_result(ctx, ctx.structure["id"], "hull", result),
                "message": f"E_hull {1000 * result.e_hull_ev:.0f} meV/atom"}


@dataclass
class _StepContext:
    job: Dict[str, Any]
    structure: Dict[str, Any]
    atoms: Any
    engine: Any
    settings: Dict[str, Any]
    check: Callable[[], bool]
    progress: Callable[[float, str], None]


_default: Optional[JobQueue] = None
_default_lock = threading.Lock()


def get_queue() -> JobQueue:
    """Process-wide queue on ``library.get_library()`` (the worker is started by the app lifespan)."""
    global _default
    from app.core.library import get_library

    with _default_lock:
        if _default is None:
            _default = JobQueue(get_library())
        return _default


__all__ = ["JobQueue", "get_queue", "normalize_settings", "STEP_KINDS", "ACTIVE_STATUSES", "HullJobContext"]
