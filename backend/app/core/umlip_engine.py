"""Universal ML interatomic potential engine (PROJECT_SPEC.md §4.3, §7 Phase 1).

``UMLIPEngine`` wraps exactly one force-field backend:

* ``mace``      MACE-MP-0 via ``mace.calculators.mace_mp`` (``backend_name`` e.g. ``"mace-mp-0-small"``)
* ``fallback``  the analytic LJ + DSF-Coulomb calculator (``backend_name == "lj-fallback"``)

The process default is the singleton ``UMLIPEngine.get_instance()``
(``LATTICEFLOW_MACE_MODEL``). Phase B (IMPROVEMENT_SUGGESTIONS.md §6.1) adds
other models per session / per request: :func:`list_models` (the picker
catalog), :func:`default_model_id` and :func:`get_engine` (``None`` / the
default -> the singleton; anything else -> a thread-safe registry keyed by
(model_id, dispersion) holding at most ``MAX_RESIDENT_MODELS`` non-default
MACE engines, LRU). Errors are :class:`ModelUnavailableError`. With
``dispersion=True`` the calculator is ``mace_mp(..., dispersion=True)`` (D3(BJ),
PBE; needs torch-dftd), ``backend_name`` gets a ``+d3`` suffix and batched
evaluation is disabled (the batched path would bypass the D3 term).
``backend_name`` is unique per (model, dispersion): ``mace-mp-0-<name>`` for
the MP-0 family, ``mace-mpa-0-medium``, ``mace-omat-0-medium``, ``...+d3``.

Never-hang checkpoint guard
---------------------------
``mace_mp(model="small")`` downloads its checkpoint on first use. Instead of
letting it do that, the engine resolves the checkpoint itself:

1. an existing local file path in ``LATTICEFLOW_MACE_MODEL`` is used as is;
2. otherwise the URL is looked up in ``mace.calculators.foundations_models.mace_mp_urls``
   and the cache path is computed exactly the way mace computes it
   (``~/.cache/mace/<sanitised basename>``, honouring ``XDG_CACHE_HOME``); if the
   file exists, no network is touched;
3. if it is missing (and ``LATTICEFLOW_MACE_ALLOW_DOWNLOAD`` is on) it is fetched
   in a daemon thread with a 15 s per-socket-operation timeout, streamed to a
   unique ``.part`` file and atomically renamed into the cache. The caller waits
   at most ``LATTICEFLOW_MACE_DOWNLOAD_TIMEOUT`` seconds of wall clock in total;
   on expiry the worker is told to abort and a ``TimeoutError`` is raised.

``mace_mp`` is then called with the *local file path*, so mace itself never
downloads. In ``auto`` mode any exception from this (offline, timeout, corrupt
file, torch/CUDA problems) falls back to the analytic calculator; in ``mace``
mode it propagates; in ``fallback`` mode mace / torch are never imported.

Thread safety
-------------
One engine (one model) is shared by all threads. Every model call goes
through ``engine.lock`` (an ``RLock``); ``get_calculator()`` returns a fresh,
lightweight ASE calculator per caller whose ``calculate`` routes through the
engine, so several FIRE optimisers in different threads can share one model.

Batched evaluation (Phase 2)
----------------------------
``energy_forces_stress_batch`` / ``forces_batch`` build one mace ``AtomicData``
graph per structure exactly as ``MACECalculator`` does, collate them with mace's
torch-geometric ``DataLoader`` and run several graphs per model forward/backward
pass (``compute_force=True``), in chunks bounded by a graph-edge budget (GPU
memory is ~60 KiB per edge in float64). Each chunk holds ``engine.lock``; the
lock is released between chunks. Non-periodic structures, the fallback backend
and any chunk whose batched call raises go through ``energy_forces_stress``.
``payload_from_results`` builds a ``CrystalStatePayload`` from already-known
energy / forces / stress (used by the relaxer to avoid a second model call).

Payload enrichment (Phase A)
----------------------------
Every payload also carries ``pressure_gpa = -(s_xx + s_yy + s_zz) / 3`` (GPa,
+ = compressed; None for non-periodic input), ``density_g_cm3``,
``crystal_system``, ``formula_units`` (Z) and ``symprec`` (1e-2), plus a
per-atom ``wyckoff`` label. Space group, crystal system and Wyckoff labels come
from one spglib dataset call (``geometry_analysis.symmetry_info``); on failure
the space group is ("P1", 1) and crystal_system / wyckoff are None.
"""

from __future__ import annotations

import collections
import gc
import logging
import os
import threading
import time
import urllib.request
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.calculators.calculator import Calculator, all_changes

from app.config import Settings, get_settings, resolve_device
from app.core.fallback_calculator import AnalyticFallbackCalculator
from app.schemas.crystal import AtomSite, CrystalStatePayload

logger = logging.getLogger(__name__)

EV_PER_A3_TO_GPA = 160.21766208
FALLBACK_BACKEND_NAME = "lj-fallback"
_SOCKET_TIMEOUT_S = 15.0
_MIN_CHECKPOINT_BYTES = 1_000_000

#: ``progress(done, total, message)`` sink for long model loads (bytes during a download).
ProgressFn = Callable[[int, int, str], None]


# --------------------------------------------------------------------------
# Checkpoint resolution / bounded download
# --------------------------------------------------------------------------
def _mace_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mace"


