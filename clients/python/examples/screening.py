"""Batch screening from Python (the same pipeline as the ``latticeflow-screen`` CLI).

    python examples/screening.py [--url http://localhost:8000] [--hull]

For each composition: symmetric-candidate search (top_k) -> relax -> primitive-cell
phonons -> elastic tensor [-> E_hull]. Failed rows keep an ``error`` and the batch goes on.
Equivalent CLI:

    latticeflow-screen examples/compositions.csv --out results.csv --elastic [--hull]
"""

import argparse
from pathlib import Path

from latticeflow_client.screen import read_compositions, screen, write_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--hull", action="store_true", help="needs MP_API_KEY on the server")
    ap.add_argument("--out", default="screening_results.csv")
    args = ap.parse_args()

    specs = read_compositions(str(Path(__file__).with_name("compositions.csv")))
    rows = screen(specs, url=args.url, top_k=3, hull=args.hull, elastic=True,
                  relax_options={"fmax": 0.02, "max_steps": 300},
                  on_row=lambda r: print(r["composition"], r.get("candidate_rank"), r.get("spacegroup"),
                                         r.get("min_frequency_thz"), r.get("error") or ""))
    write_rows(rows, args.out)

    # Rank what survived: dynamically and mechanically stable, then lowest energy per composition.
    ok = [r for r in rows if not r.get("error") and r.get("dynamically_stable") and r.get("born_stable")]
    best = {}
    for r in sorted(ok, key=lambda r: r["energy_per_atom_ev"]):
        best.setdefault(r["composition"], r)
    print("\nstable candidates (lowest energy per composition):")
    for comp, r in best.items():
        print(f"  {comp:>10}: {r['spacegroup']:>10}  E = {r['energy_per_atom_ev']:.4f} eV/atom  "
              f"K_VRH = {r['k_vrh_gpa']:.0f} GPa  E_hull = {r.get('e_hull_ev')}")


if __name__ == "__main__":
    main()
