"""Local structure analysis (IMPROVEMENT_SUGGESTIONS.md §5.7, Phase C1 ``REQUEST_LOCAL_ANALYSIS``).

:func:`analyze` turns a structure plus its *active payload bonds* into a
:class:`~app.schemas.messages.LocalAnalysisData`:

* **Partial RDFs** g_AB(r) on the payload grid (:func:`geometry_analysis.compute_partial_rdf`,
  same smearing as the total g(r), each normalised so g_AB -> 1).
* **Coordination.** Neighbours are exactly the given bonds (each bond is seen from both ends; a
  self-image bond gives the two images +n and -n).
* **Oxidation states** from pymatgen ``BVAnalyzer`` (MAP estimate from bond-valence sums); fallback
  ``Composition.oxi_state_guesses()``. All-zero results (metals, elements) or failure -> ``None``.
* **Bond-valence sums** s_ij = exp((r0 - d_ij) / b), b = 0.37 A, summed over bonded counter-ions
  (cation-anion bonds by the sign of the oxidation state). r0 is the Brese-O'Keeffe (1991)
  parametrisation r0 = r_c + r_a - r_c r_a (sqrt(c_c) - sqrt(c_a))^2 / (c_c r_c + c_a r_a) with
  pymatgen's ``bvparam_1991`` table (oxidation-state independent; reproduces the tabulated
  Ti-O 1.815, Sr-O 2.118 to ~3 mA). GII = sqrt(mean_i (BVS_i - |V_i|)^2) over all sites with a known
  non-zero oxidation state. Ideal length of a cation-anion bond: r0 - b ln(|V_c| / CN_c), with CN_c the
  number of bonded anions of the cation.
* **Polyhedra**: every cation (positive oxidation state; without oxidation states: an atom whose
  bonded neighbours are all at least 0.4 more electronegative) with 3-12 bonded counter-ions.
  Shape from CN and the ligand-centre-ligand angles; bond-length distortion
  (1/n) sum |l_i - l_mean| / l_mean; Robinson quadratic elongation <(l_i / l_0)^2> for octahedra and
  tetrahedra, l_0 = centre-vertex distance of the regular polyhedron of the same volume.
* **Octahedral tilts / Glazer** for corner-sharing BX6 networks (every X of every octahedron shared
  by exactly two octahedra): pseudo-cubic axes from the B-X-B-linked B-B vectors (clustered,
  sign-aligned, orthonormalised by SVD; ordered to follow the lattice vectors a, b, c); per-octahedron
  rotation from a Kabsch fit of the ideal octahedron (vertices on +-axes) to the actual B-X unit
  vectors; rotation vector components on the pseudo-cubic axes = tilts about a, b, c. Per axis the
  magnitude is the mean |omega_k|; below TILT_ZERO_DEG it is "0", else "+" if octahedra linked
  along that same axis rotate in the same sense (in-phase), "-" if opposite (anti-phase). Glazer
  letters: an axis whose magnitude equals that of an earlier axis (within TILT_EQUAL_DEG) reuses its
  letter, otherwise a / b / c by position (e.g. "a0a0c-", "a-a-c+", "a+b-b-").
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms

from app.core.geometry_analysis import RDF_R, compute_partial_rdf
from app.schemas.crystal import BondEdge
from app.schemas.messages import (
    LocalAnalysisData,
    NeighborInfo,
    OctahedralTilts,
    Polyhedron,
    SiteEnvironment,
)

BV_B = 0.37                 # A, universal bond-valence softness
TILT_ZERO_DEG = 0.3         # tilt magnitudes below this are "0"
TILT_EQUAL_DEG = 0.3        # equal Glazer letters (absolute, or 5 % relative)
CHI_CENTRE_GAP = 0.4        # electronegativity rule for polyhedron centres without oxidation states
MAX_CN_POLYHEDRON = 12


# --------------------------------------------------------------------------- #
# neighbours
# --------------------------------------------------------------------------- #
class _Nb:
    __slots__ = ("j", "n", "d", "vec")

    def __init__(self, j: int, n: Tuple[int, int, int], d: float, vec: np.ndarray) -> None:
        self.j, self.n, self.d, self.vec = j, n, d, vec


def _neighbors(atoms: Atoms, bonds: Sequence[BondEdge]) -> List[List[_Nb]]:
    pos = np.asarray(atoms.positions, dtype=float)
    cell = np.asarray(atoms.cell.array, dtype=float)
    out: List[List[_Nb]] = [[] for _ in range(len(atoms))]
    for b in bonds:
        i, j = int(b.source_index), int(b.target_index)
        if not (0 <= i < len(atoms) and 0 <= j < len(atoms)):
            continue
        n = np.asarray(b.image_offset, dtype=int)
        vec = pos[j] + n @ cell - pos[i]
        d = float(np.linalg.norm(vec))
        out[i].append(_Nb(j, tuple(int(x) for x in n), d, vec))
        out[j].append(_Nb(i, tuple(int(-x) for x in n), d, -vec))
    return out


# --------------------------------------------------------------------------- #
# oxidation states and bond valence
# --------------------------------------------------------------------------- #
def _structure(atoms: Atoms):
    from pymatgen.core import Lattice, Structure

    return Structure(
        Lattice(np.asarray(atoms.cell.array, dtype=float)),
        atoms.get_chemical_symbols(),
        np.asarray(atoms.positions, dtype=float),
        coords_are_cartesian=True,
    )


def oxidation_states(atoms: Atoms) -> Tuple[Optional[List[float]], str]:
    """Per-site oxidation states (BVAnalyzer, fallback composition guess) or (None, reason)."""
    symbols = atoms.get_chemical_symbols()
    if len(set(symbols)) < 2:
        return None, "single element"
    import warnings

    try:
        from pymatgen.analysis.bond_valence import BVAnalyzer

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vals = [float(v) for v in BVAnalyzer().get_valences(_structure(atoms))]
        if any(v != 0 for v in vals):
            return vals, "BVAnalyzer"
    except Exception:  # noqa: BLE001 - "Valences cannot be assigned!" etc.
        pass
    try:
        from pymatgen.core import Composition

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            guesses = Composition("".join(symbols)).oxi_state_guesses(max_sites=-50)
        if guesses:
            g = guesses[0]
            vals = [float(g.get(s, 0.0)) for s in symbols]
            if any(v != 0 for v in vals):
                return vals, "composition oxidation-state guess"
    except Exception:  # noqa: BLE001
        pass
    return None, "oxidation states undeterminable"


@lru_cache(maxsize=512)
def bv_r0(cation: str, anion: str) -> Optional[float]:
    """Brese-O'Keeffe r0 (A) for a cation-anion pair, None if a parameter is missing."""
    try:
        from pymatgen.core import Element
        from pymatgen.core.bond_valence import BV_PARAMS

        p1, p2 = BV_PARAMS[Element(cation)], BV_PARAMS[Element(anion)]
    except Exception:  # noqa: BLE001
        return None
    r1, r2, c1, c2 = p1["r"], p2["r"], p1["c"], p2["c"]
    return float(r1 + r2 - r1 * r2 * (math.sqrt(c1) - math.sqrt(c2)) ** 2 / (c1 * r1 + c2 * r2))


