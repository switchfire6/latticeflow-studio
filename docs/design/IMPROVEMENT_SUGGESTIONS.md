# LatticeFlow Studio: Improvement Suggestions for Materials Scientists

*Review date: 2026-11-12. Reviewed against the working tree with all five spec phases complete.*

**Goal of this review:** list the changes that would turn the studio from an impressive demo into a tool a working materials scientist uses every week. That covers computational researchers, experimentalists checking a phase, and students learning lattice dynamics.

Effort scale: **S** = hours to a day, **M** = a few days, **L** = a week or more.

---

## 1. Summary

### What is already strong (keep it)

- **Physics hygiene is unusually careful:**
  - float64 by default, with E(3) and periodic invariance gates;
  - residual-force subtraction in the phonons, then symmetrized force constants;
  - deterministic bases for degenerate modes;
  - a convex hull built entirely in the uMLIP, rather than mixing its energies with DFT energies.
- **The README is honest about its limits:** the generator is not a learned model, and MACE-small is soft.
- **"Follow Instability" is a genuinely new interaction.** You watch Pm-3m SrTiO₃ fall into I4/mcm, with a ΔE readout. That alone is a strong teaching and research hook.

### Where it falls short for a scientist

1. **It computes numbers it never shows, and one badge misleads.**
   - Lattice parameters and the stress tensor are in every payload, but the UI never displays them.
   - The **RELAXED** badge checks forces only. Measured with MACE-MP-0-small, 3 of the 4 presets read "RELAXED" on load while under **+1.4 to +5.1 GPa** (§2.2).
2. **Results are trapped in the app.**
   - CIF export is P1 only.
   - There is no export for phonons, trajectories or figures.
   - Any reconnect (including a `uvicorn --reload`) silently throws the session away.
3. **High-value analyses are missing even where the hard part already exists.**
   - Phonon DOS and thermodynamics need nothing new: the force constants are already cached.
   - The elastic tensor needs only a strain loop: batched stress evaluation already exists.
   - XRD patterns: one pymatgen call.
4. **Users cannot control how far to trust a number.**
   - The model is one process-wide singleton, chosen by an environment variable.
   - There is no uncertainty estimate, no provenance stamp, and no D3 dispersion switch.
5. **Getting structures in is harder than it should be.**
   - No formula or composition entry, although the backend already accepts `composition`.
   - No import by Materials Project ID, or from POSCAR files.
   - Disordered CIFs are rejected.
   - Coordinates and lattice parameters cannot be typed in.
   - Vacancies are created in primitive cells.

---

## 2. Top recommendations by impact and effort

| # | Change | Why a scientist cares | Effort |
|---|---|---|---|
| 1 | Stress-aware RELAXED badge, plus a readout of pressure and stress (§3.2) | The current badge reports multi-GPa cells as relaxed, and the phonons that follow are run on a compressed cell | S |
| 2 | Structure panel: a, b, c, α, β, γ, V, density, Wyckoff sites (§3.1) | These are the first numbers anyone checks against experiment | S |
| 3 | Restore the session on reconnect, plus undo/redo (§4.4) | Work is lost today on any reconnect | S–M |
| 4 | Symmetrized CIF and a "Refine symmetry" action (§4.1) | P1 CIFs are second-class in every downstream tool | S |
| 5 | Phonon DOS/PDOS, plus F(T), S(T), C_v(T) and ZPE (§5.1) | Free energies compare polymorphs at temperature; the force constants are already cached | S–M |
| 6 | Primitive-cell ("textbook") dispersion, plus Γ irreps with Raman/IR labels (§5.2) | The Si preset shows 24 folded branches instead of the familiar 6 | M |
| 7 | Formula entry in the generator; jargon controls moved under "Advanced" (§3.5) | Scientists think in stoichiometry ("Li₂MnO₃"), not "elements + atoms/cell" | S |
| 8 | Import by Materials Project ID or formula search; POSCAR and extxyz import (§4.3) | This is the fastest way in, and the MP key is already configured | S–M |
| 9 | Model picker: MACE medium, MPA-0, OMAT-0 and others, plus a D3 toggle (§6.1) | Explore with a fast model, then confirm with an accurate one | M |
| 10 | Elastic tensor C_ij and Born mechanical stability (§5.5) | Completes the stability scorecard and gives the K target a real check | M |
| 11 | Simulated XRD with an overlay of an experimental pattern (§5.6) | This is how experimentalists decide "is this my phase?" | S–M |
| 12 | Defects in supercells, with formation energies from the cached hull references (§5.9) | Today a "vacancy" in the 5-atom SrTiO₃ cell is a 20 % vacancy concentration | M |

