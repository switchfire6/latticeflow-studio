"""Pure geometry analysis on ``ase.Atoms`` (PROJECT_SPEC.md §4.1, §4.5, §5.1).

Lattice parameters, space group, bond graph and radial distribution function.
No force field and no torch import; every function is read-only on its input.

Periodic-image convention (shared by bonds and RDF)
---------------------------------------------------
For a pair (i, j) and integer image n = (n_a, n_b, n_c) the bond vector is

    v = r_j + L^T n - r_i          (r = atoms.positions, rows of L = atoms.cell)

computed from the positions *exactly as given*. If atoms lie outside the cell
the reported offsets are still correct for those raw positions; the caller
normally passes wrapped atoms, in which case they are the offsets the frontend
should use with the wrapped Cartesian coordinates. Re-wrapping or translating
the atoms changes offsets but never the set of bond vectors.

Fast bonding rule (``find_bonds(mode="fast")``)
-----------------------------------------------
Work in normalised distance x = d / (R_i + R_j), with R the covalent radii in
``ase.data.covalent_radii`` (floored at 0.3 Å).

1. Candidates: every (i, j, n) with D_MIN <= d and x <= X_MAX (1.25).
2. Cation-cation filter, with Pauling electronegativity chi (all neighbour
   tests below range over the step-1 candidates):
   * atom k is "anionic" if some candidate i has chi_k - chi_i >= DELTA_CHI_IONIC
     (0.8);
   * atom i is "cationic" if some candidate k has chi_k - chi_i >= 0.8, or some
     candidate k is anionic with chi_k - chi_i >= DELTA_CHI_CENTRE (0.2). The
     second clause makes the centre of a covalent polyanion (P in PS4, N in
     NO3, C in CO3, Si in SiO4, S in SO4) a cation relative to its ligands.
   A candidate between two cationic atoms is discarded unless it is a hard
   clash (x < X_CLASH = 0.6). This removes Sr-Ti / Ti-Ti / Na-Na / Li-P
   contacts in ionic solids while leaving metals, intermetallics, covalent and
   Zintl-type solids untouched (without a strongly electropositive partner no
   atom is anionic, so only the 0.8 clause can apply).
3. First coordination shell, per atom: sort the atom's surviving candidates by
   x and cut at the first gap x_{k+1} / x_k > SHELL_GAP (1.12) with
   x_k >= X_FLOOR (0.75) (clash contacts never terminate a shell). At most
   MAX_PROPOSALS (12) shortest are kept, backing off so a degenerate shell is
   never split.
4. A bond exists if either end selected it (union), deduplicated so every
   physical bond appears once; finally the degree of every atom is capped at
   MAX_DEGREE (16), keeping the shortest bonds (by x) first.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

import ase
import numpy as np
from ase.data import covalent_radii

from app.schemas.crystal import BondEdge, LatticeParams

__all__ = [
    "lattice_params",
    "get_spacegroup",
    "SymmetryInfo",
    "symmetry_info",
    "symmetry_dataset",
    "crystal_system_of",
    "density_g_cm3",
    "formula_units",
    "SYMPREC",
    "find_bonds",
    "compute_rdf",
    "RDF_R",
]

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_NAN = float("nan")
# Pauling electronegativity for Z = 1..103 (pymatgen Element.X); NaN = unknown.
_PAULING = np.array(
    [_NAN,  # Z = 0 (dummy)
     2.2, _NAN, 0.98, 1.57, 2.04, 2.55, 3.04, 3.44, 3.98, _NAN, 0.93, 1.31, 1.61,
     1.9, 2.19, 2.58, 3.16, _NAN, 0.82, 1.0, 1.36, 1.54, 1.63, 1.66, 1.55, 1.83,
     1.88, 1.91, 1.9, 1.65, 1.81, 2.01, 2.18, 2.55, 2.96, 3.0, 0.82, 0.95, 1.22,
     1.33, 1.6, 2.16, 1.9, 2.2, 2.28, 2.2, 1.93, 1.69, 1.78, 1.96, 2.05, 2.1,
     2.66, 2.6, 0.79, 0.89, 1.1, 1.12, 1.13, 1.14, 1.13, 1.17, 1.2, 1.2, 1.1,
     1.22, 1.23, 1.24, 1.25, 1.1, 1.27, 1.3, 1.5, 2.36, 1.9, 2.2, 2.2, 2.28,
     2.54, 2.0, 1.62, 2.33, 2.02, 2.0, 2.2, 2.2, 0.7, 0.9, 1.1, 1.3, 1.5, 1.38,
     1.36, 1.28, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3],
    dtype=float,
)

# Fast bond-finder parameters (see module docstring).
X_MAX = 1.25
X_CLASH = 0.6
X_FLOOR = 0.75
SHELL_GAP = 1.12
DELTA_CHI_IONIC = 0.8
DELTA_CHI_CENTRE = 0.2
MAX_PROPOSALS = 12
MAX_DEGREE = 16
D_MIN = 0.05            # Å; closer contacts (coincident atoms) are not drawn
R_FLOOR = 0.3           # Å; floor for covalent radius (dummy / unknown species)
MAX_IMAGE_RANGE = 4     # per-axis cap on |n - n0| for the bond search

# RDF parameters (§4.5).
RDF_R_MIN = 0.5
RDF_R_MAX = 8.0
RDF_DR = 0.05
RDF_SIGMA = 0.08
RDF_CUTOFF = RDF_R_MAX + 4.0 * RDF_SIGMA
RDF_R = np.round(RDF_R_MIN + RDF_DR * np.arange(151), 10)
_RDF_H = 0.0025          # fine deposition grid spacing (Å)
_RDF_MAX_IMAGE_RANGE = 12


def _radius(numbers: np.ndarray) -> np.ndarray:
    z = np.clip(numbers, 0, len(covalent_radii) - 1)
    return np.maximum(covalent_radii[z], R_FLOOR)


def _chi(numbers: np.ndarray) -> np.ndarray:
    z = np.asarray(numbers)
    out = np.full(z.shape, _NAN)
    ok = (z >= 0) & (z < len(_PAULING))
    out[ok] = _PAULING[z[ok]]
    return out


# ---------------------------------------------------------------------------
# Lattice
# ---------------------------------------------------------------------------

def _angle(u: np.ndarray, v: np.ndarray) -> float:
    nu, nv = float(np.linalg.norm(u)), float(np.linalg.norm(v))
    if nu < 1e-12 or nv < 1e-12 or not math.isfinite(nu * nv):
        return 90.0
    c = float(np.clip(np.dot(u, v) / (nu * nv), -1.0, 1.0))
    return math.degrees(math.acos(c))


def lattice_params(atoms: ase.Atoms) -> LatticeParams:
    """a, b, c (Å), alpha/beta/gamma (deg), matrix = cell rows, volume (Å^3)."""
    m = np.array(atoms.cell.array, dtype=float)
    a_v, b_v, c_v = m
    return LatticeParams(
        a=float(np.linalg.norm(a_v)),
        b=float(np.linalg.norm(b_v)),
        c=float(np.linalg.norm(c_v)),
        alpha=_angle(b_v, c_v),
        beta=_angle(a_v, c_v),
        gamma=_angle(a_v, b_v),
        matrix=m.tolist(),
        volume=float(abs(np.linalg.det(m))),
    )


# ---------------------------------------------------------------------------
# Space group
# ---------------------------------------------------------------------------

#: spglib tolerance (Å) used for every payload's space group / Wyckoff labels.
SYMPREC = 1e-2

_AMU_PER_A3_TO_G_CM3 = 1.66053906660   # 1 amu / Å^3 in g / cm^3


@dataclass(frozen=True)
class SymmetryInfo:
    """Result of one spglib dataset call (see :func:`symmetry_info`)."""

    symbol: str                        # international short symbol, "P1" on failure
    number: int                        # 1..230, 1 on failure
    crystal_system: Optional[str]      # None if detection failed
    wyckoffs: Optional[List[str]]      # per atom "<multiplicity><letter>" (ITA multiplicity), None on failure
    symprec: float
    ok: bool                           # True if spglib produced a dataset


def crystal_system_of(number: int) -> Optional[str]:
    """Crystal system of a space-group number (ITA ranges); None outside 1..230."""
    n = int(number)
    if not 1 <= n <= 230:
        return None
    for top, name in ((2, "triclinic"), (15, "monoclinic"), (74, "orthorhombic"), (142, "tetragonal"),
                      (167, "trigonal"), (194, "hexagonal"), (230, "cubic")):
        if n <= top:
            return name
    return None  # pragma: no cover


def _spglib_cell(atoms: ase.Atoms):
    """(cell, wrapped frac, numbers) or None for empty / non-finite / singular input."""
    cell = np.array(atoms.cell.array, dtype=float)
    pos = np.array(atoms.positions, dtype=float)
    numbers = np.array(atoms.numbers, dtype=int)
    if len(numbers) == 0 or not (np.isfinite(cell).all() and np.isfinite(pos).all()):
        return None
    if abs(np.linalg.det(cell)) < 1e-8:
        return None
    frac = np.linalg.solve(cell.T, pos.T).T % 1.0
    return cell, frac, numbers


def symmetry_dataset(atoms: ase.Atoms, symprec: float = SYMPREC):
    """Raw ``spglib.SpglibDataset`` for ``atoms`` (positions wrapped), or None on any failure."""
    try:
        import spglib

        spg_cell = _spglib_cell(atoms)
        if spg_cell is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = spglib.get_symmetry_dataset(spg_cell, symprec=symprec)
        if ds is None or not 1 <= int(ds.number) <= 230:
            return None
        return ds
    except Exception:  # spglib raises / returns None on degenerate input
        return None


def _wyckoff_labels(ds, n_atoms: int) -> Optional[List[str]]:
    """Per-atom "<multiplicity><letter>": ITA (conventional-cell) multiplicity of each orbit.

    multiplicity = (atoms of the orbit in the given cell) * V_std / V_given, with
    V_std / V_given = 1 / det(P) for spglib's transformation matrix P (so a
    supercell or a primitive setting reports the same label as the standard cell).
    """
    try:
        letters = list(ds.wyckoffs)
        orbits = np.asarray(ds.crystallographic_orbits, dtype=int)
        if len(letters) != n_atoms or len(orbits) != n_atoms:
            return None
        det_p = float(np.linalg.det(np.asarray(ds.transformation_matrix, dtype=float)))
        if not math.isfinite(det_p) or abs(det_p) < 1e-8:
            return None
        counts = np.bincount(orbits, minlength=n_atoms)
        out = []
        for k in range(n_atoms):
            m = counts[orbits[k]] / abs(det_p)
            mi = int(round(m))
            if mi < 1 or abs(m - mi) > 1e-6:
                return None
            out.append(f"{mi}{letters[k]}")
        return out
    except Exception:  # noqa: BLE001
        return None


def symmetry_info(atoms: ase.Atoms, symprec: float = SYMPREC) -> SymmetryInfo:
    """Space group, crystal system and Wyckoff labels from ONE spglib dataset call.

    On failure: ("P1", 1), crystal_system None, wyckoffs None, ok False.
    """
    ds = symmetry_dataset(atoms, symprec)
    if ds is None:
        return SymmetryInfo("P1", 1, None, None, float(symprec), False)
    number = int(ds.number)
    symbol = str(ds.international).strip() or "P1"
    return SymmetryInfo(symbol, number, crystal_system_of(number), _wyckoff_labels(ds, len(atoms)),
                        float(symprec), True)


def get_spacegroup(atoms: ase.Atoms, symprec: float = SYMPREC) -> tuple[str, int]:
    """spglib (international symbol, number); ("P1", 1) on any failure."""
    info = symmetry_info(atoms, symprec)
    return (info.symbol, info.number)


def density_g_cm3(atoms: ase.Atoms) -> Optional[float]:
    """Mass density (g/cm^3) from ASE's standard atomic masses; None for a singular cell."""
    vol = abs(float(np.linalg.det(np.asarray(atoms.cell.array, dtype=float)))) if atoms.cell.rank == 3 else 0.0
    if not (vol > 1e-8 and math.isfinite(vol)) or len(atoms) == 0:
        return None
    return float(np.sum(atoms.get_masses())) / vol * _AMU_PER_A3_TO_G_CM3


