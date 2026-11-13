"""Structure import/export shared by the REST routes and the WebSocket session.

Owned by the PM (shared contract between ``app/api/routes.py`` and
``app/api/ws_stream.py``). All structures entering the studio go through
:func:`atoms_from_cif` or :func:`atoms_from_payload`, so validation (ordering,
atom-count limit, wrapping into [0, 1)) lives in one place.

Phase A (IMPROVEMENT_SUGGESTIONS.md section 4.1-4.3)
----------------------------------------------------
* :func:`atoms_from_text` imports CIF / POSCAR (CONTCAR) / extxyz text, sniffing
  the format when ``fmt="auto"``.
* :func:`refine_atoms` snaps a structure to its detected space group
  (``cell="refined"``: same orientation, atom count and order) or converts it to
  spglib's standard conventional / primitive cell.
* :func:`atoms_to_cif` optionally writes a symmetrised CIF (space group +
  asymmetric unit); :func:`atoms_to_extxyz` writes energy / forces / stress.
* :func:`convert_cell` is the "as_is / conventional / primitive" switch used by
  the export route.
"""

from __future__ import annotations

import io
import re
import warnings
from typing import Optional, Sequence

import numpy as np
from ase import Atoms
from ase.io import write as ase_write

from app.schemas.crystal import CrystalStatePayload

# Interactive limit for any structure loaded into the studio (MACE single
# points stay well under the latency target below this). Phonon analysis has
# its own, tighter limit in the WebSocket layer.
MAX_ATOMS = 128


class StructureIOError(ValueError):
    """Raised for unparsable, disordered, empty or oversized structures.

    ``code`` is the ``ErrorCode`` value the API layers report.
    """

    def __init__(self, message: str, code: str = "INVALID_STRUCTURE") -> None:
        super().__init__(message)
        self.code = code


def _finalize(atoms: Atoms) -> Atoms:
    n = len(atoms)
    if n == 0:
        raise StructureIOError("structure contains no atoms")
    if n > MAX_ATOMS:
        raise StructureIOError(
            f"structure has {n} atoms; the studio limit is {MAX_ATOMS}", code="STRUCTURE_TOO_LARGE"
        )
    cell = np.asarray(atoms.cell.array, dtype=float)
    if not np.all(np.isfinite(cell)) or abs(np.linalg.det(cell)) < 1e-6:
        raise StructureIOError("structure has a degenerate or non-finite unit cell")
    scaled = np.mod(atoms.get_scaled_positions(wrap=False), 1.0)
    # Snap round-off on both sides of a lattice point (e.g. 2e-31 or 0.9999999999 after refinement) to 0.
    scaled[np.isclose(scaled, 1.0, rtol=0.0, atol=1e-10) | (np.abs(scaled) < 1e-10)] = 0.0
    atoms.set_scaled_positions(scaled)
    atoms.pbc = True
    atoms.calc = None
    return atoms


_FORMATS = ("auto", "cif", "poscar", "extxyz")
_CELL_CHOICES = ("as_is", "refined", "conventional", "primitive")
_EV_PER_A3_TO_GPA = 160.21766208


def structure_from_cif(text: str):
    """First structure of a CIF as a pymatgen ``Structure`` (partial occupancies kept).

    Used by ``atoms_from_cif`` and by ``core.ordering`` (Phase C1). Raises
    :class:`StructureIOError` (``INVALID_STRUCTURE``) for empty / unparsable input.
    """
    from pymatgen.io.cif import CifParser

    if not text or not text.strip():
        raise StructureIOError("empty CIF")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parser = CifParser(io.StringIO(text))
            structures = parser.parse_structures(primitive=False)
    except Exception as exc:  # pymatgen raises a zoo of exception types
        raise StructureIOError(f"could not parse CIF: {exc}") from None
    if not structures:
        raise StructureIOError("CIF contains no structure")
    return structures[0]


def atoms_from_cif(text: str) -> Atoms:
    """Parse the first structure of a CIF with pymatgen's ``CifParser``.

    Disordered (partially occupied) structures are rejected; the studio needs an
    ordered cell. Raises :class:`StructureIOError`.
    """
    from pymatgen.io.ase import AseAtomsAdaptor

    structure = structure_from_cif(text)
    if not structure.is_ordered:
        raise StructureIOError(
            "CIF describes a disordered structure (partial occupancies); load an ordered approximant",
            code="DISORDERED_STRUCTURE",
        )
    return _finalize(AseAtomsAdaptor.get_atoms(structure))


