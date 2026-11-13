"""Batch screening: compositions -> generate (top_k) -> relax -> phonons -> [hull] -> [elastic] -> table.

CLI::

    latticeflow-screen compositions.csv --out results.csv [--url http://localhost:8000]
                       [--top-k 3] [--hull] [--elastic] [--fmax 0.02] [--steps 60] [--seed 0]

``compositions.csv`` has a ``composition`` column (atoms per cell, e.g. ``SrTiO3``,
``Sr2Ti2O6``, ``Ba(TiO3)``) and optional per-row ``target_spacegroup``,
``target_crystal_system``, ``top_k`` and ``seed`` columns; a file without a
header is read as one composition per line (``#`` comments allowed). The output
is CSV, or JSON when ``--out`` ends in ``.json``. One row per candidate; a failed
stage records ``error`` and the row is still written (later stages are skipped
for that candidate, the batch continues; a dropped connection is reopened).

Columns: composition, candidate_rank, formula, n_atoms, spacegroup_generated,
spacegroup, spacegroup_number, energy_per_atom_ev, pressure_gpa, max_force_ev_a,
relax_converged, min_frequency_thz, dynamically_stable, imaginary_mode_count,
e_hull_ev, k_vrh_gpa, g_vrh_gpa, born_stable, model, error.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from latticeflow_client.client import Studio
from latticeflow_client.errors import StudioConnectionError, StudioError
from latticeflow_client.structures import parse_formula

COLUMNS = [
    "composition", "candidate_rank", "formula", "n_atoms", "spacegroup_generated", "spacegroup",
    "spacegroup_number", "energy_per_atom_ev", "pressure_gpa", "max_force_ev_a", "relax_converged",
    "min_frequency_thz", "dynamically_stable", "imaginary_mode_count", "e_hull_ev", "k_vrh_gpa",
    "g_vrh_gpa", "born_stable", "model", "error",
]

_ROW_OPTIONS = ("target_spacegroup", "target_crystal_system", "top_k", "seed")


def read_compositions(path: str) -> List[Dict[str, Any]]:
    """Rows ``{"composition": str, <optional generator options>}`` from a CSV or plain list."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    header = [h.strip().lower() for h in lines[0].split(",")]
    if "composition" not in header:
        return [{"composition": ln.split(",")[0].strip()} for ln in lines]
    rows = []
    for rec in csv.DictReader(lines, fieldnames=header):
        if rec is None or (rec.get("composition") or "").strip().lower() == "composition":
            continue
        row: Dict[str, Any] = {"composition": rec["composition"].strip()}
        for key in _ROW_OPTIONS:
            value = (rec.get(key) or "").strip()
            if value:
                row[key] = value if key == "target_crystal_system" else int(value)
        rows.append(row)
    return rows


def _model_name(studio: Studio) -> Optional[str]:
    if studio.model is not None:
        return studio.model.get("backend_name")
    try:
        return studio.health().get("umlip_backend")
    except StudioError:
        return None


