"""Self-consistent uMLIP convex hull (PROJECT_SPEC.md §5.3 ``POST /api/hull/evaluate``).

Method
------
Mixing the studio's uMLIP energies with Materials Project DFT energies is
inconsistent (different functional, corrections and reference states), so the
hull is built *entirely* in the studio's uMLIP:

1. **Reference structures.** For every sub-chemical-system of the candidate's
   elements (all elements, binaries, ternaries, ...) the structures of MP phases
   with ``energy_above_hull <= MP_E_HULL_CUTOFF`` (0.1 eV/atom) are fetched.
   Only MP's *structures* are used, never its energies.
2. **Selection.** Structures with more than ``MAX_REFERENCE_ATOMS`` (64) sites or
   partial occupancies are skipped (recorded). For every element the lowest-MP-
   E_hull elemental phase is always kept; the remaining phases are ranked by
   MP E_hull (then by size) and the set is capped at ``MAX_REFERENCES`` (40); the
   rest is dropped (logged and recorded).
3. **Relaxation.** Every kept reference is relaxed with the uMLIP via
   :func:`app.core.relaxer.iter_relaxation` (FIRE on ``FrechetCellFilter``,
   ``fmax = 0.05`` eV/Å, at most 150 steps, cell relaxed). References whose
   relaxation diverges or raises are skipped; unconverged ones (hit the step
   limit) are kept, because their energy is still an upper bound.
4. **Hull.** A pymatgen ``PhaseDiagram`` is built from the relaxed references
   only. By default (``relax_candidate=True``, Phase A §5.11) the candidate is
   first relaxed with exactly the reference settings (FIRE + FrechetCellFilter,
   ``RELAX_FMAX``, ``RELAX_MAX_STEPS``) and the relaxed structure is scored
   (``candidate_relaxed=True``; ``energy_per_atom_as_given`` is the single point
   of the structure as sent, i.e. step 0 of that relaxation). If the candidate
   relaxation diverges, the as-given single point is scored instead and
   ``method`` says so. With ``relax_candidate=False`` the candidate is scored
   with a uMLIP single point of the structure *as given*.
   ``e_hull_ev = E_cand/atom - E_hull(composition)/atom`` against that
   diagram, so a candidate below the reference hull gets a negative value.
   ``decomposition`` maps the reduced formula of each competing hull phase to its
   *atom* fraction at the candidate composition (pymatgen's convention; the
   fractions sum to 1). ``formation_energy_per_atom`` is relative to the relaxed
   elemental references.

Fetcher interface
-----------------
``fetcher(chemsys_list, api_key) -> Iterable[MPReference | (material_id, Structure) |
(material_id, Structure, mp_e_above_hull)]``. ``chemsys_list`` are MP-style
chemical systems (elements sorted alphabetically, joined by ``-``). The default,
:func:`fetch_reference_structures`, uses ``mp_api.client.MPRester``. Fetchers
signal failure by raising :class:`HullFetchError` (anything else they raise is
wrapped into one), and a missing key with :class:`HullNoApiKeyError`.

Cache
-----
:class:`HullCache` keeps the relaxed reference sets in memory and on disk under
``~/.cache/latticeflow/hull/`` (override with the ``directory`` argument or the
``LATTICEFLOW_HULL_CACHE_DIR`` environment variable), keyed by the candidate's
chemical system plus ``engine.backend_name``. A second evaluation in the same
chemical system therefore makes no fetch and no relaxation (and needs no API
key). Disk writes are atomic (unique temp file + ``os.replace``). A cache file
written with different selection / relaxation parameters is ignored.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms

from app.schemas.api import HullEvaluateResponse, HullReferencePhase

if TYPE_CHECKING:  # pragma: no cover
    from pymatgen.core import Structure

    from app.core.umlip_engine import UMLIPEngine

logger = logging.getLogger(__name__)

#: MP phases with ``energy_above_hull`` up to this value (eV/atom) are used as references.
MP_E_HULL_CUTOFF = 0.1
#: References with more sites than this are skipped (relaxation cost).
MAX_REFERENCE_ATOMS = 64
#: At most this many references per chemical system (elemental ground states always kept).
MAX_REFERENCES = 40
#: Reference relaxation settings.
RELAX_FMAX = 0.05
RELAX_MAX_STEPS = 150

#: Bump when the cache file layout or the meaning of its contents changes.
CACHE_VERSION = 1

_ENV_CACHE_DIR = "LATTICEFLOW_HULL_CACHE_DIR"

MP_SUMMARY_FIELDS = ["material_id", "formula_pretty", "structure", "energy_above_hull", "nsites"]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class HullError(RuntimeError):
    """Base class of every hull-analysis failure."""


class HullNoApiKeyError(HullError):
    """No (valid) Materials Project API key is configured, and a fetch is required."""


class HullFetchError(HullError):
    """Fetching reference structures from the Materials Project failed or MP is unreachable."""


class HullMissingElementalReferenceError(HullError):
    """At least one element of the candidate has no usable elemental reference phase."""

    def __init__(self, elements: Sequence[str], detail: str = "") -> None:
        self.elements = list(elements)
        msg = f"no usable elemental reference phase for {', '.join(self.elements)}"
        super().__init__(f"{msg}: {detail}" if detail else msg)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
@dataclass
class MPReference:
    """One reference structure as delivered by a fetcher."""

    material_id: str
    structure: "Structure"
    mp_e_above_hull: float = 0.0


@dataclass
class RelaxedReference:
    """A reference phase relaxed with the uMLIP (JSON-serialisable)."""

    material_id: str
    formula: str                     # reduced formula
    composition: Dict[str, float]    # element -> atoms in the cell
    energy: float                    # total uMLIP energy of the relaxed cell, eV
    n_atoms: int
    mp_e_above_hull: float
    converged: bool
    steps: int
    numbers: List[int]
    cell: List[List[float]]
    positions: List[List[float]]

    @property
    def energy_per_atom(self) -> float:
        return self.energy / self.n_atoms

    @property
    def is_elemental(self) -> bool:
        return len(self.composition) == 1

    def atoms(self) -> Atoms:
        """The relaxed structure."""
        return Atoms(numbers=self.numbers, cell=self.cell, positions=self.positions, pbc=True)


@dataclass
class OmittedReference:
    material_id: str
    formula: str
    reason: str


@dataclass
class ReferenceSet:
    """Relaxed references of one chemical system for one uMLIP backend."""

    chemsys: str
    backend_name: str
    references: List[RelaxedReference]
    skipped: List[OmittedReference] = field(default_factory=list)   # too large, disordered, relaxation failed
    dropped: List[OmittedReference] = field(default_factory=list)   # over the MAX_REFERENCES cap
    params: Dict[str, Any] = field(default_factory=dict)
    build_seconds: float = 0.0

    def phase_diagram(self):
        from pymatgen.analysis.phase_diagram import PDEntry, PhaseDiagram
        from pymatgen.core import Composition

        entries = [
            PDEntry(Composition(r.composition), r.energy, name=r.formula, attribute=r.material_id)
            for r in self.references
        ]
        return PhaseDiagram(entries)

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "chemsys": self.chemsys,
            "backend_name": self.backend_name,
            "params": self.params,
            "build_seconds": self.build_seconds,
            "references": [asdict(r) for r in self.references],
            "skipped": [asdict(s) for s in self.skipped],
            "dropped": [asdict(d) for d in self.dropped],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "ReferenceSet":
        return cls(
            chemsys=data["chemsys"],
            backend_name=data["backend_name"],
            params=dict(data.get("params", {})),
            build_seconds=float(data.get("build_seconds", 0.0)),
            references=[RelaxedReference(**r) for r in data["references"]],
            skipped=[OmittedReference(**s) for s in data.get("skipped", [])],
            dropped=[OmittedReference(**d) for d in data.get("dropped", [])],
        )


Fetcher = Callable[[List[str], Optional[str]], Iterable[Any]]


def _current_params(max_references: int) -> Dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "mp_e_hull_cutoff": MP_E_HULL_CUTOFF,
        "max_reference_atoms": MAX_REFERENCE_ATOMS,
        "max_references": int(max_references),
        "relax_fmax": RELAX_FMAX,
        "relax_max_steps": RELAX_MAX_STEPS,
    }


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def default_cache_dir() -> Path:
    override = os.environ.get(_ENV_CACHE_DIR, "").strip()
    return Path(override) if override else Path.home() / ".cache" / "latticeflow" / "hull"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


class HullCache:
    """In-memory + on-disk cache of :class:`ReferenceSet` keyed by (backend_name, chemsys).

    ``directory=None`` uses :func:`default_cache_dir`; ``persist=False`` keeps it
    in memory only. Thread-safe; :meth:`key_lock` serialises builds of one key so
    concurrent requests for the same chemical system relax the references once.
    """

    def __init__(self, directory: Optional[os.PathLike] = None, *, persist: bool = True) -> None:
        self.directory = Path(directory) if directory is not None else default_cache_dir()
        self.persist = persist
        self._memory: Dict[Tuple[str, str], ReferenceSet] = {}
        self._lock = threading.Lock()
        self._key_locks: Dict[Tuple[str, str], threading.Lock] = {}

    def path_for(self, backend_name: str, chemsys: str) -> Path:
        return self.directory / f"{_safe_name(backend_name)}__{_safe_name(chemsys)}.json"

    def key_lock(self, backend_name: str, chemsys: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault((backend_name, chemsys), threading.Lock())

    def get(self, backend_name: str, chemsys: str, params: Optional[Dict[str, Any]] = None) -> Optional[ReferenceSet]:
        key = (backend_name, chemsys)
        with self._lock:
            hit = self._memory.get(key)
        if hit is not None and (params is None or hit.params == params):
            return hit
        if not self.persist:
            return None
        path = self.path_for(backend_name, chemsys)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            refset = ReferenceSet.from_json(data)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - corrupt / foreign file: treat as a miss
            logger.warning("ignoring unreadable hull cache file %s (%r)", path, exc)
            return None
        if data.get("version") != CACHE_VERSION or refset.backend_name != backend_name or refset.chemsys != chemsys:
            return None
        if params is not None and refset.params != params:
            logger.info("hull cache %s was built with other parameters; rebuilding", path)
            return None
        with self._lock:
            self._memory[key] = refset
        return refset

    def put(self, refset: ReferenceSet) -> None:
        with self._lock:
            self._memory[(refset.backend_name, refset.chemsys)] = refset
        if not self.persist:
            return
        path = self.path_for(refset.backend_name, refset.chemsys)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(refset.to_json()), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # a read-only cache must not fail the evaluation
            logger.warning("could not write hull cache %s (%r)", path, exc)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def clear_memory(self) -> None:
        with self._lock:
            self._memory.clear()


_default_cache: Optional[HullCache] = None
_default_cache_lock = threading.Lock()


def get_default_cache() -> HullCache:
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = HullCache()
        return _default_cache


# --------------------------------------------------------------------------
# chemistry helpers
# --------------------------------------------------------------------------
def chemsys_of(elements: Iterable[str]) -> str:
    """MP / pymatgen chemical-system string: unique elements sorted alphabetically, joined by ``-``."""
    return "-".join(sorted(set(elements)))


def sub_chemsys(elements: Iterable[str]) -> List[str]:
    """Every non-empty sub-system, e.g. ``["O", "Sr", "Ti", "O-Sr", "O-Ti", "Sr-Ti", "O-Sr-Ti"]``."""
    els = sorted(set(elements))
    return ["-".join(c) for k in range(1, len(els) + 1) for c in itertools.combinations(els, k)]


def _structure_to_atoms(structure: "Structure") -> Atoms:
    """Plain ``Atoms`` (no magmoms / charges / site properties) from an ordered pymatgen structure."""
    return Atoms(
        numbers=[site.specie.Z for site in structure],
        cell=np.array(structure.lattice.matrix, dtype=float),
        positions=np.array(structure.cart_coords, dtype=float),
        pbc=True,
    )


def _normalize_fetched(items: Iterable[Any]) -> List[MPReference]:
    out: List[MPReference] = []
    for item in items:
        if isinstance(item, MPReference):
            out.append(item)
            continue
        if isinstance(item, (tuple, list)) and len(item) in (2, 3):
            ehull = float(item[2]) if len(item) == 3 and item[2] is not None else 0.0
            out.append(MPReference(str(item[0]), item[1], ehull))
            continue
        raise HullFetchError(f"fetcher returned an unsupported item {type(item).__name__}")
    return out


# --------------------------------------------------------------------------
# default fetcher (Materials Project)
# --------------------------------------------------------------------------
def _doc_get(doc: Any, name: str) -> Any:
    return doc.get(name) if isinstance(doc, dict) else getattr(doc, name, None)


def _mp_user_agent() -> str:
    """Generic User-Agent: the client library and version only (no platform, no app name)."""
    try:
        from importlib.metadata import version

        return f"mp-api/{version('mp-api')}"
    except Exception:  # noqa: BLE001
        return "mp-api"


def fetch_reference_structures(chemsys_list: List[str], api_key: Optional[str]) -> List[MPReference]:
    """Fetch MP structures with E_hull^MP <= ``MP_E_HULL_CUTOFF`` for every chemsys in ``chemsys_list``.

    One summary query for all sub-systems::

        MPRester(api_key, mute_progress_bars=True, include_user_agent=False).materials.summary.search(
            chemsys=chemsys_list, energy_above_hull=(0, MP_E_HULL_CUTOFF), deprecated=False,
            include_gnome=False, fields=MP_SUMMARY_FIELDS)

    Raises :class:`HullNoApiKeyError` for a missing or malformed key (MP keys
    are 32 characters) and :class:`HullFetchError` for anything else
    (network, HTTP errors, an unauthorised key, unexpected documents).
    """
    key = (api_key or "").strip()
    if not key:
        raise HullNoApiKeyError("MP_API_KEY is not configured; the convex-hull lookup is unavailable")
    if len(key) != 32:
        raise HullNoApiKeyError("MP_API_KEY is malformed (Materials Project keys are 32 characters)")
    try:
        from mp_api.client import MPRester
    except Exception as exc:  # noqa: BLE001
        raise HullFetchError(f"mp-api client is not importable: {exc}") from exc

    try:
        # include_user_agent=False keeps platform / Python details out of the request, but
        # then mp_api sends no User-Agent at all and MP's front end answers 403 (verified
        # 2026-11-12, mp-api 0.46.5). Send a generic library-only agent instead; MPRester
        # shares this one session with all of its sub-clients.
        with MPRester(api_key=key, mute_progress_bars=True, include_user_agent=False) as mpr:
            mpr.session.headers["user-agent"] = _mp_user_agent()
            docs = mpr.materials.summary.search(
                chemsys=list(chemsys_list),
                energy_above_hull=(0.0, MP_E_HULL_CUTOFF),
                deprecated=False,
                include_gnome=False,
                fields=list(MP_SUMMARY_FIELDS),
            )
    except HullError:
        raise
    except Exception as exc:  # noqa: BLE001 - MPRestError, requests errors, validation errors, ...
        text = str(exc)
        hint = " (HTTP 401/403: the API key was rejected or the request was blocked)" if re.search(r"\b(401|403)\b", text) else ""
        raise HullFetchError(f"Materials Project query failed{hint}: {type(exc).__name__}: {text[:300]}") from exc

    from pymatgen.core import Structure

    out: List[MPReference] = []
    for doc in docs or []:
        mid = _doc_get(doc, "material_id")
        structure = _doc_get(doc, "structure")
        if mid is None or structure is None:
            continue
        if isinstance(structure, dict):
            structure = Structure.from_dict(structure)
        ehull = _doc_get(doc, "energy_above_hull")
        out.append(MPReference(str(mid), structure, float(ehull) if ehull is not None else 0.0))
    return out


# --------------------------------------------------------------------------
# reference selection / relaxation
# --------------------------------------------------------------------------
def _select_references(
    fetched: List[MPReference], elements: Sequence[str], max_references: int
) -> Tuple[List[Tuple[MPReference, Atoms]], List[OmittedReference], List[OmittedReference]]:
    """Filter, always keep elemental ground states, cap the rest. Returns (kept, skipped, dropped)."""
    allowed = set(elements)
    usable: List[Tuple[MPReference, Atoms]] = []
    skipped: List[OmittedReference] = []
    seen: set = set()
    for ref in fetched:
        if ref.material_id in seen:
            continue
        seen.add(ref.material_id)
        try:
            formula = ref.structure.composition.reduced_formula
            ordered = ref.structure.is_ordered
        except Exception as exc:  # noqa: BLE001
            skipped.append(OmittedReference(ref.material_id, "?", f"unreadable structure ({exc})"))
            continue
        if not ordered:
            skipped.append(OmittedReference(ref.material_id, formula, "disordered structure"))
            continue
        els = {el.symbol for el in ref.structure.composition.elements}
        if not els or not els <= allowed:
            continue  # outside the chemical system (a misbehaving fetcher); silently ignore
        if ref.mp_e_above_hull > MP_E_HULL_CUTOFF + 1e-9:
            continue
        if len(ref.structure) > MAX_REFERENCE_ATOMS:
            skipped.append(
                OmittedReference(ref.material_id, formula, f"{len(ref.structure)} atoms > {MAX_REFERENCE_ATOMS}")
            )
            continue
        usable.append((ref, _structure_to_atoms(ref.structure)))

    def rank(item: Tuple[MPReference, Atoms]):
        return (item[0].mp_e_above_hull, len(item[1]), item[0].material_id)

    # One mandatory elemental phase per element: lowest MP E_hull, then smallest cell.
    mandatory: Dict[str, Tuple[MPReference, Atoms]] = {}
    for item in sorted(usable, key=rank):
        syms = set(item[1].get_chemical_symbols())
        if len(syms) == 1:
            mandatory.setdefault(next(iter(syms)), item)
    missing = [el for el in sorted(allowed) if el not in mandatory]
    if missing:
        raise HullMissingElementalReferenceError(
            missing, "the Materials Project returned no ordered elemental phase with <= "
            f"{MAX_REFERENCE_ATOMS} atoms and E_hull <= {MP_E_HULL_CUTOFF} eV/atom"
        )
    mandatory_ids = {item[0].material_id for item in mandatory.values()}
    rest = sorted((item for item in usable if item[0].material_id not in mandatory_ids), key=rank)
    budget = max(0, int(max_references) - len(mandatory))
    kept = sorted(mandatory.values(), key=rank) + rest[:budget]
    dropped = [
        OmittedReference(ref.material_id, ref.structure.composition.reduced_formula,
                         f"over the {max_references}-reference cap (MP E_hull {ref.mp_e_above_hull:.3f} eV/atom)")
        for ref, _ in rest[budget:]
    ]
    if dropped:
        logger.info(
            "hull: dropped %d references over the cap of %d: %s",
            len(dropped), max_references, ", ".join(f"{d.material_id} ({d.formula})" for d in dropped),
        )
    return kept, skipped, dropped


def _relax_reference(ref: MPReference, atoms: Atoms, engine: "UMLIPEngine") -> RelaxedReference:
    from app.core.relaxer import iter_relaxation

    last = None
    for frame in iter_relaxation(
        atoms, engine, fmax=RELAX_FMAX, max_steps=RELAX_MAX_STEPS, relax_cell=True
    ):
        last = frame
    if last is None:  # pragma: no cover - iter_relaxation always yields at least step 0
        raise RuntimeError("relaxation produced no frame")
    n = len(atoms)
    energy = float(last.crystal.energy_per_atom) * n
    if not np.isfinite(energy):
        raise RuntimeError("non-finite relaxed energy")
    comp: Dict[str, float] = {}
    for sym in atoms.get_chemical_symbols():
        comp[sym] = comp.get(sym, 0.0) + 1.0
    from pymatgen.core import Composition

    return RelaxedReference(
        material_id=ref.material_id,
        formula=Composition(comp).reduced_formula,
        composition=comp,
        energy=energy,
        n_atoms=n,
        mp_e_above_hull=float(ref.mp_e_above_hull),
        converged=bool(last.converged),
        steps=int(last.step),
        numbers=[int(z) for z in atoms.get_atomic_numbers()],
        cell=np.asarray(atoms.cell.array, dtype=float).tolist(),
        positions=np.asarray(atoms.positions, dtype=float).tolist(),
    )


def build_reference_set(
    elements: Sequence[str],
    engine: "UMLIPEngine",
    *,
    api_key: Optional[str] = None,
    fetcher: Optional[Fetcher] = None,
    max_references: int = MAX_REFERENCES,
) -> ReferenceSet:
    """Fetch, select and relax the references of ``chemsys_of(elements)`` (no caching)."""
    t0 = time.perf_counter()
    elements = sorted(set(elements))
    chemsys = chemsys_of(elements)
    fetch = fetcher or fetch_reference_structures
    try:
        raw = fetch(sub_chemsys(elements), api_key)
        fetched = _normalize_fetched(raw)
    except HullError:
        raise
    except Exception as exc:  # noqa: BLE001 - any fetcher failure is a fetch failure
        raise HullFetchError(f"reference fetch failed: {type(exc).__name__}: {exc}") from exc
    t_fetch = time.perf_counter() - t0

    kept, skipped, dropped = _select_references(fetched, elements, max_references)
    relaxed: List[RelaxedReference] = []
    for ref, atoms in kept:
        try:
            relaxed.append(_relax_reference(ref, atoms, engine))
        except Exception as exc:  # noqa: BLE001 - RelaxationDivergedError, engine errors
            logger.warning("hull: skipping reference %s: relaxation failed (%s)", ref.material_id, exc)
            skipped.append(
                OmittedReference(ref.material_id, ref.structure.composition.reduced_formula,
                                 f"relaxation failed: {exc}")
            )
    have = {next(iter(r.composition)) for r in relaxed if r.is_elemental}
    missing = [el for el in elements if el not in have]
    if missing:
        raise HullMissingElementalReferenceError(missing, "the elemental reference relaxation failed")

    elapsed = time.perf_counter() - t0
    logger.info(
        "hull: built %s references for %s with %s in %.2f s (fetch %.2f s; %d skipped, %d dropped)",
        len(relaxed), chemsys, engine.backend_name, elapsed, t_fetch, len(skipped), len(dropped),
    )
    return ReferenceSet(
        chemsys=chemsys,
        backend_name=engine.backend_name,
        references=relaxed,
        skipped=skipped,
        dropped=dropped,
        params=_current_params(max_references),
        build_seconds=elapsed,
    )


def get_reference_set(
    elements: Sequence[str],
    engine: "UMLIPEngine",
    *,
    api_key: Optional[str] = None,
    fetcher: Optional[Fetcher] = None,
    cache: Optional[HullCache] = None,
    max_references: int = MAX_REFERENCES,
) -> ReferenceSet:
    """Cached :func:`build_reference_set`."""
    cache = cache if cache is not None else get_default_cache()
    chemsys = chemsys_of(elements)
    params = _current_params(max_references)
    hit = cache.get(engine.backend_name, chemsys, params)
    if hit is not None:
        return hit
    with cache.key_lock(engine.backend_name, chemsys):
        hit = cache.get(engine.backend_name, chemsys, params)  # built by a concurrent request meanwhile?
        if hit is not None:
            return hit
        refset = build_reference_set(
            elements, engine, api_key=api_key, fetcher=fetcher, max_references=max_references
        )
        cache.put(refset)
        return refset


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def _method_string(refset: ReferenceSet, candidate: str = "candidate single point as given") -> str:
    unconverged = [r.material_id for r in refset.references if not r.converged]
    parts = [
        f"{len(refset.references)} MP reference phases of {refset.chemsys} "
        f"(E_hull^MP <= {MP_E_HULL_CUTOFF} eV/atom; MP structures only, no MP energies) "
        f"relaxed with {refset.backend_name} (FIRE + FrechetCellFilter, fmax {RELAX_FMAX} eV/A, "
        f"<= {RELAX_MAX_STEPS} steps); {candidate}; "
        f"E_hull vs. the reference hull only (negative = below it)"
    ]
    if unconverged:
        parts.append(f"{len(unconverged)} reference(s) not converged in {RELAX_MAX_STEPS} steps: {', '.join(unconverged)}")
    if refset.skipped:
        parts.append(
            f"skipped {len(refset.skipped)}: "
            + ", ".join(f"{s.material_id} {s.formula} ({s.reason})" for s in refset.skipped)
        )
    if refset.dropped:
        parts.append(f"dropped {len(refset.dropped)} over the {refset.params.get('max_references', MAX_REFERENCES)}-reference cap")
    return "; ".join(parts)


def evaluate_hull(
    atoms: Atoms,
    *,
    engine: Optional["UMLIPEngine"] = None,
    api_key: Optional[str] = None,
    fetcher: Optional[Fetcher] = None,
    cache: Optional[HullCache] = None,
    max_references: int = MAX_REFERENCES,
    relax_candidate: bool = True,
) -> HullEvaluateResponse:
    """Energy above the self-consistent uMLIP hull for ``atoms`` (not mutated). See module docstring.

    ``relax_candidate`` (default True): relax a copy of the candidate with the
    reference settings first and score the relaxed structure; False scores the
    structure as given (single point).

    ``api_key`` is only needed when the reference set is not cached. Raises
    :class:`HullNoApiKeyError`, :class:`HullFetchError` or
    :class:`HullMissingElementalReferenceError`.
    """
    from pymatgen.core import Composition

    if engine is None:
        from app.core.umlip_engine import UMLIPEngine

        engine = UMLIPEngine.get_instance()
    if len(atoms) == 0:
        raise ValueError("candidate structure has no atoms")
    symbols = atoms.get_chemical_symbols()
    refset = get_reference_set(
        symbols, engine, api_key=api_key, fetcher=fetcher, cache=cache, max_references=max_references
    )

    n = len(atoms)
    e_as_given: Optional[float] = None
    candidate_relaxed = False
    candidate_note = "candidate single point as given"
    e_cand = float("nan")
    if relax_candidate:
        e_cand, e_as_given, candidate_relaxed, candidate_note = _score_relaxed_candidate(atoms, engine)
    if not candidate_relaxed:
        energy, _, _ = engine.energy_forces_stress(atoms)
        e_cand = float(energy) / n
        if e_as_given is None and relax_candidate:
            e_as_given = e_cand
    if not np.isfinite(e_cand):
        raise RuntimeError("candidate energy is not finite")

    comp_counts: Dict[str, float] = {}
    for sym in symbols:
        comp_counts[sym] = comp_counts.get(sym, 0.0) + 1.0
    comp = Composition(comp_counts)

    pd = refset.phase_diagram()
    decomp, hull_energy = pd.get_decomp_and_hull_energy_per_atom(comp)
    e_hull = e_cand - float(hull_energy)

    decomposition: Dict[str, float] = {}
    for entry, amount in decomp.items():
        if amount <= 1e-12:
            continue
        name = entry.composition.reduced_formula
        decomposition[name] = decomposition.get(name, 0.0) + float(amount)
    total = sum(decomposition.values())
    if total > 0:
        decomposition = {k: v / total for k, v in decomposition.items()}

    el_refs = pd.el_refs
    e_elements = sum(frac * el_refs[el].energy_per_atom for el, frac in comp.fractional_composition.items())
    formation = e_cand - float(e_elements)

    return HullEvaluateResponse(
        chemsys=refset.chemsys,
        energy_per_atom=e_cand,
        e_hull_ev=float(e_hull),
        formation_energy_per_atom=float(formation),
        decomposition=decomposition,
        references=[
            HullReferencePhase(material_id=r.material_id, formula=r.formula, energy_per_atom=r.energy_per_atom)
            for r in refset.references
        ],
        method=_method_string(refset, candidate_note),
        umlip_backend=engine.backend_name,
        candidate_relaxed=candidate_relaxed,
        energy_per_atom_as_given=e_as_given,
    )


def _score_relaxed_candidate(atoms: Atoms, engine: "UMLIPEngine") -> Tuple[float, Optional[float], bool, str]:
    """Relax a copy of the candidate with the reference settings.

    Returns (E/atom of the scored structure, E/atom as given, relaxed?, method note).
    On divergence returns (nan, E as given if known, False, note) and the caller
    falls back to the as-given single point. Engine errors propagate.
    """
    from app.core.relaxer import RelaxationDivergedError, iter_relaxation

    work = Atoms(
        numbers=atoms.get_atomic_numbers().copy(),
        positions=np.array(atoms.positions, dtype=float),
        cell=np.array(atoms.cell.array, dtype=float),
        pbc=np.array(atoms.pbc, dtype=bool),
    )
    first = last = None
    try:
        for frame in iter_relaxation(work, engine, fmax=RELAX_FMAX, max_steps=RELAX_MAX_STEPS, relax_cell=True):
            if first is None:
                first = frame
            last = frame
    except RelaxationDivergedError as exc:
        logger.warning("hull: candidate relaxation diverged (%s); scoring the structure as given", exc)
        e0 = float(first.crystal.energy_per_atom) if first is not None else None
        return (float("nan"), e0, False,
                f"candidate relaxation diverged ({getattr(exc, 'reason', exc)}), single point as given")
    if last is None:  # pragma: no cover - iter_relaxation always yields step 0
        return float("nan"), None, False, "candidate relaxation produced no frame, single point as given"
    e_relaxed = float(last.crystal.energy_per_atom)
    e0 = float(first.crystal.energy_per_atom)
    if not np.isfinite(e_relaxed):
        return float("nan"), e0, False, "candidate relaxation gave a non-finite energy, single point as given"
    state = "converged" if last.converged else f"not converged in {RELAX_MAX_STEPS} steps"
    note = (f"candidate relaxed with the reference settings ({last.step} steps, {state}; "
            f"as given {e0:.4f} eV/atom, relaxed {e_relaxed:.4f} eV/atom)")
    return e_relaxed, e0, True, note