def atoms_from_payload(crystal: CrystalStatePayload) -> Atoms:
    """Rebuild ``Atoms`` from a ``CrystalStatePayload`` (lattice matrix + fractional coords)."""
    from ase.data import atomic_numbers

    if not crystal.atoms:
        raise StructureIOError("structure contains no atoms")
    sites = sorted(crystal.atoms, key=lambda s: s.index)
    for site in sites:
        if site.element not in atomic_numbers or atomic_numbers[site.element] == 0:
            raise StructureIOError(f"unknown element {site.element!r}", code="INVALID_ELEMENT")
        if len(site.frac_coords) != 3 or not np.all(np.isfinite(site.frac_coords)):
            raise StructureIOError(f"atom {site.index}: frac_coords must be 3 finite numbers")
    cell = np.asarray(crystal.lattice.matrix, dtype=float)
    if cell.shape != (3, 3) or not np.all(np.isfinite(cell)) or abs(np.linalg.det(cell)) < 1e-6:
        raise StructureIOError("lattice matrix must be a finite, non-singular 3x3 matrix")
    atoms = Atoms(
        symbols=[site.element for site in sites],
        scaled_positions=[site.frac_coords for site in sites],
        cell=cell,
        pbc=True,
    )
    return _finalize(atoms)


# --------------------------------------------------------------------------
# text import (CIF / POSCAR / extxyz)
# --------------------------------------------------------------------------
def sniff_format(text: str) -> str:
    """Best guess of ``text``'s format: "cif", "extxyz", "xyz" (no lattice) or "poscar".

    CIF: a line starting with ``data_`` or containing ``_cell_length_a``.
    (ext)xyz: the first non-blank line is a single integer; "extxyz" if the
    second line contains ``Lattice=``, otherwise plain "xyz". Anything else is
    treated as POSCAR / CONTCAR.
    """
    if re.search(r"^\s*data_", text, flags=re.MULTILINE) or "_cell_length_a" in text:
        return "cif"
    lines = text.lstrip("\r\n").splitlines()
    if lines and re.fullmatch(r"\s*\d+\s*", lines[0]):
        second = lines[1] if len(lines) > 1 else ""
        return "extxyz" if re.search(r"lattice\s*=", second, flags=re.IGNORECASE) else "xyz"
    return "poscar"


def _plain_atoms(atoms: Atoms) -> Atoms:
    """Numbers, cell, positions and pbc only (drops calculators, info, arrays, tags)."""
    return Atoms(
        numbers=np.asarray(atoms.numbers, dtype=int).copy(),
        positions=np.asarray(atoms.positions, dtype=float).copy(),
        cell=np.asarray(atoms.cell.array, dtype=float).copy(),
        pbc=np.asarray(atoms.pbc, dtype=bool).copy(),
    )


def _require_periodic(atoms: Atoms, what: str) -> None:
    if atoms.cell.rank != 3 or not np.all(atoms.pbc):
        raise StructureIOError(
            f"{what} has no 3-D lattice; the studio needs a periodic cell "
            '(add Lattice="..." and pbc="T T T" to the extxyz comment line)'
        )


def _read_poscar(text: str) -> Atoms:
    from ase.io import read as ase_read

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            atoms = ase_read(io.StringIO(text), format="vasp")
    except Exception as exc:  # noqa: BLE001 - ASE raises many types on malformed input
        raise StructureIOError(f"could not parse POSCAR: {type(exc).__name__}: {exc}") from None
    return _plain_atoms(atoms)


def _read_extxyz(text: str) -> Atoms:
    from ase.io import read as ase_read

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            atoms = ase_read(io.StringIO(text), index=-1, format="extxyz")  # last frame
    except Exception as exc:  # noqa: BLE001
        raise StructureIOError(f"could not parse extxyz: {type(exc).__name__}: {exc}") from None
    atoms = _plain_atoms(atoms)
    _require_periodic(atoms, "the xyz structure")
    return atoms


