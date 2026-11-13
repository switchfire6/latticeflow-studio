# Scientific workflows

Each workflow below works the same in two places: in the studio UI, and in Python with `latticeflow_client` (see [`clients/python`](../clients/python)). The Python snippets assume this setup:

```python
from latticeflow_client import Studio
studio = Studio("http://localhost:8000")
```

**Rule for every workflow: relax first.** Phonons, elastic constants, E_hull and defect energies of an unrelaxed structure are not meaningful. Check three things on the relaxed structure:
- `max_force` is small;
- `pressure_gpa` is about 0 (when the cell was relaxed);
- the final relaxation frame has `converged`.

For the protocol behind each call, see [`protocol.md`](protocol.md).

---

## 1. Soft-mode phase transition (SrTiO3: Pm-3m → I4/mcm)

1. **Load and relax the cubic parent under its symmetry.**
   ```python
   studio.load(preset="srtio3")
   cubic = studio.relax(fmax=0.005, max_steps=300, fix_symmetry=True, stress_tol_gpa=0.05)["crystal"]
   ```
2. **Phonons of the primitive cell.** You get the textbook dispersion, plus irreps and IR/Raman activity at Γ.
   ```python
   ph = studio.phonons(cell_mode="primitive", supercell_dim=[2, 2, 2])
   ph["is_dynamically_stable"], ph["min_frequency_thz"]
   ```
   Imaginary frequencies appear as negative numbers. In cubic SrTiO3 the antiferrodistortive mode at R is unstable at 0 K.
3. **Convergence check.** Rerun in a larger supercell; `converged` means max |Δf| < 0.1 THz and the stability verdict agrees.
   ```python
   studio.phonon_convergence()["convergence"]
   ```
4. **Follow the instability.**
   ```python
   q, band, f = studio.soft_mode("R")
   studio.follow_instability(q, band, amplitude=0.1, fmax=0.01, max_steps=400)
   studio.refine(symprec=1e-3)
   ```
   The follow builds the supercell commensurate with q (2×2×2, 40 atoms), displaces the atoms along the eigenvector and relaxes. A triply degenerate mode is followed along a single axis, so the result is reproducible.
5. **Characterise the result.**
   - `studio.local_analysis()["octahedral_tilts"]["glazer"]` should give `a0a0c-`.
   - `studio.compare(cubic, studio.crystal)` gives the group–subgroup relation, the volume change and the energy gain in meV/atom.
6. **Is the new phase stable?** Rerun `phonons()` on it. If an imaginary mode remains, follow that one too. Phases reached by repeated follows form a branch of the 0 K energy landscape. They are not a finite-temperature phase diagram.

`clients/python/examples/canonical_workflow.py` runs this workflow end to end.

## 2. Neutral point-defect formation energy

$E_f = E_{def} - E_{bulk} - \sum_i \Delta n_i \mu_i$

The chemical potentials μ come from Materials Project reference phases, relaxed with the session model. This needs `MP_API_KEY`, or references already cached from an earlier hull calculation.

1. Load and relax the bulk, then note its history position:
   ```python
   studio.load(preset="srtio3")
   studio.relax(fix_symmetry=True)
   bulk = studio.history["position"]
   ```
2. Build a supercell. Ideally defects are about 10 Å or more from their periodic images (`min_image_distance`; smaller cells trigger a warning), but the studio caps a structure at 128 atoms. For SrTiO3, 2×2×2 (40 atoms, about 7.8 Å) is the largest cubic choice. Repeat the calculation at two sizes to see the trend.
   ```python
   studio.make_supercell(2)
   ```
3. Create the defect:
   - a vacancy with `studio.delete_atom(i)`;
   - a substitution with `studio.transmute(i, "Nb")`;
   - an interstitial with `studio.add_atom("Li", [0.25, 0.25, 0.25])`.
4. Relax the ions at fixed cell, so the defect cell keeps the bulk lattice:
   ```python
   studio.relax(relax_cell=False, fmax=0.02, max_steps=500)
   ```
5. Compute the formation energy:
   ```python
   r = studio.formation_energy(bulk)
   r["defect_label"], r["formation_energies_ev"]
   ```
   The result gives E_f for each vertex of the bulk's stability region and for each "X-rich" limit. Read `warnings` as well: they flag an unrelaxed cell, a stressed bulk or a small cell.

