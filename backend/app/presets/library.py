"""Preset crystal library: structure data, metadata, and loaders.

All structures are defined here as explicit lattice parameters plus fractional
coordinates (for low-symmetry cases, an asymmetric unit expanded with ASE's
space-group tables). Each structure is built once at import time; callers get
an independent ``Atoms.copy()`` on every :func:`load_preset` call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from ase import Atoms
from ase.spacegroup import crystal
from pydantic import BaseModel, ConfigDict, Field

StabilityExpectation = Literal["stable", "unstable", "unknown"]

DEFAULT_PRESET_ID = "srtio3"


class PresetInfo(BaseModel):
    """Metadata describing one curated seed crystal."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Stable identifier used by load_preset / LOAD_STRUCTURE.")
    name: str = Field(description="Human-readable display name.")
    formula: str = Field(description="Reduced chemical formula, e.g. 'SrTiO3'.")
    description: str
    spacegroup_symbol: str = Field(description="Hermann-Mauguin symbol as reported by spglib.")
    spacegroup_number: int = Field(ge=1, le=230)
    num_atoms: int = Field(ge=1, description="Atoms in the stored cell.")
    expected_stability: StabilityExpectation = Field(
        description="Expected harmonic (0 K) dynamical stability of the stored cell."
    )
    provenance: str = Field(description="Source of the structural data.")


# --------------------------------------------------------------------------- #
# Structure builders
# --------------------------------------------------------------------------- #


def _finalize(atoms: Atoms) -> Atoms:
    """Wrap fractional coordinates into [0, 1), enable PBC, drop any calculator."""
    scaled = atoms.get_scaled_positions(wrap=False)
    scaled = np.mod(scaled, 1.0)
    # Values like 1 - 1e-16 survive np.mod; snap them to 0 so the [0, 1) contract holds.
    scaled[np.isclose(scaled, 1.0, rtol=0.0, atol=1e-10)] = 0.0
    scaled[np.isclose(scaled, 0.0, rtol=0.0, atol=1e-12)] = 0.0
    atoms.set_scaled_positions(scaled)
    atoms.pbc = True
    atoms.calc = None
    return atoms


def _cubic_perovskite(a_site: str, b_site: str, a: float) -> Atoms:
    """Ideal ABO3 cubic perovskite, Pm-3m, A at the origin and B at the body centre."""
    symbols = [a_site, b_site, "O", "O", "O"]
    scaled = [
        (0.0, 0.0, 0.0),  # A, 1a
        (0.5, 0.5, 0.5),  # B, 1b
        (0.5, 0.5, 0.0),  # O, 3c
        (0.5, 0.0, 0.5),
        (0.0, 0.5, 0.5),
    ]
    return Atoms(symbols=symbols, scaled_positions=scaled, cell=np.eye(3) * a, pbc=True)


def _diamond(element: str, a: float) -> Atoms:
    """Conventional 8-atom diamond-cubic cell, Fd-3m (origin choice 1: atom at 0,0,0)."""
    fcc = np.array([(0.0, 0.0, 0.0), (0.0, 0.5, 0.5), (0.5, 0.0, 0.5), (0.5, 0.5, 0.0)])
    scaled = np.vstack([fcc, fcc + 0.25])
    return Atoms(symbols=[element] * 8, scaled_positions=scaled, cell=np.eye(3) * a, pbc=True)


# beta-Li3PS4, ordered Pnma approximant.
#
# Source: Crystallography Open Database entry 1570310 (data from K. Homma et al.,
# Solid State Ionics 182 (2011) 53-58, doi:10.1016/j.ssi.2010.10.001; neutron
# diffraction refinement of beta-Li3PS4 at 637 K). Values below are copied from
# that CIF with standard uncertainties dropped.
#
# The refined model has Li1 (8d, occ 1.0), Li2 (4b, occ 0.70) and Li3 (4c,
# occ 0.30). The ordered approximant fully occupies Li1 and Li2 and leaves Li3
# empty: 8 + 4 = 12 Li per cell, charge-balanced Li12P4S16. Because only whole
# Wyckoff orbits are kept, the Pnma symmetry of the source is preserved.
_LI3PS4_CELLPAR = (12.8190, 8.2195, 6.1236, 90.0, 90.0, 90.0)
_LI3PS4_BASIS: Sequence[tuple[str, tuple[float, float, float]]] = (
    ("Li", (0.3562, 0.013, 0.439)),  # Li1, 8d
    ("Li", (0.0, 0.0, 0.5)),  # Li2, 4b (occ 0.70 in source -> 1.0)
    # Li3, 4c (0.999, 0.25, 0.889), occ 0.30 in source -> omitted
    ("P", (0.0872, 0.25, 0.1719)),  # P1, 4c
    ("S", (0.1551, 0.0458, 0.2852)),  # S1, 8d
    ("S", (0.9346, 0.25, 0.2574)),  # S2, 4c
    ("S", (0.1068, 0.25, 0.8366)),  # S3, 4c
)