def atoms_from_text(text: str, fmt: str = "auto") -> Atoms:
    """Parse a structure from CIF, POSCAR / CONTCAR or extxyz text.

    ``fmt`` is one of "auto", "cif", "poscar", "extxyz"; "auto" uses
    :func:`sniff_format`. "cif" delegates to :func:`atoms_from_cif` (first data
    block, disordered structures rejected). Multi-frame extxyz: the last frame is
    taken. Plain xyz without a lattice is rejected (``INVALID_STRUCTURE``). The
    result always goes through ``_finalize`` (atom limit, wrapping, pbc). Raises
    :class:`StructureIOError` (code ``INVALID_PAYLOAD`` for an unknown ``fmt``).
    """
    if not isinstance(text, str) or not text.strip():
        raise StructureIOError("empty structure text")
    kind = (fmt or "auto").lower()
    if kind not in _FORMATS:
        raise StructureIOError(f"unknown structure format {fmt!r}; expected one of {', '.join(_FORMATS)}",
                               code="INVALID_PAYLOAD")
    if kind == "auto":
        kind = sniff_format(text)
        if kind == "xyz":
            raise StructureIOError(
                'xyz file without a lattice (non-periodic); use extxyz with a Lattice="..." comment line'
            )
    if kind == "cif":
        return atoms_from_cif(text)
    atoms = _read_extxyz(text) if kind == "extxyz" else _read_poscar(text)
    return _finalize(atoms)


# --------------------------------------------------------------------------
# symmetry refinement / standard cells
# --------------------------------------------------------------------------
def _dataset_or_raise(atoms: Atoms, symprec: float):
    from app.core.geometry_analysis import symmetry_dataset

    if not (np.isfinite(symprec) and symprec > 0):
        raise StructureIOError(f"symprec must be positive, got {symprec!r}", code="INVALID_PAYLOAD")
    ds = symmetry_dataset(atoms, symprec)
    if ds is None:
        raise StructureIOError(f"symmetry detection failed at symprec = {symprec:g} A")
    return ds


def _species_order(numbers: Sequence[int], reference: Sequence[int]) -> np.ndarray:
    """Stable permutation grouping ``numbers`` by first appearance of each species in ``reference``."""
    first: dict = {}
    for k, z in enumerate(reference):
        first.setdefault(int(z), k)
    keys = np.array([first.get(int(z), len(reference) + int(z)) for z in numbers])
    return np.argsort(keys, kind="stable")


def _standard_cell(atoms: Atoms, *, primitive: bool, symprec: float) -> Atoms:
    import spglib

    _dataset_or_raise(atoms, symprec)
    cell = (np.asarray(atoms.cell.array, dtype=float), atoms.get_scaled_positions(wrap=True),
            np.asarray(atoms.numbers, dtype=int))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = spglib.standardize_cell(cell, to_primitive=primitive, no_idealize=False, symprec=symprec)
    if res is None:
        raise StructureIOError(f"spglib could not standardise the cell at symprec = {symprec:g} A")
    lattice, positions, numbers = (np.asarray(x) for x in res)
    order = _species_order(numbers, atoms.numbers)
    out = Atoms(numbers=numbers[order].astype(int), scaled_positions=positions[order], cell=lattice, pbc=True)
    return _finalize(out)


#: Origin shifts are snapped to multiples of 1/_ORIGIN_GRID (covers every ITA origin choice).
_ORIGIN_GRID = 48


def _snap_origin(p: np.ndarray, std_lat: np.ndarray, tol: Optional[float]) -> np.ndarray:
    """Nearest grid origin shift; with ``tol`` only if it moves the origin by <= tol Angstrom."""
    snapped = np.round(p * _ORIGIN_GRID) / _ORIGIN_GRID
    if tol is None:
        return snapped
    d = snapped - p
    d -= np.round(d)
    return snapped if float(np.linalg.norm(d @ std_lat)) <= tol else p