def screen_composition(studio: Studio, spec: Dict[str, Any], *, top_k: int = 3, hull: bool = False,
                       elastic: bool = False, relax_options: Optional[Dict[str, Any]] = None,
                       phonon_options: Optional[Dict[str, Any]] = None,
                       generate_options: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """All candidate rows of one composition (never raises :class:`StudioError`; connection errors propagate)."""
    composition = spec["composition"]
    base = {"composition": composition, "model": _model_name(studio)}
    opts = dict(generate_options or {})
    opts.update({k: spec[k] for k in ("target_spacegroup", "target_crystal_system", "seed") if k in spec})
    k = int(spec.get("top_k", top_k))
    try:
        gen = studio.generate(parse_formula(composition), top_k=k, **opts)
    except (StudioError, ValueError) as exc:
        if isinstance(exc, StudioConnectionError):
            raise
        return [dict(base, error=f"generate: {exc}")]
    candidates = gen["candidates"] or [{"rank": 1, "crystal": gen["crystal"]}]
    rows = []
    for cand in candidates:
        rows.append(_screen_candidate(studio, dict(base, candidate_rank=cand.get("rank", 1)), cand["crystal"],
                                      hull=hull, elastic=elastic, relax_options=relax_options,
                                      phonon_options=phonon_options))
    return rows


def _screen_candidate(studio: Studio, row: Dict[str, Any], crystal: Dict[str, Any], *, hull: bool, elastic: bool,
                      relax_options: Optional[Dict[str, Any]], phonon_options: Optional[Dict[str, Any]]
                      ) -> Dict[str, Any]:
    row["spacegroup_generated"] = crystal.get("spacegroup_symbol")
    stage = "load"
    try:
        studio.load(crystal=crystal)
        stage = "relax"
        final = studio.relax(**(relax_options or {}))
        c = final["crystal"]
        row.update(formula=c["formula"], n_atoms=len(c["atoms"]), spacegroup=c["spacegroup_symbol"],
                   spacegroup_number=c["spacegroup_number"], energy_per_atom_ev=c["energy_per_atom"],
                   pressure_gpa=c.get("pressure_gpa"), max_force_ev_a=c["max_force"],
                   relax_converged=final["converged"])
        stage = "phonons"
        ph = studio.phonons(**dict({"cell_mode": "primitive"}, **(phonon_options or {})))
        row.update(min_frequency_thz=ph["min_frequency_thz"], dynamically_stable=ph["is_dynamically_stable"],
                   imaginary_mode_count=ph["imaginary_mode_count"])
        if hull:
            stage = "hull"
            row["e_hull_ev"] = studio.hull(c, relax=False)["e_hull_ev"]   # already relaxed above
        if elastic:
            stage = "elastic"
            el = studio.elastic_tensor()
            row.update(k_vrh_gpa=el["k_vrh_gpa"], g_vrh_gpa=el["g_vrh_gpa"], born_stable=el["born_stable"])
    except StudioConnectionError:
        raise
    except StudioError as exc:
        row["error"] = f"{stage}: {exc.code}: {exc.message}"
    return row


def screen(compositions: Iterable[Any], *, url: str = "http://localhost:8000", top_k: int = 3,
           hull: bool = False, elastic: bool = False, relax_options: Optional[Dict[str, Any]] = None,
           phonon_options: Optional[Dict[str, Any]] = None, generate_options: Optional[Dict[str, Any]] = None,
           on_row: Optional[Callable[[Dict[str, Any]], Any]] = None, studio: Optional[Studio] = None,
           timeout: Optional[float] = None) -> List[Dict[str, Any]]:
    """Screen ``compositions`` (strings or row dicts, see :func:`read_compositions`); returns all rows."""
    own = studio is None
    rows: List[Dict[str, Any]] = []
    try:
        for item in compositions:
            spec = {"composition": item} if isinstance(item, str) else dict(item)
            for attempt in (1, 2):   # one reconnect per composition when we own the session
                try:
                    if studio is None:
                        studio = Studio(url, timeout=timeout)
                    new = screen_composition(studio, spec, top_k=top_k, hull=hull, elastic=elastic,
                                             relax_options=relax_options, phonon_options=phonon_options,
                                             generate_options=generate_options)
                    break
                except StudioConnectionError as exc:
                    if own and studio is not None:
                        studio.close()
                        studio = None
                    if not own or attempt == 2:
                        new = [{"composition": spec["composition"], "error": f"connection: {exc.message}"}]
                        break
            for row in new:
                rows.append(row)
                if on_row is not None:
                    on_row(row)
    finally:
        if own and studio is not None:
            studio.close()
    return rows


def write_rows(rows: Sequence[Dict[str, Any]], path: str) -> None:
    if path.lower().endswith(".json"):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{k: r.get(k) for k in COLUMNS} for r in rows], fh, indent=2)
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="latticeflow-screen", description=__doc__.split("\n\n")[0])
    ap.add_argument("compositions", help="CSV with a 'composition' column, or one formula per line")
    ap.add_argument("--out", default="results.csv", help="results file (.csv or .json)")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--top-k", type=int, default=3, help="candidates per composition (1-10)")
    ap.add_argument("--hull", action="store_true", help="E_hull per candidate (needs MP_API_KEY on the server)")
    ap.add_argument("--elastic", action="store_true", help="elastic tensor per candidate (K_VRH, G_VRH, Born)")
    ap.add_argument("--fmax", type=float, default=0.02, help="relaxation force tolerance, eV/A")
    ap.add_argument("--max-steps", type=int, default=300, help="relaxation step limit")
    ap.add_argument("--steps", type=int, default=None, help="generator flow steps")
    ap.add_argument("--seed", type=int, default=None, help="generator seed (per-row 'seed' wins)")
    ap.add_argument("--timeout", type=float, default=None, help="per-command timeout, s")
    args = ap.parse_args(argv)

    specs = read_compositions(args.compositions)
    if not specs:
        print("no compositions found", file=sys.stderr)
        return 2
    gen_opts = {k: v for k, v in (("steps", args.steps), ("seed", args.seed)) if v is not None}
    rows: List[Dict[str, Any]] = []

    def report(row: Dict[str, Any]) -> None:
        rows.append(row)
        write_rows(rows, args.out)   # rewrite after every row: partial results survive an interrupt
        status = row.get("error") or (
            f"{row.get('spacegroup')}  E={row.get('energy_per_atom_ev'):.4f} eV/atom  "
            f"fmin={row.get('min_frequency_thz', float('nan')):.2f} THz"
            if row.get("energy_per_atom_ev") is not None else "")
        print(f"{row['composition']:>14} #{row.get('candidate_rank', '-')}: {status}", flush=True)

    screen(specs, url=args.url, top_k=args.top_k, hull=args.hull, elastic=args.elastic,
           relax_options={"fmax": args.fmax, "max_steps": args.max_steps}, generate_options=gen_opts,
           on_row=report, timeout=args.timeout)
    print(f"{len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