def _pauling(symbol: str) -> Optional[float]:
    try:
        from pymatgen.core import Element

        x = Element(symbol).X
        return float(x) if np.isfinite(x) else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# polyhedra
# --------------------------------------------------------------------------- #
def _angles(vecs: np.ndarray) -> np.ndarray:
    u = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    c = np.clip(u @ u.T, -1.0, 1.0)
    iu = np.triu_indices(len(u), 1)
    return np.degrees(np.arccos(c[iu]))


def _classify(vecs: np.ndarray) -> str:
    n = len(vecs)
    ang = _angles(vecs)
    if n == 6:
        # 12 cis angles near 90, 3 trans near 180
        srt = np.sort(ang)
        if np.all(np.abs(srt[:12] - 90.0) < 25.0) and np.all(srt[12:] > 150.0):
            return "octahedron"
    elif n == 4:
        if np.all(np.abs(ang - 109.47) < 20.0):
            return "tetrahedron"
        srt = np.sort(ang)
        if np.all(np.abs(srt[:4] - 90.0) < 20.0) and np.all(srt[4:] > 155.0):
            return "square-planar"
    elif n == 3:
        if abs(ang.sum() - 360.0) < 15.0:
            return "trigonal-planar"
    return "other"


def _hull_volume(vecs: np.ndarray) -> Optional[float]:
    try:
        from scipy.spatial import ConvexHull

        return float(ConvexHull(vecs).volume)
    except Exception:  # noqa: BLE001 - degenerate (planar) ligand sets
        return None