def formula_units(atoms: ase.Atoms) -> Optional[int]:
    """Z = number of atoms / atoms in the reduced formula (gcd of the element counts)."""
    if len(atoms) == 0:
        return None
    counts = np.unique(np.asarray(atoms.numbers, dtype=int), return_counts=True)[1]
    return int(np.gcd.reduce(counts))


# ---------------------------------------------------------------------------
# Periodic pair search
# ---------------------------------------------------------------------------

def _pair_vectors(atoms: ase.Atoms, rcut: float, max_range: int):
    """All ordered (i, j, n) with |r_j + L^T n - r_i| <= rcut.

    Returns (I, J, N, d) or None for unusable input. Axes that are not periodic
    (or a singular cell) contribute no images.
    """
    pos = np.array(atoms.positions, dtype=float)
    cell = np.array(atoms.cell.array, dtype=float)
    nat = len(pos)
    if nat == 0 or not (np.isfinite(pos).all() and np.isfinite(cell).all()):
        return None
    pbc = np.array(atoms.pbc, dtype=bool)
    vol = abs(np.linalg.det(cell))
    if vol < 1e-8:
        pbc = np.zeros(3, dtype=bool)

    # Per-pair base image n0 = -round(s_j - s_i) along periodic axes.
    n0 = np.zeros((nat, nat, 3))
    if pbc.any():
        frac = np.linalg.solve(cell.T, pos.T).T
        ds = frac[None, :, :] - frac[:, None, :]
        n0[..., pbc] = -np.round(ds[..., pbc])
    base = (pos[None, :, :] - pos[:, None, :]) + n0 @ cell   # (N, N, 3)

    ranges = []
    for k in range(3):
        if not pbc[k]:
            ranges.append(np.arange(1))
            continue
        cr = np.linalg.norm(np.cross(cell[(k + 1) % 3], cell[(k + 2) % 3]))
        h = vol / cr if cr > 0 else 0.0
        mk = max_range if h <= 0 else min(max_range, int(math.ceil(rcut / h + 0.5)))
        ranges.append(np.arange(-mk, mk + 1))
    m = np.stack(np.meshgrid(*ranges, indexing="ij"), axis=-1).reshape(-1, 3)
    shift = m.astype(float) @ cell                            # (K, 3)

    # Screen |base + shift|^2 with one matmul (no (N, N, K, 3) temporary), then
    # recompute the hits exactly from vectors.
    b = base.reshape(-1, 3)
    d2 = (np.einsum("px,px->p", b, b)[:, None] + 2.0 * (b @ shift.T)
          + np.einsum("kx,kx->k", shift, shift)[None, :])
    pp, kk = np.nonzero(d2 <= rcut * rcut + 1e-6 * (1.0 + rcut * rcut))
    vec = b[pp] + shift[kk]
    d = np.sqrt(np.einsum("px,px->p", vec, vec))
    ok = d <= rcut
    pp, kk, d = pp[ok], kk[ok], d[ok]
    ii, jj = np.divmod(pp, nat)
    n = (n0[ii, jj] + m[kk]).astype(np.int64)
    return ii, jj, n, d