---

## 3. Show what is already computed (quick wins)

### 3.1 Structure panel

**Problem.** `CrystalStatePayload.lattice` carries a, b, c, α, β, γ and the volume. Yet no frontend component reads anything from it except `matrix`, which is used for rendering. A scientist cannot see the lattice constant of the structure they just relaxed.

**Suggestion.** Add a collapsible "Structure" card showing:

- **Cell:** a, b, c, α, β, γ, V, V/atom, density (g/cm³), and Z (formula units per cell);
- **Symmetry:** the space-group symbol, number and crystal system, with the `symprec` used;
- **Site table:** element, Wyckoff letter and multiplicity (from the spglib dataset), fractional coordinates and coordination number;
- **Changes:** Δa, Δb, Δc and ΔV relative to the starting structure after a relaxation. "MACE expands SrTiO₃ by 1.0 % in a" is a result in its own right.

### 3.2 Stress, pressure and an honest RELAXED badge

**Problem.**
- `stress_gpa` reaches the client on every frame ([protocol.ts](frontend/src/types/protocol.ts)) and is never displayed.
- The RELAXED badge is `max_force < 0.02` only ([TelemetryHeader.tsx:107](frontend/src/components/hud/TelemetryHeader.tsx:107)).
- In cubic perovskites and diamond Si every position is fixed by symmetry, so the forces are exactly zero at *any* lattice constant.

Measured on the presets as shipped (MACE-MP-0-small, float64):

| Preset | max \|F\| (eV/Å) | Badge shown | Pressure P = −tr σ/3 | K at given V | B₀ at the model's V_eq |
|---|---|---|---|---|---|
| SrTiO₃ (a = 3.905 Å) | 0.0000 | **RELAXED** | **+5.1 GPa** | 185 GPa | 161 GPa |
| BaTiO₃ (a = 4.00 Å) | 0.0000 | **RELAXED** | **+4.6 GPa** | 186 GPa | 168 GPa |
| Si (a = 5.431 Å) | 0.0000 | **RELAXED** | **+1.4 GPa** | 77 GPa | 70 GPa |
| Li₃PS₄ | 1.32 | UNRELAXED | +3.8 GPa | 64 GPa | ≈45 GPa (extrapolated; forces not relaxed) |

The phonon "unrelaxed" warning (`PHONON_UNRELAXED_WARN_FORCE = 0.05`, also force-only) does not fire for these presets either. A user who loads BaTiO₃ and runs the shake-test therefore computes phonons at about 4.6 GPa. Pressure suppresses the ferroelectric soft mode in BaTiO₃, so the headline result is biased.

**Suggestion.**
- Show P and the full Voigt stress (as a hover tooltip or in the Structure card).
- Base RELAXED on `max|F| < fmax` **and** `max|σ_ij| < σ_tol`, with σ_tol around 0.05–0.1 GPa and configurable.
- Show "FORCES OK · CELL STRESSED" as a third, amber state.
- Apply the same test to the phonon-button warning.

### 3.3 Bulk modulus on demand, with the full equation-of-state result