def _quadratic_elongation(shape: str, vecs: np.ndarray, lengths: np.ndarray) -> Optional[float]:
    # Regular polyhedron volume in terms of the centre-vertex distance l0.
    factor = {"octahedron": 4.0 / 3.0, "tetrahedron": 8.0 * math.sqrt(3.0) / 27.0}.get(shape)
    if factor is None:
        return None
    vol = _hull_volume(vecs)
    if not vol or vol <= 0:
        return None
    l0 = (vol / factor) ** (1.0 / 3.0)
    return float(np.mean((lengths / l0) ** 2))


# --------------------------------------------------------------------------- #
# octahedral tilts
# --------------------------------------------------------------------------- #
def _octahedral_tilts(
    atoms: Atoms, nbs: List[List[_Nb]], octa: Dict[int, List[_Nb]]
) -> Optional[OctahedralTilts]:
    if not octa:
        return None
    centres = set(octa)
    # B-X-B links: from each centre b through ligand (x, n) to centres b2 bonded to x.
    links: List[Tuple[int, int, np.ndarray, Tuple[int, int, int]]] = []   # (b, b2, B->B2 vector, image of b2)
    for b, ligs in octa.items():
        for lg in ligs:
            partners = []
            for xb in nbs[lg.j]:
                if xb.j not in centres:
                    continue
                img = tuple(int(lg.n[k] + xb.n[k]) for k in range(3))
                if xb.j == b and img == (0, 0, 0):
                    continue
                partners.append((xb.j, img, lg.vec + xb.vec))
            if len(partners) != 1:
                return None           # not a corner-sharing network (edge/face sharing, or terminal X)
            b2, img, v = partners[0]
            links.append((b, b2, v, img))
    if not links:
        return None

    # Pseudo-cubic axes: cluster B-B link directions (up to sign) into three groups.
    dirs = np.array([v / np.linalg.norm(v) for _, _, v, _ in links])
    groups: List[List[np.ndarray]] = []
    reps: List[np.ndarray] = []
    for u in dirs:
        for gi, rep in enumerate(reps):
            if abs(float(u @ rep)) > math.cos(math.radians(30.0)):
                groups[gi].append(u if u @ rep > 0 else -u)
                break
        else:
            reps.append(u)
            groups.append([u])
    if len(reps) != 3:
        return None
    raw = np.array([np.mean(g, axis=0) for g in groups])
    uu, _, vt = np.linalg.svd(raw)
    axes = uu @ vt                                  # nearest orthonormal frame
    # Order and orient the axes to follow the lattice vectors a, b, c.
    cell = np.asarray(atoms.cell.array, dtype=float)
    cn = cell / np.linalg.norm(cell, axis=1, keepdims=True)
    import itertools

    best = None
    for perm in itertools.permutations(range(3)):
        score = sum(abs(float(axes[perm[k]] @ cn[k])) for k in range(3))
        if best is None or score > best[0] + 1e-9:
            best = (score, perm)
    axes = np.array([axes[p] for p in best[1]])
    for k in range(3):
        if axes[k] @ cn[k] < 0:
            axes[k] = -axes[k]
    if np.linalg.det(axes) < 0:
        axes[2] = -axes[2]

    # Per-octahedron rotation (Kabsch) in the pseudo-cubic frame.
    from scipy.spatial.transform import Rotation

    omega: Dict[int, np.ndarray] = {}
    ideal_dirs = np.vstack([axes, -axes])
    for b, ligs in octa.items():
        actual = np.array([lg.vec / np.linalg.norm(lg.vec) for lg in ligs])
        sim = actual @ ideal_dirs.T
        assign = np.argmax(sim, axis=1)
        if len(set(assign.tolist())) != 6:
            return None
        ideal = ideal_dirs[assign]
        h = ideal.T @ actual
        u, _, vt2 = np.linalg.svd(h)
        d = np.sign(np.linalg.det(vt2.T @ u.T))
        rot = vt2.T @ np.diag([1.0, 1.0, d]) @ u.T     # actual ~= rot @ ideal
        rv = Rotation.from_matrix(rot).as_rotvec()
        omega[b] = np.degrees(axes @ rv)

    mags = [float(np.mean([abs(omega[b][k]) for b in omega])) for k in range(3)]
    phases: List[str] = []
    for k in range(3):
        if mags[k] < TILT_ZERO_DEG:
            phases.append("0")
            continue
        votes = []
        for b, b2, v, _ in links:
            u = v / np.linalg.norm(v)
            if abs(float(u @ axes[k])) > math.cos(math.radians(30.0)):
                votes.append(np.sign(omega[b][k]) * np.sign(omega[b2][k]))
        if not votes:
            phases.append("0")
            continue
        phases.append("+" if float(np.mean(votes)) > 0 else "-")

    letters: List[str] = []
    for k in range(3):
        letter = "abc"[k]
        for j in range(k):
            same_zero = phases[j] == "0" and phases[k] == "0"
            close = abs(mags[j] - mags[k]) <= max(TILT_EQUAL_DEG, 0.05 * max(mags[j], mags[k]))
            if same_zero or (phases[j] != "0" and phases[k] != "0" and close):
                letter = letters[j]
                break
        letters.append(letter)
    glazer = "".join(f"{letters[k]}{phases[k]}" for k in range(3))
    return OctahedralTilts(
        tilt_angles_deg=[round(m, 3) for m in mags],
        phases=phases,
        glazer=glazer,
        pseudo_cubic_axes=[[float(x) for x in ax] for ax in axes],
        method=(
            f"{len(octa)} corner-sharing BX6 octahedra; pseudo-cubic axes from B-X-B linked B-B vectors; "
            "per-octahedron Kabsch rotation vs the ideal octahedron, components on the pseudo-cubic axes; "
            f"in-phase/anti-phase from octahedra linked along each axis; |tilt| < {TILT_ZERO_DEG} deg -> 0"
        ),
    )


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def analyze(atoms: Atoms, bonds: List[BondEdge], *, include_partial_rdf: bool = True) -> LocalAnalysisData:
    """Local structure of ``atoms`` with neighbours from ``bonds`` (see module docstring)."""
    symbols = atoms.get_chemical_symbols()
    nat = len(atoms)
    nbs = _neighbors(atoms, bonds)
    ox, ox_note = oxidation_states(atoms)

    def is_counter(i: int, j: int) -> bool:
        if ox is None:
            return False
        return ox[i] * ox[j] < 0

    # coordination numbers of cations counted over bonded anions
    cn_anion = [sum(1 for nb in nbs[i] if is_counter(i, nb.j)) for i in range(nat)]

    sites: List[SiteEnvironment] = []
    bvs: List[Optional[float]] = [None] * nat
    for i in range(nat):
        if ox is not None and ox[i] != 0:
            s = 0.0
            for nb in nbs[i]:
                if not is_counter(i, nb.j):
                    continue
                cat, an = (symbols[i], symbols[nb.j]) if ox[i] > 0 else (symbols[nb.j], symbols[i])
                r0 = bv_r0(cat, an)
                if r0 is not None:
                    s += math.exp((r0 - nb.d) / BV_B)
            bvs[i] = s
        sites.append(
            SiteEnvironment(
                index=i,
                element=symbols[i],
                coordination_number=len(nbs[i]),
                neighbors=[
                    NeighborInfo(index=nb.j, element=symbols[nb.j], distance=nb.d, image_offset=list(nb.n))
                    for nb in sorted(nbs[i], key=lambda x: x.d)
                ],
                oxidation_state=None if ox is None else ox[i],
                bond_valence_sum=None if bvs[i] is None else round(bvs[i], 4),
            )
        )

    gii = None
    devs = [(bvs[i] - abs(ox[i])) ** 2 for i in range(nat) if ox is not None and ox[i] != 0 and bvs[i] is not None]
    if devs:
        gii = float(math.sqrt(np.mean(devs)))

    ideal: Optional[List[Optional[float]]] = None
    if ox is not None:
        ideal = []
        for b in bonds:
            i, j = int(b.source_index), int(b.target_index)
            val = None
            if 0 <= i < nat and 0 <= j < nat and ox[i] * ox[j] < 0:
                c, a = (i, j) if ox[i] > 0 else (j, i)
                r0 = bv_r0(symbols[c], symbols[a])
                if r0 is not None and cn_anion[c] > 0:
                    val = round(r0 - BV_B * math.log(abs(ox[c]) / cn_anion[c]), 4)
            ideal.append(val)

    # polyhedra
    polyhedra: List[Polyhedron] = []
    octa: Dict[int, List[_Nb]] = {}
    for i in range(nat):
        if ox is not None:
            if ox[i] <= 0:
                continue
            ligs = [nb for nb in nbs[i] if ox[nb.j] < 0]
        else:
            xi = _pauling(symbols[i])
            if xi is None or not nbs[i]:
                continue
            xs = [_pauling(symbols[nb.j]) for nb in nbs[i]]
            if any(x is None or x - xi < CHI_CENTRE_GAP for x in xs):
                continue
            ligs = list(nbs[i])
        if not (3 <= len(ligs) <= MAX_CN_POLYHEDRON):
            continue
        vecs = np.array([nb.vec for nb in ligs])
        lengths = np.array([nb.d for nb in ligs])
        shape = _classify(vecs)
        lm = float(lengths.mean())
        polyhedra.append(
            Polyhedron(
                center_index=i,
                center_element=symbols[i],
                ligand_indices=[nb.j for nb in ligs],
                ligand_image_offsets=[list(nb.n) for nb in ligs],
                shape=shape,
                mean_bond_length=round(lm, 4),
                bond_length_distortion=float(np.mean(np.abs(lengths - lm)) / lm),
                quadratic_elongation=_quadratic_elongation(shape, vecs, lengths),
            )
        )
        if shape == "octahedron":
            octa[i] = ligs

    tilts = None
    try:
        tilts = _octahedral_tilts(atoms, nbs, octa)
    except Exception:  # noqa: BLE001 - never fail the analysis because of the tilt fit
        tilts = None

    partial = None
    if include_partial_rdf:
        _, pr = compute_partial_rdf(atoms)
        partial = {k: [float(x) for x in v] for k, v in pr.items()}

    method = (
        f"neighbours = active bonds; oxidation states: {ox_note}; BVS with Brese-O'Keeffe (1991) r0 "
        f"(pymatgen bvparam_1991), b = {BV_B} A; ideal length r0 - b ln(V/CN); partial g_AB(r) with the "
        "total-g(r) smearing"
    )
    return LocalAnalysisData(
        rdf_r=[float(x) for x in RDF_R],
        partial_rdf=partial,
        sites=sites,
        bond_ideal_lengths=ideal,
        global_instability_index=gii,
        polyhedra=polyhedra,
        octahedral_tilts=tilts,
        method=method,
    )
