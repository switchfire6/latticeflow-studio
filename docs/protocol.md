# LatticeFlow Studio protocol reference

This reference covers the WebSocket session (`/ws/studio`) and the REST API (`/api/*`). The contracts behind it are:

- `backend/app/schemas/crystal.py` (`CrystalStatePayload`);
- `backend/app/schemas/messages.py` (WebSocket payloads);
- `backend/app/schemas/api.py` (REST bodies);
- the session rules in `backend/app/api/ws_stream.py`.

Field-level detail lives in those files, and in `PROJECT_SPEC.md` §5 and §10. This page lists every message and what ends it. Every change to the protocol is additive.

## 1. Session model

- **One connection is one session.** Each session has its own active structure, its own undo history (at most 50 entries), its own force field and its own phonon cache. The browser UI and a Python client on the same backend do not share a session.
- **Envelope.** Every message is `{"type": "<TYPE>", "payload": {...}}`. Payloads are validated with pydantic, so omitted fields take their schema defaults.
- **Connect.** On connect the server loads the `srtio3` preset and sends `SINGLE_POINT_UPDATE` followed by `HISTORY_STATE` (label "Load preset srtio3").
- **Ordering.** Commands are processed in the order they are sent, and replies come back in that order. Messages carry no request ids, so a reply is matched to its command by that order and by the `command` field of `ERROR`, `PROGRESS` and `STREAM_STOPPED`.
- **Streams.** At most one stream runs at a time. Starting a stream, or sending a state-changing instant command, first cancels the running stream. The cancelled stream's `STREAM_STOPPED {stopped: "<old command>"}` comes before the new reply. `STOP_STREAM` with nothing running answers `STREAM_STOPPED {stopped: null}`.
- **One terminal group per stream.** Every stream ends with exactly one terminal group, and nothing from that job follows it. The group is one of:
  - the success group listed below;
  - `STREAM_STOPPED`;
  - `ERROR`.

  When a stream is stopped or fails after it has already applied frames, the last frame stays active. In that case a `HISTORY_STATE` ("… (stopped)" or "… (failed)") follows the `STREAM_STOPPED` / `ERROR`.
- **Phonon cache.** Any state change invalidates the phonon cache. After that, `REQUEST_EIGENMODE`, `FOLLOW_INSTABILITY`, `REQUEST_PHONON_DOS`, `CHECK_PHONON_CONVERGENCE` and `EXPORT_PHONON_DATA` answer `ERROR NO_PHONON_ANALYSIS`.
- **Limits.**

  | Limit | Value |
  |---|---|
  | Atoms per structure | 128 (`MAX_ATOMS`) |
  | Atoms per phonon cell | 64 |
  | Atoms per phonon supercell | 600 |
  | Elements | Z ≤ 89 |

- **Errors.** `ERROR {code, message, command}` leaves the connection open. The codes are the `ErrorCode` enum (§4).

## 2. WebSocket commands

"Reply" is the success sequence in wire order. In the Kind column:
- **I** = instant;
- **S** = streaming (cancellable, may send `PROGRESS {command, done, total, message}`; `total = 0` means unknown);
- **(sc)** = state-changing: pushes an undo entry and cancels a running stream.

