# LatticeFlow Studio

An interactive crystal and phonon studio for materials scientists. You load, generate or edit a crystal and see the forces of a universal machine-learned interatomic potential (MACE) live in 3D. From there you can answer the usual questions in seconds to minutes, on a laptop GPU or a CPU:
- Is it relaxed?
- Is it dynamically stable?
- Is it on the hull?
- Is it mechanically stable?

You can also follow an imaginary soft mode into the lower-symmetry phase it drives. Everything the UI does is also available from Python, so the studio can serve as a viewer and controller for notebook workflows and batch screens.

- **Backend.** FastAPI, MACE (ASE calculator), phonopy, spglib, pymatgen. It serves one `/ws/studio` WebSocket session per client, plus stateless `/api/*` REST endpoints.
- **Frontend.** React, Three.js (react-three-fiber).
- **Python client.** [`clients/python`](clients/python).
- **Docs.**
  - [`docs/protocol.md`](docs/protocol.md): every command, event and endpoint.
  - [`docs/workflows.md`](docs/workflows.md): step-by-step scientific workflows.
  - [`PROJECT_SPEC.md`](PROJECT_SPEC.md): full design and contracts.

## What you can do, by question

| Question | Tools |
|---|---|
| **What is it?** | Load presets (SrTiO3, BaTiO3, Si, Li3PS4), CIF / POSCAR / extxyz files or Materials Project ids. The structure panel shows the space group, Wyckoff sites, Z, density and crystal system. You also get simulated X-ray and neutron diffraction, local analysis (coordination, polyhedra, bond-valence sums, global instability index, partial g(r), octahedral tilts in Glazer notation), structure comparison (StructureMatcher, group–subgroup relation, ΔV, ΔE) and matching against Materials Project entries. |
| **Is it relaxed?** | Live forces, stress and pressure. Relaxation with FIRE, BFGS or LBFGS, optionally under symmetry (FixSymmetry), at external pressure and with a stress tolerance. The RELAXED badge accounts for stress. Refine-to-space-group snaps a structure to its symmetry. |
| **Is it dynamically stable?** | Finite-displacement phonons (phonopy) of the displayed cell, or of the primitive cell along the standard seekpath path. Γ irreps with IR/Raman activity. A supercell convergence check. Eigenmode animation at any q. DOS, projected DOS and harmonic F, S, C_v and ZPE. phonopy export. **Follow instability**: displace along an imaginary mode in its commensurate supercell, then relax into the distorted phase. |
| **Is it on the hull?** | E_hull against Materials Project reference phases that were *relaxed with the same potential*, so energies are self-consistent (needs `MP_API_KEY`). The decomposition products and formation energy are reported. |
| **Is it mechanically stable?** | Bulk modulus from an equation-of-state fit. Full elastic tensor C_ij (relaxed-ion), Born stability, Voigt/Reuss/Hill K and G, Young's modulus, Poisson and Pugh ratios, anisotropy and Debye temperature. |
| **What happens at temperature?** | NVE / NVT / NPT molecular dynamics (Langevin, isotropic barostat) with live T, P, V and per-element MSD, and tracer diffusion coefficients. |
| **Defects and disorder** | Supercells, vacancies, substitutions (single, all symmetry-equivalent sites, or a fractional substitution with configuration search), interstitials. Neutral defect formation energies across the chemical-potential limits. Ordered approximants of disordered CIFs. |
| **How much do I trust the model?** | Model picker: MACE-MP-0 small / medium / large, MACE-MPA-0, MACE-OMAT-0, optional D3 dispersion. A model committee reports the spread of pressure, forces, volume and lattice across models. If MACE cannot load, a clear fallback watermark is shown. |
| **New candidates** | A symmetric-candidate generator (space-group-conditioned Wyckoff search, top-k ranked candidates, animated as a flow onto the endpoint). Prototype substitution into Materials Project structures, ranked by ionic-substitution probability and E_hull. |

Every committed change goes into the undo/redo history (50 entries). The session is restored after a reconnect.

## Canonical workflows

Full recipes are in [`docs/workflows.md`](docs/workflows.md):

