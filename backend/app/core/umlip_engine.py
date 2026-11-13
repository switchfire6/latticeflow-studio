"""Universal ML interatomic potential engine (PROJECT_SPEC.md §4.3, §7 Phase 1).

``UMLIPEngine`` wraps exactly one force-field backend for the whole process:

* ``mace``      MACE-MP-0 via ``mace.calculators.mace_mp`` (``backend_name`` e.g. ``"mace-mp-0-small"``)
* ``fallback``  the analytic LJ + DSF-Coulomb calculator (``backend_name == "lj-fallback"``)

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

import logging
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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


def _download_with_deadline(url: str, dest: Path, timeout_s: float) -> None:
    """Download ``url`` to ``dest`` atomically; never blocks the caller > timeout_s."""
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
                with open(tmp, "wb") as out:
                    while not cancel.is_set():
                        block = resp.read(1 << 18)
                        if not block:
                            break
                        out.write(block)
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


def resolve_mace_checkpoint(model: str, *, allow_download: bool, timeout_s: float) -> Tuple[Path, str]:
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
        _download_with_deadline(url, path, timeout_s)
    return path, f"mace-mp-0-{model}" if not model.startswith("https://") else "mace-mp-0-custom"


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
    ) -> None:
        s = settings or get_settings()
        mode = (backend or s.umlip).lower()
        if mode not in ("auto", "mace", "fallback"):
            raise ValueError(f"unknown uMLIP backend {mode!r}")
        self.requested_backend = mode
        self.lock = threading.RLock()
        self.load_error: Optional[BaseException] = None
        self.model = model or s.mace_model
        self._calc: Calculator

        if mode in ("auto", "mace"):
            try:
                self._load_mace(
                    device=device or s.device,
                    dtype=dtype or s.dtype,
                    allow_download=s.mace_allow_download if allow_download is None else allow_download,
                    timeout_s=download_timeout_s or s.mace_download_timeout_s,
                )
                return
            except Exception as exc:
                if mode == "mace":
                    raise RuntimeError(f"MACE backend could not be loaded: {exc}") from exc
                self.load_error = exc
                logger.warning("MACE unavailable (%s); using analytic fallback calculator", exc)
        self._init_fallback()

    # ------------------------------------------------------------ loading
    def _load_mace(self, *, device: str, dtype: str, allow_download: bool, timeout_s: float) -> None:
        path, name = resolve_mace_checkpoint(self.model, allow_download=allow_download, timeout_s=timeout_s)
        from mace.calculators import mace_mp

        dev = resolve_device(device)
        self._calc = mace_mp(model=str(path), device=dev, default_dtype=dtype)
        self.backend_name = name
        self.device = dev
        self.dtype = dtype
        self.checkpoint_path: Optional[Path] = path

    def _init_fallback(self) -> None:
        self._calc = AnalyticFallbackCalculator()
        self.backend_name = FALLBACK_BACKEND_NAME
        self.device = "cpu"
        self.dtype = "float64"
        self.checkpoint_path = None

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
            self._calc.reset()  # never reuse results cached for another structure/thread
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
        if not self.is_mace:
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


def reduced_formula(atoms: Atoms) -> str:
    """Reduced formula in Materials Project / pymatgen convention (e.g. ``SrTiO3``)."""
    if len(atoms) == 0:
        return ""
    try:
        from pymatgen.core import Composition

        return Composition(atoms.get_chemical_formula(mode="hill")).reduced_formula
    except Exception:
        return atoms.get_chemical_formula(mode="metal", empirical=True)
