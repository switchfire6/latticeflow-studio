"""Simulated powder diffraction (IMPROVEMENT_SUGGESTIONS.md §5.6, ``POST /api/diffraction``).

pymatgen ``XRDCalculator`` (X-ray, atomic scattering factors) or ``NDCalculator`` (neutron,
coherent scattering lengths) on the structure, with ``symprec = DIFFRACTION_SYMPREC``: the
structure is refined to its conventional standard cell first, so hkl are textbook indices
(e.g. Si (111), SrTiO3 (110)) whatever cell is displayed. Intensities are scaled so the
strongest peak in the requested 2θ range is 100. Hexagonal 4-index hkl (h k i l) are reduced
to (h k l). No Debye-Waller factors (static lattice).

Wavelength: ``wavelength_angstrom`` if given (``radiation`` reported as None), otherwise the
pymatgen table value of ``radiation`` (CuKa = 1.54184 Å, a weighted Kα1/Kα2 mean; MoKa, CrKa,
FeKa, CoKa, AgKa and their Kα1 / Kα2 / Kβ1 lines; keys case-insensitive). The neutron
calculator uses the same table to turn a radiation key into a wavelength.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ase import Atoms

from app.schemas.api import DiffractionHkl, DiffractionPeak, DiffractionResponse

DIFFRACTION_SYMPREC = 1e-2


class DiffractionError(ValueError):
    """Invalid diffraction request (unknown radiation, empty 2theta range, ...)."""


def resolve_wavelength(radiation: str, wavelength_angstrom: Optional[float]) -> Tuple[Optional[str], float]:
    """(radiation key or None, wavelength in Angstrom)."""
    if wavelength_angstrom is not None:
        wl = float(wavelength_angstrom)
        if not 0 < wl <= 5:
            raise DiffractionError("wavelength_angstrom must be in (0, 5]")
        return None, wl
    from pymatgen.analysis.diffraction.xrd import WAVELENGTHS

    key = str(radiation or "").strip()
    match = next((k for k in WAVELENGTHS if k.lower() == key.lower()), None)
    if match is None:
        raise DiffractionError(f"unknown radiation {radiation!r}; choose one of {', '.join(WAVELENGTHS)}")
    return match, float(WAVELENGTHS[match])


def compute_pattern(
    atoms: Atoms,
    *,
    kind: str = "xray",
    radiation: str = "CuKa",
    wavelength_angstrom: Optional[float] = None,
    two_theta_min: float = 5.0,
    two_theta_max: float = 90.0,
) -> DiffractionResponse:
    """Powder pattern peaks of ``atoms`` (not mutated). Raises :class:`DiffractionError`."""
    from pymatgen.io.ase import AseAtomsAdaptor

    if kind not in ("xray", "neutron"):
        raise DiffractionError(f"kind must be 'xray' or 'neutron', got {kind!r}")
    lo, hi = float(two_theta_min), float(two_theta_max)
    if not (0 <= lo < hi <= 180):
        raise DiffractionError(f"need 0 <= two_theta_min < two_theta_max <= 180 (got {lo}, {hi})")
    if len(atoms) == 0:
        raise DiffractionError("structure has no atoms")
    rad, wl = resolve_wavelength(radiation, wavelength_angstrom)

    plain = Atoms(numbers=atoms.get_atomic_numbers(), positions=atoms.positions, cell=atoms.cell, pbc=True)
    structure = AseAtomsAdaptor.get_structure(plain)
    if kind == "xray":
        from pymatgen.analysis.diffraction.xrd import XRDCalculator

        calc = XRDCalculator(wavelength=rad if rad is not None else wl, symprec=DIFFRACTION_SYMPREC)
    else:
        from pymatgen.analysis.diffraction.neutron import NDCalculator

        calc = NDCalculator(wavelength=wl, symprec=DIFFRACTION_SYMPREC)
    pattern = calc.get_pattern(structure, scaled=True, two_theta_range=(lo, hi))

    peaks = []
    for x, y, fams, d in zip(pattern.x, pattern.y, pattern.hkls, pattern.d_hkls):
        hkls = []
        for fam in fams:
            hkl = [int(v) for v in fam["hkl"]]
            if len(hkl) == 4:
                hkl = [hkl[0], hkl[1], hkl[3]]
            hkls.append(DiffractionHkl(hkl=hkl, multiplicity=int(fam["multiplicity"])))
        peaks.append(DiffractionPeak(two_theta=round(float(x), 4), intensity=round(float(y), 4),
                                     d_spacing=round(float(d), 5), hkls=hkls))
    return DiffractionResponse(kind=kind, radiation=rad, wavelength_angstrom=wl, peaks=peaks)
