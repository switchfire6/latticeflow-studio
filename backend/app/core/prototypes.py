"""Prototype substitution search (IMPROVEMENT_SUGGESTIONS.md §7.5, ``PROTOTYPE_SEARCH``).

Known structure types are often the most productive route to a new compound: take
the Materials Project structures of chemically similar compositions and swap the
ions. :func:`search` does this in the session's uMLIP:

1. **Oxidation states.** The target composition's states are guessed with pymatgen
   ``Composition.oxi_state_guesses`` (first, most probable guess; non-integer
   (mixed-valence) guesses are rejected).
2. **Sources.** pymatgen ``SubstitutionPredictor(threshold=probability_threshold)
   .composition_prediction(target, to_this_composition=True)`` (the Hautier et al.
   ionic-substitution model) lists source compositions with the probability that
   their structures carry over to the target. Predictions with the same element
   mapping (they differ only in source oxidation states) are merged (max
   probability). Mappings are visited by decreasing probability, at most
   ``MAX_SOURCE_FORMULAS`` (12) of them, and stop early once
   ``2 x max_candidates`` distinct substituted structures are collected.
3. **Fetch.** For each source formula, ``client.search(formula)`` (MP, sorted by MP
   E_hull) and ``client.get_structure`` for the ``ENTRIES_PER_FORMULA`` (2) lowest-E_hull
   entries with at most ``MAX_SOURCE_SITES`` (40) sites. Unknown ids are skipped.
4. **Substitute + scale.** Every site's element is replaced by the mapping and the
   cell is scaled isotropically by sum r_target^3 / sum r_source^3 over the sites,
   with r the Shannon ionic radius of the species at its (target / source) oxidation
   state (``Species.ionic_radius``), falling back to the element's atomic radius.
   (A simple ionic-radius scaling, not pymatgen's ``DLSVolumePredictor``: it needs no
   bond-length table and the relaxation fixes the volume anyway.)
5. **Dedupe** (``StructureMatcher``, default tolerances) the substituted structures,
   keeping the most probable source of each.
6. **Relax** (``relax=True``): FIRE + ``FrechetCellFilter`` (positions + cell),
   ``fmax`` 0.05 eV/Å, at most ``hull_analyzer.RELAX_MAX_STEPS`` (150) steps, i.e. the
   hull reference settings. A diverging relaxation keeps the unrelaxed structure
   (``relaxed=False``).
7. **E_hull** (``with_e_hull=True``): against the self-consistent uMLIP hull of the
   target chemical system (``hull_analyzer.get_reference_set``, cached per backend and
   chemsys, so only the first search in a chemical system relaxes references).
8. **Rank.** Dedupe again after relaxation, sort by E_hull (unknown last), then
   energy per atom, cap at ``max_candidates``.

``PrototypeCandidate.substitutions`` lists only the species that actually change
(``{}`` when the source is the target formula itself, i.e. a known MP phase of it).
``n_sources`` counts the MP structures fetched and substituted (before deduplication).

The MP client is injected (``client``), and the hull ``fetcher`` / ``cache`` too, so
tests run without network. ``progress(done, total, message)`` and ``should_cancel()``
(raises :class:`PrototypeCancelled`) are polled per fetch, per relaxation step and
per hull reference.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from ase import Atoms

from app.core import hull_analyzer, mp_client
from app.schemas.messages import PrototypeCandidate, PrototypeCandidates, PrototypeSearchPayload

logger = logging.getLogger(__name__)

__all__ = ["PrototypeCancelled", "search", "MAX_SOURCE_FORMULAS", "ENTRIES_PER_FORMULA", "MAX_SOURCE_SITES"]

MAX_SOURCE_FORMULAS = 12
ENTRIES_PER_FORMULA = 2
MAX_SOURCE_SITES = 40
RELAX_FMAX = 0.05


class PrototypeCancelled(RuntimeError):
    pass


def _target_species(composition: Dict[str, int]):
    from pymatgen.core import Composition, Element, Species

    comp: Dict[str, float] = {}
    for el, n in composition.items():
        sym = str(el).strip().capitalize()
        try:
            Element(sym)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown element {el!r}") from exc
        if int(n) < 0:
            raise ValueError(f"negative count for {sym}")
        if int(n) > 0:
            comp[sym] = comp.get(sym, 0) + int(n)
    if not comp:
        raise ValueError("composition is empty")
    target = Composition(comp)
    guesses = target.oxi_state_guesses()
    if not guesses:
        raise ValueError(f"no charge-balanced oxidation states for {target.reduced_formula}; "
                         "prototype substitution needs an ionic composition")
    states: Dict[str, int] = {}
    for el, q in guesses[0].items():
        if abs(q - round(q)) > 1e-6:
            raise ValueError(f"mixed-valence oxidation state guess for {el} ({q:g}) is not supported")
        states[el] = int(round(q))
    species = Composition({Species(el.symbol, states[el.symbol]): amt for el, amt in target.items()})
    return target, species, states


def _radius(symbol: str, oxi: Optional[float]) -> float:
    from pymatgen.core import Element, Species

    r = None
    if oxi is not None:
        try:
            r = Species(symbol, oxi).ionic_radius
        except Exception:  # noqa: BLE001
            r = None
    if r is None:
        el = Element(symbol)
        r = el.atomic_radius or el.average_ionic_radius or 1.5
    return float(r)


def _predictions(species_comp, threshold: float) -> List[Tuple[float, Dict[str, str], Dict[str, Tuple[str, float, float]]]]:
    """[(probability, {source el: target el}, {source el: (target el, source oxi, target oxi)})], best first."""
    from pymatgen.analysis.structure_prediction.substitution_probability import SubstitutionPredictor

    raw = SubstitutionPredictor(threshold=float(threshold)).composition_prediction(
        species_comp, to_this_composition=True)
    merged: Dict[Tuple[Tuple[str, str], ...], Tuple[float, Dict[str, Tuple[str, float, float]]]] = {}
    for pred in raw:
        subs = pred["substitutions"]
        detail = {src.symbol: (tgt.symbol, float(src.oxi_state), float(tgt.oxi_state)) for src, tgt in subs.items()}
        if len(detail) != len(subs) or len({v[0] for v in detail.values()}) != len(detail):
            continue                      # two source species of one element / not one-to-one
        key = tuple(sorted((s, v[0]) for s, v in detail.items()))
        p = float(pred["probability"])
        if key not in merged or p > merged[key][0]:
            merged[key] = (p, detail)
    out = [(p, dict(key), detail) for key, (p, detail) in merged.items()]
    out.sort(key=lambda x: (-x[0], sorted(x[1].items())))
    return out


def _source_formula(target, mapping: Dict[str, str]) -> str:
    from pymatgen.core import Composition

    inverse = {t: s for s, t in mapping.items()}
    return Composition({inverse[el.symbol]: amt for el, amt in target.items()}).reduced_formula


def _substitute(structure, detail: Dict[str, Tuple[str, float, float]]):
    from pymatgen.core import Structure

    species, r_src, r_tgt = [], 0.0, 0.0
    for site in structure:
        sym = site.specie.symbol
        tgt, q_src, q_tgt = detail[sym]
        species.append(tgt)
        r_src += _radius(sym, q_src) ** 3
        r_tgt += _radius(tgt, q_tgt) ** 3
    new = Structure(structure.lattice, species, structure.frac_coords)
    if r_src > 0 and r_tgt > 0:
        new.scale_lattice(structure.volume * r_tgt / r_src)
    return new


def _to_atoms(structure) -> Atoms:
    return Atoms(numbers=[site.specie.Z for site in structure], cell=np.array(structure.lattice.matrix, dtype=float),
                 positions=np.array(structure.cart_coords, dtype=float), pbc=True)


def search(
    request: PrototypeSearchPayload,
    engine,
    *,
    client: mp_client.MPClient,
    api_key: Optional[str] = None,
    fetcher: Optional[hull_analyzer.Fetcher] = None,
    cache: Optional[hull_analyzer.HullCache] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> PrototypeCandidates:
    """Prototype substitution candidates for ``request.composition`` (see module docstring).

    Raises ``ValueError`` (bad / non-ionic composition, nothing found), ``mp_client.MPError``
    subclasses from the client, ``hull_analyzer.HullError`` subclasses from the hull, and
    :class:`PrototypeCancelled`.
    """
    from pymatgen.analysis.structure_matcher import StructureMatcher
    from pymatgen.io.ase import AseAtomsAdaptor

    from app.core.relaxer import RelaxationDivergedError, iter_relaxation
    from app.core.structure_io import max_atoms as _studio_max_atoms

    def check() -> None:
        if should_cancel is not None and should_cancel():
            raise PrototypeCancelled("prototype search cancelled")

    def report(done: int, total: int, message: str) -> None:
        if progress is not None:
            try:
                progress(done, total, message)
            except Exception:  # noqa: BLE001
                pass

    target, species_comp, _ = _target_species(request.composition)
    target_formula = target.reduced_formula
    preds = _predictions(species_comp, request.probability_threshold)
    if not preds:
        raise ValueError(f"SubstitutionPredictor found no source compositions for {target_formula} "
                         f"above probability {request.probability_threshold:g}")

    matcher = StructureMatcher()
    wanted = 2 * int(request.max_candidates)
    # (structure, probability, source id, source formula, element mapping)
    pool: List[Tuple[Any, float, str, str, Dict[str, str]]] = []
    n_sources = 0
    n_formulas = 0
    visited = preds[:MAX_SOURCE_FORMULAS]
    for i, (prob, mapping, detail) in enumerate(visited):
        if len(pool) >= wanted:
            break
        check()
        n_formulas += 1
        formula = _source_formula(target, mapping)
        report(i, len(visited), f"Fetching Materials Project structures of {formula} ({i + 1}/{len(visited)})")
        entries = [e for e in client.search(formula) if 0 < e.nsites <= MAX_SOURCE_SITES]
        for entry in entries[:ENTRIES_PER_FORMULA]:
            check()
            try:
                _, structure = client.get_structure(entry.material_id)
            except mp_client.MPNotFoundError:
                continue
            if not structure.is_ordered or structure.composition.reduced_formula != formula:
                continue
            n_sources += 1
            try:
                new = _substitute(structure, detail)
            except KeyError:
                continue
            if new.composition.reduced_formula != target_formula or len(new) > _studio_max_atoms():
                continue
            if any(matcher.fit(new, other[0]) for other in pool):
                continue
            pool.append((new, prob, entry.material_id, formula, mapping))
    if not pool:
        raise ValueError(f"no Materials Project prototype structures found for {target_formula} "
                         f"({len(visited)} source compositions tried)")

    refset = None
    if request.with_e_hull:
        check()
        report(0, 0, f"Building the {engine.backend_name} reference hull of {hull_analyzer.chemsys_of([e.symbol for e in target.elements])}")
        refset = hull_analyzer.get_reference_set(
            [e.symbol for e in target.elements], engine, api_key=api_key, fetcher=fetcher, cache=cache,
            should_cancel=should_cancel, progress=progress,
        )
    pd = refset.phase_diagram() if refset is not None else None
    e_hull_ref = float(pd.get_decomp_and_hull_energy_per_atom(target)[1]) if pd is not None else None

    results: List[Tuple[PrototypeCandidate, Any]] = []
    for k, (structure, prob, mid, formula, mapping) in enumerate(pool):
        check()
        report(k, len(pool), f"{'Relaxing' if request.relax else 'Evaluating'} {target_formula} from {mid} "
                             f"({formula}) {k + 1}/{len(pool)}")
        atoms = _to_atoms(structure)
        relaxed = False
        if request.relax:
            work = atoms.copy()
            last = None
            try:
                for frame in iter_relaxation(work, engine, fmax=RELAX_FMAX, max_steps=hull_analyzer.RELAX_MAX_STEPS,
                                             relax_cell=True, should_cancel=should_cancel):
                    last = frame
                check()
                if last is not None and np.isfinite(last.crystal.energy_per_atom):
                    atoms, relaxed = work, True
            except RelaxationDivergedError as exc:
                logger.warning("prototype %s from %s: relaxation diverged (%s); keeping it unrelaxed", target_formula, mid, exc)
        check()
        payload = engine.evaluate(atoms, bond_mode="fast")
        e = float(payload.energy_per_atom)
        if not np.isfinite(e):
            continue
        e_hull = e - e_hull_ref if e_hull_ref is not None else None
        if e_hull is not None:
            payload.e_hull_ev = e_hull
        cand = PrototypeCandidate(
            rank=0, formula=target_formula, source_material_id=mid, source_formula=formula,
            substitutions={s: t for s, t in mapping.items() if s != t}, probability=prob,
            spacegroup_symbol=payload.spacegroup_symbol, energy_per_atom=e, e_hull_ev=e_hull,
            relaxed=relaxed, crystal=payload,
        )
        results.append((cand, AseAtomsAdaptor.get_structure(atoms)))

    results.sort(key=lambda x: (x[0].e_hull_ev is None, x[0].e_hull_ev if x[0].e_hull_ev is not None else 0.0,
                                x[0].energy_per_atom))
    final: List[PrototypeCandidate] = []
    kept_structs: List[Any] = []
    for cand, struct in results:
        if any(matcher.fit(struct, other) for other in kept_structs):
            continue                      # converged onto an already listed structure
        kept_structs.append(struct)
        final.append(cand.model_copy(update={"rank": len(final) + 1}))
        if len(final) >= request.max_candidates:
            break
    report(1, 1, f"{len(final)} prototype candidates for {target_formula}")

    method = (
        f"pymatgen SubstitutionPredictor (threshold {request.probability_threshold:g}, oxidation states "
        f"{dict(_target_species(request.composition)[2])}); {n_sources} MP source structures from "
        f"{n_formulas} source compositions (<= {ENTRIES_PER_FORMULA} lowest-E_hull entries each, "
        f"<= {MAX_SOURCE_SITES} sites); volume scaled by the ionic-radius cube ratio; "
        + (f"relaxed with {engine.backend_name} (FIRE + FrechetCellFilter, fmax {RELAX_FMAX} eV/A, "
           f"<= {hull_analyzer.RELAX_MAX_STEPS} steps); " if request.relax else "unrelaxed single points; ")
        + (f"E_hull vs. the {engine.backend_name} reference hull of {refset.chemsys} "
           f"({len(refset.references)} MP structures relaxed in the same model)" if refset is not None
           else "no E_hull")
        + "; deduplicated with StructureMatcher; sorted by E_hull, then energy"
    )
    return PrototypeCandidates(target_formula=target_formula, n_sources=n_sources, candidates=final, method=method)