def _refined_same_cell(atoms: Atoms, symprec: float, *, force_origin: bool = False) -> Atoms:
    ds = _dataset_or_raise(atoms, symprec)
    P = np.asarray(ds.transformation_matrix, dtype=float)
    std_lat = np.asarray(ds.std_lattice, dtype=float)        # rows, idealised + rotated
    # The origin is a free choice (any rigid translation keeps the symmetry). spglib's
    # origin_shift carries the noise of the input; snapping it to the 1/48 grid when that
    # moves the structure by <= symprec gives clean coordinates (0, 1/4, 1/2, ...) and clean
    # CIF symmetry operations. ``force_origin`` always snaps (a rigid shift of the structure).
    p = _snap_origin(np.asarray(ds.origin_shift, dtype=float), std_lat, None if force_origin else symprec)
    R = np.asarray(ds.std_rotation_matrix, dtype=float)
    std_pos = np.asarray(ds.std_positions, dtype=float)
    std_map = np.asarray(ds.std_mapping_to_primitive, dtype=int)
    map_prim = np.asarray(ds.mapping_to_primitive, dtype=int)

    # spglib: (a_s b_s c_s) = (a b c) P^-1 and (a'_s b'_s c'_s) = R (a_s b_s c_s), lattice
    # vectors as columns. The idealised input lattice in the input orientation is therefore
    # A = R^T A'_s P, i.e. as rows L = P^T L'_s R.
    new_cell = P.T @ std_lat @ R
    frac_in = atoms.get_scaled_positions(wrap=True)
    xs = frac_in @ P.T + p                                    # input atoms in the std basis
    P_inv = np.linalg.inv(P)
    new_frac = np.empty_like(frac_in)
    for i in range(len(atoms)):
        cand = np.flatnonzero(std_map == map_prim[i])
        if len(cand) == 0:
            raise StructureIOError("symmetry refinement could not map an atom onto the standard cell")
        d = std_pos[cand] - xs[i]
        d -= np.round(d)
        j = int(np.argmin(np.linalg.norm(d @ std_lat, axis=1)))
        new_frac[i] = (xs[i] + d[j] - p) @ P_inv.T
    out = Atoms(numbers=np.asarray(atoms.numbers, dtype=int).copy(), scaled_positions=new_frac % 1.0,
                cell=new_cell, pbc=True)
    return _finalize(out)


def refine_atoms(atoms: Atoms, *, cell: str = "refined", symprec: float = 1e-2) -> Atoms:
    """Snap ``atoms`` to its detected space group (``symprec`` in Angstrom, spglib).

    ``cell``:

    * ``"refined"`` - the given cell, symmetrised. The lattice is replaced by
      spglib's idealised standard lattice mapped back into the *input* basis and
      orientation (``L = P^T L_std R`` with the dataset's transformation matrix
      P and std_rotation_matrix R), and every atom is moved onto its idealised
      standard-cell position (matched through ``mapping_to_primitive``, nearest
      lattice image), expressed back in input fractional coordinates. Atom count,
      order and species are preserved exactly; each atom moves by the
      symmetry-breaking noise only (<~ symprec). Chosen over pymatgen's
      ``get_refined_structure``, which returns the conventional cell in a new
      orientation and atom order.
    * ``"conventional"`` / ``"primitive"`` - spglib ``standardize_cell``
      (idealised, standard orientation), atoms grouped by species in the input's
      order of first appearance. The atom count changes in general (8-atom
      diamond Si <-> 2-atom primitive).

    Raises :class:`StructureIOError` if symmetry detection fails. The input is
    not mutated; the result passes ``_finalize``.
    """
    if cell not in ("refined", "conventional", "primitive"):
        raise StructureIOError(f"unknown cell choice {cell!r}", code="INVALID_PAYLOAD")
    work = _finalize(_plain_atoms(atoms))
    if cell == "refined":
        return _refined_same_cell(work, symprec)
    return _standard_cell(work, primitive=(cell == "primitive"), symprec=symprec)


def convert_cell(atoms: Atoms, cell: str = "as_is", *, symprec: float = 1e-2) -> Atoms:
    """Export helper: "as_is" (a finalized copy), "refined", "conventional" or "primitive"."""
    if cell == "as_is":
        return _finalize(_plain_atoms(atoms))
    if cell not in _CELL_CHOICES:
        raise StructureIOError(f"unknown cell choice {cell!r}", code="INVALID_PAYLOAD")
    return refine_atoms(atoms, cell=cell, symprec=symprec)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------
