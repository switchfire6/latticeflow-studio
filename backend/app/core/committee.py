"""Model committee: the same structure through several uMLIPs (IMPROVEMENT_SUGGESTIONS.md §6.2).

``evaluate(atoms, model_ids, ...)`` loads each model with ``umlip_engine.get_engine``
(:class:`~app.core.umlip_engine.ModelUnavailableError` propagates), evaluates a
single point of the structure as given (pressure, max force) and, with
``relax``, relaxes a private copy (positions + cell, FIRE, fmax 0.02 eV/A,
<= ``RELAX_MAX_STEPS`` steps) for the relaxed volume, lattice, space group and
energy. ``atoms`` is never mutated.

Spreads (max - min over the models): ``pressure_gpa``, ``max_force`` (eV/A),
and with ``relax`` ``volume_percent`` (of the mean relaxed volume per atom)
and ``lattice_percent`` (largest of the a, b, c spreads, percent of each mean).
Absolute energies are deliberately not compared: each model has its own
energy reference, so an energy spread would be meaningless.

``progress(done, total, message)`` is called before each model (done = models
finished so far) and once at the end (total, total). ``should_cancel()`` is
polled between models and every relaxation step; a cancel raises
:class:`CommitteeCancelled`.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
from ase import Atoms

from app.core import relaxer
from app.core.umlip_engine import EV_PER_A3_TO_GPA, get_engine
from app.schemas.messages import CommitteeEntry, CommitteeResult

__all__ = ["evaluate", "CommitteeCancelled", "RELAX_FMAX", "RELAX_MAX_STEPS"]

RELAX_FMAX = 0.02
RELAX_MAX_STEPS = 200


class CommitteeCancelled(Exception):
    """``should_cancel()`` returned True."""


def _spread(values: Sequence[float]) -> float:
    return float(max(values) - min(values))


def _percent_spread(values: Sequence[float]) -> float:
    mean = float(np.mean(values))
    return float(_spread(values) / mean * 100.0) if mean else 0.0


def evaluate(
    atoms: Atoms,
    model_ids: Sequence[str],
    *,
    relax: bool = True,
    dispersion: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> CommitteeResult:
    def check() -> None:
        if should_cancel is not None and should_cancel():
            raise CommitteeCancelled()

    def report(done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)

    ids = list(dict.fromkeys(str(m).strip() for m in model_ids))  # unique, order kept
    if len(ids) < 2:
        raise ValueError("a committee needs at least two distinct models")
    periodic = bool(np.all(atoms.pbc)) and atoms.cell.rank == 3
    n = len(ids)
    entries: List[CommitteeEntry] = []
    notes: List[str] = []
    for i, model_id in enumerate(ids):
        check()
        report(i, n, f"Evaluating {model_id}{' + D3' if dispersion else ''} ({i + 1}/{n})")
        engine = get_engine(model_id, dispersion)
        check()
        _, forces, stress = engine.energy_forces_stress(atoms)
        pressure = float(-np.mean(stress[:3]) * EV_PER_A3_TO_GPA) if periodic else 0.0
        entry = CommitteeEntry(
            model_id=model_id, backend_name=engine.backend_name, pressure_gpa=pressure,
            max_force=float(np.linalg.norm(forces, axis=1).max()),
        )
        if relax:
            work = atoms.copy()
            work.calc = None
            last = None
            try:
                for frame in relaxer.iter_relaxation(
                    work, engine, fmax=RELAX_FMAX, max_steps=RELAX_MAX_STEPS, relax_cell=periodic,
                    should_cancel=should_cancel,
                ):
                    last = frame
            except relaxer.RelaxationDivergedError as exc:
                notes.append(f"{model_id}: relaxation diverged ({exc})")
                last = None
            check()
            if last is not None:
                c = last.crystal
                entry.relaxed_volume_per_atom = float(c.lattice.volume / len(c.atoms))
                entry.relaxed_lattice_abc = [float(c.lattice.a), float(c.lattice.b), float(c.lattice.c)]
                entry.relaxed_spacegroup = c.spacegroup_symbol
                entry.relaxed_energy_per_atom = float(c.energy_per_atom)
                if not last.converged:
                    notes.append(f"{model_id}: not converged in {RELAX_MAX_STEPS} steps")
        entries.append(entry)
    report(n, n, "Committee complete")

    spread: Dict[str, float] = {
        "pressure_gpa": _spread([e.pressure_gpa for e in entries]),
        "max_force": _spread([e.max_force for e in entries]),
    }
    relaxed = [e for e in entries if e.relaxed_volume_per_atom is not None]
    if relax and periodic and len(relaxed) >= 2:
        spread["volume_percent"] = _percent_spread([e.relaxed_volume_per_atom for e in relaxed])
        abc = np.array([e.relaxed_lattice_abc for e in relaxed], dtype=float)
        spread["lattice_percent"] = float(max(_percent_spread(abc[:, k]) for k in range(3)))
    method = (
        f"{n} models ({', '.join(ids)}{', + D3' if dispersion else ''}); single point of the structure as given"
        + (f"; FIRE relaxation of positions + cell (fmax {RELAX_FMAX} eV/A, <= {RELAX_MAX_STEPS} steps) "
           "per model" if relax else "")
        + "; spread = max - min (volume / lattice in percent of the mean). Absolute energies are not "
        "compared: each model has its own energy reference."
        + (" Notes: " + "; ".join(notes) if notes else "")
    )
    return CommitteeResult(entries=entries, spread=spread, method=method)
