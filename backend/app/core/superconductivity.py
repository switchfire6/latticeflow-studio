"""Superconductivity screening, Tier 0 (docs/design/SUPERCONDUCTIVITY_TIER0.md).

A **proxy**, not a prediction. The uMLIP has no electrons, so the electron–phonon
coupling λ is not computed: the descriptors below are the phonon energy scale of a
constant-coupling proxy α²F(ω) ∝ F(ω), and T_c is Allen–Dynes evaluated at *assumed*
λ and μ* values. ``tc_ref_k`` (λ = 1.0, μ* = 0.10) only orders candidates by that scale.

* :func:`eph_proxy_descriptors` — ω_log, ω̄₂, ⟨ω⟩, ω_max, light-element share and the
  λ × μ* T_c grid from a phonon DOS (only f > ``F_CUT``; trapezoid integration).
* :func:`allen_dynes_tc` — Allen–Dynes with the f1 / f2 strong-coupling and shape factors.
* :func:`sc_flags` — ``light_elements``, ``high_omega_log``, ``soft_mode``, ``unstable``,
  ``insulating``.
* :func:`assess_metallicity` — Materials Project (best ``StructureMatcher`` match, GGA gap),
  then a composition heuristic. Never raises for MP errors.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.schemas.api import MetallicityResponse
from app.schemas.messages import F_TOL_THZ, EphProxyDescriptors, PhononDosData, TcGridPoint

log = logging.getLogger("latticeflow.superconductivity")

__all__ = [
    "THZ_TO_K", "F_CUT", "LAMBDA_GRID", "MU_STAR_GRID", "LIGHT_ELEMENTS", "REF_LAMBDA", "REF_MU_STAR",
    "allen_dynes_tc", "eph_proxy_descriptors", "descriptors_from_dos", "sc_flags", "assess_metallicity",
    "heuristic_metallicity", "mp_client_or_reason", "gate_pass",
]

THZ_TO_K = 47.99243                      # h / k_B in K per THz
F_CUT = F_TOL_THZ                        # only f > F_CUT enters the integrals
LAMBDA_GRID = (0.3, 0.5, 0.75, 1.0, 1.5, 2.0)
MU_STAR_GRID = (0.10, 0.13)
REF_LAMBDA, REF_MU_STAR = 1.0, 0.10
LIGHT_ELEMENTS = frozenset({"H", "Li", "Be", "B", "C", "N"})
MAX_IMAGINARY_FRACTION = 0.01
OMEGA_MAX_REL = 1e-3                     # ω_max: highest f with F(f) > 1e-3 · max F
LIGHT_FLAG_FRACTION = 0.05
HIGH_OMEGA_LOG_K = 400.0
SOFT_MODE_Q = 0.1                        # |q_frac| above which a low branch counts as a soft mode
SOFT_MODE_REL = 0.1                      # lowest branch < 0.1 · ω_max
METHOD = ("constant-coupling proxy α²F(ω) ∝ F(ω) (no electrons, λ assumed); Allen–Dynes T_c on an assumed "
          "λ × μ* grid; T_c,ref (λ = 1.0, μ* = 0.10) is a ranking scale, not a prediction")
INSULATING = ("insulator", "likely_insulator")
_HEURISTIC_NONMETALS = frozenset({"B", "C", "N", "H", "Si", "P", "As"})

_trapz = getattr(np, "trapezoid", None) or np.trapz  # numpy >= 2 / older


# --------------------------------------------------------------------------- Allen–Dynes
def allen_dynes_tc(lam: float, mu_star: float, omega_log_k: float, omega_2_k: Optional[float] = None) -> float:
    """Allen–Dynes T_c (K) at an assumed λ, μ*; 0 when λ − μ*(1 + 0.62 λ) ≤ 0 or ω_log ≤ 0."""
    lam, mu = float(lam), float(mu_star)
    w_log = float(omega_log_k)
    if not (math.isfinite(w_log) and w_log > 0):
        return 0.0
    den = lam - mu * (1.0 + 0.62 * lam)
    if den <= 0:
        return 0.0
    ratio = float(omega_2_k) / w_log if omega_2_k is not None and omega_2_k > 0 else 1.0
    l1 = 2.46 * (1.0 + 3.8 * mu)
    l2 = 1.82 * (1.0 + 6.3 * mu) * ratio
    f1 = (1.0 + (lam / l1) ** 1.5) ** (1.0 / 3.0)
    f2 = 1.0 + (ratio - 1.0) * lam ** 2 / (lam ** 2 + l2 ** 2)
    return float(f1 * f2 * (w_log / 1.2) * math.exp(-1.04 * (1.0 + lam) / den))


# --------------------------------------------------------------------------- descriptors
def eph_proxy_descriptors(frequencies_thz: Sequence[float], total_dos: Sequence[float],
                          projected_dos: Optional[Dict[str, Sequence[float]]] = None,
                          imaginary_fraction: float = 0.0) -> EphProxyDescriptors:
    """Tier 0 descriptors of one phonon DOS (states / THz / cell on a signed THz grid)."""
    f = np.asarray(frequencies_thz, dtype=float)
    F = np.clip(np.nan_to_num(np.asarray(total_dos, dtype=float)), 0.0, None)
    if f.shape != F.shape:
        raise ValueError(f"frequencies ({f.shape}) and total_dos ({F.shape}) differ in shape")
    sel = f > F_CUT
    w, Fw = f[sel], F[sel]
    warnings: List[str] = []
    omega_log = omega_2 = omega_mean = omega_max = None
    light = 0.0
    norm = float(_trapz(Fw, w)) if len(w) >= 2 else 0.0
    if len(w) >= 2 and norm > 0:
        weight = Fw / w                                         # F(ω) / ω
        wnorm = float(_trapz(weight, w))
        omega_log = THZ_TO_K * math.exp(float(_trapz(weight * np.log(w), w)) / wnorm)
        omega_2 = THZ_TO_K * math.sqrt(float(_trapz(Fw * w, w)) / wnorm)
        omega_mean = THZ_TO_K * float(_trapz(Fw * w, w)) / norm
        above = np.nonzero(Fw > OMEGA_MAX_REL * float(Fw.max()))[0]
        omega_max = THZ_TO_K * float(w[above[-1]]) if len(above) else None
        light_int = 0.0
        for sym, pdos in (projected_dos or {}).items():
            if sym in LIGHT_ELEMENTS:
                p = np.clip(np.nan_to_num(np.asarray(pdos, dtype=float)), 0.0, None)
                if p.shape == F.shape:
                    light_int += float(_trapz(p[sel], w))
        light = float(min(1.0, max(0.0, light_int / norm)))
    else:
        warnings.append(f"no phonon density of states above {F_CUT} THz; the descriptors are undefined")
    imag = float(imaginary_fraction or 0.0)
    if imag > MAX_IMAGINARY_FRACTION:
        warnings.append(f"{100 * imag:.1f}% of the DOS mesh modes are imaginary (> {100 * MAX_IMAGINARY_FRACTION:.0f}%): "
                        "the structure is dynamically unstable, so the proxies are not reliable")
    reliable = omega_log is not None and imag <= MAX_IMAGINARY_FRACTION
    grid: List[TcGridPoint] = []
    if omega_log is not None:
        grid = [TcGridPoint(lam=lam, mu_star=mu, tc_k=allen_dynes_tc(lam, mu, omega_log, omega_2))
                for lam in LAMBDA_GRID for mu in MU_STAR_GRID]
    tc_ref = allen_dynes_tc(REF_LAMBDA, REF_MU_STAR, omega_log, omega_2) if reliable else None
    return EphProxyDescriptors(
        omega_log_k=omega_log, omega_2_k=omega_2, omega_mean_k=omega_mean, omega_max_k=omega_max,
        light_element_fraction=light, tc_grid=grid, tc_ref_k=tc_ref, reliable=bool(reliable),
        warnings=warnings, method=METHOD,
    )


def descriptors_from_dos(dos: PhononDosData) -> EphProxyDescriptors:
    return eph_proxy_descriptors(dos.frequencies_thz, dos.total_dos, dos.projected_dos, dos.imaginary_fraction)


# --------------------------------------------------------------------------- flags
def sc_flags(desc: EphProxyDescriptors, verdict: Optional[str], *, dynamically_stable: bool,
             band_frequencies_thz: Any = None, band_qpoints: Any = None,
             labelled_indices: Any = None) -> List[str]:
    """Screening flags in a fixed order (see the module docstring).

    ``soft_mode`` is checked only at the labelled high-symmetry q-points other than Γ
    (``labelled_indices``; every q-point if it is None). Checking every q-point with |q| > 0.1
    flags ordinary acoustic branches near Γ in stable crystals (MgO, Al).
    """
    flags: List[str] = []
    if desc.light_element_fraction >= LIGHT_FLAG_FRACTION:
        flags.append("light_elements")
    if desc.omega_log_k is not None and desc.omega_log_k >= HIGH_OMEGA_LOG_K:
        flags.append("high_omega_log")
    if (dynamically_stable and desc.omega_max_k and band_frequencies_thz is not None
            and band_qpoints is not None):
        freqs = np.asarray(band_frequencies_thz, dtype=float)
        qs = np.asarray(band_qpoints, dtype=float)
        if freqs.ndim == 2 and len(freqs) == len(qs) and len(qs):
            far = np.linalg.norm(qs, axis=1) > SOFT_MODE_Q
            if labelled_indices is not None:
                keep = np.zeros(len(qs), dtype=bool)
                keep[[i for i in labelled_indices if 0 <= int(i) < len(qs)]] = True
                far &= keep
            limit = SOFT_MODE_REL * desc.omega_max_k / THZ_TO_K
            if np.any(far) and bool(np.any(freqs[far].min(axis=1) < limit)):
                flags.append("soft_mode")
    if not desc.reliable:
        flags.append("unstable")
    if verdict in INSULATING:
        flags.append("insulating")
    return flags


def gate_pass(reliable: bool, verdict: Optional[str]) -> bool:
    return bool(reliable) and verdict not in INSULATING


# --------------------------------------------------------------------------- metallicity
def heuristic_metallicity(formula: str, prefix: str = "") -> MetallicityResponse:
    """Composition heuristic on the reduced formula (contract §3 step 2)."""
    try:
        from pymatgen.core import Composition

        comp = Composition(formula).reduced_composition
        elements = list(comp.elements)
        if not elements or any(not getattr(el, "Z", 0) for el in elements):
            raise ValueError("no elements")
        reduced = comp.reduced_formula
        nonmetals = [el.symbol for el in elements if not el.is_metal]
        has_tm = any(el.is_transition_metal or el.is_lanthanoid or el.is_actinoid for el in elements)
    except Exception as exc:  # noqa: BLE001
        return MetallicityResponse(verdict="unknown", source="heuristic",
                                   reason=f"{prefix}the composition {formula!r} cannot be parsed ({type(exc).__name__}).")

    def resp(verdict: str, why: str) -> MetallicityResponse:
        return MetallicityResponse(verdict=verdict, source="heuristic",
                                   reason=f"{prefix}composition heuristic: {reduced} {why}.")

    if not nonmetals:
        return resp("likely_metal", "is a metallic element or an intermetallic (every element is a metal)")
    if len(elements) == 1:
        return resp("likely_insulator", "is a non-metallic element (semiconductor or insulator, e.g. Si, Ge, "
                                        "diamond; semimetals such as graphite are misclassified)")
    if has_tm and set(nonmetals) <= _HEURISTIC_NONMETALS:
        return resp("likely_metal", "has a transition metal / lanthanide / actinide and only "
                                    f"{', '.join(sorted(nonmetals))} as non-metals (carbide / nitride / boride / "
                                    "hydride / silicide-like)")
    try:
        guesses = comp.oxi_state_guesses()
    except Exception:  # noqa: BLE001
        guesses = ()
    if guesses:
        states = ", ".join(f"{el}{v:+g}" for el, v in guesses[0].items())
        return resp("likely_insulator", f"can be charge-balanced ({states}), as ionic insulators are")
    return resp("likely_metal", "cannot be charge-balanced with common oxidation states")


def mp_client_or_reason(api_key: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """(MaterialsProjectClient, None) or (None, why there is no client)."""
    from app.core import mp_client

    try:
        return mp_client.MaterialsProjectClient(api_key), None
    except mp_client.MPNoApiKeyError as exc:
        return None, f"no Materials Project lookup ({exc})"


def assess_metallicity(atoms: Any, client: Optional[Any], use_mp: bool = True, *,
                       unavailable_reason: Optional[str] = None) -> MetallicityResponse:
    """Materials Project gap of the best match, else the composition heuristic. Never raises for MP errors.

    ``unavailable_reason`` explains a missing ``client`` (e.g. no API key) in the heuristic's reason.
    """
    try:
        formula = atoms.get_chemical_formula(mode="metal") if hasattr(atoms, "get_chemical_formula") else str(atoms)
    except Exception:  # noqa: BLE001
        formula = ""
    prefix = ""
    if use_mp and client is not None:
        try:
            from app.core import compare

            res = compare.match_mp(atoms, client)
            if res.matches:
                best = res.matches[0]
                e = best.entry
                gap = e.band_gap
                common = dict(source="materials_project", band_gap_ev=gap, mp_id=e.material_id,
                              rms_displacement=best.rms_displacement)
                gap_txt = f"{gap:.2f} eV" if gap is not None else "n/a"
                if e.is_metal or (gap is not None and gap <= 0.0):
                    return MetallicityResponse(verdict="metal", reason=(
                        f"Materials Project {e.material_id} (best structure match) is metallic in GGA/GGA+U "
                        f"(gap {gap_txt}); GGA gaps are underestimated, so a small-gap material may show as a metal."),
                        **common)
                if gap is not None and gap > 0.0:
                    return MetallicityResponse(verdict="insulator", reason=(
                        f"Materials Project {e.material_id} (best structure match) has a GGA/GGA+U gap of "
                        f"{gap_txt}; GGA underestimates gaps, so the true gap is likely larger."), **common)
                prefix = f"Materials Project {e.material_id} has no band-gap data; "
            else:
                prefix = (f"no Materials Project entry matches the structure ({res.n_searched} of formula "
                          f"{res.formula} searched); ")
        except Exception as exc:  # noqa: BLE001 - MP errors never fail the gate
            log.warning("metallicity: Materials Project lookup failed: %s", exc)
            prefix = f"Materials Project lookup failed ({type(exc).__name__}: {str(exc)[:200]}); "
    elif use_mp:
        prefix = f"{unavailable_reason or 'no Materials Project client'}; "
    else:
        prefix = "Materials Project lookup disabled; "
    return heuristic_metallicity(formula, prefix)
