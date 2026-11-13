"""Partial substitution and ordering of disordered structures (IMPROVEMENT_SUGGESTIONS.md §5.10).

Partial substitution (:func:`substitute_fraction`, WS ``SUBSTITUTE_FRACTION``)
----------------------------------------------------------------------------
Replace n = round_half_up(fraction * n_from) of the ``from_element`` atoms by ``to_element``.
Configurations are reduced to symmetry-distinct classes with the parent's spglib space-group
operations (incl. the pure translations of a supercell) acting as site permutations; the canonical
form of a configuration is the lexicographically smallest image over all operations.

* C(n_from, n) <= MAX_EXHAUSTIVE: every combination is enumerated and reduced to its classes; if
  there are more classes than ``n_candidates``, a seeded random subset of them is scored.
* otherwise: seeded random combinations, deduplicated by class, until ``n_candidates`` distinct
  classes (or 50 x n_candidates draws).

Every candidate gets a uMLIP single point (not relaxed); the lowest energy is returned.

Ordering of disordered CIFs (:func:`order_disordered`, ``POST /api/ordering``)
-----------------------------------------------------------------------------
1. Parse with pymatgen (partial occupancies kept); an ordered CIF is returned as the only candidate.
2. Sites are grouped by their species/occupancy dict (as pymatgen's
   ``OrderDisorderedStructureTransformation`` does). The smallest supercell multiplier m making every
   group's species counts integer is found (occupancies rationalised with denominators <= 24), and the
   most compact diagonal supercell with det = m is used. If the ordered cell has more than
   ``max_atoms`` atoms -> ``StructureIOError(STRUCTURE_TOO_LARGE)``.
3. Enumeration: with oxidation states (``add_oxidation_state_by_guess``) and at most
   MAX_EWALD_CONFIGS configurations, ``OrderDisorderedStructureTransformation`` (Ewald-energy ranked,
   ALGO_FAST) returns its best max(3 n_candidates, 24) orderings; otherwise (no oxidation states, or
   too many configurations) seeded random sampling with exact per-group counts (24 + 4 n_candidates
   draws, deduplicated). enumlib is not used (not installed).
4. Every ordering gets a uMLIP single point; candidates that coincide (same space group and energy
   within 1e-5 eV/atom) are merged; the ``n_candidates`` lowest are returned.
"""

from __future__ import annotations

import math
import warnings
from fractions import Fraction
from itertools import combinations
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms
from ase.data import atomic_numbers

from app.core.geometry_analysis import get_spacegroup, symmetry_dataset
from app.core.structure_io import max_atoms as _studio_max_atoms, StructureIOError, atoms_to_cif, structure_from_cif
from app.schemas.api import OrderingCandidate, OrderingResponse
from app.schemas.messages import SubstitutionResult

MAX_EXHAUSTIVE = 3000
MAX_EWALD_CONFIGS = 200_000
MAX_DENOMINATOR = 24
SEED_DEFAULT = 0