**Problem.**
- K is only computed at the end of a flow generation ([ws_stream.py:820](backend/app/api/ws_stream.py:820)). Presets, CIF imports and relaxed structures all show "—".
- `fit_bulk_modulus` already returns `v_eq` and `k_eq_gpa` (B₀ at the model's equilibrium volume), but only K at the input volume is surfaced. Those two numbers differ by 15 % for SrTiO₃ (table above).

**Suggestion.**
- Add a "Compute K" action available on any structure.
- Report K(V_given), B₀, V₀, and B₀′ (from a wider ±6 % window when requested).
- Plot E(V) and P(V) in the dock.
- Label it as clamped-ion, since the docstring already notes this.

### 3.4 Expose relaxation and phonon settings

**Problem.** The UI hard-codes these settings:
- relaxation: `fmax 0.02, max_steps 100, relax_cell true` ([TransmuteToolbar.tsx:29](frontend/src/components/hud/TransmuteToolbar.tsx:29));
- phonons: `supercell [2,2,2], Δu 0.01` ([TransmuteToolbar.tsx:44](frontend/src/components/hud/TransmuteToolbar.tsx:44)).

The backend accepts all of them as parameters.

**Suggestion.** Add a small "settings" popover next to each action with these options:

- **Relaxation:** fmax, max steps, relax cell on/off, **keep symmetry** (ASE `FixSymmetry`; already used inside the generator), **target pressure** (`FrechetCellFilter(scalar_pressure=…)`), a separate stress tolerance, and an optimizer choice (FIRE, or BFGS/LBFGS, which converge faster near a minimum).
- **Phonons:** supercell dimensions with an auto-suggestion (§5.3), and Δu.
- When a relaxation stops at max_steps unconverged, offer a one-click **"Continue relaxation"**.

### 3.5 Generator form in the scientist's vocabulary

**Problem.** `StartFlowGenerationPayload.composition` exists ([messages.py:55](backend/app/schemas/messages.py:55)), but the sidebar only offers an element list and a 4–32 "Atoms / cell" slider ([GeneratorSidebar.tsx:186](frontend/src/components/hud/GeneratorSidebar.tsx:186)). The backend allows up to 64. Meanwhile "Physics guidance η" and "Integration steps" are sampler internals that mean nothing to most users.

**Suggestion.**
- Add a formula field ("Li2MnO3", "SrTiO3") plus "formula units per cell", mapped to `composition`.
- Keep the element-only mode as "let the server choose the stoichiometry".
- Move η, steps and seed under "Advanced".
- Show the chosen stoichiometry and space group before the run starts.

### 3.6 Units that match each community

- **Phonon frequencies:** THz ↔ cm⁻¹ ↔ meV. Raman and IR users think in cm⁻¹, neutron users in meV.
- **Energy differences:** meV/atom and meV per formula unit. The "Follow Instability" ΔE is already in meV/atom, so add per formula unit.
- **Pressure:** GPa ↔ kbar. VASP users think in kbar.
- Remember the user's choices in localStorage, as the dock layout already is.

---

## 4. Get results out (interoperability and reproducibility)

### 4.1 Symmetrized CIF and a "Refine symmetry" action

**Problem.** `atoms_to_cif` writes a symmetry-agnostic **P1** CIF ([structure_io.py:110](backend/app/core/structure_io.py:110)). A relaxed or Follow-Instability product, such as I4/mcm SrTiO₃, is exported as its 40-atom commensurate supercell in P1. VESTA, FullProf, GSAS-II and ICSD-style workflows all prefer a symmetrized CIF.

**Suggestion.**
- Export with `CifWriter(structure, symprec=…)` (space group plus asymmetric unit), with a choice of "conventional standard / primitive / as displayed".
- Add a **Refine** action that snaps the structure to its detected space group (spglib `standardize_cell` / pymatgen `get_refined_structure`) and optionally reduces it to the primitive cell. This removes the ~1e-4 Å noise left after a relaxation. It also shrinks the Follow-Instability supercell to its true primitive cell before the next shake-test (the 40-atom R-point supercell becomes a 10-atom I4/mcm primitive cell).

### 4.2 More export formats

| Export | Why |
|---|---|
| extxyz with energy, forces and stress | Fine-tuning data for MLIPs, and an input for ASE scripts |
| ASE `.traj` / extxyz of relaxation, flow and Follow trajectories | Lets users analyse the transition path elsewhere, or make movies |
| `phonopy_params.yaml` (with force constants) and `band.yaml` | Users can continue in phonopy (DOS, thermal properties, plotting) without recomputing |
| CSV for every chart (g(r), dispersion, relaxation curve) | Replotting for papers |
| SVG/PNG for charts; high-DPI viewport render on a white background, without bloom | Publication figures; the dark bloom theme is not printable |
| QE / LAMMPS / CASTEP inputs via ASE writers | Hand-off to DFT verification |

### 4.3 Getting structures in

**Problem.**
- Only a CIF file upload is offered.
- `atoms_from_cif` silently takes the first data block ([structure_io.py:76](backend/app/core/structure_io.py:76)).
- `MP_API_KEY` is already configured for the hull, but it cannot be used to load anything.

**Suggestion.**
- **Load from Materials Project:** accept "mp-5229" or a formula search. List the polymorphs with MP's E_hull and space group, and load the one picked.
- Import POSCAR, CONTCAR and extxyz files; allow drag-and-drop onto the viewport and pasting text.
- For multi-block CIFs, show a picker listing every block.
- Optionally fetch from COD or OPTIMADE: one OPTIMADE client covers MP, OQMD, Alexandria, COD and others.

### 4.4 Session persistence, undo and snapshots

**Problem.** On every (re)connect, the client calls `resetSession()` ([wsClient.ts:325](frontend/src/services/wsClient.ts:325)) and the server loads the default SrTiO₃ (`_cmd_init`). A backend `--reload`, a laptop sleep or a Wi-Fi blip therefore discards the user's structure, phonons and relaxation history without warning.

**Suggestion.**
- **Restore on reconnect (S):** the client keeps the last structure, as a CIF or payload, and re-sends `LOAD_STRUCTURE` on reconnect. Phonons can be marked "stale – re-run".
- **Undo/redo (M):** the server already versions structures (`StudioSession.version`). Keep a bounded stack of snapshots and add `UNDO` / `REDO` / `GOTO_VERSION` commands. Every transmute, drag or delete is one step.
- **Named snapshots and a history sidebar**, e.g. "parent Pm-3m", "after Follow R-ν0", "Na-doped".
- **Project files** (`*.latticeflow.json`) that bundle the structures, settings, results and model identity, so a session can be reopened tomorrow or sent to a colleague.

### 4.5 Provenance and a methods paragraph

Stamp every result (relaxation, phonons, K, hull) with:
- the model name and checkpoint hash, dtype and device;
- the code version;
- the full settings (fmax, stress tolerance, supercell, Δu, symprec, k-path source);
- the seed.

Add a **"Copy methods text"** button that writes, for example: *"Phonons were computed by finite displacement (0.01 Å) in a 2×2×2 supercell with phonopy vX using MACE-MP-0-small (float64)…"*. Reviewers ask for exactly this, and it makes the tool safe to cite.

---

## 5. Science capabilities with high value for little new code

### 5.1 Phonon DOS, projected DOS and thermodynamics (S–M)

`PhononAnalysis` keeps the `Phonopy` object (see the [phonon_engine.py](backend/app/core/phonon_engine.py) docstring), so no new force evaluations are needed:

- `run_mesh` → total and atom/element-projected DOS, as a pane next to the dispersion (the phonon DOS is already listed in spec §2.3);
- `run_thermal_properties` → F(T), S(T), C_v(T) and the zero-point energy.

The scientific payoff is in **Follow Instability**: comparing the parent and the product by *harmonic free energy*, not just 0 K energy, is the question people actually ask.

### 5.2 Textbook dispersions and mode symmetry (M)

**Problem.** Phonons run on the cell as displayed, with `primitive_matrix=np.eye(3)` ([phonon_engine.py:555](backend/app/core/phonon_engine.py:555)). The Si preset is the 8-atom conventional cell, so the chart shows **24 folded branches instead of the 6** in every textbook. Any conventional CIF from ICSD or COD has the same problem.

**Suggestion.**
- Add a "Standard dispersion" mode: `primitive_matrix="auto"` plus the standard seekpath path of the primitive cell, for comparison with literature and inelastic neutron data.
- Keep the current mode for animating the displayed cell. The eigenvectors map back through the primitive→cell transformation.
- **Band connection** (`is_band_connection=True`), so branches keep their colour through crossings.
- **Γ-point irreducible representations** (`phonopy.set_irreps`) with Raman/IR activity labels. Spectroscopists can then match computed modes to measured peaks, and the soft mode gets a name (e.g. R₄⁺ in SrTiO₃).

### 5.3 Supercell size and convergence (S)

The 2×2×2 default is fixed. For a 3.9 Å perovskite that gives 7.8 Å, and for a 2-atom fcc primitive cell less than that.

- Auto-choose the supercell so that every supercell vector is at least about 10–12 Å, subject to an atom budget, and show the chosen size.
- Add a **"Check convergence"** option: re-run at the next size and report the maximum |Δf| and the change in the soft-mode frequency.

### 5.4 LO–TO splitting (non-analytical correction) (S for the note, M for the feature)

Both perovskite presets are ionic insulators with large Born effective charges, so their LO–TO splitting near Γ is large. A uMLIP gives no Born charges, so the LO branches near Γ are wrong.

- Allow uploading a `BORN` file (Z\* and ε∞ from DFPT or the literature); phonopy then applies the NAC.
- Otherwise, show a "no NAC: LO modes near Γ not reliable" note for ionic insulators.

The soft modes and zone-boundary modes are unaffected, which is worth saying in the UI.

### 5.5 Elastic tensor and mechanical stability (M)

Fit stress against strain over 6×4 deformations of ±0.5 % and ±1 % (pymatgen `DeformedStructureSet` + `ElasticTensor.from_independent_strains`). The deformations can be batched through the existing `energy_forces_stress_batch`. For relaxed-ion constants, relax the positions at each fixed strain.

**Report:**
- the C_ij matrix;
- Voigt-Reuss-Hill K, G and E, Poisson's ratio and Pugh's G/K (ductile vs brittle);
- the Debye temperature;
- the **Born stability criteria**, shown as a "MECHANICALLY STABLE / UNSTABLE" badge next to the dynamical-stability badge.

This also gives the generator's bulk-modulus target a real check, since it is currently steered heuristically through the target pressure.

### 5.6 Simulated diffraction (S–M)

- Compute an XRD pattern with pymatgen's `XRDCalculator` (Cu Kα by default, wavelength selectable), plus a neutron pattern. Show it as a dock pane with hkl labels on hover.
- Let users upload an experimental `.xy` / `.xye` pattern and overlay it, with a peak-shift slider to account for MACE's lattice error.

For experimentalists this is the "is this my phase?" test, and it would probably bring in more users than any other single feature.

### 5.7 Local structure beyond the total g(r)

- **Partial g_αβ(r) and running coordination numbers.** `compute_rdf` returns only the total today.
- A bond-length and bond-angle table for the selected atom; a distance/angle measurement tool in the viewport (§7.4).
- **Polyhedra** (TiO₆, PS₄): render them, and report **octahedral tilt angles and Glazer notation** for perovskites. Follow Instability on SrTiO₃ at R should read "a⁰a⁰c⁻, tilt φ = …°". That single label makes the transition legible to every perovskite researcher.
- **Bond-valence sums and the global instability index.** These also fix a known open issue. Bond strain is currently measured against covalent-radius sums ([geometry_analysis.py:347](backend/app/core/geometry_analysis.py:347)), so ionic bonds always read as "compressed". Colouring by BVS deviation, or by the change relative to the relaxed parent's bond lengths, gives a physically meaningful heat map.

### 5.8 Finite-temperature molecular dynamics (M–L)

Harmonic phonons answer "is this a 0 K minimum?". They do not answer "is it stable at 300 K?", which is the spec's own "0 K trap" framing.

- Add ASE Langevin / NPT (Nosé–Hoover or Berendsen) with the same engine, streamed like a relaxation, with the temperature as a slider. You could watch cubic BaTiO₃ distort at low T and stay cubic on average at high T.
- **For Li₃PS₄**, a solid electrolyte preset: compute the mean-squared displacement → D_Li → σ (Nernst–Einstein), at several temperatures → an Arrhenius activation energy. Ionic conductivity is exactly the number that community wants.
- Add NEB (ASE `NEB`) for a vacancy or interstitial hop between two snapshots.

### 5.9 Defects done physically (M)

**Problem.** "Create Vacancy Defect" removes an atom from whatever cell is displayed. In the 5-atom SrTiO₃ cell that is one vacancy per formula unit, i.e. a 20 % vacancy concentration, not a point defect. The UI gives no warning. There is also no way to add an atom (interstitial).

**Suggestion.**
- A **"Make supercell"** action: n×m×k, plus non-diagonal shapes such as √2×√2×2. Warn when a defect is created in a cell whose shortest periodic distance is under about 10 Å.
- A new `ADD_ATOM` command for interstitials and adsorbates.
- **Defect formation energy:** E_f = E_def − E_bulk ± Σ μ_i. The chemical potentials μ_i come from the elemental and competing phases that the hull analyzer has *already relaxed and cached*, which lets you show the μ-range of the stability region. The same applies to substitution energies for "Li → Na".

### 5.10 Disorder, doping and solid solutions (M)

**Problem.** Disordered CIFs are rejected (`DISORDERED_STRUCTURE`, [structure_io.py:80](backend/app/core/structure_io.py:80)), yet most experimental CIFs of battery and solid-solution materials are partially occupied, including the real β-Li₃PS₄.

**Suggestion.**
- Offer to order the structure on import: enumerate small-cell orderings (pymatgen `OrderDisorderedStructureTransformation`, or enumlib), rank them by uMLIP energy, and let the user pick one. Alternatively, generate a quasi-random special-quasirandom-structure (SQS)-like cell.
- **Partial transmutation:** "replace 25 % of the Li sites with Na" produces N enumerated or random configurations, ranked by energy.

### 5.11 Hull: relax first, then put the number in context (S–M)

**Problem.** The candidate is scored as a single point of the structure **as given** ([hull_analyzer.py:24](backend/app/core/hull_analyzer.py:24)). For an unrelaxed import, or a preset at the experimental lattice constant, E_hull is inflated by strain energy: SrTiO₃ at +5 GPa is not at its uMLIP minimum. The green cut-off is a single fixed 25 meV/atom ([HullPopover.tsx:55](frontend/src/components/hud/HullPopover.tsx:55)).

**Suggestion.**
- Relax the candidate by default, using the same FIRE + cell settings as the references, or at least warn about it the way the phonon button does.
- Plot the **binary or ternary phase diagram**, with the candidate marked.
- Show the matched Materials Project entry (pymatgen `StructureMatcher`) and **MP's own DFT E_hull side by side** as a sanity check on the uMLIP hull.
- Put the value in context with the empirical metastability scale (Sun et al., *Sci. Adv.* 2016) rather than a single 25 meV cut.

---

## 6. Trust: models, uncertainty and validation

### 6.1 Model picker and modern foundation models (M)

**Problem.** One process-wide `UMLIPEngine` singleton is fixed by `LATTICEFLOW_MACE_MODEL`. The README itself says MACE-MP-0-small is soft: bulk moduli run 10–20 % low, and the Si optical mode is about 11.7 THz against about 15.5 THz in experiment.

**Suggestion.**
- Load models lazily by name and select them per session or per calculation. The installed `mace` already knows `medium`, `large`, `medium-mpa-0` and `medium-omat-0`.
- Add adapters for other ASE-calculator uMLIPs (CHGNet, SevenNet, MatterSim, ORB…).
- Recommended workflow: **explore with small, confirm with a larger model.**
- **D3 dispersion toggle:** `mace_mp(..., dispersion=True)` is supported by the installed version. It is essential for layered, vdW and molecular crystals.
- The hull cache is already keyed by `backend_name`, so the hull side is ready for this.

### 6.2 Uncertainty and out-of-distribution warnings (M)

- **Committee mode:** compute the key numbers (E_hull, K, the minimum phonon frequency, ΔE of the Follow product) with 2–3 models and show the spread. Highlight cases where the models **disagree on the sign** of a soft mode; that is the most useful single warning the tool could give.
- **Out-of-distribution flags:** contacts below about 0.75× the covalent-radius sum (the generator already uses this threshold), unusual coordination numbers, and elements that are sparse in the training data (lanthanides, actinides up to Z = 89).

### 6.3 Built-in benchmark page (S–M)

Add a page that compares each model with experiment or PBE on the presets and a few more reference systems:

- lattice constants and B₀;
- Γ and X phonon frequencies;
- E_hull = 0 for known ground states;
- the soft-mode frequency of SrTiO₃ at R.

Most of these values are already computed in the test suite. Seeing them up front is how users calibrate their trust before quoting a number.

### 6.4 Make fallback results unmistakable (S)

With the LJ fallback, the header shows an amber badge, but results in the dock and the hull popover are formatted exactly like MACE results. Watermark or disable K, the hull and the phonon results when the backend is `lj-fallback`.

---

## 7. Workflow and user experience

### 7.1 Stability scorecard and guided workflow (S–M)

The product's pitch is "is this crystal real?". Make that the organising UI element: one card with four checks.

| Check | Criterion | Source |
|---|---|---|
| Relaxed | forces **and** stress below tolerance | §3.2 |
| Dynamically stable | no imaginary modes; supercell converged | existing, plus §5.3 |
| Mechanically stable | Born criteria | §5.5 |
| Thermodynamically stable | E_hull of the *relaxed* candidate, in context | §5.11 |

Each check is marked **stale** when the structure changes. The hull pane already does this, so generalise it. Each check gets a "run" button, and running one offers to run its prerequisite first: "This cell is under 5 GPa. Relax first?"

A first-run **guided tour** should follow the canonical path: load SrTiO₃ → relax → shake-test → click the R-point soft mode → Follow → compare.

### 7.2 Compare mode (M)

Show two snapshots side by side, e.g. the Pm-3m parent and the I4/mcm product:

- ΔE per atom and per formula unit, and ΔF(T) once §5.1 exists;
- the change in lattice parameters and volume, and the tilt angles;
- overlaid dispersions and DOS;
- a structure-similarity check against known MP phases: "this product matches mp-XXXX".

### 7.3 Precise editing (S–M)

Dragging is great for intuition, but research needs numbers.

- **Numeric coordinate entry** (fractional or Cartesian) for the selected atom.
- **Lattice editing and applied strain:** edit a, b, c, α, β, γ directly, or apply hydrostatic, uniaxial or **biaxial (epitaxial)** strain. Epitaxial-strain phase diagrams of perovskite films are a classic use case.
- **Snap-to-Wyckoff** while dragging, and an "add atom at fractional coordinates" field.
- Keyboard shortcuts: R relax, P phonons, Ctrl+Z / Ctrl+Y undo/redo, 1–4 tool modes, and Esc stop (Esc already works).

### 7.4 A viewport for crystallographers (M)

- Buttons to view along a, b, c, [110], [111] or any [uvw], plus an **orthographic camera** toggle. Perspective hides layering and diffusion channels.
- Atom labels (element and index); a distance, angle and dihedral **measurement tool**.
- Polyhedra rendering (§5.7); n×m×k repeats beyond the 2×2×2 halo; display of (hkl) lattice planes.
- Colour atoms by a property: |F|, mode participation, coordination number, BVS deviation, displacement since the parent.
- A publication render: white background, no bloom, an axes triad, high DPI. Also a light theme.

### 7.5 Generator: honest framing and more useful output (M–L)

**Problem.** The main button says **"Synthesize Crystal (Riemannian Flow)"**, but the README says plainly that the sampler is not a learned model. Materials scientists will judge the whole tool by how accurate that claim is.

**Suggestion.**
- Rename it, e.g. "Generate symmetric candidate".
- Show what the search did: the Wyckoff assignment chosen, the number of candidates scored, and the energy ranking.
- **Return the top-k candidates** as a ranked list (energy, space group, and E_hull on demand) instead of a single winner.
- Add **prototype substitution**: replace ions in known MP prototypes, ranked by ionic-substitution probability (pymatgen `SubstitutionPredictor`). This is often the most productive practical route to new compounds.
- Build the **MatterGen adapter** that the spec promises, for genuinely learned generation and band-gap conditioning.

### 7.6 Headless mode and a Python client

Most computational materials scientists live in Jupyter. A small Python client would make the studio a *viewer and controller for notebook workflows* instead of a separate island:

```python
studio = latticeflow.connect()
studio.push(atoms)
studio.relax()
ph = studio.phonons()
```

Batch screening (a queue of compositions → generate → relax → phonons → hull → results table/CSV) follows naturally. This overlaps with the "Phase 6" idea already proposed (AI-driven or batch research automation), which has not been started.

### 7.7 Configurable size limits (S)

The limits are `MAX_ATOMS = 128` ([structure_io.py:23](backend/app/core/structure_io.py:23)), `PHONON_MAX_ATOMS = 64` ([ws_stream.py:106](backend/app/api/ws_stream.py:106)) and a generator slider capped at 32. Defect supercells, complex oxides and Follow-Instability products often need 100–300 atoms.

- Make the limits configurable through environment variables, with defaults that depend on whether a GPU is present.
- Show a time estimate before a large shake-test (number of displacements × supercell size).

### 7.8 Help text for learners (S)

Add hover explanations, with a short formula, for terms such as "imaginary mode", "E_hull", "Wyckoff site", "clamped-ion K" and "folded bands". The tool is already a strong teaching aid; a few sentences in the right places make it usable in a classroom without an instructor standing behind each student.

---

## 8. Suggested sequencing

**Phase A: trust and quick wins (about 1–2 weeks).**
- §3.1–3.6: structure panel, stress-aware badge, K on demand, settings, formula entry, units;
- §4.1: symmetrized CIF and Refine;
- §4.4: restore on reconnect, plus undo;
- §5.1: DOS and thermodynamics;
- §5.11: relax before computing the hull;
- §6.4: fallback watermark.

**Phase B: research-grade analyses (about 2–4 weeks).**
- §5.2 primitive dispersion and irreps;
- §5.3 supercell convergence;
- §5.5 elastic tensor;
- §5.6 XRD;
- §4.2–4.3 import/export (MP ID, POSCAR, phonopy files, figures);
- §6.1 model picker and D3;
- §7.1 stability scorecard.

**Phase C: new workflows (about 1–2 months).**
- §5.7 local structure, tilts and BVS;
- §5.8 MD and diffusion;
- §5.9–5.10 defects and disorder;
- §6.2 committee uncertainty;
- §7.2 compare mode;
- §7.5 generator top-k and prototypes;
- §7.6 Python client and batch screening.

---

## 9. Housekeeping noticed along the way (not user-facing)

- `__pycache__/*.pyc` files are staged in the git index. Add them to `.gitignore` and run `git rm -r --cached`.
- `docker-compose.yml` is untested. A one-command deployment matters for sharing within a lab group.
- To keep the "show what we compute" promise (§3.1–3.3), add a frontend test that checks every numeric field of `CrystalStatePayload` is displayed somewhere.
