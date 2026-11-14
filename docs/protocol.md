# LatticeFlow Studio protocol reference

This reference covers the WebSocket session (`/ws/studio`) and the REST API (`/api/*`). The contracts behind it are:

- `backend/app/schemas/crystal.py` (`CrystalStatePayload`);
- `backend/app/schemas/messages.py` (WebSocket payloads);
- `backend/app/schemas/api.py` (REST bodies);
- `backend/app/schemas/library.py` (library and job-queue bodies, §6–§7);
- the session rules in `backend/app/api/ws_stream.py`.

Field-level detail lives in those files, and in [`design/PROJECT_SPEC.md`](design/PROJECT_SPEC.md) §5 and §10. This page lists every message and what ends it. Every change to the protocol is additive.

## 1. Session model

- **One connection is one session.** Each session has its own active structure, its own undo history (at most 50 entries), its own force field and its own phonon cache. The browser UI and a Python client on the same backend do not share a session.
- **Envelope.** Every message is `{"type": "<TYPE>", "payload": {...}}`, plus an optional client-chosen `"request_id"` (string, ≤ 64 characters). Payloads are validated with pydantic, so omitted fields take their schema defaults.
- **Connect.** On connect the server loads the `srtio3` preset and sends `SINGLE_POINT_UPDATE` followed by `HISTORY_STATE` (label "Load preset srtio3").
- **Ordering.** Commands are processed in the order they are sent, and replies come back in that order.
- **Request ids.** A command sent with `request_id` gets that id back as a top-level `"request_id"` on every server message it causes:
  - its replies, stream frames, `PROGRESS` and terminal messages;
  - the `HISTORY_STATE` it pushes and its `ERROR` (including `INVALID_PAYLOAD` / `UNKNOWN_COMMAND`, and `INVALID_MESSAGE` when the id itself could be read);
  - the `STREAM_STOPPED` of its own stream, even when a later command (with a different id, or none) cancelled it, and the "… (stopped)" `HISTORY_STATE` after it.

  `STOP_STREAM`'s own id appears only on `STREAM_STOPPED {stopped: null}`. Messages with no originating request (the connect-time preset load) carry no id. Coalesced drag frames are never answered, so their ids never appear. Without ids, match replies by order and by the `command` field of `ERROR`, `PROGRESS` and `STREAM_STOPPED`.
- **Streams.** At most one stream runs at a time. Starting a stream, or sending a state-changing instant command, first cancels the running stream. The cancelled stream's `STREAM_STOPPED {stopped: "<old command>"}` comes before the new reply. `STOP_STREAM` with nothing running answers `STREAM_STOPPED {stopped: null}`.
- **One terminal group per stream.** Every stream ends with exactly one terminal group, and nothing from that job follows it. The group is one of:
  - the success group listed below;
  - `STREAM_STOPPED`;
  - `ERROR`.

  When a stream is stopped or fails after it has already applied frames, the last frame stays active. In that case a `HISTORY_STATE` ("… (stopped)" or "… (failed)") follows the `STREAM_STOPPED` / `ERROR`, and that message has `history_pushed: true`, so a client knows whether to wait for it. An `ERROR` with `after_terminal: true` follows the stream's own terminal frame (e.g. `RELAXATION_DIVERGED` after the final `RELAXATION_FRAME`); the stream is over either way.
- **Phonon cache.** Any state change invalidates the phonon cache. After that, `REQUEST_EIGENMODE`, `FOLLOW_INSTABILITY`, `REQUEST_PHONON_DOS`, `CHECK_PHONON_CONVERGENCE` and `EXPORT_PHONON_DATA` answer `ERROR NO_PHONON_ANALYSIS`.
- **Limits.**

  | Limit | Value |
  |---|---|
  | Atoms per structure | 128 (`LATTICEFLOW_MAX_ATOMS`) |
  | Atoms per phonon cell | 64 (`LATTICEFLOW_PHONON_MAX_ATOMS`) |
  | Atoms per phonon supercell | 600 |
  | Elements | Z ≤ 89 |

  The first two are configurable and reported by `GET /api/health` (`max_atoms`, `phonon_max_atoms`).
