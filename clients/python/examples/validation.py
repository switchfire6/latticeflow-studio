"""Validation sweep: run the studio's main workflows and compare them with reference values.

    python clients/python/examples/validation.py [http://localhost:8000] [--model medium-mpa-0] [--skip-mp]

Two kinds of checks:

* **consistency** checks must pass with any model (two routes to the same number agree,
  symmetry is respected, sum rules hold). A FAIL here is a bug in the studio.
* **reference** checks compare with experiment within a generous tolerance. A WARN here
  usually shows the force field's own error (MACE-MP-0 small is soft), not a bug.

Needs a running backend with MACE. The Materials Project checks need MP_API_KEY on the server.
Exit code 1 when any check FAILs.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import traceback

from latticeflow_client import Studio

ROWS = []


def row(section: str, name: str, value, expect: str, status: str) -> None:
    ROWS.append((section, name, value, expect, status))
    shown = f"{value:.4g}" if isinstance(value, float) else str(value)
    print(f"  [{status:4}] {name:<46} {shown:<22} {expect}", flush=True)


def consistency(section: str, name: str, ok: bool, value, expect: str) -> None:
    row(section, name, value, expect, "PASS" if ok else "FAIL")


def reference(section: str, name: str, value: float, ref: float, tol: float, unit: str = "") -> None:
    ok = abs(value - ref) <= tol * abs(ref)
    row(section, name, value, f"ref {ref:g} {unit} (±{tol:.0%})", "PASS" if ok else "WARN")


def guarded(section: str):
    def wrap(fn):
        def run(*a, **kw):
            print(f"\n== {section}", flush=True)
            t0 = time.time()
            try:
                fn(section, *a, **kw)
            except Exception as exc:  # one broken section must not hide the others
                traceback.print_exc()
                row(section, "section crashed", type(exc).__name__, str(exc)[:80], "FAIL")
            print(f"   ({time.time() - t0:.0f} s)", flush=True)
        return run
    return wrap


def abc(crystal):
    lat = crystal["lattice"]
    return lat["a"], lat["b"], lat["c"]


def gamma_frequencies(ph):
    q = next(k["index"] for k in ph["kpath_labels"] if "gamma" in k["label"].lower() or k["label"] in ("G", "Γ"))
    return ph["frequencies_thz"][q]


# --------------------------------------------------------------------------- #
@guarded("Si: structure, elasticity, phonons, diffraction")
def silicon(sec, s: Studio):
    s.load(preset="si")
    r = s.relax(fmax=0.002)
    c = r["crystal"]
    n = len(c["atoms"])
    a = (c["lattice"]["volume"] * 8 / n) ** (1 / 3)
    consistency(sec, "relaxation converged, |P| < 0.05 GPa", r["converged"] and abs(c["pressure_gpa"]) < 0.05,
                round(c["pressure_gpa"], 4), "GPa")
    consistency(sec, "space group", c["spacegroup_number"] == 227, c["spacegroup_symbol"], "Fd-3m (#227)")
    reference(sec, "lattice constant a", a, 5.431, 0.02, "Å")

    bm = s.bulk_modulus()
    el = s.elastic_tensor()
    C = el["c_ij_gpa"]
    consistency(sec, "K: EOS fit vs elastic tensor", abs(bm["k_eq_gpa"] - el["k_vrh_gpa"]) < 0.03 * el["k_vrh_gpa"],
                f"{bm['k_eq_gpa']:.1f} / {el['k_vrh_gpa']:.1f}", "agree within 3 %")
    consistency(sec, "K: energy fit vs stress derivative", abs(bm["k_gpa"] - bm["k_stress_gpa"]) < 0.03 * bm["k_gpa"],
                f"{bm['k_gpa']:.1f} / {bm['k_stress_gpa']:.1f}", "agree within 3 %")
    cubic = (max(abs(C[0][0] - C[1][1]), abs(C[0][0] - C[2][2]), abs(C[0][1] - C[0][2]), abs(C[0][1] - C[1][2]),
                 abs(C[3][3] - C[4][4]), abs(C[3][3] - C[5][5])) < 0.5
             and max(abs(C[i][j]) for i in range(3) for j in range(3, 6)) < 0.5)
    consistency(sec, "C_ij has cubic symmetry", cubic, f"C11 {C[0][0]:.1f} C12 {C[0][1]:.1f} C44 {C[3][3]:.1f}", "3 independent constants")
    consistency(sec, "C_ij symmetric, Born stable", el["born_stable"] and all(abs(C[i][j] - C[j][i]) < 0.5 for i in range(6) for j in range(6)),
                el["born_stable"], "True")
    consistency(sec, "EOS B0'", 3.0 < bm["k_eq_prime"] < 5.5, round(bm["k_eq_prime"], 2), "3 to 5.5")
    reference(sec, "bulk modulus K", el["k_vrh_gpa"], 97.8, 0.15, "GPa")
    reference(sec, "C11", C[0][0], 165.7, 0.15, "GPa")
    reference(sec, "C12", C[0][1], 63.9, 0.15, "GPa")
    reference(sec, "C44", C[3][3], 79.6, 0.15, "GPa")

    ph = s.phonons(cell_mode="primitive", dos=True)
    g = gamma_frequencies(ph)
    consistency(sec, "dynamically stable", ph["is_dynamically_stable"] and ph["imaginary_mode_count"] == 0, ph["min_frequency_thz"], "no imaginary modes")
    consistency(sec, "primitive cell: 6 branches", len(g) == 6 and ph["n_primitive_atoms"] == 2, len(g), "6")
    consistency(sec, "acoustic sum rule at Γ", max(abs(f) for f in g[:3]) < 0.02, round(max(abs(f) for f in g[:3]), 4), "< 0.02 THz")
    consistency(sec, "Γ optical mode triply degenerate", abs(g[5] - g[3]) < 0.01, round(g[5] - g[3], 5), "< 0.01 THz")
    reference(sec, "Γ optical phonon", g[5], 15.53, 0.10, "THz")
    labels = [m["label"] for m in ph.get("gamma_modes") or []]
    consistency(sec, "Γ irreps", sorted(labels) == ["T1u", "T2g"], ",".join(labels), "T1u (acoustic) + T2g (Raman)")
    raman = [m for m in ph.get("gamma_modes") or [] if m["label"] == "T2g"]
    consistency(sec, "T2g is Raman active, IR inactive", bool(raman) and raman[0]["raman_active"] and not raman[0]["ir_active"], bool(raman), "True")
    dos = ph["dos"]
    f, d = dos["frequencies_thz"], dos["total_dos"]
    integral = sum(0.5 * (d[i] + d[i + 1]) * (f[i + 1] - f[i]) for i in range(len(f) - 1))
    consistency(sec, "DOS integrates to 3N", abs(integral - 6) < 0.1, round(integral, 3), "6 (primitive cell)")
    th = dos["thermal"]
    T, cv = th["temperatures_k"], th["heat_capacity_kb_per_atom"]
    cv300 = min(zip(T, cv), key=lambda p: abs(p[0] - 300))[1]
    consistency(sec, "C_v rises towards 3 k_B (Dulong-Petit)", cv[-1] > cv300 and cv[-1] < 3.0001, f"{cv[-1]:.3f} at {T[-1]:.0f} K", "< 3 k_B")
    reference(sec, "C_v(300 K)", cv300, 2.38, 0.12, "k_B/atom")
    reference(sec, "zero-point energy", th["zero_point_energy_ev_per_atom"] * 1000, 61.0, 0.15, "meV/atom")

    full = s.phonons(cell_mode="as_displayed")
    gf = gamma_frequencies(full)
    consistency(sec, "Γ optical: primitive vs displayed cell", abs(max(gf) - g[5]) < 0.05, f"{g[5]:.3f} / {max(gf):.3f}", "agree within 0.05 THz")

    xrd = s.diffraction()
    peaks = xrd["peaks"]
    lam = xrd["wavelength_angstrom"]
    hkls = [tuple(sorted(abs(i) for i in p["hkls"][0]["hkl"])) for p in peaks[:3]]
    consistency(sec, "XRD: first reflections", hkls == [(1, 1, 1), (0, 2, 2), (1, 1, 3)], str(hkls), "111, 220, 311 (200 forbidden)")
    bragg = 2 * math.degrees(math.asin(lam / (2 * a / math.sqrt(3))))
    consistency(sec, "XRD: 111 obeys Bragg's law for this a", abs(peaks[0]["two_theta"] - bragg) < 0.02, f"{peaks[0]['two_theta']:.3f} / {bragg:.3f}", "2θ agree")

    e1 = c["energy_per_atom"]
    sc = s.make_supercell([2, 1, 1])
    consistency(sec, "supercell: energy per atom unchanged", abs(sc["energy_per_atom"] - e1) < 1e-6, f"{(sc['energy_per_atom'] - e1) * 1e6:.2f} µeV", "< 1 µeV/atom")
    s.undo()
    back = s.load(cif=s.export("cif"))
    consistency(sec, "CIF export -> import round trip", back["spacegroup_number"] == 227 and abs(back["energy_per_atom"] - e1) < 1e-5,
                f"{(back['energy_per_atom'] - e1) * 1e6:.2f} µeV", "same structure")
    back = s.load(text=s.export("poscar", crystal=c), text_format="poscar")
    consistency(sec, "POSCAR export -> import round trip", abs(back["energy_per_atom"] - e1) < 1e-5, f"{(back['energy_per_atom'] - e1) * 1e6:.2f} µeV", "same structure")


@guarded("Si: molecular dynamics")
def silicon_md(sec, s: Studio):
    s.load(preset="si")
    s.relax(fmax=0.01)
    s.make_supercell(2)
    nvt = s.md(ensemble="NVT", temperature_k=300, n_steps=600, timestep_fs=2.0, friction_per_fs=0.02, seed=1)
    consistency(sec, "NVT: mean temperature near the target", abs(nvt["mean_temperature_k"] - 300) < 60, round(nvt["mean_temperature_k"], 1), "300 ± 60 K (short run)")
    d = nvt["diffusion_coefficients_cm2_s"].get("Si")
    consistency(sec, "NVT: no diffusion in the solid", d is None or abs(d) < 1e-6, d, "< 1e-6 cm²/s")
    consistency(sec, "NVT: crystal keeps its atoms", len(nvt["final_crystal"]["atoms"]) == 64, len(nvt["final_crystal"]["atoms"]), "64")
    s.load(preset="si")
    s.relax(fmax=0.01)
    s.make_supercell(2)
    nve = s.md(ensemble="NVE", temperature_k=300, n_steps=400, timestep_fs=1.0, seed=1)
    consistency(sec, "NVE: equipartition from a perfect lattice", 100 < nve["mean_temperature_k"] < 200, round(nve["mean_temperature_k"], 1), "about T/2 = 150 K")


@guarded("SrTiO3: soft mode, follow instability, local structure")
def strontium_titanate(sec, s: Studio, mp: bool):
    s.load(preset="srtio3")
    r = s.relax(fmax=0.002)
    cubic = r["crystal"]
    parent = s.history["position"]
    reference(sec, "cubic lattice constant", abc(cubic)[0], 3.905, 0.02, "Å")
    ph = s.phonons(cell_mode="primitive")
    q, band, f = s.soft_mode("R")
    row(sec, "R-point soft mode", f, "imaginary in DFT (antiferrodistortive)", "PASS" if f < 0 else "WARN")
    fr = s.follow_instability(q, band)
    low = fr["crystal"]
    a, b, c = abc(low)
    consistency(sec, "follow: converged and relaxed", fr["converged"] and low["max_force"] < 0.006 and abs(low["pressure_gpa"]) < 0.1,
                f"|F| {low['max_force']:.4f}, P {low['pressure_gpa']:.3f}", "fmax 0.005, |P| < 0.1 GPa")
    consistency(sec, "follow: space group", low["spacegroup_number"] == 140, low["spacegroup_symbol"], "I4/mcm (#140)")
    de = (low["energy_per_atom"] - cubic["energy_per_atom"]) * 1000
    consistency(sec, "follow: energy lowered", de < 0, round(de, 3), "meV/atom < 0")
    consistency(sec, "follow: tetragonal, c/a > 1", abs(a - b) < 1e-3 and c > a, round(c / a, 5), "> 1 (as in experiment)")
    la = s.local_analysis()
    tilts = la["octahedral_tilts"]
    consistency(sec, "Glazer notation", bool(tilts) and tilts["glazer"] == "a0a0c-", tilts and tilts["glazer"], "a0a0c-")
    if tilts:
        row(sec, "octahedral tilt angle", max(tilts["tilt_angles_deg"]), "exp 2.1° (4 K); PBE about 5 to 6°", "PASS" if 0.5 < max(tilts["tilt_angles_deg"]) < 9 else "WARN")
    longest = max(nb["distance"] for site in la["sites"] for nb in site["neighbors"])
    consistency(sec, "neighbours are nearest images", longest < 3.6, round(longest, 3), "longest bond < 3.6 Å")
    bvs = {}
    for site in la["sites"]:
        bvs.setdefault(site["element"], []).append(site["bond_valence_sum"])
    for el, ref in (("Ti", 4.0), ("Sr", 2.0), ("O", 2.0)):
        reference(sec, f"bond-valence sum of {el}", sum(bvs[el]) / len(bvs[el]), ref, 0.12, "v.u.")
    consistency(sec, "global instability index", la["global_instability_index"] < 0.2, round(la["global_instability_index"], 3), "< 0.2 v.u.")
    cmp_ = s.compare(parent)
    consistency(sec, "compare: group-subgroup relation", "subgroup" in (cmp_["group_subgroup"] or ""), cmp_["group_subgroup"], "I4/mcm is a subgroup of Pm-3m")
    consistency(sec, "compare: ΔE matches the relaxation", abs(cmp_["energy_difference_mev_per_atom"] - de) < 1e-3, round(cmp_["energy_difference_mev_per_atom"], 3), f"{de:.3f} meV/atom")

    if mp:
        s.load(crystal=cubic)
        h = s.hull(relax=False)
        consistency(sec, "E_hull of SrTiO3 (a known stable phase)", abs(h["e_hull_ev"]) < 0.05, round(h["e_hull_ev"], 4), "within 0.05 eV/atom of 0")
        m = s.mp_match()
        ids = [x["entry"]["material_id"] for x in m["matches"]]
        consistency(sec, "Materials Project match", "mp-5229" in ids, ",".join(ids[:3]), "includes mp-5229")
        # oxygen vacancy in a 2x2x2 supercell
        s.make_supercell(2)
        bulk = s.history["position"]
        o = next(i for i, at in enumerate(s.crystal["atoms"]) if at["element"] == "O")
        s.delete_atom(o)
        s.relax(relax_cell=False, fmax=0.02, max_steps=300)
        fe = s.formation_energy(bulk)
        vals = fe["formation_energies_ev"]
        consistency(sec, "V_O: O-poor limit below O-rich limit", min(vals.values()) < max(vals.values()), {k: round(v, 2) for k, v in vals.items()}, "E_f depends on μ_O")
        row(sec, "V_O formation energy, O-rich", max(vals.values()), "DFT about 5.5 to 6.5 eV (neutral)", "PASS" if 3.5 < max(vals.values()) < 8 else "WARN")


@guarded("BaTiO3: ferroelectric instability")
def barium_titanate(sec, s: Studio):
    s.load(preset="batio3")
    r = s.relax(fmax=0.002)
    cubic = r["crystal"]
    reference(sec, "cubic lattice constant", abc(cubic)[0], 4.00, 0.02, "Å")
    s.phonons(cell_mode="primitive")
    q, band, f = s.soft_mode("GAMMA")
    row(sec, "Γ soft mode (ferroelectric)", f, "imaginary", "PASS" if f < 0 else "WARN")
    if f < 0:
        fr = s.follow_instability(q, band)
        low = fr["crystal"]
        de = (low["energy_per_atom"] - cubic["energy_per_atom"]) * 1000
        consistency(sec, "follow: energy lowered", de < 0, round(de, 3), "meV/atom < 0")
        consistency(sec, "follow: polar space group", low["spacegroup_number"] in (99, 38, 160), low["spacegroup_symbol"], "P4mm, Amm2 or R3m")


@guarded("NaCl: Materials Project import, hull, generator")
def rock_salt(sec, s: Studio):
    s.load(mp_id="mp-22862")
    r = s.relax(fmax=0.005)
    c = r["crystal"]
    a = (c["lattice"]["volume"] * 8 / len(c["atoms"])) ** (1 / 3)
    consistency(sec, "space group after import + relax", c["spacegroup_number"] == 225, c["spacegroup_symbol"], "Fm-3m (#225)")
    reference(sec, "lattice constant a", a, 5.640, 0.025, "Å")
    h = s.hull(relax=False)
    consistency(sec, "E_hull of NaCl", abs(h["e_hull_ev"]) < 0.03, round(h["e_hull_ev"], 4), "within 0.03 eV/atom of 0")
    row(sec, "formation energy", h["formation_energy_per_atom"], "exp -2.13 eV/atom", "PASS" if -2.4 < (h["formation_energy_per_atom"] or 0) < -1.8 else "WARN")
    g = s.generate("NaCl", target_spacegroup=225, seed=3)
    gen = s.relax(fmax=0.005)["crystal"]
    cmp_ = s.compare(c, gen)
    consistency(sec, "generator finds rock salt for NaCl in #225", cmp_["structures_match"], f"{len(g['crystal']['atoms'])} atoms, {gen['spacegroup_symbol']}", "matches the MP structure")
    consistency(sec, "generated vs imported energy", abs(cmp_["energy_difference_mev_per_atom"]) < 1.0, round(cmp_["energy_difference_mev_per_atom"], 3), "< 1 meV/atom")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("url", nargs="?", default="http://localhost:8000")
    ap.add_argument("--model", default=None, help="session model id (default: the server default)")
    ap.add_argument("--skip-mp", action="store_true", help="skip the checks that need MP_API_KEY")
    ap.add_argument("--only", default=None, help="comma-separated sections: si, md, sto, bto, nacl")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    with Studio(args.url) as s:
        health = s.health()
        mp = health["hull_available"] and not args.skip_mp
        if args.model:
            s.set_model(args.model)
        print(f"backend {health['umlip_backend']} on {health['device']}; session model {args.model or 'default'}; Materials Project {'on' if mp else 'off'}")
        for key, fn, extra in (("si", silicon, ()), ("md", silicon_md, ()), ("sto", strontium_titanate, (mp,)),
                               ("bto", barium_titanate, ()), ("nacl", rock_salt, ())):
            if only and key not in only:
                continue
            if key == "nacl" and not mp:
                continue
            fn(s, *extra)

    n = {k: sum(r[4] == k for r in ROWS) for k in ("PASS", "WARN", "FAIL")}
    print(f"\n{n['PASS']} passed, {n['WARN']} outside the reference tolerance (model accuracy), {n['FAIL']} failed (bugs)")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
