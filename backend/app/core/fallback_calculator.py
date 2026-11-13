"""Deterministic analytic fallback force field (PROJECT_SPEC.md §7 Phase 1).

Used when the MACE-MP-0 checkpoint cannot be loaded (offline CI) or when
``LATTICEFLOW_UMLIP=fallback``. It is *not* meant to be quantitatively
accurate; it is meant to be

* deterministic and purely analytic (numpy only, no torch / MACE import),
* smooth: energy is C1 everywhere, C2 except at the short-range core joint,
  and forces / stress are the exact analytic derivatives of that energy, so
  FIRE relaxation and finite-difference phonons behave consistently,
* periodic-safe: all pair sums run over every periodic image within the
  cutoff (``ase.neighborlist.neighbor_list``), so cells smaller than the
  cutoff are handled correctly,
* robust: it never raises on transmuted / vacancy cells, unusual elements or
  overlapping atoms (energies stay finite).

Energy model
------------
E = 1/2 sum_{i, (j,n) != (i,0)} [ phi_LJ(r_ijn) + phi_C(r_ijn) ] + E_self

phi_LJ: Lennard-Jones whose minimum sits at r0_ij = R_i + R_j (covalent radii
from ``ase.data.covalent_radii``, clipped to [0.3, 2.6] Å), sigma_ij =
r0_ij / 2^(1/6), multiplied by a quintic switch S(r) that takes it smoothly
(C2) to zero between 2.0 sigma_ij and 2.5 sigma_ij. Below r_core = 0.6 r0_ij
the LJ is continued linearly (value and slope matched), so coincident atoms
give a large but finite energy instead of inf/NaN.

phi_C: damped shifted-force (DSF, Fennell & Gezelter 2006) real-space Coulomb
k q_i q_j [erfc(a r)/r - erfc(a Rc)/Rc + (erfc(a Rc)/Rc^2 + 2a/sqrt(pi) e^{-a^2 Rc^2}/Rc)(r - Rc)]
for r < Rc; energy and force both vanish at Rc. Same linear core continuation
below r_core. E_self = -k sum_i q_i^2 (erfc(a Rc)/(2 Rc) + a/sqrt(pi)).

Charges: a fixed table of formal oxidation states (``FORMAL_CHARGES``) scaled
by ``charge_scale`` (default 0.5, rigid-ion "partial charge" style, which
keeps the LJ repulsion able to balance the Coulomb attraction). The Coulomb
term is applied only if *every* element present is in the table and the
formal charges sum to exactly zero and at least one is non-zero; otherwise
(vacancies, transmutations, metals / unknown elements) the energy is LJ only.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import numpy as np
from ase import Atoms
from ase.calculators.calculator import Calculator, all_changes
from ase.data import covalent_radii
from ase.neighborlist import neighbor_list
from scipy.special import erfc

COULOMB_K = 14.399645478425668  # e^2 / (4 pi eps0) in eV * Å

# Single fixed formal oxidation state per element (most common in oxides /
# chalcogenides / halides). Elements absent from this table disable Coulomb.
FORMAL_CHARGES: Dict[str, int] = {
    # noble gases
    "He": 0, "Ne": 0, "Ar": 0, "Kr": 0, "Xe": 0,
    # +1
    "H": 1, "Li": 1, "Na": 1, "K": 1, "Rb": 1, "Cs": 1, "Ag": 1, "Tl": 1,
    # +2
    "Be": 2, "Mg": 2, "Ca": 2, "Sr": 2, "Ba": 2, "Zn": 2, "Cd": 2, "Hg": 2,
    "Mn": 2, "Co": 2, "Ni": 2, "Cu": 2, "Pb": 2, "Pd": 2, "Eu": 2,
    # +3
    "B": 3, "Al": 3, "Ga": 3, "In": 3, "Sc": 3, "Y": 3, "La": 3, "Ce": 3,
    "Pr": 3, "Nd": 3, "Sm": 3, "Gd": 3, "Tb": 3, "Dy": 3, "Ho": 3, "Er": 3,
    "Tm": 3, "Yb": 3, "Lu": 3, "Bi": 3, "Cr": 3, "Fe": 3, "Sb": 3, "Au": 3,
    # +4
    "C": 4, "Si": 4, "Ge": 4, "Sn": 4, "Ti": 4, "Zr": 4, "Hf": 4, "Ru": 4,
    "Ir": 4, "Pt": 4, "Th": 4, "U": 4,
    # +5
    "P": 5, "As": 5, "V": 5, "Nb": 5, "Ta": 5,
    # +6
    "Mo": 6, "W": 6,
    # anions
    "N": -3, "O": -2, "S": -2, "Se": -2, "Te": -2,
    "F": -1, "Cl": -1, "Br": -1, "I": -1,
}

_TWO_POW_SIXTH = 2.0 ** (1.0 / 6.0)
_SQRT_PI = math.sqrt(math.pi)


def assign_formal_charges(symbols) -> Optional[np.ndarray]:
    """Formal charges if the cell is charge-neutral under the fixed table, else None."""
    try:
        q = np.array([FORMAL_CHARGES[s] for s in symbols], dtype=float)
    except KeyError:
        return None
    if q.size == 0 or not np.any(q) or int(round(q.sum())) != 0:
        return None
    return q


_DENSE_PAIR_LIMIT = 4_000_000  # max (images * N^2) for the dense vectorised pair search


def periodic_pairs(atoms: Atoms, cutoff: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Full (both-direction) pair list (i, j, D) with D = r_j + n.L - r_i, |D| < cutoff.

    Includes every periodic image (also images of atom i itself), excludes only
    the (i, i, n=0) self term. For fully periodic cells this uses a dense numpy
    search over the required image range (a few ms for ~40 atoms at 9 Å, ~20x
    faster than ``ase.neighborlist`` for these sizes); otherwise, or for very
    large cells, it defers to ``ase.neighborlist.neighbor_list``.
    """
    n_atoms = len(atoms)
    cell = np.asarray(atoms.cell.array, dtype=float)
    if not (np.all(atoms.pbc) and atoms.cell.rank == 3):
        i, j, D = neighbor_list("ijD", atoms, cutoff, self_interaction=False)
        return i, j, D

    inv = np.linalg.inv(cell)                    # columns are reciprocal vectors / (2 pi)
    plane_spacing = 1.0 / np.linalg.norm(inv, axis=0)
    n_max = np.ceil(cutoff / plane_spacing + 0.5).astype(int)
    n_images = int(np.prod(2 * n_max + 1))
    if n_images * n_atoms * n_atoms > _DENSE_PAIR_LIMIT:
        i, j, D = neighbor_list("ijD", atoms, cutoff, self_interaction=False)
        return i, j, D

    frac = atoms.positions @ inv
    dfrac = frac[None, :, :] - frac[:, None, :]  # (N, N, 3): s_j - s_i
    shift0 = -np.floor(dfrac + 0.5)              # minimum-image integer shift
    base = (dfrac + shift0) @ cell               # (N, N, 3)
    grids = np.meshgrid(*[np.arange(-m, m + 1) for m in n_max], indexing="ij")
    offsets = np.stack([g.ravel() for g in grids], axis=1).astype(float)  # (M, 3)
    disp = base[None, :, :, :] + (offsets @ cell)[:, None, None, :]       # (M, N, N, 3)
    r2 = np.einsum("mijk,mijk->mij", disp, disp)
    mask = r2 < cutoff * cutoff
    zero = np.flatnonzero(np.all(offsets == 0, axis=1))[0]
    mask[zero, np.arange(n_atoms), np.arange(n_atoms)] = False
    _, i, j = np.nonzero(mask)
    return i, j, disp[mask]


