"""Materials Project structure search / download (IMPROVEMENT_SUGGESTIONS.md §4.3).

``GET /api/mp/search?q=`` and ``GET /api/mp/structure/{material_id}`` use an ``MPClient``:

* ``search(query) -> List[MPEntry]``: ``query`` is a material id ("mp-5229"), a chemical
  system ("Li-Fe-O", any order / case of the separators' elements) or a formula ("SrTiO3",
  "Fe2O3"). Non-deprecated MP materials (GNoME entries excluded), sorted by MP's DFT
  ``energy_above_hull`` (unknown last, then by id), at most ``MAX_RESULTS`` (50).
* ``get_structure(material_id) -> (MPEntry, Structure)``.

The CIF served for a structure (:func:`structure_cif`) is the **conventional standard cell**
(pymatgen ``SpacegroupAnalyzer(symprec=0.01).get_conventional_standard_structure()``, e.g. the
8-atom Si cube, 5-atom SrTiO3) when it has at most ``MAX_CONVENTIONAL_ATOMS`` (64, the phonon
limit) sites, otherwise MP's own (primitive) structure; written as a symmetrised CIF.

Errors: :class:`MPNoApiKeyError` (no / malformed key -> 503), :class:`MPFetchError` (network,
HTTP, unexpected documents -> 502), :class:`MPNotFoundError` (unknown id -> 404),
:class:`MPQueryError` (unparsable query -> 422).

MP's front end answers 403 to requests without a User-Agent, and ``include_user_agent=True``
would send platform / Python details; we send a generic library-only agent ("mp-api/<version>").
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Protocol, Tuple

from app.schemas.api import MPEntry

MAX_RESULTS = 50
MAX_CONVENTIONAL_ATOMS = 64
MP_SEARCH_FIELDS = ["material_id", "formula_pretty", "symmetry", "nsites", "energy_above_hull",
                    "is_stable", "theoretical", "band_gap", "is_metal"]
# Classic ids are numeric ("mp-5229"); newer Materials Project entries use letters ("mp-aaaieiuj").
_ID_RE = re.compile(r"^(mp|mvc)-[a-z0-9]+$", re.IGNORECASE)


class MPError(RuntimeError):
    pass


class MPNoApiKeyError(MPError):
    pass


class MPFetchError(MPError):
    pass


class MPNotFoundError(MPError):
    pass


class MPQueryError(MPError, ValueError):
    pass


class MPClient(Protocol):
    def search(self, query: str) -> List[MPEntry]: ...

    def get_structure(self, material_id: str) -> Tuple[MPEntry, Any]: ...


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def mp_user_agent() -> str:
    """Generic User-Agent: the client library and version only (no platform, no app name)."""
    try:
        from importlib.metadata import version

        return f"mp-api/{version('mp-api')}"
    except Exception:  # noqa: BLE001
        return "mp-api"


def check_api_key(api_key: Optional[str]) -> str:
    key = (api_key or "").strip()
    if not key:
        raise MPNoApiKeyError("MP_API_KEY is not configured; Materials Project import is unavailable")
    if len(key) != 32:
        raise MPNoApiKeyError("MP_API_KEY is malformed (Materials Project keys are 32 characters)")
    return key


def normalize_material_id(material_id: str) -> str:
    mid = str(material_id or "").strip()
    if not _ID_RE.match(mid):
        raise MPQueryError(f"not a Materials Project id: {material_id!r} (expected e.g. 'mp-5229')")
    return mid.lower()


def parse_query(query: str) -> Tuple[str, Any]:
    """("material_ids" | "chemsys" | "formula", value) for an MP summary search."""
    from pymatgen.core import Composition, Element

    q = str(query or "").strip()
    if not q:
        raise MPQueryError("empty query")
    if len(q) > 100:
        raise MPQueryError("query too long")
    if _ID_RE.match(q):
        return "material_ids", [q.lower()]
    if "-" in q:
        parts = [p.strip() for p in q.split("-")]
        if any(not p for p in parts):
            raise MPQueryError(f"cannot parse chemical system {q!r}")
        try:
            els = sorted({Element(p.capitalize()).symbol for p in parts})
        except Exception as exc:  # noqa: BLE001
            raise MPQueryError(f"cannot parse chemical system {q!r}: {exc}") from exc
        return "chemsys", "-".join(els)
    if not re.fullmatch(r"[A-Za-z0-9().]+", q):
        raise MPQueryError(f"cannot parse formula {q!r}")
    try:
        comp = Composition(q)
        if comp.num_atoms <= 0 or any(not getattr(el, "Z", 0) for el in comp.elements):
            raise ValueError("no elements")
    except Exception as exc:  # noqa: BLE001
        raise MPQueryError(f"cannot parse formula {q!r}: {exc}") from exc
    return "formula", q


def _get(doc: Any, name: str) -> Any:
    return doc.get(name) if isinstance(doc, dict) else getattr(doc, name, None)


def entry_from_doc(doc: Any) -> MPEntry:
    sym = _get(doc, "symmetry")
    symbol = _get(sym, "symbol") if sym is not None else None
    number = _get(sym, "number") if sym is not None else None
    ehull = _get(doc, "energy_above_hull")
    stable = _get(doc, "is_stable")
    theo = _get(doc, "theoretical")
    gap = _float_or_none(_get(doc, "band_gap"))
    metal = _get(doc, "is_metal")
    return MPEntry(
        material_id=str(_get(doc, "material_id")),
        formula=str(_get(doc, "formula_pretty") or ""),
        spacegroup_symbol=str(symbol or ""),
        spacegroup_number=int(number or 0),
        nsites=int(_get(doc, "nsites") or 0),
        energy_above_hull=float(ehull) if ehull is not None else None,
        is_stable=bool(stable) if stable is not None else None,
        theoretical=bool(theo) if theo is not None else None,
        band_gap=gap,
        is_metal=bool(metal) if metal is not None else None,
    )


def _float_or_none(x: Any) -> Optional[float]:
    """Tolerant read of an optional numeric field (missing / unparsable / non-finite -> None)."""
    import math

    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def sort_entries(entries: List[MPEntry]) -> List[MPEntry]:
    def key(e: MPEntry):
        num = re.sub(r"\D", "", e.material_id)
        return (e.energy_above_hull is None, e.energy_above_hull or 0.0, int(num) if num else 0, e.material_id)

    return sorted(entries, key=key)[:MAX_RESULTS]


def structure_cif(structure: Any) -> str:
    """Symmetrised CIF of the conventional standard cell (see module docstring)."""
    import numpy as np
    from ase import Atoms
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    from app.core.structure_io import atoms_to_cif

    if not structure.is_ordered:
        raise MPFetchError("the Materials Project structure is disordered")
    chosen = structure
    try:
        conv = SpacegroupAnalyzer(structure, symprec=0.01).get_conventional_standard_structure()
        if len(conv) <= MAX_CONVENTIONAL_ATOMS:
            chosen = conv
    except Exception:  # noqa: BLE001 - keep MP's cell
        chosen = structure
    atoms = Atoms(numbers=[site.specie.Z for site in chosen], cell=np.array(chosen.lattice.matrix, dtype=float),
                  positions=np.array(chosen.cart_coords, dtype=float), pbc=True)
    return atoms_to_cif(atoms, symprec=0.01)


# --------------------------------------------------------------------------
# default client (mp_api)
# --------------------------------------------------------------------------
class MaterialsProjectClient:
    """``MPClient`` backed by ``mp_api.client.MPRester`` (one session per call)."""

    def __init__(self, api_key: Optional[str]) -> None:
        self.api_key = check_api_key(api_key)

    def _summary_search(self, **kwargs) -> List[Any]:
        try:
            from mp_api.client import MPRester
        except Exception as exc:  # noqa: BLE001
            raise MPFetchError(f"mp-api client is not importable: {exc}") from exc
        try:
            with MPRester(api_key=self.api_key, mute_progress_bars=True, include_user_agent=False) as mpr:
                mpr.session.headers["user-agent"] = mp_user_agent()
                return list(mpr.materials.summary.search(deprecated=False, include_gnome=False, **kwargs) or [])
        except MPError:
            raise
        except Exception as exc:  # noqa: BLE001 - MPRestError, requests errors, validation errors
            text = str(exc)
            hint = (" (HTTP 401/403: the API key was rejected or the request was blocked)"
                    if re.search(r"\b(401|403)\b", text) else "")
            raise MPFetchError(f"Materials Project query failed{hint}: {type(exc).__name__}: {text[:300]}") from exc

    def search(self, query: str) -> List[MPEntry]:
        field, value = parse_query(query)
        docs = self._summary_search(**{field: value}, fields=list(MP_SEARCH_FIELDS))
        try:
            return sort_entries([entry_from_doc(d) for d in docs if _get(d, "material_id") is not None])
        except Exception as exc:  # noqa: BLE001
            raise MPFetchError(f"unexpected Materials Project document: {exc}") from exc

    def get_structure(self, material_id: str) -> Tuple[MPEntry, Any]:
        from pymatgen.core import Structure

        mid = normalize_material_id(material_id)
        docs = self._summary_search(material_ids=[mid], fields=list(MP_SEARCH_FIELDS) + ["structure"])
        docs = [d for d in docs if str(_get(d, "material_id")).lower() == mid]
        if not docs:
            raise MPNotFoundError(f"Materials Project has no (non-deprecated) material {mid}")
        doc = docs[0]
        structure = _get(doc, "structure")
        if structure is None:
            raise MPFetchError(f"Materials Project returned no structure for {mid}")
        if isinstance(structure, dict):
            structure = Structure.from_dict(structure)
        return entry_from_doc(doc), structure
