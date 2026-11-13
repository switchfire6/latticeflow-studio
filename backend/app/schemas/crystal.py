"""Core data contracts (PROJECT_SPEC.md §5.1).

This file is the shared contract between the physics core, the WebSocket/REST
layer and the frontend. Do not rename or remove fields; additive changes only,
and mirror any change in PROJECT_SPEC.md §5.1.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class LatticeParams(BaseModel):
    a: float
    b: float
    c: float
    alpha: float               # degrees
    beta: float                # degrees
    gamma: float               # degrees
    matrix: List[List[float]]  # 3x3, rows are the Cartesian lattice vectors a, b, c (Angstroms)
    volume: float              # Angstrom^3


class AtomSite(BaseModel):
    index: int
    element: str               # e.g., "Li", "Fe", "O"
    atomic_number: int         # Z
    frac_coords: List[float]   # [u, v, w] in [0, 1)
    cart_coords: List[float]   # [x, y, z] in Angstroms (consistent with frac_coords: r = L^T s)
    force: List[float]         # [Fx, Fy, Fz] in eV/Angstrom
    force_norm: float          # ||F|| in eV/Angstrom
    is_unstable_actor: bool = False  # True if high participation in imaginary phonon mode
    # Phase A (IMPROVEMENT_SUGGESTIONS.md §3.1): Wyckoff label "<multiplicity><letter>", e.g. "3c",
    # from the spglib dataset at the payload's symprec; None if symmetry detection failed.
    wyckoff: Optional[str] = None


class BondEdge(BaseModel):
    source_index: int
    target_index: int
    # Periodic image (n_a, n_b, n_c) of the target atom:
    # bond vector = r_target + L^T n - r_source.
    image_offset: List[int] = Field(default_factory=lambda: [0, 0, 0])
    length: float              # in Angstroms
    strain_ratio: float        # (length - r_cov_sum) / r_cov_sum for heatmap coloring
    is_unstable: bool = False


class CrystalStatePayload(BaseModel):
    formula: str
    spacegroup_symbol: str     # e.g., "Pm-3m"
    spacegroup_number: int     # e.g., 221
    lattice: LatticeParams
    atoms: List[AtomSite]
    bonds: List[BondEdge]
    energy_per_atom: float     # eV/atom from uMLIP
    max_force: float           # max ||F_i|| in eV/Angstrom
    stress_gpa: List[float]    # Voigt 6-vector [xx, yy, zz, yz, xz, xy] in GPa (ASE sign: + = tensile)
    bulk_modulus_gpa: Optional[float] = None
    e_hull_ev: Optional[float] = None
    rdf_r: List[float]
    rdf_g: List[float]
    # ---- Phase A additions (IMPROVEMENT_SUGGESTIONS.md §3.1-3.2); optional, filled by
    # UMLIPEngine._build_payload for every payload (cheap enough for per-frame use).
    pressure_gpa: Optional[float] = None       # -(s_xx + s_yy + s_zz) / 3; + = compressed cell
    density_g_cm3: Optional[float] = None      # sum of atomic masses / volume
    crystal_system: Optional[str] = None       # "triclinic" ... "cubic" from spacegroup_number
    formula_units: Optional[int] = None        # Z: atoms / atoms in the reduced formula
    symprec: Optional[float] = None            # spglib tolerance used for spacegroup / wyckoff (1e-2)