def _beta_li3ps4() -> Atoms:
    symbols = [s for s, _ in _LI3PS4_BASIS]
    basis = [xyz for _, xyz in _LI3PS4_BASIS]
    # ASE setting 1 of #62 is the standard Pnma setting, identical to the
    # symmetry operators listed in COD 1570310.
    return crystal(
        symbols=symbols,
        basis=basis,
        spacegroup=62,
        setting=1,
        cellpar=list(_LI3PS4_CELLPAR),
        primitive_cell=False,
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Preset:
    info: PresetInfo
    atoms: Atoms  # private template; never handed out directly


def _make(info: PresetInfo, atoms: Atoms) -> _Preset:
    return _Preset(info=info, atoms=_finalize(atoms))


_PRESETS: tuple[_Preset, ...] = (
    _make(
        PresetInfo(
            id="srtio3",
            name="Strontium Titanate (cubic perovskite)",
            formula="SrTiO3",
            description=(
                "Ideal cubic perovskite SrTiO3 (Pm-3m), the room-temperature phase of the "
                "archetypal incipient ferroelectric. At 0 K the cubic structure is "
                "dynamically unstable to antiferrodistortive TiO6 octahedral rotations "
                "(soft mode at the R point), which drive the 105 K transition to I4/mcm."
            ),
            spacegroup_symbol="Pm-3m",
            spacegroup_number=221,
            num_atoms=5,
            expected_stability="unstable",
            provenance=(
                "Ideal Pm-3m perovskite positions (Sr 1a, Ti 1b, O 3c) with the "
                "room-temperature experimental lattice constant a = 3.905 Å."
            ),
        ),
        _cubic_perovskite("Sr", "Ti", 3.905),
    ),
    _make(
        PresetInfo(
            id="li3ps4",
            name="β-Lithium Thiophosphate (ordered approximant)",
            formula="Li3PS4",
            description=(
                "β-Li3PS4, the orthorhombic Pnma superionic solid electrolyte built from "
                "isolated PS4 tetrahedra; the experimental phase is stable above ~573 K "
                "and has partially occupied Li sites. This 32-atom cell is a fully "
                "ordered approximant with Li on the 8d and 4b sites only."
            ),
            spacegroup_symbol="Pnma",
            spacegroup_number=62,
            num_atoms=32,
            expected_stability="unknown",
            provenance=(
                "Crystallography Open Database entry COD 1570310 (K. Homma, M. Yonemura, "
                "T. Kobayashi, M. Nagao, M. Hirayama, R. Kanno, Solid State Ionics 182 "
                "(2011) 53-58, doi:10.1016/j.ssi.2010.10.001; neutron refinement at 637 K; "
                "a = 12.8190, b = 8.2195, c = 6.1236 Å). Cell, P and S positions and the "
                "Li1 (8d) site are taken verbatim; the partially occupied Li2 (4b, occ "
                "0.70) is fully occupied and Li3 (4c, occ 0.30) is removed, which keeps "
                "Pnma symmetry and the Li12P4S16 stoichiometry. Unrelaxed."
            ),
        ),
        _beta_li3ps4(),
    ),
    _make(
        PresetInfo(
            id="si",
            name="Silicon (diamond cubic)",
            formula="Si",
            description=(
                "Diamond-cubic silicon (Fd-3m) in its 8-atom conventional cell, with each "
                "atom tetrahedrally bonded to four neighbours at 2.35 Å. It is dynamically "
                "stable and serves as the reference benchmark for the phonon pipeline."
            ),
            spacegroup_symbol="Fd-3m",
            spacegroup_number=227,
            num_atoms=8,
            expected_stability="stable",
            provenance=(
                "Ideal diamond structure (Si on 8a, origin choice 1) with the "
                "room-temperature experimental lattice constant a = 5.431 Å."
            ),
        ),
        _diamond("Si", 5.431),
    ),
    _make(
        PresetInfo(
            id="batio3",
            name="Barium Titanate (cubic perovskite)",
            formula="BaTiO3",
            description=(
                "Cubic paraelectric BaTiO3 (Pm-3m), the phase stable above ~393 K. At 0 K "
                "it is dynamically unstable to a zone-centre (Γ) polar soft mode in which "
                "Ti displaces against the O6 cage, the origin of its ferroelectric phases."
            ),
            spacegroup_symbol="Pm-3m",
            spacegroup_number=221,
            num_atoms=5,
            expected_stability="unstable",
            provenance=(
                "Ideal Pm-3m perovskite positions (Ba 1a, Ti 1b, O 3c) with a = 4.00 Å, "
                "close to the experimental high-temperature cubic lattice constant "
                "(~4.00-4.01 Å)."
            ),
        ),
        _cubic_perovskite("Ba", "Ti", 4.00),
    ),
)

_BY_ID: dict[str, _Preset] = {p.info.id: p for p in _PRESETS}

assert DEFAULT_PRESET_ID in _BY_ID


def list_presets() -> list[PresetInfo]:
    """Return metadata for every preset, in spec order (srtio3, li3ps4, si, batio3)."""
    return [p.info for p in _PRESETS]


def load_preset(preset_id: str) -> Atoms:
    """Return a fresh, independent copy of the preset structure.

    The returned ``Atoms`` has ``pbc=True``, no calculator, and fractional
    coordinates in [0, 1). Raises ``KeyError`` for an unknown ``preset_id``.
    """
    try:
        preset = _BY_ID[preset_id]
    except KeyError:
        raise KeyError(
            f"Unknown preset id {preset_id!r}; available: {sorted(_BY_ID)}"
        ) from None
    atoms = preset.atoms.copy()
    atoms.calc = None
    return atoms
