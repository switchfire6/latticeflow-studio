"""Canonical LatticeFlow workflow: the antiferrodistortive soft mode of SrTiO3.

cubic SrTiO3 -> relax (keep Pm-3m) -> phonons (R-point instability) -> follow the R mode
-> relaxed tetragonal phase -> Glazer tilt system -> compare with the cubic parent -> CIF.

Start the backend first (see the repo README), then:

    python examples/canonical_workflow.py [--url http://localhost:8000]

With the real MACE model the follow lands in I4/mcm (a0a0c-). With the analytic
fallback force field (``GET /api/health`` reports "lj-fallback") the numbers are
not physical; the script still runs.
"""

import argparse

from latticeflow_client import Studio, StudioError


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()

    with Studio(args.url) as studio:
        print("backend:", studio.health()["umlip_backend"])

        # 1. Is it relaxed? Relax the cubic cell under its symmetry (positions are fixed by symmetry).
        studio.load(preset="srtio3")
        final = studio.relax(fmax=0.005, max_steps=300, fix_symmetry=True, stress_tol_gpa=0.05)
        cubic = final["crystal"]
        print(f"relaxed: a = {cubic['lattice']['a']:.4f} A, P = {cubic['pressure_gpa']:.3f} GPa, "
              f"converged = {final['converged']}")

        # 2. Is it dynamically stable? Textbook dispersion of the primitive cell + DOS / thermodynamics.
        ph = studio.phonons(cell_mode="primitive", supercell_dim=[2, 2, 2], dos=True)
        print(f"phonons: stable = {ph['is_dynamically_stable']}, min f = {ph['min_frequency_thz']:.2f} THz, "
              f"ZPE = {ph['dos']['thermal']['zero_point_energy_ev_per_atom'] * 1000:.1f} meV/atom")
        for mode in ph.get("gamma_modes") or []:
            print(f"   Gamma {mode['label'] or '?':>4}  {mode['frequency_thz']:7.2f} THz"
                  f"  IR={mode['ir_active']} Raman={mode['raman_active']}")

        # 3. Follow the R-point instability into the lower-symmetry phase.
        q, band, f = studio.soft_mode("R")
        print(f"softest mode at R: q_index {q}, band {band}, f = {f:.2f} THz")
        try:
            frames = []
            final = studio.follow_instability(q, band, amplitude=0.1, fmax=0.01, max_steps=400,
                                              on_frame=frames.append)
        except StudioError as exc:   # MODE_NOT_IMAGINARY when the model finds cubic SrTiO3 stable
            print("cannot follow:", exc)
            return
        distorted = final["crystal"]
        dE = (distorted["energy_per_atom"] - cubic["energy_per_atom"]) * 1000
        print(f"follow: {len(frames)} frames -> {distorted['spacegroup_symbol']} "
              f"({len(distorted['atoms'])} atoms), dE = {dE:.2f} meV/atom")
        studio.refine(symprec=1e-3)   # snap the result to its space group

        # 4. Local structure: octahedral tilts in Glazer notation.
        local = studio.local_analysis()
        tilts = local.get("octahedral_tilts")
        if tilts:
            print("Glazer:", tilts["glazer"], "angles:", [round(a, 2) for a in tilts["tilt_angles_deg"]])

        # 5. Compare with the cubic parent (StructureMatcher, group-subgroup, volume, energy).
        cmp = studio.compare(cubic, studio.crystal)
        print(f"compare: {cmp['spacegroup_a']} -> {cmp['spacegroup_b']} ({cmp['group_subgroup']}), "
              f"dV = {cmp['volume_change_percent']:.2f} %, dE = {cmp['energy_difference_mev_per_atom']:.2f} meV/atom")

        # 6. Is the new phase stable? Re-run phonons on it, then save it.
        ph2 = studio.phonons(cell_mode="primitive", min_supercell_length=10)
        print(f"distorted phase: stable = {ph2['is_dynamically_stable']}, min f = {ph2['min_frequency_thz']:.2f} THz")
        studio.export("cif", path=f"{studio.crystal['formula']}_{studio.crystal['spacegroup_number']}.cif")
        print("history:", [e["label"] for e in studio.history["entries"]])


if __name__ == "__main__":
    main()