1. **Soft-mode transition.** Cubic SrTiO3: relax, then phonons show an imaginary R-point mode. Follow it, and the structure relaxes into I4/mcm. Local analysis gives Glazer `a0a0c-`; comparing with the parent shows the group–subgroup relation and ΔE.
2. **Defect formation energy.** Relax the bulk, build a supercell, create a vacancy or substitution, relax at fixed cell, then compute E_f at each chemical-potential limit.
3. **Ionic diffusion.** Run NVT MD at several temperatures, take D(T) from the MSD, and fit an Arrhenius E_a.
4. **Model committee.** Measure the spread across MACE models before trusting a number; switch the session to a larger model.
5. **Screening.** Prototype search, or the generator with top-k. Then relax, take primitive phonons, the hull and the elastic tensor, and write a table.

## Quick start

**Backend.** Python 3.11, run from `backend/`.

```bash
uv venv .venv --python 3.11
# GPU (optional): install the CUDA build of torch first, e.g.
#   uv pip install --python .venv/Scripts/python.exe torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --env-file ../.env   # http://127.0.0.1:8000
```

On Linux or macOS, use `.venv/bin/python` instead of `.venv/Scripts/python.exe`.

- **Requirements.** `requirements.txt` mirrors `backend/pyproject.toml`. It includes:
  - `mace-torch`, `phonopy`, `seekpath`, `spglib`, `pymatgen`, `pyxtal` (the generator's Wyckoff tables);
  - `torch-dftd` (D3 dispersion for MACE models);
  - `mp-api` (Materials Project).
- **Environment file.** `--env-file ../.env` loads local settings, such as `MP_API_KEY`, from the repo-root `.env` (gitignored; copy `.env.example`). Without the file the backend still starts, and the hull, Materials Project import, prototype search and defect chemical potentials report themselves unavailable.
- **First start.** The first start downloads the MACE-MP-0 *small* checkpoint (about 40 MB) into `~/.cache/mace`. Every start then loads the model and runs a warm-up single point before accepting connections. Larger models from the model picker download on first use.
- **Fallback.** If the checkpoint can't be loaded (for example offline with no cache), the backend falls back to an analytic Lennard-Jones + Coulomb calculator. `GET /api/health` then reports `"umlip_backend": "lj-fallback"`. Fallback numbers are for testing only.

**Frontend.** Node 20+, run from `frontend/`.

```bash
npm install
npm run dev          # http://localhost:5173 (proxies /api and /ws to the backend on :8000)
npm run build        # type-check + production build into dist/
npm test             # vitest
npm run typecheck
```

To talk to a backend elsewhere, set `VITE_BACKEND_URL=http://host:8000` in `frontend/.env`.

**Docker (untested scaffold).** Run `docker compose up --build`, then open http://localhost:5173. It uses CPU torch by default; see the comments in `docker-compose.yml` for GPU.

## Python client

```bash
pip install -e clients/python          # deps: websockets, httpx; ase / pymatgen optional for conversions
```

```python
from latticeflow_client import Studio

with Studio("http://localhost:8000") as studio:          # its own session (separate from the browser UI)
    studio.load(preset="srtio3")                          # or cif=..., atoms=<ase.Atoms>, structure=<pymatgen>, mp_id=...
    studio.relax(fmax=0.005, fix_symmetry=True)
    ph = studio.phonons(cell_mode="primitive", dos=True)
    studio.follow_instability(label="R")                  # softest mode at R
    print(studio.local_analysis()["octahedral_tilts"]["glazer"])
    atoms = studio.to_ase()
```

How the client behaves:
- Each method sends one command and blocks until the documented terminal message arrives.
- `on_frame` and `on_progress` callbacks stream intermediate frames and progress; `stop()` cancels.
- An `ERROR` raises `StudioError(code, message)`; a cancelled stream raises `StreamStopped`.
- `AsyncStudio` offers the same API as coroutines.
- REST helpers: `hull()`, `diffraction()`, `compare()`, `mp_search()`, `mp_match()`, `export()` and more.

Batch screening: `latticeflow-screen compositions.csv --out results.csv --elastic [--hull]`.

Examples, including a Jupyter notebook, are in [`clients/python/examples`](clients/python/examples).

## Configuration

All settings are optional; see [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `LATTICEFLOW_DEVICE` | `auto` | `cuda`, `cuda:N` or `cpu` |
| `LATTICEFLOW_DTYPE` | `float64` | `float32` is faster but too noisy for the invariance gate and finite-displacement phonons |
| `LATTICEFLOW_UMLIP` | `auto` | `mace` (fail if unavailable) or `fallback` (analytic calculator only) |
| `LATTICEFLOW_MACE_MODEL` | `small` | process default model: a `mace_mp` name or a local checkpoint path (sessions can switch with the model picker) |
| `LATTICEFLOW_MACE_DOWNLOAD_TIMEOUT` | `180` | seconds allowed for a first-time checkpoint download |
| `LATTICEFLOW_MACE_ALLOW_DOWNLOAD` | `1` | `0` = never touch the network (local cache only) |
| `MP_API_KEY` | – | Materials Project key: hull, MP search/import/match, prototype search, defect chemical potentials |
| `LATTICEFLOW_HULL_CACHE_DIR` | `~/.cache/latticeflow/hull` | cache of relaxed hull reference phases (per chemical system and model) |
| `LATTICEFLOW_CORS_ORIGINS` | Vite on :5173 | comma-separated allowed origins |
| `VITE_BACKEND_URL` (frontend) | – (Vite proxy) | backend URL for the frontend |

Hard limits: 128 atoms per structure, 64 atoms per phonon cell, 600 atoms per phonon supercell, elements up to Z = 89.

## Tests

Backend, run from `backend/`:

```bash
.venv/Scripts/python.exe -m pytest -q                               # full suite (MACE tests skip if the checkpoint is unavailable)
.venv/Scripts/python.exe -m pytest tests/test_umlip_invariance.py   # Gate 1: E(3) + periodic invariance
.venv/Scripts/python.exe -m pytest tests/test_phonon_modes.py       # Gate 2: Si stable, BaTiO3/SrTiO3 soft modes
.venv/Scripts/python.exe -m pytest tests/test_ws_protocol.py        # Gate 3: WebSocket protocol round trips
LATTICEFLOW_SKIP_MACE=1 .venv/Scripts/python.exe -m pytest -q       # fallback calculator only
```

Frontend, from `frontend/`: `npm test`.

Python client, from the repo root (it starts an in-process backend with the fallback calculator; about 10 s):

```bash
uv pip install --python backend/.venv/Scripts/python.exe -e clients/python
backend/.venv/Scripts/python.exe -m pytest clients/python/tests -q
```

## Scientific scope and limits

- **The force field is a screening-grade universal potential.** MACE-MP-0 *small*, the default, is systematically soft:
  - bulk and elastic moduli run low (Si C11 ≈ 96 GPa against ≈ 166 GPa experimentally);
  - phonon frequencies are underestimated (the Si optical mode is about 11.7 THz against about 15.5 THz);
  - barriers come out too low.

  For quantitative numbers, use the larger models (MACE-MPA-0 / OMAT-0 medium) and check the committee spread. The potential gives no electronic properties: no band gaps, magnetism or charge states. Confirm anything important with DFT.
- **Phonons are harmonic,** computed by finite displacement in a finite supercell. There is no anharmonic renormalisation, so "unstable at 0 K" does not mean unstable at room temperature. Cubic SrTiO3 and BaTiO3 are the high-temperature phases.
- **No LO–TO splitting.** There is no non-analytical correction, so polar crystals show no LO–TO splitting at Γ. Check supercell convergence before trusting soft modes. Results for unrelaxed structures, such as the Li3PS4 preset as loaded, are not physically meaningful; relax first.
- **E_hull is self-consistent in the uMLIP.** It is measured against Materials Project structures relaxed with the same potential, not against MP's DFT energies. Its accuracy is bounded by the model's formation-energy error, typically tens of meV/atom.
- **Defects are neutral only.** There are no charge states and no finite-size or image-charge corrections. Cells are capped at 128 atoms, so defect–image distances are often below 10 Å; the studio warns when they are.
- **MD diffusion coefficients are tracer D from the MSD.** Nernst–Einstein conductivities assume uncorrelated hops (Haven ratio 1). Short runs in small cells give noisy D, and Arrhenius extrapolation to room temperature amplifies errors.
- **The generator is not a learned model.** It is a seeded, budgeted search over symmetric Wyckoff arrangements of the target space group. The lowest-energy candidates under the force field are kept (top-k), and the run is animated as a torus-geodesic flow onto the endpoint. High-symmetry targets come out clean (perovskite, diamond, rock salt). Low-symmetry targets with many free parameters give locally relaxed structures, not global minima. Compositions are atoms per cell (conventional for centred groups). For plausible new compounds, prototype substitution is usually the better route.