# ---------------------------------------------------------------------------
# Bonds
# ---------------------------------------------------------------------------

def _canonical(i: np.ndarray, j: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Map ordered entries to a canonical (a, b, na, nb, nc) key per physical bond."""
    flip = i > j
    same = i == j
    # For self-image bonds keep the lexicographically positive offset.
    if same.any():
        ns = n[same]
        first_nz = np.argmax(ns != 0, axis=1)
        lead = ns[np.arange(len(ns)), first_nz]
        flip_same = np.zeros_like(flip)
        flip_same[np.nonzero(same)[0]] = lead < 0
        flip = flip | flip_same
    a = np.where(flip, j, i)
    b = np.where(flip, i, j)
    nn = np.where(flip[:, None], -n, n)
    return np.column_stack([a, b, nn])


def _bonds_fast(atoms: ase.Atoms) -> list[BondEdge]:
    numbers = np.array(atoms.numbers, dtype=int)
    if len(numbers) == 0:
        return []
    rad = _radius(numbers)
    chi = _chi(numbers)
    rcut = X_MAX * 2.0 * float(rad.max())
    found = _pair_vectors(atoms, rcut, MAX_IMAGE_RANGE)
    if found is None:
        return []
    I, J, N, d = found
    rsum = rad[I] + rad[J]
    x = d / rsum
    keep = (d >= D_MIN) & (x <= X_MAX)
    I, J, N, d, x, rsum = I[keep], J[keep], N[keep], d[keep], x[keep], rsum[keep]
    if len(I) == 0:
        return []

    # 2. cation-cation filter
    with np.errstate(invalid="ignore"):
        dchi = chi[J] - chi[I]                       # NaN -> comparisons False
        ionic_partner = dchi >= DELTA_CHI_IONIC
    # anions: atoms with a much more electropositive candidate neighbour
    anionic = np.zeros(len(numbers), dtype=bool)
    anionic[np.unique(J[ionic_partner])] = True
    # cations: ionic partner, or the electropositive centre of an anion
    # (P in PS4, N in NO3, ...): a candidate anion more electronegative than it
    with np.errstate(invalid="ignore"):
        centre_of_anion = anionic[J] & (dchi >= DELTA_CHI_CENTRE)
    cationic = np.zeros(len(numbers), dtype=bool)
    cationic[np.unique(I[ionic_partner | centre_of_anion])] = True
    keep = ~(cationic[I] & cationic[J] & (x >= X_CLASH))
    I, J, N, d, x, rsum = I[keep], J[keep], N[keep], d[keep], x[keep], rsum[keep]
    if len(I) == 0:
        return []

    # 3. per-atom first shell (entries sorted by atom, then x)
    order = np.lexsort((x, I))
    I, J, N, d, x, rsum = I[order], J[order], N[order], d[order], x[order], rsum[order]
    starts = np.searchsorted(I, np.arange(len(numbers) + 1))
    selected = np.zeros(len(I), dtype=bool)
    for a in range(len(numbers)):
        s, e = starts[a], starts[a + 1]
        if s == e:
            continue
        xs = x[s:e]
        cut = len(xs)
        if cut > 1:
            gaps = (xs[1:] > SHELL_GAP * xs[:-1]) & (xs[:-1] >= X_FLOOR)
            hit = np.flatnonzero(gaps)
            if len(hit):
                cut = int(hit[0]) + 1
        if cut > MAX_PROPOSALS:
            c = MAX_PROPOSALS
            while c > 0 and xs[c] - xs[c - 1] < 1e-4:
                c -= 1
            cut = c if c > 0 else MAX_PROPOSALS
        selected[s:s + cut] = True

    # 4. union over both ends, dedupe, cap degree
    I, J, N, d, x, rsum = (arr[selected] for arr in (I, J, N, d, x, rsum))
    keys = _canonical(I, J, N)
    _, first = np.unique(keys, axis=0, return_index=True)
    keys, d, x, rsum = keys[first], d[first], x[first], rsum[first]

    a_idx, b_idx = keys[:, 0], keys[:, 1]
    deg = np.bincount(a_idx, minlength=len(numbers)) + np.bincount(b_idx, minlength=len(numbers))
    if deg.max() > MAX_DEGREE:
        accept = np.zeros(len(keys), dtype=bool)
        deg = np.zeros(len(numbers), dtype=int)
        for t in np.argsort(x, kind="stable"):
            a, b = a_idx[t], b_idx[t]
            need = 2 if a == b else 1
            if a == b:
                if deg[a] + 2 <= MAX_DEGREE:
                    deg[a] += need
                    accept[t] = True
            elif deg[a] < MAX_DEGREE and deg[b] < MAX_DEGREE:
                deg[a] += 1
                deg[b] += 1
                accept[t] = True
        keys, d, x, rsum = keys[accept], d[accept], x[accept], rsum[accept]

    order = np.lexsort(keys.T[::-1])
    return [
        BondEdge(
            source_index=int(keys[t, 0]),
            target_index=int(keys[t, 1]),
            image_offset=[int(v) for v in keys[t, 2:]],
            length=float(d[t]),
            strain_ratio=float((d[t] - rsum[t]) / rsum[t]),
            is_unstable=False,
        )
        for t in order
    ]


def _bonds_crystalnn(atoms: ase.Atoms) -> list[BondEdge]:
    from pymatgen.analysis.local_env import CrystalNN
    from pymatgen.core import Lattice, Structure

    numbers = np.array(atoms.numbers, dtype=int)
    pos = np.array(atoms.positions, dtype=float)
    cell = np.array(atoms.cell.array, dtype=float)
    if len(numbers) == 0 or abs(np.linalg.det(cell)) < 1e-8:
        return []
    # Raw (unwrapped) positions so pymatgen images match our convention.
    structure = Structure(Lattice(cell), numbers.tolist(), pos,
                          coords_are_cartesian=True, to_unit_cell=False)
    cnn = CrystalNN()
    rad = _radius(numbers)
    entries: dict[tuple, float] = {}
    for i in range(len(numbers)):
        try:
            infos = cnn.get_nn_info(structure, i)
        except Exception:
            continue
        for info in infos:
            j = int(info["site_index"])
            n = np.array(info["image"], dtype=np.int64).round().astype(np.int64)
            vec = pos[j] + n @ cell - pos[i]
            dist = float(np.linalg.norm(vec))
            if dist < D_MIN:
                continue
            key = tuple(_canonical(np.array([i]), np.array([j]), n[None, :])[0].tolist())
            entries[key] = dist
    out = []
    for key in sorted(entries):
        a, b = key[0], key[1]
        rsum = float(rad[a] + rad[b])
        dist = entries[key]
        out.append(BondEdge(source_index=a, target_index=b, image_offset=list(key[2:]),
                            length=dist, strain_ratio=(dist - rsum) / rsum, is_unstable=False))
    return out


def find_bonds(atoms: ase.Atoms, mode: str = "fast") -> list[BondEdge]:
    """Periodic bond graph; each physical bond once (see module docstring)."""
    if mode == "fast":
        return _bonds_fast(atoms)
    if mode == "crystalnn":
        return _bonds_crystalnn(atoms)
    raise ValueError(f"unknown bond mode {mode!r}; expected 'fast' or 'crystalnn'")


# ---------------------------------------------------------------------------
# Radial distribution function (§4.5)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _rdf_kernel() -> tuple[np.ndarray, int]:
    """Gaussian kernel from the fine deposition grid onto RDF_R (incl. 1/(4 pi r^2))."""
    nodes = np.arange(int(math.ceil(RDF_CUTOFF / _RDF_H)) + 2) * _RDF_H
    diff = RDF_R[:, None] - nodes[None, :]
    k = np.exp(-0.5 * (diff / RDF_SIGMA) ** 2) / math.sqrt(2.0 * math.pi * RDF_SIGMA ** 2)
    k /= (4.0 * math.pi * RDF_R ** 2)[:, None]
    k.setflags(write=False)
    return k, len(nodes)


def compute_rdf(atoms: ase.Atoms) -> tuple[np.ndarray, np.ndarray]:
    """g(r) on the fixed 151-point grid r = 0.5 ... 8.0 Å (§4.5).

    Every pair distance d <= 8.0 + 4 sigma over all periodic images (including
    self-images, excluding (i, 0)) is deposited linearly onto a 0.0025 Å grid,
    then smeared with the sigma = 0.08 Å Gaussian. This matches the direct sum
    to ~1e-4 of the peak height. Non-periodic or degenerate cells give g = 0.
    """
    r = RDF_R.copy()
    pairs = _rdf_pairs(atoms)
    if pairs is None:
        return r, np.zeros_like(r)
    _, _, d, vol = pairs
    nat = len(atoms)
    return r, _rdf_smear(d) * (vol / (nat * nat))


def _rdf_pairs(atoms: ase.Atoms):
    """(I, J, d, volume) of every RDF pair (self-pair (i, 0) excluded), or None for unusable input."""
    nat = len(atoms)
    cell = np.array(atoms.cell.array, dtype=float)
    if nat == 0 or not np.all(atoms.pbc) or not np.isfinite(cell).all():
        return None
    vol = abs(np.linalg.det(cell))
    if vol < 1e-8:
        return None
    found = _pair_vectors(atoms, RDF_CUTOFF, _RDF_MAX_IMAGE_RANGE)
    if found is None:
        return None
    ii, jj, nn, d = found
    keep = ~((ii == jj) & ~nn.any(axis=1))     # exclude (j, n) == (i, 0)
    return ii[keep], jj[keep], d[keep], vol


def _rdf_smear(d: np.ndarray) -> np.ndarray:
    """Linear deposition on the fine grid + Gaussian smearing / (4 pi r^2) onto RDF_R (unnormalised)."""
    if len(d) == 0:
        return np.zeros_like(RDF_R)
    kernel, n_nodes = _rdf_kernel()
    t = d / _RDF_H
    idx = np.floor(t).astype(np.int64)
    w = t - idx
    hist = np.bincount(idx, weights=1.0 - w, minlength=n_nodes)
    hist += np.bincount(idx + 1, weights=w, minlength=n_nodes)
    return kernel @ hist[:n_nodes]


def compute_partial_rdf(atoms: ase.Atoms) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Partial g_AB(r) on the RDF_R grid, same smearing as :func:`compute_rdf` (Phase C1, §5.7).

    g_AB(r) = V / (N_A N_B) sum_{i in A, j in B, images} delta_sigma(r - r_ij) / (4 pi r^2), so every
    g_AB -> 1 at large r and the total is g = sum_{A,B ordered} c_A c_B g_AB. Keys are "A-B" with the
    two symbols in alphabetical order (A <= B). Unusable input gives an empty dict.
    """
    r = RDF_R.copy()
    pairs = _rdf_pairs(atoms)
    if pairs is None:
        return r, {}
    ii, jj, d, vol = pairs
    symbols = np.array(atoms.get_chemical_symbols())
    species = sorted(set(symbols.tolist()))
    counts = {sp: int(np.sum(symbols == sp)) for sp in species}
    si, sj = symbols[ii], symbols[jj]
    out: dict[str, np.ndarray] = {}
    for a_i, a in enumerate(species):
        for b in species[a_i:]:
            mask = (si == a) & (sj == b)   # ordered pairs i in A, j in B (one direction suffices)
            out[f"{a}-{b}"] = _rdf_smear(d[mask]) * (vol / (counts[a] * counts[b]))
    return r, out