def mace_checkpoint_url(model: str) -> Optional[str]:
    """URL of a named MACE-MP model, or None if ``model`` is not a known name."""
    from mace.calculators.foundations_models import mace_mp_urls

    if model in mace_mp_urls:
        return mace_mp_urls[model]
    if model.startswith("https://"):
        return model
    return None


def mace_cached_checkpoint_path(url: str) -> Path:
    # Mirrors mace.calculators.foundations_models.download_mace_mp_checkpoint.
    name = "".join(c for c in os.path.basename(url) if c.isalnum() or c in "_")
    return _mace_cache_dir() / name


def _download_with_deadline(url: str, dest: Path, timeout_s: float, progress: Optional[ProgressFn] = None) -> None:
    """Download ``url`` to ``dest`` atomically; never blocks the caller > timeout_s.

    ``progress(done_bytes, total_bytes, message)`` (optional) is called from the
    download thread at most every ~0.25 s (``total_bytes`` is 0 when unknown).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.part")
    cancel = threading.Event()
    outcome: dict = {}

    def worker() -> None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "latticeflow-studio"})
            with urllib.request.urlopen(req, timeout=min(_SOCKET_TIMEOUT_S, timeout_s)) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype == "text/html":
                    raise RuntimeError(f"checkpoint URL returned HTML, not a model: {url}")
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
                done = 0
                last = 0.0
                with open(tmp, "wb") as out:
                    while not cancel.is_set():
                        block = resp.read(1 << 18)
                        if not block:
                            break
                        out.write(block)
                        done += len(block)
                        now = time.monotonic()
                        if progress is not None and now - last >= 0.25:
                            last = now
                            _safe_progress(progress, done, total, f"Downloading {os.path.basename(url)}")
            if cancel.is_set():
                raise TimeoutError("download cancelled")
            size = tmp.stat().st_size
            with open(tmp, "rb") as fh:
                magic = fh.read(2)
            if size < _MIN_CHECKPOINT_BYTES or magic != b"PK":  # torch.save zip archive
                raise RuntimeError(f"downloaded file is not a torch checkpoint ({size} bytes)")
            os.replace(tmp, dest)
            outcome["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            outcome["error"] = exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    thread = threading.Thread(target=worker, name="mace-checkpoint-download", daemon=True)
    t0 = time.monotonic()
    logger.info("Downloading MACE checkpoint %s (budget %.0f s)", url, timeout_s)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        cancel.set()
        raise TimeoutError(f"MACE checkpoint download exceeded {timeout_s:.0f} s: {url}")
    if "error" in outcome:
        raise RuntimeError(f"MACE checkpoint download failed: {outcome['error']!r}") from outcome["error"]
    logger.info("Cached MACE checkpoint at %s in %.1f s", dest, time.monotonic() - t0)


def mace_backend_name(model: str) -> str:
    """``backend_name`` of a named MACE model (the hull cache is keyed by it).

    The MACE-MP-0 family keeps its historical ``mace-mp-0-<name>`` names (so
    existing hull caches stay valid); the newer families get their own prefix:
    ``medium-mpa-0`` -> ``mace-mpa-0-medium``, ``medium-omat-0`` -> ``mace-omat-0-medium``.
    A D3 engine appends ``+d3``.
    """
    if model.startswith("https://"):
        return "mace-mp-0-custom"
    for family in ("mpa-0", "omat-0"):
        suffix = f"-{family}"
        if model.endswith(suffix):
            return f"mace-{family}-{model[: -len(suffix)]}"
    return f"mace-mp-0-{model}"


def resolve_mace_checkpoint(
    model: str, *, allow_download: bool, timeout_s: float, progress: Optional[ProgressFn] = None
) -> Tuple[Path, str]:
    """Return (local checkpoint path, backend_name) without ever blocking unboundedly."""
    if Path(model).is_file():
        return Path(model), f"mace-{Path(model).stem}"
    url = mace_checkpoint_url(model)
    if url is None:
        raise FileNotFoundError(f"unknown MACE model {model!r} and no such local file")
    path = mace_cached_checkpoint_path(url)
    if not path.is_file():
        if not allow_download:
            raise FileNotFoundError(f"MACE checkpoint not cached at {path} and downloads are disabled")
        _download_with_deadline(url, path, timeout_s, progress)
    return path, mace_backend_name(model)


def _safe_progress(progress: Optional[ProgressFn], done: int, total: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(int(done), int(total), message)
    except Exception:  # noqa: BLE001 - a progress sink must never break a load
        logger.debug("progress callback failed", exc_info=True)


def dispersion_available() -> bool:
    """True if torch-dftd (needed for ``mace_mp(dispersion=True)``) is importable."""
    import importlib.util

    try:
        return importlib.util.find_spec("torch_dftd") is not None
    except (ImportError, ValueError):
        return False


class ModelUnavailableError(RuntimeError):
    """A requested force field is unknown or cannot be loaded (``ErrorCode.MODEL_UNAVAILABLE``)."""


# --------------------------------------------------------------------------
# ASE calculator facade
# --------------------------------------------------------------------------
class EngineCalculator(Calculator):
    """ASE calculator that evaluates through a shared ``UMLIPEngine`` (lock-serialised)."""

    implemented_properties = ["energy", "free_energy", "forces", "stress"]
    nolabel = True

    def __init__(self, engine: "UMLIPEngine", **kwargs):
        super().__init__(**kwargs)
        self.engine = engine

    def calculate(self, atoms=None, properties=("energy",), system_changes=all_changes):
        super().calculate(atoms, properties, system_changes)
        energy, forces, stress = self.engine.energy_forces_stress(self.atoms)
        self.results = {"energy": energy, "free_energy": energy, "forces": forces, "stress": stress}


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class UMLIPEngine:
    """Process-wide uMLIP. Use ``UMLIPEngine.get_instance()`` in application code.

    Direct construction (``UMLIPEngine(backend="fallback")``) builds an
    independent engine and never touches the singleton; tests use that, or
    ``set_instance`` / ``reset_instance`` to swap the singleton explicitly.
    """

    _instance: Optional["UMLIPEngine"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        backend: Optional[str] = None,
        *,
        device: Optional[str] = None,
        dtype: Optional[str] = None,
        model: Optional[str] = None,
        settings: Optional[Settings] = None,
        download_timeout_s: Optional[float] = None,
        allow_download: Optional[bool] = None,
        dispersion: bool = False,
        progress: Optional[ProgressFn] = None,
    ) -> None:
        s = settings or get_settings()
        mode = (backend or s.umlip).lower()
        if mode not in ("auto", "mace", "fallback"):
            raise ValueError(f"unknown uMLIP backend {mode!r}")
        self.requested_backend = mode
        self.lock = threading.RLock()
        self.load_error: Optional[BaseException] = None
        self.model = model or s.mace_model
        self.dispersion = False
        self._calc: Calculator

        if mode in ("auto", "mace"):
            try:
                self._load_mace(
                    device=device or s.device,
                    dtype=dtype or s.dtype,
                    allow_download=s.mace_allow_download if allow_download is None else allow_download,
                    timeout_s=download_timeout_s or s.mace_download_timeout_s,
                    dispersion=dispersion,
                    progress=progress,
                )
                return
            except Exception as exc:
                if mode == "mace":
                    raise RuntimeError(f"MACE backend could not be loaded: {exc}") from exc
                self.load_error = exc
                logger.warning("MACE unavailable (%s); using analytic fallback calculator", exc)
        self._init_fallback()

    # ------------------------------------------------------------ loading
    def _load_mace(
        self,
        *,
        device: str,
        dtype: str,
        allow_download: bool,
        timeout_s: float,
        dispersion: bool = False,
        progress: Optional[ProgressFn] = None,
    ) -> None:
        if dispersion and not dispersion_available():
            raise ModelUnavailableError(
                "D3 dispersion needs the torch-dftd package, which is not installed in the backend "
                "environment; install it (pip install torch-dftd) and restart the server"
            )
        path, name = resolve_mace_checkpoint(
            self.model, allow_download=allow_download, timeout_s=timeout_s, progress=progress
        )
        from mace.calculators import mace_mp

        dev = resolve_device(device)
        if dispersion:
            # SumCalculator([MACECalculator, TorchDFTD3Calculator]): D3(BJ) with PBE parameters.
            self._calc = mace_mp(
                model=str(path), device=dev, default_dtype=dtype,
                dispersion=True, dispersion_damping="bj", dispersion_xc="pbe",
            )
            name = f"{name}+d3"
        else:
            self._calc = mace_mp(model=str(path), device=dev, default_dtype=dtype)
        self.backend_name = name
        self.device = dev
        self.dtype = dtype
        self.dispersion = bool(dispersion)
        self.checkpoint_path: Optional[Path] = path
        self.model_id = self.model

    def _init_fallback(self) -> None:
        self._calc = AnalyticFallbackCalculator()
        self.backend_name = FALLBACK_BACKEND_NAME
        self.device = "cpu"
        self.dtype = "float64"
        self.dispersion = False
        self.checkpoint_path = None
        self.model_id = FALLBACK_BACKEND_NAME

    @property
    def label(self) -> str:
        """Human-readable model name, e.g. ``"medium-mpa-0 + D3"``."""
        return f"{self.model_id} + D3" if self.dispersion else self.model_id

    @property
    def is_mace(self) -> bool:
        return self.backend_name != FALLBACK_BACKEND_NAME

    # ---------------------------------------------------------- singleton
    @classmethod
    def get_instance(cls) -> "UMLIPEngine":
        inst = cls._instance
        if inst is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
                inst = cls._instance
        return inst

    @classmethod
    def set_instance(cls, engine: Optional["UMLIPEngine"]) -> None:
        with cls._instance_lock:
            cls._instance = engine

    @classmethod
    def reset_instance(cls) -> None:
        cls.set_instance(None)

    # ------------------------------------------------------------ compute
    @staticmethod
    def _clean_copy(atoms: Atoms) -> Atoms:
        return Atoms(
            numbers=atoms.get_atomic_numbers().copy(),
            positions=np.array(atoms.positions, dtype=float, copy=True),
            cell=np.array(atoms.cell.array, dtype=float, copy=True),
            pbc=np.array(atoms.pbc, copy=True),
        )

    def energy_forces_stress(self, atoms: Atoms) -> Tuple[float, np.ndarray, np.ndarray]:
        """(E eV, F (N,3) eV/Å, stress Voigt (6,) eV/Å^3 in ASE order xx,yy,zz,yz,xz,xy)."""
        work = self._clean_copy(atoms)
        periodic = bool(np.all(work.pbc)) and work.cell.rank == 3
        with self.lock:
            work.calc = self._calc
            _reset_calculator(self._calc)  # never reuse results cached for another structure/thread
            energy = float(work.get_potential_energy())
            forces = np.array(work.get_forces(), dtype=float, copy=True)
            stress = (
                np.array(work.get_stress(voigt=True), dtype=float, copy=True)
                if periodic
                else np.zeros(6)
            )
            work.calc = None
        return energy, forces, stress

    def forces_batch(self, atoms_list: Sequence[Atoms], *, max_edges: Optional[int] = None) -> List[np.ndarray]:
        """Forces (N_k, 3) in eV/Å for many structures (Phase 2 phonons).

        MACE: true batches (several graphs per forward/backward pass, chunked by
        ``max_edges``, see ``energy_forces_stress_batch``); stress is not
        computed. Fallback backend: sequential. Inputs are not mutated.
        """
        return [f for _, f, _ in self.energy_forces_stress_batch(atoms_list, compute_stress=False, max_edges=max_edges)]

    # ------------------------------------------------------- batched compute
    #: Chunk budget for one batched MACE call, in graph edges (pairs within r_max).
    #: float64 MACE-MP-0-small needs ~60 KiB of GPU memory per edge, so 16384
    #: edges is ~1 GiB peak (the GPU is shared). On CPU, large batches are slower
    #: than small ones (cache effects), so the CPU budget is small.
    BATCH_MAX_EDGES_CUDA = 16384
    BATCH_MAX_EDGES_CPU = 4096
    BATCH_MAX_GRAPHS = 64

    @property
    def supports_batching(self) -> bool:
        """True when the backend can evaluate several graphs in one model call."""
        if not self.is_mace or self.dispersion:
            # The batched path runs the bare MACE model and would silently drop the D3
            # term of the SumCalculator: dispersion engines evaluate sequentially.
            return False
        c = self._calc
        return (
            getattr(c, "model_type", None) == "MACE"
            and getattr(c, "num_models", 1) == 1
            and not getattr(c, "use_compile", False)
            and getattr(c, "pad_num_atoms", 0) == 0
            and getattr(c, "pad_num_edges", 0) == 0
            and getattr(c, "external_field", None) is None
            and not getattr(c, "compute_atomic_stresses", False)
        )

    def _batch_edge_budget(self) -> int:
        override = getattr(self, "batch_max_edges", None)
        if override:
            return int(override)
        return self.BATCH_MAX_EDGES_CUDA if str(self.device).startswith("cuda") else self.BATCH_MAX_EDGES_CPU

    def _mace_keyspec(self):
        from mace import data as mace_data

        c = self._calc
        arrays = dict(c.arrays_keys)
        arrays["charges"] = c.charges_key  # what MACECalculator._atoms_to_batch adds
        return mace_data.KeySpecification(info_keys=dict(c.info_keys), arrays_keys=arrays)

    def _mace_graph(self, atoms: Atoms, keyspec):
        """One mace ``AtomicData`` graph built exactly like ``MACECalculator._atoms_to_batch``.

        Must be called under ``self.lock``: torch's default dtype is process-wide
        and is switched by ``torch_tools.default_dtype``.
        """
        from mace import data as mace_data
        from mace.tools import torch_tools

        c = self._calc
        with torch_tools.default_dtype(c.default_dtype):
            config = mace_data.config_from_atoms(atoms, key_specification=keyspec, head_name=c.head)
            return mace_data.AtomicData.from_config(
                config, z_table=c.z_table, cutoff=c.r_max, heads=c.available_heads
            )

    def _mace_run_graphs(self, graphs, compute_stress: bool):
        """Evaluate several graphs in one forward/backward pass (caller holds the lock)."""
        import torch
        from ase.stress import full_3x3_to_voigt_6_stress
        from mace.tools import torch_geometric, torch_tools

        c = self._calc
        model = c.models[0]
        model_dtype = next(model.parameters()).dtype
        loader = torch_geometric.dataloader.DataLoader(graphs, batch_size=len(graphs), shuffle=False, drop_last=False)
        batch = next(iter(loader)).to(c.device)
        for key in batch.keys:
            value = batch[key]
            if torch.is_tensor(value) and torch.is_floating_point(value):
                batch[key] = value.to(dtype=model_dtype)
        with torch_tools.default_dtype(c.default_dtype):
            out = model(batch.to_dict(), compute_force=True, compute_stress=compute_stress, training=False)
        energy = out["energy"].detach().cpu().numpy().astype(float)
        forces = out["forces"].detach().cpu().numpy().astype(float)
        stress = out["stress"].detach().cpu().numpy().astype(float) if compute_stress else None
        ptr = batch["ptr"].cpu().numpy()
        del out, batch
        results = []
        for k in range(len(graphs)):
            s = full_3x3_to_voigt_6_stress(stress[k]) * c.energy_units_to_eV / c.length_units_to_A**3 if stress is not None else np.zeros(6)
            results.append(
                (
                    float(energy[k]) * c.energy_units_to_eV,
                    forces[ptr[k] : ptr[k + 1]].copy() * (c.energy_units_to_eV / c.length_units_to_A),
                    np.asarray(s, dtype=float),
                )
            )
        return results

    def energy_forces_stress_batch(
        self,
        atoms_list: Sequence[Atoms],
        *,
        compute_stress: bool = True,
        max_edges: Optional[int] = None,
    ) -> List[Tuple[float, np.ndarray, np.ndarray]]:
        """``energy_forces_stress`` for many structures, batched through MACE when possible.

        Structures are packed into chunks of at most ``max_edges`` graph edges
        (default: ``BATCH_MAX_EDGES_CUDA`` / ``_CPU``) and ``BATCH_MAX_GRAPHS``
        graphs; each chunk is one model forward/backward pass under ``self.lock``
        (the lock is released between chunks so interactive callers can
        interleave). Fully periodic, non-empty structures are batched; anything
        else, the fallback backend, or a chunk whose batched call raises (e.g.
        CUDA OOM) goes through the sequential ``energy_forces_stress`` path.
        With ``compute_stress=False`` the returned stress is zeros. Inputs are
        not mutated. Results equal the sequential path to ~1e-14 in float64.
        """
        items = list(atoms_list)
        results: List[Optional[Tuple[float, np.ndarray, np.ndarray]]] = [None] * len(items)

        def sequential(idx: int) -> None:
            e, f, s = self.energy_forces_stress(items[idx])
            results[idx] = (e, f, s if compute_stress else np.zeros(6))

        if not self.supports_batching:
            for k in range(len(items)):
                sequential(k)
            return results  # type: ignore[return-value]

        budget = int(max_edges) if max_edges else self._batch_edge_budget()
        keyspec = self._mace_keyspec()
        pending: List[Tuple[int, object]] = []
        pending_edges = 0
        used_cuda = str(self.device).startswith("cuda")

        def flush() -> None:
            nonlocal pending, pending_edges
            if not pending:
                return
            chunk, pending, pending_edges = pending, [], 0
            try:
                with self.lock:
                    out = self._mace_run_graphs([g for _, g in chunk], compute_stress)
                for (idx, _), r in zip(chunk, out):
                    results[idx] = r
            except Exception as exc:  # noqa: BLE001 - e.g. CUDA OOM; degrade, don't fail
                logger.warning("batched MACE call failed (%r); evaluating %d structures sequentially", exc, len(chunk))
                self._release_cuda_cache()
                for idx, _ in chunk:
                    sequential(idx)

        for idx, atoms in enumerate(items):
            work = self._clean_copy(atoms)
            if len(work) == 0 or not (bool(np.all(work.pbc)) and work.cell.rank == 3):
                sequential(idx)
                continue
            try:
                with self.lock:
                    graph = self._mace_graph(work, keyspec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not build MACE graph (%r); evaluating sequentially", exc)
                sequential(idx)
                continue
            n_edges = int(graph["edge_index"].shape[1])
            if pending and (pending_edges + n_edges > budget or len(pending) >= self.BATCH_MAX_GRAPHS):
                flush()
            pending.append((idx, graph))
            pending_edges += n_edges
        flush()
        if used_cuda:
            self._release_cuda_cache()
        return results  # type: ignore[return-value]

    def _release_cuda_cache(self) -> None:
        """Hand cached-but-unused CUDA memory back to the driver (the GPU is shared)."""
        if not str(getattr(self, "device", "cpu")).startswith("cuda"):
            return
        try:
            import torch

            with self.lock:
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def get_calculator(self) -> Calculator:
        """A new ASE calculator bound to this engine; safe to use from several threads."""
        return EngineCalculator(self)

    # ----------------------------------------------------------- payload
    def evaluate(self, atoms: Atoms, bond_mode: str = "fast") -> CrystalStatePayload:
        """Full ``CrystalStatePayload`` for ``atoms`` (not mutated)."""
        work = self._clean_copy(atoms)
        frac = _wrap_in_place(work)
        energy, forces, stress = self.energy_forces_stress(work)
        return self._build_payload(work, frac, energy, forces, stress, bond_mode)

    def payload_from_results(
        self,
        atoms: Atoms,
        energy: float,
        forces: np.ndarray,
        stress: np.ndarray,
        bond_mode: str = "fast",
    ) -> CrystalStatePayload:
        """``CrystalStatePayload`` for ``atoms`` from already-computed results (no model call).

        ``energy`` in eV, ``forces`` (N, 3) in eV/Å, ``stress`` Voigt (6,) in
        eV/Å^3 (ASE order and sign, as returned by ``energy_forces_stress``) for
        exactly this geometry; the caller is responsible for that. Equivalent to
        ``evaluate(atoms, bond_mode)`` given those results. ``atoms`` is not mutated.
        """
        work = self._clean_copy(atoms)
        forces = np.asarray(forces, dtype=float).reshape(len(work), 3)
        stress = np.asarray(stress, dtype=float).reshape(6)
        frac = _wrap_in_place(work)
        return self._build_payload(work, frac, float(energy), forces, stress, bond_mode)

    def _build_payload(
        self,
        work: Atoms,
        frac: np.ndarray,
        energy: float,
        forces: np.ndarray,
        stress: np.ndarray,
        bond_mode: str,
    ) -> CrystalStatePayload:
        from app.core import geometry_analysis as geo

        norms = np.linalg.norm(forces, axis=1)
        n = len(work)

        symbols = work.get_chemical_symbols()
        numbers = work.get_atomic_numbers()
        # One spglib dataset call per payload: space group, crystal system and Wyckoff labels.
        sym = geo.symmetry_info(work, geo.SYMPREC)
        wyckoffs = sym.wyckoffs if sym.wyckoffs is not None and len(sym.wyckoffs) == n else [None] * n
        sites = [
            AtomSite(
                index=k,
                element=symbols[k],
                atomic_number=int(numbers[k]),
                frac_coords=[float(x) for x in frac[k]],
                cart_coords=[float(x) for x in work.positions[k]],
                force=[float(x) for x in forces[k]],
                force_norm=float(norms[k]),
                wyckoff=wyckoffs[k],
            )
            for k in range(n)
        ]
        periodic = bool(np.all(work.pbc)) and work.cell.rank == 3
        stress_gpa = np.asarray(stress, dtype=float) * EV_PER_A3_TO_GPA
        pressure = float(-np.mean(stress_gpa[:3])) if periodic and np.all(np.isfinite(stress_gpa[:3])) else None
        density = geo.density_g_cm3(work) if periodic else None
        rdf_r, rdf_g = geo.compute_rdf(work)
        return CrystalStatePayload(
            formula=reduced_formula(work),
            spacegroup_symbol=sym.symbol,
            spacegroup_number=int(sym.number),
            lattice=geo.lattice_params(work),
            atoms=sites,
            bonds=geo.find_bonds(work, mode=bond_mode),
            energy_per_atom=energy / n if n else 0.0,
            max_force=float(norms.max()) if n else 0.0,
            stress_gpa=[float(x) for x in stress_gpa],
            bulk_modulus_gpa=None,
            e_hull_ev=None,
            rdf_r=[float(x) for x in rdf_r],
            rdf_g=[float(x) for x in rdf_g],
            pressure_gpa=pressure,
            density_g_cm3=density,
            crystal_system=sym.crystal_system,
            formula_units=geo.formula_units(work),
            symprec=sym.symprec,
        )


def _reset_calculator(calc) -> None:
    """Drop cached results; also for ASE combination calculators (the D3 ``SumCalculator``
    is a ``BaseCalculator`` without ``reset``) and each of their sub-calculators."""
    reset = getattr(calc, "reset", None)
    if callable(reset):
        reset()
        return
    calc.results = {}
    calc.atoms = None
    for sub in getattr(getattr(calc, "mixer", None), "calcs", ()):
        _reset_calculator(sub)


def _wrap_in_place(work: Atoms) -> np.ndarray:
    """Wrap ``work`` into the cell along periodic axes; return its fractional coordinates."""
    if work.cell.rank == 3:
        frac = work.cell.scaled_positions(work.positions)
        pbc = np.asarray(work.pbc, dtype=bool)
        wrapped = np.mod(frac[:, pbc], 1.0)
        # -1e-17 % 1 == 1.0 in floating point; also snap the last 1e-12 so payload
        # frac_coords match phonon_engine._wrapped_scaled exactly (the Bloch phase
        # q.R of the frontend animation assumes both use the same lattice image).
        wrapped[wrapped >= 1.0 - 1e-12] = 0.0
        frac[:, pbc] = wrapped
        work.positions = frac @ work.cell.array
        return frac
    return np.zeros((len(work), 3))


#: The one wrapping rule of the studio. The relaxer and the session use it too, so the
#: atoms they keep and the payload they send always sit on the same lattice image.
wrap_in_place = _wrap_in_place


def reduced_formula(atoms: Atoms) -> str:
    """Reduced formula in Materials Project / pymatgen convention (e.g. ``SrTiO3``)."""
    if len(atoms) == 0:
        return ""
    try:
        from pymatgen.core import Composition

        return Composition(atoms.get_chemical_formula(mode="hill")).reduced_formula
    except Exception:
        return atoms.get_chemical_formula(mode="metal", empirical=True)


# --------------------------------------------------------------------------
# Model catalog and per-model engine registry (Phase B, IMPROVEMENT_SUGGESTIONS.md §6.1)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _CatalogEntry:
    id: str
    label: str
    description: str
    approx_size_mb: Optional[float]


#: Foundation models offered in the model picker (ids are ``mace_mp_urls`` keys).
#: Sizes are the checkpoint Content-Length of the release assets (MB = 1e6 bytes).
MODEL_CATALOG: Tuple[_CatalogEntry, ...] = (
    _CatalogEntry(
        "small", "MACE-MP-0 small",
        "MACE-MP-0 (L=0, 128 channels) trained on MPtrj (Materials Project relaxation trajectories, "
        "PBE/PBE+U, MP2020-compatible energies). Fastest model; known to be soft: bulk moduli and "
        "phonon frequencies are typically underestimated by 10-20 %. Use it to explore, then confirm "
        "with a larger model.",
        32.6,
    ),
    _CatalogEntry(
        "medium", "MACE-MP-0 medium",
        "MACE-MP-0 (L=1, 128 channels) trained on MPtrj (PBE/PBE+U, MP-compatible energies). "
        "Stiffer and more accurate than small at roughly 2-3x the cost.",
        44.4,
    ),
    _CatalogEntry(
        "large", "MACE-MP-0 large",
        "The original MACE-MP-0 large checkpoint (L=2) trained on MPtrj (PBE/PBE+U). Highest capacity "
        "of the MP-0 family but the slowest, and not consistently better than medium; prefer "
        "MACE-MPA-0 for confirmation runs.",
        133.8,
    ),
    _CatalogEntry(
        "medium-mpa-0", "MACE-MPA-0 medium",
        "MACE-MPA-0 (medium) trained on MPtrj plus sAlex (a subsample of the Alexandria PBE/PBE+U "
        "database), MP-compatible energies. Clearly more accurate than MACE-MP-0 on Matbench Discovery "
        "(energies, stability, phonons); the recommended model for confirming results, including "
        "hull energies against MP-based references.",
        79.5,
    ),
    _CatalogEntry(
        "medium-omat-0", "MACE-OMAT-0 medium",
        "MACE-OMAT-0 (medium) trained on OMat24 (about 100 M strongly non-equilibrium PBE/PBE+U "
        "structures: rattled and AIMD). Robust far from equilibrium and good for phonons. Its PBE(+U) "
        "reference (pseudopotentials, U values, no MP2020 anion corrections) differs from the "
        "Materials Project one, so its absolute and formation energies are not directly comparable "
        "with MP DFT values (the studio hull re-evaluates its references with the same model, which "
        "keeps E_hull self-consistent). Academic Software License (non-commercial use).",
        79.5,
    ),
)
_CATALOG_BY_ID: Dict[str, _CatalogEntry] = {e.id: e for e in MODEL_CATALOG}

#: Max resident non-default MACE engines in the registry (the default singleton is extra).
MAX_RESIDENT_MODELS = 2


def _known_mace_urls() -> Dict[str, str]:
    """``mace_mp_urls`` of the installed package ({} if mace is disabled or not importable)."""
    if get_settings().umlip == "fallback":
        return {}
    try:
        from mace.calculators.foundations_models import mace_mp_urls
    except Exception:  # noqa: BLE001 - mace / torch missing or broken
        return {}
    return dict(mace_mp_urls)


def default_model_id() -> str:
    """Id of the process default model (``LATTICEFLOW_MACE_MODEL``); ``"lj-fallback"`` if it fell back.

    Does not create the singleton if it does not exist yet (then the settings decide).
    """
    inst = UMLIPEngine._instance
    if inst is not None:
        return inst.model_id if inst.is_mace else FALLBACK_BACKEND_NAME
    s = get_settings()
    return FALLBACK_BACKEND_NAME if s.umlip == "fallback" else s.mace_model


def list_models() -> List["ModelInfo"]:
    """Models for ``GET /api/models``: catalog entries the installed mace knows, plus the fallback."""
    from app.schemas.api import ModelInfo

    default = default_model_id()
    urls = _known_mace_urls()
    d3 = bool(urls) and dispersion_available()
    out: List[ModelInfo] = []
    for entry in MODEL_CATALOG:
        url = urls.get(entry.id)
        if url is None:
            continue
        out.append(ModelInfo(
            id=entry.id, label=entry.label, description=entry.description, family="mace",
            checkpoint_cached=mace_cached_checkpoint_path(url).is_file(),
            approx_size_mb=entry.approx_size_mb, supports_dispersion=d3, is_default=entry.id == default,
        ))
    if default != FALLBACK_BACKEND_NAME and default not in _CATALOG_BY_ID:
        # A custom default (another mace_mp name or a local checkpoint): list it so the picker can return to it.
        url = urls.get(default)
        cached = Path(default).is_file() or (url is not None and mace_cached_checkpoint_path(url).is_file())
        out.insert(0, ModelInfo(
            id=default, label=f"MACE {Path(default).name}", family="mace",
            description="The configured server default checkpoint (LATTICEFLOW_MACE_MODEL).",
            checkpoint_cached=cached, approx_size_mb=None, supports_dispersion=d3, is_default=True,
        ))
    out.append(ModelInfo(
        id=FALLBACK_BACKEND_NAME, label="Analytic fallback (LJ + Coulomb)", family="fallback",
        description="Analytic Lennard-Jones + damped-shifted-force Coulomb potential with generic "
        "parameters. Not a materials model: numbers are qualitative only. Used when MACE is "
        "unavailable, and for testing.",
        checkpoint_cached=True, approx_size_mb=None, supports_dispersion=False,
        is_default=default == FALLBACK_BACKEND_NAME,
    ))
    return out


class _EngineRegistry:
    """Process-wide engines keyed by (model_id, dispersion), LRU-bounded, thread-safe.

    * At most ``MAX_RESIDENT_MODELS`` non-default MACE engines are held (LRU).
      An evicted engine is dropped from the registry, garbage-collected and the
      CUDA cache emptied. A session still using it keeps it alive (and keeps
      working) until it switches or disconnects; while alive it is tracked
      weakly and reused rather than loaded a second time.
    * The analytic fallback engine is cheap and cached separately.
    * Loads are serialised by one lock (a concurrent request for the same key
      waits and then gets the same engine); lookups of resident engines never
      wait for a load.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._lru: "collections.OrderedDict[Tuple[str, bool], UMLIPEngine]" = collections.OrderedDict()
        self._live: "weakref.WeakValueDictionary[Tuple[str, bool], UMLIPEngine]" = weakref.WeakValueDictionary()
        self._fallback: Optional[UMLIPEngine] = None

    def _lookup(self, key: Tuple[str, bool]) -> Tuple[Optional[UMLIPEngine], List[UMLIPEngine]]:
        """Under ``_lock``: the engine for ``key`` (moved to MRU) and the engines that evicted."""
        engine = self._lru.get(key)
        if engine is not None:
            self._lru.move_to_end(key)
            return engine, []
        engine = self._live.get(key)
        if engine is None:
            return None, []
        return engine, self._insert(key, engine)

    def _insert(self, key: Tuple[str, bool], engine: UMLIPEngine) -> List[UMLIPEngine]:
        self._lru[key] = engine
        self._live[key] = engine
        self._lru.move_to_end(key)
        evicted = []
        while len(self._lru) > MAX_RESIDENT_MODELS:
            _, old = self._lru.popitem(last=False)
            evicted.append(old)
        return evicted

    @staticmethod
    def _release(evicted: List[UMLIPEngine]) -> None:
        """Drop the last registry references to ``evicted`` and hand freed GPU memory back."""
        if not evicted:
            return
        cuda = any(str(e.device).startswith("cuda") for e in evicted)
        names = [e.backend_name for e in evicted]
        evicted.clear()
        gc.collect()
        if cuda:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
        logger.info("evicted force field(s) %s from the model registry", names)

    def get(self, model_id: str, dispersion: bool, progress: Optional[ProgressFn]) -> UMLIPEngine:
        if model_id == FALLBACK_BACKEND_NAME:
            if dispersion:
                raise ModelUnavailableError("D3 dispersion is only available for MACE models, not the analytic fallback")
            with self._lock:
                if self._fallback is None:
                    self._fallback = UMLIPEngine(backend="fallback")
                return self._fallback
        if get_settings().umlip == "fallback":
            raise ModelUnavailableError(
                f"MACE model {model_id!r} is unavailable: the server runs with LATTICEFLOW_UMLIP=fallback"
            )
        urls = _known_mace_urls()
        if model_id not in urls:
            known = ", ".join([e.id for e in MODEL_CATALOG if e.id in urls] + [FALLBACK_BACKEND_NAME])
            raise ModelUnavailableError(f"unknown model {model_id!r}; available: {known}")
        key = (model_id, bool(dispersion))
        with self._lock:
            engine, evicted = self._lookup(key)
        self._release(evicted)
        if engine is not None:
            return engine
        with self._load_lock:
            with self._lock:
                engine, evicted = self._lookup(key)  # loaded by a concurrent request meanwhile?
            self._release(evicted)
            if engine is not None:
                return engine
            label = f"{model_id} + D3" if dispersion else model_id
            _safe_progress(progress, 0, 0, f"Loading {label}")
            try:
                engine = _build_engine(model_id, dispersion, progress)
            except ModelUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001
                cause = exc.__cause__ if exc.__cause__ is not None else exc
                if isinstance(cause, ModelUnavailableError):
                    raise cause from None
                raise ModelUnavailableError(f"model {label!r} could not be loaded: {cause}") from exc
            _safe_progress(progress, 1, 1, f"Loaded {engine.backend_name} on {engine.device}")
            with self._lock:
                evicted = self._insert(key, engine)
            self._release(evicted)
            return engine

    def resident(self) -> List[Tuple[str, bool]]:
        """Keys of the engines currently held (LRU order, oldest first)."""
        with self._lock:
            return list(self._lru)

    def clear(self) -> None:
        with self._lock:
            evicted = list(self._lru.values())
            self._lru.clear()
            self._live = weakref.WeakValueDictionary()
            self._fallback = None
        self._release(evicted)