def _quintic_switch(r: np.ndarray, r_on: np.ndarray, r_off: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """S = 1 for r <= r_on, 0 for r >= r_off, C2 in between. Returns (S, dS/dr)."""
    width = r_off - r_on
    x = np.clip((r - r_on) / width, 0.0, 1.0)
    x2 = x * x
    s = 1.0 - x2 * x * (10.0 - 15.0 * x + 6.0 * x2)
    ds = -30.0 * x2 * (1.0 - x) ** 2 / width
    return s, ds


def _with_linear_core(r, r_core, func):
    """Evaluate func(r) -> (phi, dphi), continued linearly below r_core."""
    inside = r < r_core
    r_eval = np.where(inside, r_core, r)
    phi, dphi = func(r_eval)
    phi = np.where(inside, phi + dphi * (r - r_core), phi)
    return phi, dphi


class AnalyticFallbackCalculator(Calculator):
    """Smoothly truncated LJ + DSF Coulomb (charge-neutral cells only)."""

    implemented_properties = ["energy", "free_energy", "forces", "stress"]
    nolabel = True
    default_parameters = dict(
        epsilon=0.25,           # LJ well depth (eV), all pairs
        lj_switch_on=2.0,       # switch starts at lj_switch_on * sigma_ij
        lj_cutoff=2.5,          # LJ is exactly zero beyond lj_cutoff * sigma_ij
        core_factor=0.6,        # linear core continuation below core_factor * r0_ij
        radius_min=0.3,
        radius_max=2.6,
        charge_scale=0.5,
        coulomb_alpha=0.2,      # DSF damping (1/Å)
        coulomb_cutoff=9.0,     # DSF cutoff (Å)
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ------------------------------------------------------------------ core
    def compute(self, atoms: Atoms) -> Tuple[float, np.ndarray, np.ndarray]:
        """Return (energy eV, forces (N,3) eV/Å, stress Voigt (6,) eV/Å^3)."""
        p = self.parameters
        n_atoms = len(atoms)
        forces = np.zeros((n_atoms, 3))
        stress = np.zeros(6)
        if n_atoms == 0:
            return 0.0, forces, stress

        numbers = atoms.get_atomic_numbers()
        radii = np.clip(covalent_radii[numbers], p.radius_min, p.radius_max)
        charges = assign_formal_charges(atoms.get_chemical_symbols())
        if charges is not None:
            charges = charges * p.charge_scale

        sigma_max = 2.0 * radii.max() / _TWO_POW_SIXTH
        cutoff = p.lj_cutoff * sigma_max
        if charges is not None:
            cutoff = max(cutoff, p.coulomb_cutoff)

        i, j, D = periodic_pairs(atoms, cutoff)
        energy = 0.0
        if i.size:
            r = np.sqrt(np.einsum("ij,ij->i", D, D))
            r0 = radii[i] + radii[j]
            r_core = p.core_factor * r0

            # ---- Lennard-Jones, smoothly switched off
            sigma = r0 / _TWO_POW_SIXTH
            eps4 = 4.0 * p.epsilon

            def lj(rr):
                sr6 = (sigma / rr) ** 6
                sr12 = sr6 * sr6
                return eps4 * (sr12 - sr6), eps4 * (-12.0 * sr12 + 6.0 * sr6) / rr

            phi, dphi = _with_linear_core(r, r_core, lj)
            s, ds = _quintic_switch(r, p.lj_switch_on * sigma, p.lj_cutoff * sigma)
            e_pair = phi * s
            de_pair = dphi * s + phi * ds

            # ---- damped shifted-force Coulomb
            if charges is not None:
                a, rc = p.coulomb_alpha, p.coulomb_cutoff
                kqq = COULOMB_K * charges[i] * charges[j]
                e_rc = math.erfc(a * rc) / rc
                de_rc = -e_rc / rc - 2.0 * a / _SQRT_PI * math.exp(-(a * rc) ** 2) / rc

                def dsf(rr):
                    e_r = erfc(a * rr) / rr
                    de_r = -e_r / rr - 2.0 * a / _SQRT_PI * np.exp(-(a * rr) ** 2) / rr
                    return kqq * (e_r - e_rc - de_rc * (rr - rc)), kqq * (de_r - de_rc)

                c_phi, c_dphi = _with_linear_core(r, r_core, dsf)
                inside = r < rc
                e_pair = e_pair + np.where(inside, c_phi, 0.0)
                de_pair = de_pair + np.where(inside, c_dphi, 0.0)
                energy -= COULOMB_K * float(np.sum(charges**2)) * (e_rc / 2.0 + a / _SQRT_PI)

            energy += 0.5 * float(np.sum(e_pair))

            # F_i = 1/2 sum_{(i,j)} phi' D/r - 1/2 sum_{(j,i)} phi' D/r
            with np.errstate(divide="ignore", invalid="ignore"):
                coef = np.where(r > 1e-12, de_pair / r, 0.0)
            fvec = coef[:, None] * D
            for k in range(3):
                forces[:, k] = 0.5 * (
                    np.bincount(i, weights=fvec[:, k], minlength=n_atoms)
                    - np.bincount(j, weights=fvec[:, k], minlength=n_atoms)
                )

            volume = abs(atoms.cell.volume) if atoms.cell.rank == 3 else 0.0
            if volume > 1e-12:
                # sigma_ab = (1/V) dE/d eps_ab = (1/2V) sum phi'/r D_a D_b
                virial = 0.5 * np.einsum("p,pa,pb->ab", coef, D, D) / volume
                stress = virial[[0, 1, 2, 1, 0, 0], [0, 1, 2, 2, 2, 1]]
        elif charges is not None:
            a, rc = p.coulomb_alpha, p.coulomb_cutoff
            e_rc = math.erfc(a * rc) / rc
            energy -= COULOMB_K * float(np.sum(charges**2)) * (e_rc / 2.0 + a / _SQRT_PI)

        return energy, forces, stress

    # ----------------------------------------------------------- ASE hooks
    def calculate(self, atoms=None, properties=("energy",), system_changes=all_changes):
        super().calculate(atoms, properties, system_changes)
        energy, forces, stress = self.compute(self.atoms)
        self.results = {
            "energy": energy,
            "free_energy": energy,
            "forces": forces,
            "stress": stress,
        }