| Command | Kind | Payload (defaults) | Reply |
|---|---|---|---|
| `LOAD_STRUCTURE` | I (sc) | exactly one of `preset_id`, `cif`, `text` + `text_format` (`auto`/`cif`/`poscar`/`extxyz`), `crystal` (a payload) | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` |
| `PERTURB_OR_TRANSMUTE_ATOM` | I (sc) | `atom_index`, `new_cart_coords?`, `new_element?`, `apply_to_equivalent_sites=false`, `auto_relax=false` | `SINGLE_POINT_UPDATE`, `HISTORY_STATE`; with `auto_relax` a relaxation stream follows (as `START_RELAXATION`, errors and stops tagged with this command) |
| `DELETE_ATOM` | I (sc) | `atom_index` | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` |
| `ADD_ATOM` | I (sc) | `element`, `frac_coords` | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` (`INVALID_OPERATION` if the new atom is < 0.5 Å from another) |
| `MAKE_SUPERCELL` | I (sc) | `matrix` 3×3 int, det > 0 | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` |
| `REFINE_STRUCTURE` | I (sc) | `symprec=0.01`, `cell=refined`/`conventional`/`primitive` | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` |
| `UNDO` / `REDO` | I (sc) | – | `SINGLE_POINT_UPDATE`, `HISTORY_STATE` (`INVALID_OPERATION` at either end) |
| `REQUEST_EIGENMODE` | I | `q_index`, `band_index` | `EIGENMODE_DATA {eigenmode}` |
| `REQUEST_PHONON_DOS` | I | `mesh?`, `t_min=0`, `t_max=1000`, `t_step=10` | `PHONON_DOS_DATA` |
| `EXPORT_PHONON_DATA` | I | – | `PHONON_EXPORT {filename, phonopy_yaml}` |
| `COMPUTE_BULK_MODULUS` | I | `strain_window=0.02`, `n_points=5` | `BULK_MODULUS_RESULT` (also stores `bulk_modulus_gpa` on the active payload) |
| `REQUEST_LOCAL_ANALYSIS` | I | `include_partial_rdf=true` | `LOCAL_ANALYSIS_DATA` |
| `START_RELAXATION` | S (sc) | `fmax=0.02`, `max_steps=100`, `relax_cell=true`, `fix_symmetry=false`, `pressure_gpa=0`, `stress_tol_gpa?`, `optimizer=FIRE`/`BFGS`/`LBFGS` | `RELAXATION_FRAME`… ending with `final=true`, then `HISTORY_STATE`. Divergence: final frame, `ERROR RELAXATION_DIVERGED`, `HISTORY_STATE` |
| `FOLLOW_INSTABILITY` | S (sc) | `q_index`, `band_index`, `amplitude=0.1` Å, `fmax=0.02`, `max_steps=200` | same as `START_RELAXATION`; the commensurate supercell becomes active (`MODE_NOT_IMAGINARY`, `NO_PHONON_ANALYSIS`, `STRUCTURE_TOO_LARGE`) |
| `START_FLOW_GENERATION` | S (sc) | `elements`, `num_atoms=12`, `composition?` (atoms per cell), `target_spacegroup?`, `target_crystal_system?`, `target_bulk_modulus_gpa?`, `steps=60`, `physics_guidance_scale=1.5`, `seed?`, `top_k=1` | `FLOW_TRAJECTORY_FRAME`…, then [`GENERATION_CANDIDATES` if `top_k > 1`], `CRYSTAL_STATE_FINAL`, `HISTORY_STATE`. Rank 1 is the active endpoint; load the other candidates with `LOAD_STRUCTURE {crystal}` |
| `RUN_PHONON_SHAKE_TEST` | S | `supercell_dim=[2,2,2]`, `displacement_distance=0.01`, `cell_mode=as_displayed`/`primitive`, `min_supercell_length?` (overrides `supercell_dim`) | `PHONON_ANALYSIS_COMPLETE`, then a `SINGLE_POINT_UPDATE` that flags the atoms and bonds of the softest mode (no new energy evaluation, no history entry) |
| `CHECK_PHONON_CONVERGENCE` | S | `supercell_dim?` (default: current + 1 on every axis, ≤ 600 atoms) | `PHONON_CONVERGENCE_RESULT`, `PHONON_ANALYSIS_COMPLETE` (replaces the cache), flagging `SINGLE_POINT_UPDATE` |
| `COMPUTE_ELASTIC_TENSOR` | S | `max_strain=0.01`, `n_strains=4`, `relax_ions=true`, `fmax=0.01` | `ELASTIC_TENSOR_RESULT` |
| `SET_MODEL` | S (sc) | `model_id` (from `GET /api/models`), `dispersion=false` (D3(BJ), MACE only) | `MODEL_CHANGED`, `SINGLE_POINT_UPDATE` (re-evaluated), `HISTORY_STATE` ("Switch model to …"); `MODEL_UNAVAILABLE` on failure (the old model is kept) |
| `COMMITTEE_EVALUATE` | S | `model_ids` (2–4), `relax=true`, `dispersion=false` | `COMMITTEE_RESULT` |
| `COMPUTE_FORMATION_ENERGY` | S | `bulk_position` (history index of the bulk) | `FORMATION_ENERGY_RESULT` (needs `MP_API_KEY` or cached references, else `INVALID_OPERATION`) |
| `SUBSTITUTE_FRACTION` | S (sc) | `from_element`, `to_element`, `fraction` (0, 1], `n_candidates=12`, `seed?` | `SUBSTITUTION_RESULT`, `SINGLE_POINT_UPDATE`, `HISTORY_STATE` |
| `START_MD` | S (sc) | `temperature_k=300`, `ensemble=NVT` (`NVE`/`NVT`/`NPT`), `timestep_fs=1`, `n_steps=2000`, `friction_per_fs=0.01`, `pressure_gpa=0` (NPT), `frame_interval=10`, `seed?` | `MD_FRAME` at step 0 and every `frame_interval` steps, then the last `MD_FRAME`, `MD_COMPLETE`, `HISTORY_STATE`. Divergence: `ERROR COMPUTE_FAILED` (the last frame stays active) |
| `PROTOTYPE_SEARCH` | S | `composition` (dict), `max_candidates=8`, `probability_threshold=1e-3`, `relax=true`, `with_e_hull=true` | `PROTOTYPE_CANDIDATES` (no active structure needed; no `MP_API_KEY`: `INVALID_OPERATION`) |
| `STOP_STREAM` | – | – | `STREAM_STOPPED {stopped}` |

Notes:

- **Drag coalescing.** Consecutive coordinate-only `PERTURB_OR_TRANSMUTE_ATOM` commands for the same atom (drag frames) are coalesced. One drag gesture is one undo step.
- **Stop while queued.** `STOP_STREAM` sent while a streaming command is still queued stops that command, not the one running before it.
- **Undo and models.** `UNDO` / `REDO` to an entry that was evaluated with another model re-evaluate it with the current model. Undo does not switch the model back.

## 3. Server events

| Event | Payload | Notes |
|---|---|---|
| `SINGLE_POINT_UPDATE` | `{crystal}` | new active structure (or phonon flags) |
| `HISTORY_STATE` | `can_undo`, `can_redo`, `position`, `length`, `label`, `entries[]` (`position`, `label`, `formula`, `n_atoms`, `energy_per_atom`, `spacegroup_symbol`) | after every history change |
| `RELAXATION_FRAME` | `step`, `converged`, `final`, `crystal` | step 0 is the start structure |
| `FLOW_TRAJECTORY_FRAME` | `step` (1..`total_steps`), `total_steps`, `t`, `crystal` | |
| `CRYSTAL_STATE_FINAL` | `{crystal}` | full-quality bonds and `bulk_modulus_gpa` |
| `GENERATION_CANDIDATES` | `summary` (space group, composition, Wyckoff combinations, structures screened and scored), `candidates[]` (`rank`, `energy_per_atom`, `relative_energy_mev_per_atom`, `spacegroup_*`, `wyckoff_assignment`, `crystal`) | |
| `PHONON_ANALYSIS_COMPLETE` | `is_dynamically_stable`, `min_frequency_thz`, `imaginary_mode_count`, `kpath_distances`, `kpath_labels`, `frequencies_thz[q][branch]` (ascending, signed: negative = imaginary), `eigenmodes` (every band at each labelled point), `cell_mode`, `n_primitive_atoms`, `supercell_dim`, `min_image_distance`, `band_order`, `gamma_modes` (irreps, IR/Raman activity), `band_path_source` | eigenvectors are indexed by the displayed atoms in both cell modes |
| `EIGENMODE_DATA` | `{eigenmode}` (`q_frac`, `frequency_thz`, N×3 real / imaginary eigenvectors) | |
| `PHONON_DOS_DATA` | `mesh`, `frequencies_thz`, `total_dos`, `projected_dos{el}`, `thermal` (F, S, C_v per atom, ZPE), `imaginary_fraction` | imaginary modes are excluded from the thermodynamics |
| `PHONON_CONVERGENCE_RESULT` | supercells and min frequencies a/b, `max_abs_delta_thz`, `rms_delta_thz`, `per_label_max_delta_thz`, `tolerance_thz=0.1`, `converged` | |
| `BULK_MODULUS_RESULT` | `k_gpa`, `k_stress_gpa`, `v0`, `pressure_gpa`, `v_eq`, `k_eq_gpa`, `k_eq_prime`, `extrapolated`, E(V) / P(V) data, `method` | clamped ion, fixed shape, BM-3 fit |
| `ELASTIC_TENSOR_RESULT` | `c_ij_gpa` 6×6 Voigt, `eigenvalues_gpa`, `born_stable`, K/G (Voigt, Reuss, VRH), Young's modulus, Poisson's ratio, Pugh's ratio, `universal_anisotropy`, `debye_temperature_k`, `relaxed_ions`, `reference_pressure_gpa`, `fit_residual_gpa`, `method` | |
| `PHONON_EXPORT` | `filename`, `phonopy_yaml` | loadable with `phonopy.load` |
| `MODEL_CHANGED` | `model_id`, `backend_name`, `dispersion`, `device`, `dtype` | |
| `LOCAL_ANALYSIS_DATA` | `rdf_r`, `partial_rdf`, `sites[]` (CN, neighbours, oxidation state, BVS), `bond_ideal_lengths`, `global_instability_index`, `polyhedra[]`, `octahedral_tilts` (`glazer`, angles, phases), `method` | |
| `FORMATION_ENERGY_RESULT` | `defect_label`, `delta_n`, `e_defect_ev`, `e_bulk_ev`, `chemical_potentials{limit}`, `formation_energies_ev{limit}`, `min_image_distance`, `warnings`, `method` | |
| `SUBSTITUTION_RESULT` | `n_substituted`, `n_tried`, `energies_per_atom`, `chosen_energy_per_atom`, `method` | |
| `MD_FRAME` | `step`, `time_ps`, `temperature_k`, potential and kinetic energy, `pressure_gpa`, `volume`, `msd_by_element`, `crystal` | |
| `MD_COMPLETE` | means of T, P and V, `times_ps`, `msd_by_element`, `diffusion_coefficients_cm2_s`, `final_crystal`, `method` | D = slope / 6 over the last half |
| `COMMITTEE_RESULT` | `entries[]` (pressure, max force, relaxed volume per atom, lattice, space group, energy), `spread{pressure_gpa, max_force, volume_percent, lattice_percent}`, `method` | absolute energies are not comparable between models |
| `PROTOTYPE_CANDIDATES` | `target_formula`, `n_sources`, `candidates[]` (`source_material_id`, `substitutions`, `probability`, `energy_per_atom`, `e_hull_ev`, `relaxed`, `crystal`), `method` | sorted by E_hull |
| `PROGRESS` | `command`, `done`, `total`, `message` | non-terminal |
| `STREAM_STOPPED` | `stopped` (command or null) | |
| `ERROR` | `code`, `message`, `command` | |

`CrystalStatePayload` holds:
- `formula`, space group (symbol and number), `lattice` (a, b, c, α, β, γ, row-vector `matrix`, `volume`);
- `atoms[]`: element, Z, fractional and Cartesian coordinates, force, `wyckoff`, `is_unstable_actor`;
- `bonds[]`: with `image_offset`;
- `energy_per_atom`, `max_force`, `stress_gpa` (Voigt, ASE sign);
- `pressure_gpa` (+ = compressed), `density_g_cm3`, `crystal_system`, `formula_units`, `symprec`;
- `bulk_modulus_gpa?`, `e_hull_ev?`, `rdf_r` / `rdf_g`.

## 4. Error codes

| Code | Meaning |
|---|---|
| `INVALID_MESSAGE` | not JSON, or not an envelope |
| `UNKNOWN_COMMAND` | unknown command type |
| `INVALID_PAYLOAD` | the payload failed validation |
| `INDEX_OUT_OF_RANGE` | atom, q, band or history index outside the active data |
| `INVALID_ELEMENT` | unknown symbol, or Z > 89 |
| `INVALID_OPERATION` | the operation is not possible (e.g. deleting the last atom) |
| `NO_ACTIVE_STRUCTURE` | no structure loaded |
| `NO_PHONON_ANALYSIS` | no valid phonon analysis for the current structure |
| `MODE_NOT_IMAGINARY` | `FOLLOW_INSTABILITY` on a stable mode |
| `UNKNOWN_PRESET` | unknown `preset_id` |
| `INVALID_STRUCTURE` | unparsable file, or an empty or degenerate cell |
| `DISORDERED_STRUCTURE` | CIF with partial occupancies (use `POST /api/ordering`) |
| `STRUCTURE_TOO_LARGE` | over an atom limit |
| `GENERATION_FAILED` | e.g. the composition does not fit the Wyckoff positions of the target group |
| `RELAXATION_DIVERGED` | the relaxation diverged |
| `COMPUTE_FAILED` | any other engine exception |
| `MODEL_UNAVAILABLE` | the model is unknown or cannot be loaded |
| `HULL_UNAVAILABLE` | REST only |
| `MP_UNAVAILABLE` | REST only |
| `MP_NOT_FOUND` | REST only |

## 5. REST endpoints

Every endpoint is stateless and none touches a WebSocket session. Errors return `{code, message}` with status:
- 422: validation or a bad structure;
- 404: `MP_NOT_FOUND`;
- 502 / 503: Materials Project or hull unavailable;
- 500: `COMPUTE_FAILED`.

| Endpoint | Body / query | Response |
|---|---|---|
| `GET /api/health` | – | `status`, `umlip_backend` (`lj-fallback` = analytic fallback), `device`, `dtype`, `load_error`, `hull_available` |
| `GET /api/presets` | – | preset list: `id` (`srtio3`, `li3ps4`, `si`, `batio3`), name, formula, space group, expected stability |
| `GET /api/models` | – | `models[]` (`id`, `label`, `description`, `family`, `checkpoint_cached`, `supports_dispersion`, `is_default`), `default_model_id` |
| `POST /api/import/cif` | `{cif}` | `CrystalStatePayload` (preview only; load it with `LOAD_STRUCTURE`) |
| `POST /api/export/cif` | `crystal`, `format=cif`/`poscar`/`extxyz`, `symmetrize=true`, `symprec=0.01`, `cell=as_is`/`conventional`/`primitive` (extxyz: `as_is` only) | text attachment |
| `POST /api/hull/evaluate` | `crystal`, `relax=true`, `model_id?`, `dispersion=false` | `chemsys`, `energy_per_atom`, `e_hull_ev`, `formation_energy_per_atom`, `decomposition`, `references[]`, `candidate_relaxed`, `energy_per_atom_as_given`, `method`. MP references are relaxed with the same model and cached. 503 `HULL_UNAVAILABLE` without `MP_API_KEY` |
| `POST /api/diffraction` | `crystal`, `kind=xray`/`neutron`, `radiation=CuKa`, `wavelength_angstrom?`, `two_theta_min=5`, `two_theta_max=90` | `peaks[]` (2θ, intensity scaled to a maximum of 100, d, hkls) |
| `GET /api/mp/search?q=` | formula, chemical system (`Li-Fe-O`) or mp-id | `results[]` (MP entries, ≤ 50, sorted by MP E_hull) |
| `GET /api/mp/structure/{id}` | – | `{entry, cif}` |
| `POST /api/mp/match` | `{crystal}` | `formula`, `n_searched`, `matches[]` (`entry`, `rms_displacement`) |
| `POST /api/compare` | `{crystal_a, crystal_b}` | `same_composition`, `structures_match`, rms / max displacement, both space groups, `group_subgroup`, volume per atom and change (%), energy difference (meV/atom, meV/f.u.), `method`. The energies are those carried by the payloads (same model assumed) |
| `POST /api/ordering` | `cif` (disordered), `max_atoms=64`, `n_candidates=8`, `model_id?`, `dispersion` | ordered `candidates[]` (`cif`, energy, relative energy), `n_enumerated`, `method` |