def _build_engine(model_id: str, dispersion: bool, progress: Optional[ProgressFn]) -> UMLIPEngine:
    """Load one registry engine (module-level so tests can substitute it)."""
    return UMLIPEngine(backend="mace", model=model_id, dispersion=dispersion, progress=progress)


_REGISTRY = _EngineRegistry()


def get_engine(
    model_id: Optional[str] = None, dispersion: bool = False, *, progress: Optional[ProgressFn] = None
) -> UMLIPEngine:
    """Engine for ``model_id`` (+ D3(BJ) when ``dispersion``).

    ``None`` or the default model without dispersion returns the process
    singleton (``UMLIPEngine.get_instance()``), so existing behaviour is
    unchanged; ``None`` with dispersion means the default model + D3. Anything
    else comes from a thread-safe, LRU-bounded registry (:class:`_EngineRegistry`).
    Loading resolves the checkpoint through the bounded download path and
    reports ``progress(done_bytes, total_bytes, message)`` (once at the start
    with total 0, during a download, and once at the end). Raises
    :class:`ModelUnavailableError` for unknown ids, a checkpoint that is not
    cached while downloads are disabled, a missing torch-dftd (D3), or any
    other load failure.
    """
    default = UMLIPEngine.get_instance() if model_id is not None or not dispersion else None
    if model_id is None:
        if not dispersion:
            return default  # type: ignore[return-value]
        model_id = default_model_id()
    model_id = str(model_id).strip()
    if not dispersion:
        assert default is not None
        if model_id == (default.model_id if default.is_mace else FALLBACK_BACKEND_NAME):
            return default
    return _REGISTRY.get(model_id, bool(dispersion), progress)


def resident_models() -> List[Tuple[str, bool]]:
    """(model_id, dispersion) of the registry engines currently held, oldest first."""
    return _REGISTRY.resident()


def reset_registry() -> None:
    """Drop every registry engine (tests). The default singleton is untouched."""
    _REGISTRY.clear()