**Scope.** Results are neutral defects only. There are no charge states, Fermi-level dependence or finite-size (image-charge) corrections.

## 3. Ionic diffusion and Arrhenius analysis

1. Relax the structure. A larger cell gives better statistics, for example `studio.make_supercell([2, 2, 2])`.
2. Run NVT MD at several temperatures, starting each run from the same relaxed structure:
   ```python
   start = studio.history["position"]
   D = {}
   for T in (600, 800, 1000, 1200):
       while studio.history["position"] > start:
           studio.undo()          # back to the relaxed start structure
       r = studio.md(temperature_k=T, ensemble="NVT", timestep_fs=1.0, n_steps=20000, frame_interval=50, seed=1)
       D[T] = r["diffusion_coefficients_cm2_s"]["Li"]
   ```
   Use `on_frame=` to watch the temperature and MSD live. `studio.stop()` ends a run early; the call then raises `StreamStopped`.
3. **Arrhenius fit.** Fit ln D against 1/T: the slope is −E_a/k_B. Include only temperatures with a linear MSD regime; D is `None` where there is none.

**Caveats.**
- **Nernst–Einstein and tracer D.** D is the tracer diffusion coefficient (MSD slope / 6 over the last half of the run). Converting it to an ionic conductivity with Nernst–Einstein assumes uncorrelated hops (Haven ratio 1), so treat the result as an order-of-magnitude estimate.
- **Statistics.** Small cells and runs of a few picoseconds give noisy D, often with only a handful of hops. Report how many hops there were and the length of the run.
- **Temperatures.** Use temperatures high enough to see hops within the simulation time but below melting. Extrapolating to 300 K amplifies any error in E_a.
- **Model softness.** MACE-small softness lowers barriers. Check E_a with a larger model (workflow 4).

## 4. Model committee (how much can I trust the force field?)

1. Get the model ids from `studio.models()`. Ids without a cached checkpoint download on first use.
2. Run the committee:
   ```python
   c = studio.committee(["small", "medium-mpa-0", "medium-omat-0"], relax=True)
   c["spread"]   # pressure_gpa, max_force, volume_percent, lattice_percent (max - min)
   ```
   A volume spread of a few percent or a large pressure spread means the prediction is model-limited. Absolute energies are deliberately not compared, because each model has its own reference.
3. To redo a whole workflow with a stronger model, switch the session: `studio.set_model("medium-mpa-0", dispersion=True)`. Then relax and recompute. D3 dispersion matters for layered and molecular crystals. The undo history keeps structures, not models.

## 5. Prototype and generator screening

The studio has two routes to candidate structures:

- **Prototype substitution** (`PROTOTYPE_SEARCH`, needs `MP_API_KEY`). Ions in known Materials Project structures are swapped using pymatgen's ionic-substitution probabilities. Each candidate is relaxed and scored against the hull:
  ```python
  r = studio.prototype_search("NaNbO3", max_candidates=8)
  best = r["candidates"][0]
  studio.load(crystal=best["crystal"])
  ```
  This is usually the more productive route to plausible new compounds.
- **Symmetric-candidate generator** (`generate`). A seeded, budgeted search over Wyckoff arrangements of a target space group, ranked by energy:
  ```python
  g = studio.generate("SrTiO3", target_spacegroup=221, top_k=3, seed=0)
  g["candidates"]
  ```
  Compositions are given per cell, conventional cell for centred groups (rock salt Fm-3m is `Na4Cl4`, diamond Fd-3m is `Si8`).

**Batch screening.** `latticeflow-screen compositions.csv --out results.csv --elastic [--hull]` runs, for each candidate: generate (`--top-k`), relax, primitive-cell phonons, then E_hull and the elastic tensor (K_VRH, G_VRH, Born stability) if requested. It writes one row per candidate. A failure is recorded in the row's `error` column, and the batch continues.

Rank candidates by three gates:
- dynamically stable (no imaginary modes);
- Born stable;
- E_hull ≲ 50–100 meV/atom (model-dependent; MACE errors are of that order).

Then confirm the survivors with a larger model and, ideally, DFT. See `clients/python/examples/screening.py`.