class OrderingCancelled(RuntimeError):
    """``should_cancel()`` returned True during :func:`substitute_fraction`."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _site_permutations(atoms: Atoms) -> np.ndarray:
    """(n_ops, N) permutations p with op(site i) = site p[i]; identity only if symmetry fails."""
    n = len(atoms)
    ident = np.arange(n)[None, :]
    ds = symmetry_dataset(atoms)
    if ds is None:
        return ident
    frac = np.mod(atoms.get_scaled_positions(wrap=False), 1.0)
    numbers = atoms.get_atomic_numbers()
    cell = np.asarray(atoms.cell.array, dtype=float)
    perms = []
    for rot, tr in zip(ds.rotations, ds.translations):
        new = frac @ np.asarray(rot, dtype=float).T + np.asarray(tr, dtype=float)
        diff = new[:, None, :] - frac[None, :, :]
        diff -= np.round(diff)
        dist = np.linalg.norm(diff @ cell, axis=2)
        dist[numbers[:, None] != numbers[None, :]] = np.inf
        p = np.argmin(dist, axis=1)
        if np.max(dist[np.arange(n), p]) > 0.1 or len(set(p.tolist())) != n:
            continue
        perms.append(p)
    return np.unique(np.array(perms), axis=0) if perms else ident


def _canonical_keys(masks: np.ndarray, perms: np.ndarray) -> List[bytes]:
    """Canonical (smallest over the operations) byte key of each boolean configuration mask."""
    keys = []
    inv = np.argsort(perms, axis=1)          # new_mask = mask[inv]
    for m in masks:
        imgs = m[inv]                          # (n_ops, N)
        packed = np.packbits(imgs, axis=1)
        best = min(row.tobytes() for row in packed)
        keys.append(best)
    return keys


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _formula(atoms: Atoms) -> str:
    from app.core.umlip_engine import reduced_formula

    return reduced_formula(atoms)


# --------------------------------------------------------------------------- #
# partial substitution
# --------------------------------------------------------------------------- #
def substitute_fraction(
    atoms: Atoms,
    from_element: str,
    to_element: str,
    fraction: float,
    engine,
    *,
    n_candidates: int = 12,
    seed: Optional[int] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[Atoms, SubstitutionResult]:
    """Lowest-energy configuration of a partial substitution (module docstring).

    ValueError: unknown / identical elements, ``from_element`` absent, fraction outside (0, 1] or
    rounding to zero atoms. :class:`OrderingCancelled` on cancellation.
    """
    if to_element not in atomic_numbers or atomic_numbers[to_element] == 0:
        raise ValueError(f"unknown element {to_element!r}")
    if from_element == to_element:
        raise ValueError("from_element and to_element are the same")
    if not (0.0 < float(fraction) <= 1.0):
        raise ValueError("fraction must be in (0, 1]")
    symbols = atoms.get_chemical_symbols()
    sites = [i for i, s in enumerate(symbols) if s == from_element]
    if not sites:
        raise ValueError(f"the structure contains no {from_element}")
    n_sub = _round_half_up(fraction * len(sites))
    if n_sub == 0:
        raise ValueError(
            f"{fraction:g} x {len(sites)} {from_element} rounds to zero atoms; use a larger fraction or a supercell"
        )
    n_candidates = max(1, int(n_candidates))
    rng = np.random.default_rng(SEED_DEFAULT if seed is None else seed)
    perms = _site_permutations(atoms)
    nat = len(atoms)
    sites_arr = np.array(sites)

    def mask_of(combo: Sequence[int]) -> np.ndarray:
        m = np.zeros(nat, dtype=bool)
        m[list(combo)] = True
        return m

    total = math.comb(len(sites), n_sub)
    configs: List[np.ndarray] = []
    if total <= MAX_EXHAUSTIVE:
        combos = list(combinations(sites, n_sub))
        masks = np.array([mask_of(c) for c in combos])
        keys = _canonical_keys(masks, perms)
        classes: Dict[bytes, np.ndarray] = {}
        for k, m in zip(keys, masks):
            classes.setdefault(k, m)
        ordered = [classes[k] for k in sorted(classes)]
        n_classes = len(ordered)
        if n_classes > n_candidates:
            pick = sorted(rng.choice(n_classes, size=n_candidates, replace=False).tolist())
            ordered = [ordered[i] for i in pick]
        configs = ordered
        how = (f"exhaustive: {total} combinations -> {n_classes} symmetry-distinct "
               f"({len(perms)} operations); {len(configs)} scored")
    else:
        seen = set()
        draws = 0
        while len(configs) < n_candidates and draws < 50 * n_candidates:
            draws += 1
            combo = sorted(rng.choice(sites_arr, size=n_sub, replace=False).tolist())
            m = mask_of(combo)
            k = _canonical_keys(m[None, :], perms)[0]
            if k in seen:
                continue
            seen.add(k)
            configs.append(m)
        how = (f"random sampling ({total} combinations > {MAX_EXHAUSTIVE}): {draws} draws -> "
               f"{len(configs)} symmetry-distinct scored")

    results: List[Tuple[float, Atoms]] = []
    to_z = atomic_numbers[to_element]
    for idx, m in enumerate(configs):
        if should_cancel is not None and should_cancel():
            raise OrderingCancelled("substitution cancelled")
        if progress is not None:
            try:
                progress(idx, len(configs), f"Configuration {idx + 1}/{len(configs)}")
            except Exception:  # noqa: BLE001
                pass
        cand = atoms.copy()
        cand.calc = None
        nums = cand.get_atomic_numbers()
        nums[m] = to_z
        cand.set_atomic_numbers(nums)
        e, _, _ = engine.energy_forces_stress(cand)
        results.append((float(e) / nat, cand))
    results.sort(key=lambda t: t[0])
    best_e, best = results[0]
    return best, SubstitutionResult(
        from_element=from_element,
        to_element=to_element,
        n_substituted=n_sub,
        n_tried=len(results),
        energies_per_atom=[e for e, _ in results],
        chosen_energy_per_atom=best_e,
        method=f"{how}; uMLIP single points ({engine.backend_name}, not relaxed); lowest kept",
    )


# --------------------------------------------------------------------------- #
# ordering of disordered structures
# --------------------------------------------------------------------------- #
def _groups(structure) -> List[Tuple[Dict[str, float], List[int]]]:
    groups: Dict[Tuple, List[int]] = {}
    keyspec: Dict[Tuple, Dict[str, float]] = {}
    for i, site in enumerate(structure):
        spec = {str(getattr(sp, "element", sp)): float(occ) for sp, occ in site.species.items()}
        key = tuple(sorted((k, round(v, 4)) for k, v in spec.items()))
        groups.setdefault(key, []).append(i)
        keyspec[key] = spec
    return [(keyspec[k], v) for k, v in groups.items()]


def _multiplier(groups) -> int:
    m = 1
    for spec, idx in groups:
        n = len(idx)
        for occ in list(spec.values()) + [1.0 - sum(spec.values())]:
            if occ < 1e-6:
                continue
            frac = Fraction(occ * n).limit_denominator(MAX_DENOMINATOR)
            m = m * frac.denominator // math.gcd(m, frac.denominator)
    return m


def _compact_diagonal(structure, m: int) -> List[int]:
    best = None
    abc = np.array(structure.lattice.abc, dtype=float)
    for a in range(1, m + 1):
        if m % a:
            continue
        for b in range(1, m // a + 1):
            if (m // a) % b:
                continue
            c = m // (a * b)
            lengths = abc * np.array([a, b, c])
            score = lengths.max() / lengths.min()
            if best is None or score < best[0] - 1e-9:
                best = (score, [a, b, c])
    return best[1]


def _n_configs(groups, m: int) -> float:
    total = 0.0  # log
    for spec, idx in groups:
        n = len(idx) * m
        counts = [round(o * n) for o in spec.values()]
        vac = n - sum(counts)
        if len(counts) == 1 and vac == 0:
            continue
        total += math.lgamma(n + 1) - sum(math.lgamma(c + 1) for c in counts + [vac])
    return math.exp(min(total, 700.0))


def _random_orderings(structure, n_draws: int, seed: int) -> List:
    from pymatgen.core import Structure

    rng = np.random.default_rng(seed)
    groups = _groups(structure)
    out = []
    seen = set()
    for _ in range(n_draws):
        species: List[Optional[str]] = [None] * len(structure)
        for spec, idx in groups:
            n = len(idx)
            labels: List[Optional[str]] = []
            for el, occ in sorted(spec.items()):
                labels += [el] * int(round(occ * n))
            labels += [None] * (n - len(labels))
            labels = labels[:n]
            perm = rng.permutation(n)
            for k, site in enumerate(idx):
                species[site] = labels[perm[k]]
        key = tuple(species)
        if key in seen:
            continue
        seen.add(key)
        keep = [i for i, s in enumerate(species) if s is not None]
        out.append(Structure(structure.lattice, [species[i] for i in keep],
                             [structure[i].frac_coords for i in keep]))
    return out


def _to_atoms(structure) -> Atoms:
    return Atoms(
        symbols=[str(getattr(site.specie, "element", site.specie)) for site in structure],
        cell=np.array(structure.lattice.matrix, dtype=float),
        scaled_positions=np.mod(np.array(structure.frac_coords, dtype=float), 1.0),
        pbc=True,
    )


def order_disordered(cif_text: str, engine, *, max_atoms: int = 64, n_candidates: int = 8) -> OrderingResponse:
    """Ordered approximants of a disordered CIF ranked by uMLIP energy (module docstring).

    Raises :class:`StructureIOError` (INVALID_STRUCTURE for parse errors, STRUCTURE_TOO_LARGE).
    """
    structure = structure_from_cif(cif_text)
    max_atoms = min(int(max_atoms), _studio_max_atoms())
    dis_formula = structure.composition.reduced_formula
    if structure.is_ordered:
        atoms = _to_atoms(structure)
        if len(atoms) > max_atoms:
            raise StructureIOError(f"structure has {len(atoms)} atoms (> {max_atoms})", code="STRUCTURE_TOO_LARGE")
        e, _, _ = engine.energy_forces_stress(atoms)
        return OrderingResponse(
            disordered_formula=dis_formula,
            n_enumerated=1,
            candidates=[_candidate(atoms, float(e) / len(atoms), float(e) / len(atoms))],
            method=f"input is already ordered; returned as the only candidate (single point, {engine.backend_name})",
        )

    groups = _groups(structure)
    m = _multiplier(groups)
    n_ordered = int(round(m * sum(sum(spec.values()) for spec, idx in groups for _ in idx)))
    if n_ordered > max_atoms:
        raise StructureIOError(
            f"the smallest ordered cell ({m} x the CIF cell) has {n_ordered} atoms > max_atoms {max_atoms}",
            code="STRUCTURE_TOO_LARGE",
        )
    scaling = _compact_diagonal(structure, m)
    sc = structure.copy()
    if m > 1:
        sc.make_supercell(scaling)
    n_conf = _n_configs(groups, m)
    want = max(3 * n_candidates, 24)

    orderings = []
    how = ""
    oxi = None
    if n_conf <= MAX_EWALD_CONFIGS:
        try:
            oxi = sc.copy()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                oxi.add_oxidation_state_by_guess(max_sites=-1)
        except Exception:  # noqa: BLE001
            oxi = None
    if oxi is not None:
        try:
            from pymatgen.transformations.standard_transformations import (
                OrderDisorderedStructureTransformation,
            )

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ranked = OrderDisorderedStructureTransformation().apply_transformation(
                    oxi, return_ranked_list=want
                )
            orderings = [r["structure"] for r in ranked]
            how = (f"pymatgen OrderDisorderedStructureTransformation (Ewald ranking with guessed oxidation "
                   f"states; {n_conf:.0f} configurations) best {len(orderings)}")
        except Exception as exc:  # noqa: BLE001
            orderings = []
            how = f"Ewald enumeration failed ({type(exc).__name__}); "
    if not orderings:
        reason = ("too many configurations" if n_conf > MAX_EWALD_CONFIGS
                  else "no oxidation states" if oxi is None else "fallback")
        orderings = _random_orderings(sc, 24 + 4 * n_candidates, SEED_DEFAULT)
        how += (f"seeded random sampling with exact site-group counts ({reason}; ~{n_conf:.3g} configurations): "
                f"{len(orderings)} distinct")

    scored: List[Tuple[float, Atoms]] = []
    for s in orderings:
        atoms = _to_atoms(s)
        if len(atoms) == 0:
            continue
        e, _, _ = engine.energy_forces_stress(atoms)
        scored.append((float(e) / len(atoms), atoms))
    if not scored:
        raise StructureIOError("no ordered approximant could be generated")
    scored.sort(key=lambda t: t[0])
    unique: List[Tuple[float, Atoms, str]] = []
    for e, atoms in scored:
        sg = get_spacegroup(atoms)[0]
        if any(abs(e - u[0]) < 1e-5 and sg == u[2] for u in unique):
            continue
        unique.append((e, atoms, sg))
    e0 = unique[0][0]
    cands = [_candidate(a, e, e0, sg) for e, a, sg in unique[:n_candidates]]
    return OrderingResponse(
        disordered_formula=dis_formula,
        n_enumerated=len(orderings),
        candidates=cands,
        method=(f"supercell {'x'.join(map(str, scaling))} of the CIF cell (multiplier {m}); {how}; "
                f"uMLIP single points ({engine.backend_name}, not relaxed); duplicates merged"),
    )


def _candidate(atoms: Atoms, e: float, e0: float, sg: Optional[str] = None) -> OrderingCandidate:
    return OrderingCandidate(
        cif=atoms_to_cif(atoms),
        formula=_formula(atoms),
        n_atoms=len(atoms),
        spacegroup_symbol=sg or get_spacegroup(atoms)[0],
        energy_per_atom=e,
        relative_energy_mev_per_atom=1000.0 * (e - e0),
    )