- **Message size.** Some messages are large: `PHONON_ANALYSIS_COMPLETE` of a 64-atom cell is several MB. Many WebSocket clients cap incoming messages at 1 MB (Python `websockets` by default), so disable or raise the client's maximum message size (`websockets.connect(..., max_size=None)`).

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
| `REQUEST_HISTORY_ENTRY` | I | `position` | `HISTORY_ENTRY {position, label, crystal}` (the stored structure; changes nothing, cancels nothing; `INDEX_OUT_OF_RANGE`) |
| `REQUEST_EIGENMODE` | I | `q_index`, `band_index` | `EIGENMODE_DATA {eigenmode}` |
| `REQUEST_PHONON_DOS` | I | `mesh?`, `t_min=0`, `t_max=1000`, `t_step=10` | `PHONON_DOS_DATA` |
| `EXPORT_PHONON_DATA` | I | – | `PHONON_EXPORT {filename, phonopy_yaml}` |
| `COMPUTE_BULK_MODULUS` | I | `strain_window=0.02`, `n_points=5` | `BULK_MODULUS_RESULT` (also stores `bulk_modulus_gpa` on the active payload) |
| `REQUEST_LOCAL_ANALYSIS` | I | `include_partial_rdf=true` | `LOCAL_ANALYSIS_DATA` |
| `START_RELAXATION` | S (sc) | `fmax=0.02`, `max_steps=100`, `relax_cell=true`, `fix_symmetry=false`, `pressure_gpa=0`, `stress_tol_gpa?`, `optimizer=FIRE`/`BFGS`/`LBFGS` | `RELAXATION_FRAME`… ending with `final=true`, then `HISTORY_STATE`. Divergence: final frame, `ERROR RELAXATION_DIVERGED`, `HISTORY_STATE` |
| `FOLLOW_INSTABILITY` | S (sc) | `q_index`, `band_index`, `amplitude=0.1` Å, `fmax=0.005`, `max_steps=300` | same as `START_RELAXATION`; the commensurate supercell becomes active (`MODE_NOT_IMAGINARY`, `NO_PHONON_ANALYSIS`, `STRUCTURE_TOO_LARGE`) |
| `START_FLOW_GENERATION` | S (sc) | `elements`, `num_atoms=12`, `composition?` (atoms per cell), `target_spacegroup?`, `target_crystal_system?`, `target_bulk_modulus_gpa?`, `steps=60`, `physics_guidance_scale=1.5`, `seed?`, `top_k=1` | `FLOW_TRAJECTORY_FRAME`…, then [`GENERATION_CANDIDATES` if `top_k > 1`], `CRYSTAL_STATE_FINAL`, `HISTORY_STATE`. Rank 1 is the active endpoint; load the other candidates with `LOAD_STRUCTURE {crystal}`. A `composition` that does not fit the target group is scaled ×2, ×3, ×4 (within the atom limit) before `GENERATION_FAILED`, e.g. NaCl in #225 becomes Na4Cl4 |
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
| `HISTORY_ENTRY` | `position`, `label`, `crystal` | reply to `REQUEST_HISTORY_ENTRY` |
| `HISTORY_STATE` | `can_undo`, `can_redo`, `position`, `length`, `label`, `entries[]` (`position`, `label`, `formula`, `n_atoms`, `energy_per_atom`, `spacegroup_symbol`) | after every history change |
| `RELAXATION_FRAME` | `step`, `converged`, `final`, `crystal` | step 0 is the start structure |
| `FLOW_TRAJECTORY_FRAME` | `step` (1..`total_steps`), `total_steps`, `t`, `crystal` | |
| `CRYSTAL_STATE_FINAL` | `{crystal}` | full-quality bonds and `bulk_modulus_gpa` |
| `GENERATION_CANDIDATES` | `summary` (space group, composition, Wyckoff combinations, structures screened and scored), `candidates[]` (`rank`, `energy_per_atom`, `relative_energy_mev_per_atom`, `spacegroup_*`, `wyckoff_assignment`, `crystal`) | |
| `PHONON_ANALYSIS_COMPLETE` | `is_dynamically_stable`, `min_frequency_thz`, `imaginary_mode_count`, `kpath_distances`, `kpath_labels`, `frequencies_thz[q][branch]` (ascending, signed: negative = imaginary), `eigenmodes` (every band at each labelled point), `cell_mode`, `n_primitive_atoms`, `supercell_dim`, `min_image_distance`, `band_order`, `gamma_modes` (irreps, IR/Raman activity), `band_path_source` | eigenvectors are indexed by the displayed atoms in both cell modes |
| `EIGENMODE_DATA` | `{eigenmode}` (`q_frac`, `frequency_thz`, N×3 real / imaginary eigenvectors) | |
| `PHONON_DOS_DATA` | `mesh`, `frequencies_thz`, `total_dos`, `projected_dos{el}`, `thermal` (F, S, C_v per atom, ZPE), `imaginary_fraction`, `eph_proxy?` | imaginary modes are excluded from the thermodynamics. `eph_proxy` (superconductivity Tier 0 proxies, `null` if they could not be computed): `omega_log_k`, `omega_2_k`, `omega_mean_k`, `omega_max_k`, `light_element_fraction`, `tc_grid[]` (`lam`, `mu_star`, `tc_k`; λ ∈ {0.3, 0.5, 0.75, 1, 1.5, 2} × μ* ∈ {0.10, 0.13}, λ-major), `tc_ref_k` (λ = 1, μ* = 0.10; `null` when not `reliable`), `reliable`, `warnings[]`, `method`. These come from a constant-coupling proxy α²F ∝ F(ω): the force field has no electrons, so λ is **assumed** and every T_c is Allen–Dynes at that assumed λ, a ranking scale, not a prediction (docs/design/SUPERCONDUCTIVITY_TIER0.md) |
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
| `STREAM_STOPPED` | `stopped` (command or null), `history_pushed` | `history_pushed`: a `HISTORY_STATE` for the stopped stream follows |
| `ERROR` | `code`, `message`, `command`, `history_pushed`, `after_terminal` | see §1 (one terminal group per stream) |

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
| `NOT_FOUND` | REST only: unknown project / structure / result / job id (404, §6–§7) |