def atoms_to_cif(atoms: Atoms, *, symprec: Optional[float] = None) -> str:
    """CIF via pymatgen ``CifWriter``; round-trips through :func:`atoms_from_cif`.

    ``symprec=None``: symmetry-agnostic P1 CIF of the cell as given. Otherwise
    the structure is first snapped with ``refine_atoms(cell="refined")`` (same
    lattice and atoms, noise removed) and written with
    ``CifWriter(structure, symprec=symprec, refine_struct=False)``: the detected
    space group, its operations in the basis of the given cell and the
    asymmetric unit. Re-expanding it gives the same cell and atom count (checked by
    parsing the text back). If that check fails (origin far from any special
    position), the structure is first translated rigidly onto the standard origin;
    if symmetry detection or the check still fails, the P1 CIF is written.
    """
    from pymatgen.io.ase import AseAtomsAdaptor
    from pymatgen.io.cif import CifWriter

    if symprec is None:
        return str(CifWriter(AseAtomsAdaptor.get_structure(_plain_atoms(atoms))))
    try:
        refined = refine_atoms(atoms, cell="refined", symprec=float(symprec))
    except StructureIOError as exc:
        if exc.code != "INVALID_STRUCTURE":
            raise
        return str(CifWriter(AseAtomsAdaptor.get_structure(_plain_atoms(atoms))))
    def write(at: Atoms) -> str:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return str(CifWriter(AseAtomsAdaptor.get_structure(at), symprec=float(symprec), refine_struct=False))

    def round_trips(text: str) -> bool:
        try:
            back = atoms_from_cif(text)
        except StructureIOError:
            return False
        if len(back) != len(refined) or sorted(back.numbers) != sorted(refined.numbers):
            return False
        # Symmetry operations are written as fractions (e.g. 13/690 for an arbitrary origin);
        # require the re-expanded structure to keep the space group at a tight tolerance.
        return get_spacegroup(back, symprec=2e-6)[1] == number

    from app.core.geometry_analysis import get_spacegroup

    number = get_spacegroup(refined, symprec=2e-6)[1]

    text = write(refined)
    if round_trips(text):
        return text
    # An origin far from a special position gives symmetry operations with irrational
    # translations that a reader cannot re-expand exactly: move the structure rigidly onto
    # the standard origin and retry; as a last resort write P1.
    refined = _refined_same_cell(refined, float(symprec), force_origin=True)
    text = write(refined)
    if round_trips(text):
        return text
    return str(CifWriter(AseAtomsAdaptor.get_structure(_plain_atoms(atoms))))


def atoms_to_extxyz(
    atoms: Atoms,
    *,
    energy: Optional[float] = None,
    forces: Optional[np.ndarray] = None,
    stress_voigt_gpa: Optional[Sequence[float]] = None,
) -> str:
    """Extended XYZ (ASE writer) with optional total energy (eV), per-atom forces (eV/A)
    and stress. ``stress_voigt_gpa`` is the payload's Voigt 6-vector in GPa (ASE order
    and sign); it is written in ASE's convention, eV/A^3, as the ``stress`` key of the
    comment line (ASE reads it back as ``atoms.get_stress()``)."""
    from ase.calculators.singlepoint import SinglePointCalculator

    work = _plain_atoms(atoms)
    results = {}
    if energy is not None:
        results["energy"] = float(energy)
        results["free_energy"] = float(energy)
    if forces is not None:
        results["forces"] = np.asarray(forces, dtype=float).reshape(len(work), 3)
    if stress_voigt_gpa is not None:
        results["stress"] = np.asarray(stress_voigt_gpa, dtype=float).reshape(6) / _EV_PER_A3_TO_GPA
    if results:
        work.calc = SinglePointCalculator(work, **results)
    buf = io.StringIO()
    ase_write(buf, work, format="extxyz")
    return buf.getvalue()


def atoms_to_poscar(atoms: Atoms) -> str:
    """VASP 5 POSCAR (direct coordinates, element line included)."""
    buf = io.StringIO()
    ase_write(buf, atoms, format="vasp", direct=True, sort=False)
    return buf.getvalue()
