# LatticeFlow Studio

An interactive inverse-crystal and phonon discovery studio. You can generate crystals with a symmetry-conditioned geodesic flow, drag and transmute atoms under live MACE-MP-0 forces, relax with FIRE, run a phonon "shake-test", and follow an imaginary soft mode into the lower-symmetry phase it drives.

The full design is in [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

## Quick start

**Backend.** Python 3.11; run from `backend/`.

```bash
uv venv .venv --python 3.11
# GPU (optional): install the CUDA build of torch first, e.g.
#   uv pip install --python .venv/Scripts/python.exe torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --env-file ../.env   # http://127.0.0.1:8000
```

`--env-file ../.env` loads local settings such as `MP_API_KEY` from the repo-root `.env` (gitignored; copy `.env.example`). Without that file the backend still starts, and the hull endpoint reports itself unavailable.

On Linux or macOS, use `.venv/bin/python` instead of `.venv/Scripts/python.exe`.

The first start downloads the MACE-MP-0 *small* checkpoint (about 40 MB) into `~/.cache/mace`. Every start then loads the model before accepting connections. If the checkpoint can't be loaded (for example offline with no cache), the backend falls back to an analytic Lennard-Jones + Coulomb calculator. In that case `GET /api/health` reports `"umlip_backend": "lj-fallback"`.

**Frontend.** Node 20+; run from `frontend/`.

```bash
npm install
npm run dev                                                    # http://localhost:5173
```

The Vite dev server proxies `/api` and `/ws` to the backend on port 8000.

**Docker (untested scaffold):** run `docker compose up --build`, then open http://localhost:5173. It uses CPU torch by default; see the comments in `docker-compose.yml` for GPU.

## Configuration

All settings are optional; see [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `LATTICEFLOW_DEVICE` | `auto` | `cuda`, `cuda:N` or `cpu` |
| `LATTICEFLOW_DTYPE` | `float64` | `float32` is faster but too noisy for the invariance gate and finite-displacement phonons |
| `LATTICEFLOW_UMLIP` | `auto` | `mace` (fail if unavailable) or `fallback` (analytic calculator only) |
| `LATTICEFLOW_MACE_MODEL` | `small` | any `mace_mp` model name or a local checkpoint path |
| `LATTICEFLOW_MACE_ALLOW_DOWNLOAD` | `1` | `0` = never touch the network |
| `MP_API_KEY` | – | enables `POST /api/hull/evaluate` (Materials Project reference phases) |
| `LATTICEFLOW_HULL_CACHE_DIR` | `~/.cache/latticeflow/hull` | relaxed hull reference cache |
| `LATTICEFLOW_CORS_ORIGINS` | Vite on :5173 | comma-separated allowed origins |

## Tests

Run from `backend/`:

```bash
.venv/Scripts/python.exe -m pytest -q                        # full suite (MACE tests skip if the checkpoint is unavailable)
.venv/Scripts/python.exe -m pytest tests/test_umlip_invariance.py   # Gate 1: E(3) + periodic invariance
.venv/Scripts/python.exe -m pytest tests/test_phonon_modes.py       # Gate 2: Si stable, BaTiO3/SrTiO3 soft modes
.venv/Scripts/python.exe -m pytest tests/test_ws_protocol.py        # Gate 3: WebSocket protocol round trips
LATTICEFLOW_SKIP_MACE=1 .venv/Scripts/python.exe -m pytest -q       # fallback calculator only
```

Frontend tests run from `frontend/` with `npm test`.

## Scientific scope and limits

- **The generator is not a learned model.** The built-in flow sampler searches a seeded, budgeted set of symmetric Wyckoff arrangements, keeps the lowest-energy one under the force field, and streams a torus-geodesic flow onto it with a MACE Langevin corrector. High-symmetry targets come out clean (perovskite, diamond, rock salt). Low-symmetry targets with many free parameters give locally relaxed structures, not global minima.
- **MACE-MP-0 small is a screening-grade universal potential.** It is systematically soft: bulk moduli run roughly 10–20 % low, and the Si optical phonon is about 11.7 THz against about 15.5 THz experimentally. It provides no electronic properties.
- **Phonons are harmonic,** computed by finite displacement in a 2×2×2 supercell of the cell as displayed. Results for unrelaxed structures, such as the Li₃PS₄ preset, are not physically meaningful; relax first.
- **E_hull is computed self-consistently in the uMLIP,** against Materials Project reference structures relaxed with the same potential. It is not compared with MP's DFT energies directly.