## 5. REST endpoints

Every endpoint in this section is stateless and none touches a WebSocket session (the persistent library and job queue are §6–§7). Errors return `{code, message}` with status:
- 422: validation or a bad structure;
- 404: `MP_NOT_FOUND`;
- 502 / 503: Materials Project or hull unavailable;
- 500: `COMPUTE_FAILED`.

| Endpoint | Body / query | Response |
|---|---|---|
| `GET /api/health` | – | `status`, `umlip_backend` (`lj-fallback` = analytic fallback), `device`, `dtype`, `load_error`, `hull_available`, `max_atoms`, `phonon_max_atoms` |
| `GET /api/presets` | – | preset list: `id` (`srtio3`, `li3ps4`, `si`, `batio3`), name, formula, space group, expected stability |
| `GET /api/models` | – | `models[]` (`id`, `label`, `description`, `family`, `checkpoint_cached`, `supports_dispersion`, `is_default`), `default_model_id` |
| `POST /api/import/cif` | `{cif}` | `CrystalStatePayload` (preview only; load it with `LOAD_STRUCTURE`) |
| `POST /api/export/cif` | `crystal`, `format=cif`/`poscar`/`extxyz`, `symmetrize=true`, `symprec=0.01`, `cell=as_is`/`conventional`/`primitive` (extxyz: `as_is` only) | text attachment |
| `POST /api/hull/evaluate` | `crystal`, `relax=true`, `model_id?`, `dispersion=false` | `chemsys`, `energy_per_atom`, `e_hull_ev`, `formation_energy_per_atom`, `decomposition`, `references[]`, `candidate_relaxed`, `energy_per_atom_as_given`, `method`, `phase_diagram` (plot data, §8). MP references are relaxed with the same model and cached. 503 `HULL_UNAVAILABLE` without `MP_API_KEY` |
| `POST /api/diffraction` | `crystal`, `kind=xray`/`neutron`, `radiation=CuKa`, `wavelength_angstrom?`, `two_theta_min=5`, `two_theta_max=90` | `peaks[]` (2θ, intensity scaled to a maximum of 100, d, hkls) |
| `GET /api/mp/search?q=` | formula, chemical system (`Li-Fe-O`) or mp-id | `results[]` (MP entries, ≤ 50, sorted by MP E_hull; each also carries `band_gap` (eV) and `is_metal` when MP has them, else `null`) |
| `GET /api/mp/structure/{id}` | – | `{entry, cif}` |
| `POST /api/mp/match` | `{crystal}` | `formula`, `n_searched`, `matches[]` (`entry`, `rms_displacement`) |
| `POST /api/superconductivity/metallicity` | `crystal`, `use_mp=true` | `verdict` (`metal` / `likely_metal` / `likely_insulator` / `insulator` / `unknown`), `source` (`materials_project` / `heuristic`), `band_gap_ev`, `mp_id`, `rms_displacement`, `reason`. With an MP key: the GGA/GGA+U gap of the best `StructureMatcher` match (`metal` / `insulator`; GGA underestimates gaps). Without a key, on an MP error or with no match it falls back to a composition heuristic (`likely_*`) and is still a 200; `reason` says why |
| `POST /api/compare` | `{crystal_a, crystal_b}` | `same_composition`, `structures_match`, rms / max displacement, both space groups, `group_subgroup`, volume per atom and change (%), energy difference (meV/atom, meV/f.u.), `method`. The energies are those carried by the payloads (same model assumed) |
| `POST /api/ordering` | `cif` (disordered), `max_atoms=64`, `n_candidates=8`, `model_id?`, `dispersion` | ordered `candidates[]` (`cif`, energy, relative energy), `n_enumerated`, `method` |

## 6. Project library (`/api/projects`, `/api/structures`, `/api/results`)

The library persists structures and results across sessions in one SQLite file, `$LATTICEFLOW_DATA_DIR/library.db` (default `~/.latticeflow`). It is shared by every session and the job queue. It never changes a WebSocket session; to work on a stored structure, send `LOAD_STRUCTURE {crystal}`.

Conventions:
- **Ids** are integers. **Timestamps** are ISO 8601 UTC strings with milliseconds, e.g. `"2026-11-13T09:30:00.123Z"`.
- **Errors** use the usual `{code, message}` body: 404 `NOT_FOUND` (unknown id), 409 `INVALID_OPERATION` (state conflict), 422 `INVALID_PAYLOAD` (or `INVALID_ELEMENT` / `INVALID_STRUCTURE` for a bad `crystal`).
- **Deletes** answer 204 with no body.

### 6.1 Projects

A default project "Scratch" (`is_default: true`) always exists. It can be renamed but not deleted.

| Endpoint | Body / query | Response |
|---|---|---|
| `GET /api/projects` | – | `Project[]`, default project first, then by id |
| `POST /api/projects` | `{name}` (1–200 chars) | 201 `Project` |
| `GET /api/projects/{pid}` | – | `Project` |
| `PATCH /api/projects/{pid}` | `{name}` | `Project` |
| `DELETE /api/projects/{pid}` | – | 204. Deletes its structures, results and finished jobs. 409 for the default project or while the project has queued / running jobs |
| `GET /api/projects/{pid}/export` | – | the export bundle (§6.4) |
| `POST /api/projects/import?name=` | the export bundle | 201 `Project`: a **new** project (`name` overrides the bundle's name). 413 `INVALID_PAYLOAD` when the body is larger than `LATTICEFLOW_MAX_IMPORT_MB` (default 50 MB) |

`Project`:

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `name` | string | names need not be unique |
| `is_default` | bool | |
| `created_at`, `updated_at` | string | `updated_at` changes with any structure or result change in the project |
| `n_structures` | int | |

### 6.2 Structures

| Endpoint | Body / query | Response |
|---|---|---|
| `POST /api/projects/{pid}/structures` | `StructureCreate` | 201 `Structure` (`existing: false`), or 200 the already stored `Structure` (`existing: true`, nothing changed) when the project holds the same structure |
| `GET /api/projects/{pid}/structures` | – | `StructureListRow[]` by id (no `crystal`) |
| `GET /api/structures/{sid}` | – | `Structure` |
| `PATCH /api/structures/{sid}` | `{name?, notes?}` | `Structure` |
| `DELETE /api/structures/{sid}` | – | 204. Deletes its results; children keep existing with `parent_id: null`. 409 while a queued / running job uses it |

`StructureCreate`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `crystal` | `CrystalStatePayload` | required | stored verbatim, exactly as the app sends it |
| `name` | string? | `crystal.formula` | |
| `parent_id` | int? | null | must be a structure of the same project (else 422) |
| `origin` | string | `"import"` | free label. The queue writes `"relax"` and `"follow <q> ν<band>"` (e.g. `"follow R ν0"`) |
| `notes` | string | `""` | |

`Structure` = `StructureRow` + `crystal` (`CrystalStatePayload`) + `existing` (bool; meaningful on POST only, otherwise false).

`StructureRow`: `id`, `project_id`, `name`, `formula`, `n_atoms`, `spacegroup_symbol`, `spacegroup_number`, `content_hash`, `parent_id` (int or null), `origin`, `notes`, `created_at`, `updated_at`.

`StructureListRow` = `StructureRow` + two fields for a results table:
- `latest`: object. The newest summary (§6.3) of every kind stored for the structure, merged into one flat object. Summary keys are unique across kinds, so e.g. `{"energy_per_atom_ev": -5.42, "steps": 35, "min_frequency_thz": -0.3, "dynamically_stable": false, "k_gpa": 97.1, ...}`. "Newest" means the latest `created_at` per kind, whatever the model or settings. Empty `{}` when there are no results.
- `latest_result_ids`: object, kind → id of that newest result (fetch its `data` with `GET /api/results/{id}`).

**Dedup (`content_hash`).** The hash is SHA-256 over:
- the lattice matrix, rounded to 0.001 Å;
- the sorted list of (element, u, v, w), with fractional coordinates wrapped into [0, 1) and rounded to 0.001 (a coordinate that rounds to 1.0 counts as 0).

Atom order, energies, forces and bonds do not matter. It is unique per project, so the same structure can be stored in several projects.

### 6.3 Results

| Endpoint | Body / query | Response |
|---|---|---|
| `POST /api/structures/{sid}/results` | `ResultCreate` | 201 `ResultRow` (`replaced: false`), or 200 `ResultRow` (`replaced: true`) when it overwrote a result with the same key |
| `GET /api/structures/{sid}/results?kind=` | optional `kind` filter | `ResultRow[]`, newest first (no `data`) |
| `GET /api/results/{rid}` | – | `Result` (= `ResultRow` + `data`) |
| `DELETE /api/results/{rid}` | – | 204 |

`ResultCreate`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `relax` \| `phonons` \| `elastic` \| `eos` \| `hull` \| `md` \| `diffraction` | required | |
| `data` | object | required | must validate as that kind's shape (table below), else 422 |
| `model_id` | string? | the process default model id (`GET /api/models` `default_model_id`) | send the session's model (`MODEL_CHANGED.model_id`) |
| `dispersion` | bool | false | |
| `settings` | object | `{}` | the request that produced it, e.g. the `RUN_PHONON_SHAKE_TEST` payload |

**Replacement.** One row exists per key (structure, kind, model_id, dispersion, settings), where `settings` is compared as canonical JSON. A newer result with the same key overwrites that row **in place**: same `id`, new `data`, `summary`, `created_at` and `code_version`.

`ResultRow`:

| Field | Type | Notes |
|---|---|---|
| `id`, `structure_id` | int | |
| `kind` | string | |
| `model_id` | string | e.g. `"small"`, `"lj-fallback"` |
| `dispersion` | bool | |
| `settings` | object | |
| `summary` | object | computed by the server from `data` (table below) |
| `created_at` | string | |
| `code_version` | string | backend version that stored it, e.g. `"0.3.0"` |
| `replaced` | bool or null | POST only |

`data` has the same JSON as the existing event or response for that calculation, so it can be fed straight back into the existing panes. Large `data` is compressed on disk, but the API always returns plain JSON.

| Kind | `data` is the payload of | `summary` keys |
|---|---|---|
| `relax` | the **final** `RELAXATION_FRAME` (`{step, converged, final: true, crystal}`). A `FOLLOW_INSTABILITY` result uses the same shape | `energy_per_atom_ev` (crystal.energy_per_atom), `max_force_ev_a` (crystal.max_force), `pressure_gpa` (crystal.pressure_gpa), `relax_converged` (converged), `steps` (step) |
| `phonons` | `PHONON_ANALYSIS_COMPLETE` | `min_frequency_thz`, `imaginary_mode_count`, `dynamically_stable` (is_dynamically_stable), `supercell` (string such as `"3x3x3"`, or null) |
| `elastic` | `ELASTIC_TENSOR_RESULT` | `k_vrh_gpa`, `g_vrh_gpa`, `born_stable` |
| `eos` | `BULK_MODULUS_RESULT` | `k_gpa`, `v0` |
| `hull` | the `POST /api/hull/evaluate` response (`HullEvaluateResponse`, including `phase_diagram`) | `e_hull_ev`, `formation_energy_ev` (formation_energy_per_atom) |
| `md` | `MD_COMPLETE` | `temperature_k` (mean_temperature_k), plus `diffusion_<El>_cm2_s` per element of `diffusion_coefficients_cm2_s` (e.g. `diffusion_Li_cm2_s`) |
| `diffraction` | the `POST /api/diffraction` response | `n_peaks`, `strongest_peak_two_theta` |
| `superconductivity` | `{descriptors, metallicity, flags[], dos_mesh, imaginary_fraction}`: `descriptors` is the `eph_proxy` object of `PHONON_DOS_DATA`, `metallicity` the `POST /api/superconductivity/metallicity` response | `omega_log_k`, `omega_2_k`, `tc_ref_k` (assumed-λ ranking scale, not a prediction), `light_element_fraction`, `metallicity` (the verdict), `sc_flags` (comma-joined flags, `""` if none), `sc_gate_pass` (`reliable` and the verdict is not `insulator` / `likely_insulator`) |

Numeric summary values are `null` when not finite or missing.

### 6.4 Export bundle

`GET /api/projects/{pid}/export` returns one self-contained JSON object. Jobs are not exported.

```json
{
  "format": "latticeflow-project",
  "version": 1,
  "exported_at": "2026-11-13T09:30:00.123Z",
  "code_version": "0.3.0",
  "project": {"name": "Perovskites", "created_at": "..."},
  "structures": [{"id": 3, "name": "SrTiO3", "formula": "SrTiO3", "content_hash": "...", "parent_id": null,
                  "origin": "import", "notes": "", "created_at": "...", "crystal": {}}],
  "results": [{"structure_id": 3, "kind": "phonons", "model_id": "small", "dispersion": false, "settings": {},
               "summary": {}, "data": {}, "created_at": "...", "code_version": "0.3.0"}]
}
```

Ids inside the bundle are only references within the bundle. `POST /api/projects/import` creates a new project with new ids, keeps the `parent_id` lineage, recomputes hashes and summaries, and gives imported results the import time as `created_at`. It answers 422 if `format` is not `latticeflow-project` or `version` is newer than 1, and 413 if the body is larger than `LATTICEFLOW_MAX_IMPORT_MB` (default 50; checked against `Content-Length` and against the bytes received, before the bundle is parsed).

## 7. Job queue (`/api/jobs`)

The queue runs calculations in the background and writes their results into the library. It has one worker thread for the whole server.

**Chains.** `POST /api/jobs` creates one **chain** per structure. A chain is one job per step, sharing a `chain_id`, with `step_index` 0..`n_steps`-1.
- Each step runs on the output structure of the last `relax` / `follow` step before it, or on the chain's starting structure. A job's `structure_id` is its input; for later steps it is updated when an earlier `relax` / `follow` finishes.
- A failed step marks the remaining queued steps of its chain `failed`; a cancelled step marks them `cancelled`. In both cases `error` says "previous step (job N) failed/was cancelled". Other chains continue.
- A `skipped` step lets the chain continue on the same structure.

**Order and priority.**
- The worker runs the queued job with the lowest `position` whose previous chain step is `done` or `skipped`.
- Interactive work comes first. While any WebSocket session is streaming, no job starts, and a running job pauses at its next checkpoint. Its `message` is then `"paused: an interactive session is computing"` and `GET /api/jobs` reports `paused: true`.
- Model calls use the same engines as the sessions (`model_id` / `dispersion`; null = process default).

**Cancellation.**
- A queued job is cancelled immediately.
- A running job gets `message: "cancelling"` and becomes `cancelled` at its next checkpoint: a relaxation step, a phonon force chunk or an elastic strain. EOS and hull steps are checked only before and after they run.

**Restart.** On startup, jobs left `running` go back to `queued` (`message: "requeued after restart"`). A server shutdown also requeues the running job.

**Status values:** `queued`, `running`, `done`, `failed`, `cancelled`, `skipped`. The finished statuses are `done`, `failed`, `cancelled` and `skipped`.

| Endpoint | Body / query | Response |
|---|---|---|
| `POST /api/jobs` | `JobCreate` | 201 `Job[]`: every created job, chain by chain in step order |
| `GET /api/jobs?project_id=&active=` | `project_id` optional; `active=true` = queued + running, `false` = finished, omitted = all | `{jobs: Job[], paused: bool}`, ordered by `position`. Cheap; poll it every ~1.5 s |
| `GET /api/jobs/{id}` | – | `Job` |
| `POST /api/jobs/{id}/cancel` | – | `Job` after the change. 409 if already finished |
| `POST /api/jobs/{id}/move` | `{position}` (int ≥ 0) | `Job`. `position` is the target index in the list of *queued* jobs (0 = runs next; clamped to the end). 409 unless queued |
| `DELETE /api/jobs/{id}` | – | 204. 409 unless finished |
| `POST /api/jobs/clear-finished?project_id=` | `project_id` optional | `{deleted: int}`: removes finished jobs (the results and structures stay) |

`JobCreate`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id` | int | required | |
| `structure_ids` | int[] (1–500) | required | structures of that project (else 422) |
| `steps` | `JobStep[]` (1–20) | required | |
| `model_id` | string? | null (process default) | an id from `GET /api/models` |
| `dispersion` | bool | false | |

`JobStep`: `{kind, settings = {}, only_if_unstable = false}`. Omitted settings take the **interactive UI defaults**. The job stores the full, normalised settings.

| `kind` | `settings` (defaults) | Writes |
|---|---|---|
| `relax` | `START_RELAXATION` fields: `fmax=0.02`, `max_steps=100`, `relax_cell=true`, `fix_symmetry=false`, `pressure_gpa=0`, `stress_tol_gpa=0.05` (ignored without `relax_cell`), `optimizer=FIRE` | a child structure (`origin "relax"`, `parent_id` = input) and a `relax` result **on the child**. If the relaxed structure dedups to an existing structure, that one is used. Divergence fails the step (`RELAXATION_DIVERGED: …`) |
| `phonons` | `RUN_PHONON_SHAKE_TEST` fields: `cell_mode=primitive`, `min_supercell_length=10` (dropped when you give `supercell_dim`), `displacement_distance=0.01` | a `phonons` result on the input |
| `elastic` | `COMPUTE_ELASTIC_TENSOR` fields: `max_strain=0.01`, `n_strains=4`, `relax_ions=true`, `fmax=0.01` | `elastic` result |
| `eos` | `COMPUTE_BULK_MODULUS` fields: `strain_window=0.02`, `n_points=5` | `eos` result |
| `hull` | `relax=true` (as `POST /api/hull/evaluate`) | `hull` result. Needs `MP_API_KEY` or cached references, else the step fails (`HULL_UNAVAILABLE: …`) |
| `follow` | `amplitude=0.1`, `fmax=0.005`, `max_steps=300` | see below |
| `superconductivity` | `mesh=null` (automatic DOS mesh) or `[i, j, k]`, `use_mp=true` | a `superconductivity` result on the input (see below) |

How `follow` works:
- It must come after a `phonons` step with no `relax` / `follow` in between; otherwise `POST` answers 422.
- It follows the softest imaginary mode among the labelled high-symmetry modes (`eigenmodes`) whose commensurate supercell fits `max_atoms`, exactly like `FOLLOW_INSTABILITY`.
- It writes a child structure (`origin "follow <q> ν<band>"`, e.g. `"follow R ν0"`; Γ written as `Γ`) and a `relax` result on the child. That result's settings add `follow: true`, `q_index`, `band_index`, `q_label` and `frequency_thz`.
- If the phonons are dynamically stable, the step is `skipped` when `only_if_unstable` is true, else `failed` (`MODE_NOT_IMAGINARY: …`).

`only_if_unstable` on any other kind is a 422.

How `superconductivity` works (Tier 0 pre-screen; proxies at an assumed λ, no electrons):
- Like `follow`, it must come after a `phonons` step with no `relax` / `follow` in between (else 422). It does not use up the phonons, so `phonons → superconductivity → follow` is valid.
- It reuses the chain's phonon analysis (or reruns the last phonons settings), computes the DOS on `mesh` and takes its `eph_proxy`, then runs the metallicity gate with the server's MP key. MP errors never fail the step; the gate falls back to the composition heuristic.
- `flags`: `light_elements` (light-element fraction ≥ 0.05), `high_omega_log` (ω_log ≥ 400 K), `soft_mode` (stable phonons, but at a labelled high-symmetry q-point other than Γ the lowest branch is below 0.1 · ω_max), `unstable` (descriptors not reliable), `insulating` (verdict `insulator` / `likely_insulator`).
- The job message reads like `ω_log 312 K · T_c,ref 21.9 K · metal (MP)`.

`Job`:

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `project_id` | int | |
| `structure_id` | int or null | input structure of this step (null if it was deleted) |
| `chain_id` | string | 32 hex characters |
| `step_index`, `n_steps` | int | |
| `kind` | string | step kind |
| `settings` | object | full settings |
| `only_if_unstable` | bool | |
| `model_id` | string or null | as requested (null = default) |
| `dispersion` | bool | |
| `status` | string | see above |
| `progress` | float 0..1 | relaxations: step / max_steps; elastic: strains done; phonons stay 0 until done (the `message` counts force evaluations); 1 when finished |
| `message` | string | human-readable state, e.g. `"step 12"`, `"converged in 35 steps"`, `"unstable (min -2.31 THz)"`, `"skipped: the phonons are dynamically stable"` |
| `error` | string or null | for `failed` (prefixed with an error code where one applies, e.g. `"STRUCTURE_TOO_LARGE: …"`) and `cancelled` |
| `result_id` | int or null | the result written (for `relax` / `follow`, it lives on `output_structure_id`). A later identical result overwrites it in place (§6.3), so the id stays valid |
| `output_structure_id` | int or null | `relax` / `follow` only |
| `position` | int | queue order (global across projects) |
| `created_at`, `started_at`, `finished_at` | string or null | |

## 8. Phase-diagram data (`POST /api/hull/evaluate` → `phase_diagram`)

The hull response (and a stored `hull` result's `data`) carries `phase_diagram`. It holds everything a binary or ternary formation-energy plot needs. The MP calls and the reference cache are unchanged.

| Field | Type | Notes |
|---|---|---|
| `elements` | string[] | alphabetical, the same as `chemsys` |
| `entries` | `PhaseDiagramPoint[]` | one per reference phase, in the same order as `references` |
| `candidate` | `PhaseDiagramPoint` | the evaluated structure (it is not part of the hull) |
| `facets` | int[][] | hull simplices as indices into `entries`, each sorted: index pairs (tie lines) for a binary, triples for a ternary. Empty for a single element |

`PhaseDiagramPoint`:

| Field | Type | Notes |
|---|---|---|
| `material_id` | string or null | MP id; null for the candidate |
| `formula` | string | reduced formula |
| `composition` | object | element → atom fraction for **every** element of `elements` (0 when absent; sums to 1). For a binary A-B use `composition[B]` as x; for a ternary use barycentric coordinates |
| `formation_energy_per_atom` | float | eV/atom relative to the relaxed elemental references (elements: 0) |
| `e_above_hull` | float | eV/atom; 0 for hull vertices. For the candidate it equals `e_hull_ev` (negative = below the reference hull) |
| `on_hull` | bool | references: a stable vertex of the hull; candidate: `e_hull_ev ≤ 1e-6` |
