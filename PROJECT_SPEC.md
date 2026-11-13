# LatticeFlow Studio: Inverse Crystal & Phonon Discovery Engine
**Agent Execution Blueprint & System Specification (`v1.1`)** — see §9 for the v1.0 → v1.1 consistency changelog.

---

## 1. Executive Summary & Product Vision

**LatticeFlow Studio** is an interactive, real-time 3D generative materials discovery workbench that bridges open-weights crystal foundation models with Universal Machine Learning Interatomic Potentials (uMLIPs) and lattice dynamics.

Currently, computational materials scientists rely on disconnected command-line workflows: generating static crystallographic text files (`.cif` or `POSCAR`) at absolute zero ($T = 0\text{ K}$) and manually inspecting them in legacy desktop software without live physics feedback. Approximately $40\%$ to $60\%$ of AI-generated crystals suffer from the **"$0\text{ K}$ Hallucination Trap"**—they appear geometrically plausible as frozen, symmetry-constrained static structures, but exhibit imaginary harmonic phonon modes ($\omega^2 < 0$), meaning the structure is a saddle point of the potential-energy surface rather than a true minimum and will spontaneously distort into a different (usually lower-symmetry) phase once the symmetry constraint is released.

LatticeFlow Studio solves this by unifying three engines into a single $60\text{ FPS}$ WebGL/Three.js workspace:
1. **Riemannian Flow Matching Viewport:** Streams the continuous denoising trajectory of fractional atomic coordinates $\mathbf{S}(t)$, lattice vectors $\mathbf{L}(t)$, and chemical species $\mathbf{A}(t)$ conditioned on target macroscopic properties (bulk modulus $K$, space group symmetry, and element filters; band gap $E_g$ conditioning is available only through the optional MatterGen adapter, since the built-in uMLIP-guided sampler cannot predict electronic properties).
2. **"Grab-and-Transmute" Neural Force-Field Sandbox:** Allows users to physically drag atoms in 3D, create vacancies, or transmute chemical elements (e.g., $\text{Li} \rightarrow \text{Na}$ or $\text{Si} \rightarrow \text{Ge}$) while a Universal Machine Learning Interatomic Potential (**MACE-MP-0** / **CHGNet**) computes real-time interatomic forces $\mathbf{F}_i$, stress tensors $\sigma_{\alpha\beta}$, and structural relaxation trajectories in milliseconds.
3. **Phonon Stability Shake-Test Engine:** Computes the supercell dynamical matrix $D(\mathbf{q})$ via **Phonopy** + uMLIPs in seconds, animating the 3D harmonic thermal vibrations of the crystal and highlighting dynamically unstable bonds ($\omega^2 < 0$) in pulsing crimson alongside an interactive phonon dispersion band chart.

---

## 2. System Architecture & Technology Stack

### 2.1 High-Level Architecture
The system operates as a decoupled, low-latency client-server application communicating via REST (for metadata, file export, and Materials Project database lookups) and binary/JSON **WebSockets** (for real-time streaming of flow-matching trajectories, FIRE relaxation steps, and phonon eigenmode tensors).

### 2.2 Backend Stack (Python $3.11+$)
* **API & Streaming Server:** `FastAPI`, `uvicorn`, `websockets`, `pydantic >= 2.0`
* **Crystallography & Symmetry:** `pymatgen`, `ase` (Atomic Simulation Environment), `spglib` (space group identification and Wyckoff symmetry constraints)
* **Universal Neural Force Field (uMLIP):** `mace-torch` (**MACE-MP-0** medium/small foundation model) with fallback support for `chgnet` or `matgl`
* **Generative Crystal Engine:** Modular adapter supporting **MatterGen** / **FlowMM** checkpoints, plus a built-in **Symmetry-Conditioned Riemannian Langevin Flow Sampler** (powered by MACE energy gradients + `spglib` symmetry projections) so the system works out-of-the-box without requiring a $20\text{ GB}$ external checkpoint download on day one
* **Lattice Dynamics & Phonons:** `phonopy`, `seekpath` (automatic high-symmetry Brillouin zone $\mathbf{k}$-path generation)
* **Thermodynamic Hull Verification:** `mp-api` (Materials Project REST client) for $E_{\text{hull}}$ convex hull phase diagram construction

### 2.3 Frontend Stack (TypeScript & WebGL)
* **Framework:** `Vite` + `React 18+` + `TypeScript`
* **3D Rendering Engine:** `three`, `@react-three/fiber` (R3F), `@react-three/drei`, `@react-three/postprocessing`
* **State Management:** `zustand` (high-frequency transient state updates for $60\text{ FPS}$ atomic coordinate buffers without triggering React re-renders)
* **Scientific Plotting:** `plotly.js-dist-min` or `visx` (Phonon Dispersion $\omega(\mathbf{q})$ curves, Phonon Density of States, Radial Distribution Function $g(r)$, and FIRE Relaxation Energy curves)
* **Styling & UI:** `tailwindcss`, `lucide-react`, `leva` or custom glassmorphic scientific HUD panels

---

## 3. Repository Directory Structure

The coding agent MUST initialize and maintain the following monorepo structure. The repository root is the existing project folder that contains this file (the `latticeflow-studio/` name below is illustrative; do not create a nested folder). `backend/pyproject.toml` is the single source of truth for Python dependencies; `requirements.txt` mirrors it.

```text
latticeflow-studio/
├── README.md
├── PROJECT_SPEC.md
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                  # FastAPI entrypoint & CORS configuration
│   │   ├── config.py                # Model paths, GPU device selection, MP_API_KEY
│   │   ├── api/
│   │   │   ├── routes.py            # REST endpoints (.cif export, presets, hull check)
│   │   │   └── ws_stream.py         # WebSocket handler for live trajectories
│   │   ├── schemas/
│   │   │   ├── crystal.py           # Pydantic models for Lattice, Structure, Forces
│   │   │   └── messages.py          # WebSocket request/response payload schemas
│   │   ├── core/
│   │   │   ├── umlip_engine.py      # MACE-MP-0 singleton wrapper, energy/force/stress
│   │   │   ├── relaxer.py           # ASE FIRE / FrechetCellFilter streaming relaxer
│   │   │   ├── flow_generator.py    # Riemannian Flow Matching & MACE-guided sampler
│   │   │   ├── phonon_engine.py     # Phonopy supercell, dynamical matrix, eigenmodes
│   │   │   ├── geometry_analysis.py # CrystalNN bonds, RDF g(r), spglib spacegroups
│   │   │   └── hull_analyzer.py     # Materials Project convex hull E_hull calculator
│   │   └── presets/                 # Curated seed crystals (Perovskites, Solid Electrolytes)
│   └── tests/
│       ├── test_umlip_invariance.py # SE(3) & periodic boundary invariance tests
│       ├── test_phonon_modes.py     # Stable vs. unstable (imaginary mode) verification
│       └── test_ws_protocol.py      # Async WebSocket streaming integration tests
└── frontend/
    ├── package.json
    ├── tsconfig.json
    ├── vite.config.ts
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── store/
        │   └── useCrystalStore.ts   # Zustand store with transient Float32Array buffers
        ├── services/
        │   ├── wsClient.ts          # Reconnecting WebSocket client & frame dispatcher
        │   └── apiClient.ts         # REST client for CIF import/export & MP hull lookup
        ├── components/
        │   ├── viewport/
        │   │   ├── CrystalCanvas.tsx        # R3F Canvas, lighting, post-processing
        │   │   ├── InstancedAtoms.tsx       # GPU-instanced atoms with CPK colors & drag
        │   │   ├── InstancedBonds.tsx       # Dynamic cylinder bonds + strain coloring
        │   │   ├── UnitCellBox.tsx          # Deforming triclinic wireframe (a, b, c)
        │   │   ├── ForceVectors.tsx         # 3D arrow helpers for -grad(E) vectors
        │   │   ├── FlowTrails.tsx           # Ribbon trails during generative denoising
        │   │   └── PhononDisplacement.tsx   # Real-time harmonic eigenmode vertex animator
        │   ├── hud/
        │   │   ├── GeneratorSidebar.tsx     # Target sliders (Bulk Mod, Space Group, Elements)
        │   │   ├── TransmuteToolbar.tsx     # Periodic table popover, vacancy/drag tools
        │   │   ├── TelemetryHeader.tsx      # Spacegroup, Energy/atom, Max Force, E_hull
        │   │   └── PhononControlBar.tsx     # Mode selector, amplitude & temperature sliders
        │   └── charts/
        │       ├── PhononBandChart.tsx      # Interactive dispersion plot (click k-point)
        │       ├── RdfChart.tsx             # Live Radial Distribution Function g(r)
        │       └── RelaxationCurve.tsx      # Energy & max force convergence plot
        └── utils/
            ├── cpkColors.ts         # Atomic radii & standard Jmol/CPK hex colors
            └── latticeMath.ts       # Fractional-to-Cartesian matrix conversions
```

---

## 4. Mathematical & Scientific Core Specifications

All backend physics modules and frontend shaders MUST strictly adhere to the following mathematical formulations.

### 4.1 Crystallographic Representation & Periodic Tuple
A periodic crystal unit cell containing $N$ atoms is represented by the tuple $\mathcal{C} = (\mathbf{A}, \mathbf{S}, \mathbf{L})$:
1. **Atomic Species:** $\mathbf{A} = (Z_1, Z_2, \dots, Z_N) \in \mathbb{Z}_+^N$, representing atomic numbers.
2. **Fractional Coordinates:** $\mathbf{S} = [\mathbf{s}_1, \mathbf{s}_2, \dots, \mathbf{s}_N]^T \in [0, 1)^{N \times 3}$, living on the flat 3-torus $\mathbb{T}^3$ to strictly enforce periodic boundary conditions (PBC).
3. **Lattice Matrix:** $\mathbf{L} = [\mathbf{a}, \mathbf{b}, \mathbf{c}]^T \in \mathbb{R}^{3 \times 3}$, derived from the 6 lattice parameters $(a, b, c, \alpha, \beta, \gamma)$.

The conversion from fractional coordinates $\mathbf{s}_i = (u_i, v_i, w_i)^T$ to Cartesian coordinates $\mathbf{r}_i = (x_i, y_i, z_i)^T$ (in $\text{\AA}$) is given by:
$$\mathbf{r}_i = \mathbf{L}^T \mathbf{s}_i$$
where the rows of $\mathbf{L}$ are the lattice vectors $\mathbf{a}, \mathbf{b}, \mathbf{c}$ (the ASE / pymatgen row-vector convention, so equivalently $\mathbf{r}_i^T = \mathbf{s}_i^T \mathbf{L}$) and the standard lower-triangular orientation ($\mathbf{a} \parallel \hat{x}$, $\mathbf{b}$ in the $xy$-plane) is:
$$\mathbf{L} = \begin{bmatrix} a & 0 & 0 \\ b \cos\gamma & b \sin\gamma & 0 \\ c \cos\beta & c \frac{\cos\alpha - \cos\beta \cos\gamma}{\sin\gamma} & c \frac{\sqrt{1 - \cos^2\alpha - \cos^2\beta - \cos^2\gamma + 2\cos\alpha\cos\beta\cos\gamma}}{\sin\gamma} \end{bmatrix}$$

### 4.2 Riemannian Flow Matching on the Periodic Torus $\mathbb{T}^3$
During inverse crystal generation from $t = 0$ (prior noise) to $t = 1$ (target crystal manifold):
* **Fractional Coordinate Geodesics:** Because $\mathbf{S} \in \mathbb{T}^3$, the shortest vector between $\mathbf{s}_0$ and $\mathbf{s}_1$ wraps around the periodic boundary:
  $$\Delta \mathbf{s} = (\mathbf{s}_1 - \mathbf{s}_0 + 0.5) \bmod 1.0 - 0.5$$
  The conditional flow trajectory at time $t \in [0, 1]$ follows:
  $$\mathbf{s}_t = \left(\mathbf{s}_0 + t \cdot \Delta \mathbf{s}\right) \bmod 1.0$$
* **Physics-Guided Predictor-Corrector Step:** At each integration step $t_k \rightarrow t_{k+1}$, the generator applies a velocity field update $\mathbf{v}_\theta(\mathcal{C}_t, t, \mathbf{y})$ conditioned on target vector $\mathbf{y} = (K_{\text{target}}, \text{SpaceGroup}, \text{ElementSet})$, and blends a Langevin corrector step using the MACE-MP-0 force field when $t > 0.65$ to eliminate atomic overlaps (Pauli repulsion):
  $$\mathbf{r}_{t + \Delta t} \leftarrow \mathbf{r}_t + \mathbf{v}_\theta \Delta t + \eta(t) \frac{\mathbf{F}_{\text{MACE}}(\mathbf{r}_t)}{\|\mathbf{F}_{\text{MACE}}(\mathbf{r}_t)\| + \epsilon} + \sqrt{2\,\eta(t)\,\tau(t)}\;\boldsymbol{\xi}, \quad \boldsymbol{\xi} \sim \mathcal{N}(0, \mathbf{I})$$
  where $\eta(t)$ has units of $\text{\AA}$ (step length, scaled by `physics_guidance_scale`) and the noise temperature $\tau(t) \rightarrow 0$ as $t \rightarrow 1$ so the final frames are deterministic. The corrector acts on Cartesian positions; the result is mapped back to the torus via $\mathbf{s} = (\mathbf{L}^{-T}\mathbf{r}) \bmod 1$ after every step.
* **Built-in sampler target (no trained network):** For the built-in sampler, $\mathbf{v}_\theta$ is not a learned network. The endpoint $\mathbf{s}_1$ is constructed by projecting the current configuration onto the Wyckoff positions of the target space group (`spglib` / `pymatgen.symmetry`), and $\mathbf{v}_\theta = \Delta\mathbf{s} / (1 - t)$ along the torus geodesic above. $K_{\text{target}}$ steers the lattice volume (stiffer target $\rightarrow$ denser cell). A learned $\mathbf{v}_\theta$ (and band-gap conditioning) is provided only by the MatterGen / FlowMM adapter.

### 4.3 Neural Force-Field Relaxation ("Grab-and-Transmute")
Given any perturbed or transmuted structure $\mathcal{C}$, the uMLIP evaluates total potential energy $E_{\text{tot}}$ (in $\text{eV}$), atomic forces $\mathbf{F}_i \in \mathbb{R}^3$ (in $\text{eV/\AA}$), and virial stress tensor $\sigma_{\alpha\beta} \in \mathbb{R}^{3 \times 3}$ (in $\text{GPa}$):
$$\mathbf{F}_i = -\nabla_{\mathbf{r}_i} E_{\text{tot}}(\mathbf{A}, \mathbf{S}, \mathbf{L}), \quad \sigma_{\alpha\beta} = \frac{1}{\Omega} \frac{\partial E_{\text{tot}}}{\partial \varepsilon_{\alpha\beta}}$$
where $\Omega = |\det(\mathbf{L})|$ is the unit cell volume in $\text{\AA}^3$. Stress follows the ASE sign convention (positive = tensile) and is converted from $\text{eV/\AA}^3$ to $\text{GPa}$ ($1\text{ eV/\AA}^3 = 160.21766\text{ GPa}$).
* **Interactive Relaxation Loop:** Uses ASE's `FIRE` optimizer wrapped with `FrechetCellFilter` (allowing simultaneous atomic position and unit-cell volume/angle relaxation). Every optimizer step emits a WebSocket frame until $\max_i \|\mathbf{F}_i\| < f_{\text{max}}$ (default $f_{\text{max}} = 0.02\text{ eV/\AA}$).
* **Bulk Modulus Estimation:** Computed on-the-fly by applying isotropic volumetric strains $\epsilon = \Delta V / V_0 \in \{-0.02, -0.01, 0, +0.01, +0.02\}$ (i.e., scaling $\mathbf{L}$ by $(1+\epsilon)^{1/3}$ at fixed fractional coordinates) and fitting the Birch-Murnaghan equation of state:
  $$K = V_0 \left. \frac{\partial^2 E}{\partial V^2} \right|_{V = V_0}$$
  Here $V_0$ is the volume of the structure **as given** (not the model's equilibrium volume), so $K$ describes the displayed cell; the two differ by roughly $10\%$ for the unrelaxed presets. The estimator returns `null` when the fit is not meaningful (non-periodic cell, non-convex energy, or disagreement with the stress-derived $-V\,\mathrm{d}P/\mathrm{d}V$).
* **Divergence handling:** If a relaxation diverges (non-finite energies or forces, runaway volume or forces), the relaxer restores the last good structure, emits a last `RELAXATION_FRAME` with `final = true, converged = false`, and the server follows it with an `ERROR` event (`code = "RELAXATION_DIVERGED"`).

### 4.4 Phonon Dynamical Matrix & Harmonic Eigenmodes
To execute the **Phonon Stability Shake-Test**:
1. Construct a $2 \times 2 \times 2$ supercell via `phonopy` and generate symmetry-inequivalent finite atomic displacements ($\Delta u = 0.01\text{ \AA}$).
2. Batch-evaluate forces on all displaced supercells in a single GPU pass using `mace-torch` to assemble the harmonic force constant matrix:
   $$\Phi_{l\kappa\alpha, l'\kappa'\beta} = \frac{\partial^2 E_{\text{tot}}}{\partial u_{l\kappa\alpha} \partial u_{l'\kappa'\beta}} \approx -\frac{F_{l'\kappa'\beta}(\Delta u_{l\kappa\alpha}) - F_{l'\kappa'\beta}(0)}{\Delta u_{l\kappa\alpha}}$$
3. For any wavevector $\mathbf{q}$ along the Brillouin zone high-symmetry path (computed via `seekpath`), construct the $3N \times 3N$ Hermitian Dynamical Matrix $D_{\kappa\alpha, \kappa'\beta}(\mathbf{q})$:
   $$D_{\kappa\alpha, \kappa'\beta}(\mathbf{q}) = \frac{1}{\sqrt{m_\kappa m_{\kappa'}}} \sum_{l'} \Phi_{0\kappa\alpha, l'\kappa'\beta} \exp\left(i \mathbf{q} \cdot (\mathbf{R}_{l'\kappa'} - \mathbf{R}_{0\kappa})\right)$$
4. Solve the eigenvalue problem:
   $$D(\mathbf{q}) \mathbf{e}_{\mathbf{q}\nu} = \omega_{\mathbf{q}\nu}^2 \mathbf{e}_{\mathbf{q}\nu}, \quad \nu \in \{1, \dots, 3N\}$$
5. **Imaginary Mode Convention:** Frequencies are reported as signed real numbers in THz (this is exactly what `phonopy` returns, so no extra conversion is applied):
   $$f_{\mathbf{q}\nu} = \text{sign}(\omega_{\mathbf{q}\nu}^2) \frac{\sqrt{|\omega_{\mathbf{q}\nu}^2|}}{2\pi} \quad (\text{in THz})$$
   A mode is **dynamically unstable** (`is_imaginary = true`) iff $f_{\mathbf{q}\nu} < -f_{\text{tol}}$ with a single global tolerance $f_{\text{tol}} = 0.05\text{ THz}$, which absorbs numerical noise in the 3 translational acoustic modes near $\mathbf{q} = \Gamma$. The same $f_{\text{tol}}$ defines `is_dynamically_stable`, `imaginary_mode_count`, and Verification Gate 2.
6. **Real-Time 3D Atomic Displacement Animation:** In the Three.js viewport, for a selected mode $(\mathbf{q}, \nu)$ at animation time $t$, the Cartesian position of atom $\kappa$ in supercell image $l$ is displaced by:
   $$\mathbf{u}_{l\kappa}(t) = \frac{A_{\text{amp}}}{2\pi \max(|f_{\mathbf{q}\nu}|, f_{\text{floor}})} \sqrt{\frac{\text{k}_\text{B} T}{m_\kappa}} \text{Re}\left[ \mathbf{e}_\kappa(\mathbf{q}, \nu) \exp\left(i (\mathbf{q} \cdot \mathbf{R}_{l\kappa} - 2\pi |f_{\mathbf{q}\nu}| t_{\text{anim}})\right) \right]$$
   This is the classical equipartition amplitude $\sqrt{\text{k}_\text{B} T / (m_\kappa \omega^2)}$ (a length, not a velocity); $f_{\text{floor}} = 0.5\text{ THz}$ prevents divergence for near-zero acoustic modes, $A_{\text{amp}}$ is a dimensionless user gain (default $1$), and $t_{\text{anim}}$ is wall-clock time multiplied by a slow-motion factor so THz oscillations are visible at $60\text{ FPS}$.

### 4.5 Radial Distribution Function $g(r)$
Computed dynamically on every structure update over $r \in [0.5\text{ \AA}, 8.0\text{ \AA}]$ with Gaussian smearing $\sigma_r = 0.08\text{ \AA}$:
$$g(r) = \frac{\Omega}{4\pi r^2 N^2} \sum_{i=1}^N \sum_{(j,\mathbf{n}) \neq (i,\mathbf{0})} \frac{1}{\sqrt{2\pi \sigma_r^2}} \exp\left(-\frac{(r - d_{ij\mathbf{n}})^2}{2\sigma_r^2}\right)$$
The inner sum runs over **all periodic images** $\mathbf{n} \in \mathbb{Z}^3$ of every atom $j$ (including images of atom $i$ itself) with $d_{ij\mathbf{n}} = \|\mathbf{r}_j + \mathbf{L}^T\mathbf{n} - \mathbf{r}_i\| \le 8.0\text{ \AA} + 4\sigma_r$, so that $g(r) \rightarrow 1$ at large $r$ even when the cell is smaller than the $8\text{ \AA}$ cutoff. The grid is fixed at $\Delta r = 0.05\text{ \AA}$ ($151$ points), giving `rdf_r` and `rdf_g` constant length.

---

## 5. Backend API & WebSocket Protocol Specification

### 5.1 Core Data Contracts (`backend/app/schemas/crystal.py`)

```python
from pydantic import BaseModel, Field
from typing import List, Optional, Dict

class LatticeParams(BaseModel):
    a: float
    b: float
    c: float
    alpha: float
    beta: float
    gamma: float
    matrix: List[List[float]]  # 3x3 row-major Cartesian lattice vectors (Angstroms)
    volume: float

class AtomSite(BaseModel):
    index: int
    element: str               # e.g., "Li", "Fe", "O"
    atomic_number: int         # Z
    frac_coords: List[float]   # [u, v, w] in [0, 1)
    cart_coords: List[float]   # [x, y, z] in Angstroms
    force: List[float]         # [Fx, Fy, Fz] in eV/Angstrom
    force_norm: float          # ||F|| in eV/Angstrom
    is_unstable_actor: bool = False  # True if high participation in imaginary phonon mode

class BondEdge(BaseModel):
    source_index: int
    target_index: int
    image_offset: List[int] = [0, 0, 0]  # periodic image (n_a, n_b, n_c) of the target atom:
                               # bond vector = r_target + L^T n - r_source. Required because
                               # in small cells the same atom pair bonds through several images.
    length: float              # in Angstroms
    strain_ratio: float        # (length - r_cov_sum) / r_cov_sum for heatmap coloring
    is_unstable: bool = False

class CrystalStatePayload(BaseModel):
    formula: str
    spacegroup_symbol: str     # e.g., "Pm-3m"
    spacegroup_number: int     # e.g., 221
    lattice: LatticeParams
    atoms: List[AtomSite]
    bonds: List[BondEdge]
    energy_per_atom: float     # eV/atom from uMLIP
    max_force: float           # max ||F_i|| in eV/Angstrom
    stress_gpa: List[float]    # Voigt 6-vector [xx, yy, zz, yz, xz, xy] in GPa
    bulk_modulus_gpa: Optional[float] = None
    e_hull_ev: Optional[float] = None
    rdf_r: List[float]
    rdf_g: List[float]
```

### 5.2 WebSocket Event Multiplexer (`/ws/studio`)

All WebSocket messages follow the envelope `{ "type": "<EVENT_TYPE>", "payload": { ... } }`.

#### Client-to-Server Commands:
1. `START_FLOW_GENERATION`:
   * **Payload:** `{ "elements": ["Li", "Ti", "O"], "num_atoms": 12, "target_spacegroup": 221, "target_bulk_modulus_gpa": 160.0, "steps": 60, "physics_guidance_scale": 1.5 }`, plus optional `"composition": {"Sr": 1, "Ti": 1, "O": 3}` (explicit atoms per cell; overrides `num_atoms`) and `"seed": 42` (deterministic sampling). When `composition` is omitted the server chooses a charge-neutral stoichiometry of the given `elements` whose site multiplicities are compatible with the target space group, with a cell size as close as possible to `num_atoms`; the actual atom count is whatever the emitted frames contain. Atom indices are stable across all frames of one generation.
   * **Behavior:** Initiates Riemannian flow matching on the server and streams `steps` (default 60) `FLOW_TRAJECTORY_FRAME` events at $\ge 25\text{ Hz}$, followed by a `CRYSTAL_STATE_FINAL` event. `target_spacegroup` is an international number $1\text{--}230$; when the UI selects only a crystal family, the client sends the optional `"target_crystal_system": "cubic"` instead and the server picks a representative space group. To hold the frame rate, flow frames use the fast distance-based bond finder (never `CrystalNN`).
2. `PERTURB_OR_TRANSMUTE_ATOM`:
   * **Payload:** `{ "atom_index": 3, "new_cart_coords": [1.45, 2.10, 0.85], "new_element": "Na", "apply_to_equivalent_sites": false, "auto_relax": false }` (`new_cart_coords` and `new_element` are each optional; `apply_to_equivalent_sites` transmutes every symmetry-equivalent Wyckoff site and applies to `new_element` only.)
   * **Behavior:** Updates the atom's position or element in the active ASE `Atoms` object (positions are wrapped back into $[0,1)$ fractional), runs a single-point `MACE-MP-0` pass (target $<35\text{ ms}$ on GPU for cells of $\le 32$ atoms), and returns `SINGLE_POINT_UPDATE` with updated forces, bonds, and RDF $g(r)$.
3. `START_RELAXATION`:
   * **Payload:** `{ "fmax": 0.02, "max_steps": 100, "relax_cell": true }`
   * **Behavior:** Streams `RELAXATION_FRAME` at every FIRE optimizer iteration until convergence.
4. `RUN_PHONON_SHAKE_TEST`:
   * **Payload:** `{ "supercell_dim": [2, 2, 2], "displacement_distance": 0.01 }`
   * **Behavior:** Computes the Phonopy dynamical matrix, evaluates the band structure along the `seekpath` Brillouin zone trajectory, and emits `PHONON_ANALYSIS_COMPLETE`. The `eigenmodes` array carries eigenvectors only at the labelled high-symmetry points; the force constants are cached server-side so any other point can be fetched with `REQUEST_EIGENMODE`.
5. `LOAD_STRUCTURE`:
   * **Payload:** exactly one of `{ "preset_id": "srtio3" }` or `{ "cif": "<CIF file contents>" }`
   * **Behavior:** Replaces the session's active ASE `Atoms` object and returns `SINGLE_POINT_UPDATE`. This is the only way a structure enters a WebSocket session; the REST preset/import endpoints are stateless. On connect, the server auto-loads the default `srtio3` preset.
6. `DELETE_ATOM`:
   * **Payload:** `{ "atom_index": 3 }`
   * **Behavior:** Removes the atom (vacancy defect), re-indexes the remaining atoms, and returns `SINGLE_POINT_UPDATE`.
7. `REQUEST_EIGENMODE`:
   * **Payload:** `{ "q_index": 37, "band_index": 2 }` (indices into `kpath_distances` / `frequencies_thz` of the latest phonon analysis)
   * **Behavior:** Returns `EIGENMODE_DATA` with a single eigenmode object (same shape as an element of `eigenmodes`, plus `q_frac: [q_a, q_b, q_c]`).
8. `FOLLOW_INSTABILITY`:
   * **Payload:** `{ "q_index": 0, "band_index": 0, "amplitude": 0.1, "fmax": 0.02, "max_steps": 200 }`
   * **Behavior:** Builds the supercell commensurate with $\mathbf{q}$, displaces atoms along the real part of the imaginary eigenvector (max displacement `amplitude` in $\text{\AA}$), makes it the active structure, and streams `RELAXATION_FRAME` events as in `START_RELAXATION`.
9. `STOP_STREAM`:
   * **Payload:** `{}`
   * **Behavior:** Cancels the in-flight flow / relaxation / phonon job, keeps the last emitted structure as the active one, and emits `STREAM_STOPPED`. Starting any new streaming command implicitly cancels the previous one.

Any state-changing command (2, 3, 5, 6, 8, or a completed 1) invalidates the cached phonon analysis.

**Session & streaming rules.**
* Streaming commands are 1, 3, 4, 8, and 2 with `auto_relax = true` (its `SINGLE_POINT_UPDATE` first, then a relaxation stream with the `START_RELAXATION` defaults). At most one stream runs per session.
* Starting a stream while one is in flight, or sending a state-changing instant command (2, 5, 6) during one, first cancels it and emits `STREAM_STOPPED { "stopped": "<old command>" }`. `REQUEST_EIGENMODE` cancels nothing. `STOP_STREAM` with nothing running emits `STREAM_STOPPED { "stopped": null }`.
* Every stream ends with exactly one terminal message, and nothing from that job is sent after it:
  * flow: `CRYSTAL_STATE_FINAL`, `STREAM_STOPPED` or `ERROR`;
  * relaxation / follow: the `final = true` frame (followed by `ERROR RELAXATION_DIVERGED` if it diverged) or `STREAM_STOPPED`;
  * phonon: `PHONON_ANALYSIS_COMPLETE` or `STREAM_STOPPED` or `ERROR`.
* After a successful `RUN_PHONON_SHAKE_TEST` the server also sends a `SINGLE_POINT_UPDATE` (no new energy evaluation) that flags the atoms taking part in the softest mode, i.e. band 0 at the q-point of `min_frequency_thz`. An atom gets `is_unstable_actor` when that mode is imaginary and $|\mathbf{e}_\kappa|^2 / \max_\kappa |\mathbf{e}_\kappa|^2 \ge 0.5$. A bond gets `is_unstable` when either of its atoms is flagged, because soft modes are often carried by one sublattice whose atoms don't bond each other (e.g. the O cage in the SrTiO₃ $R$-point rotation).
* Limits: loaded structures have at most 128 atoms; `RUN_PHONON_SHAKE_TEST` accepts at most 64 atoms (`STRUCTURE_TOO_LARGE`). `new_element` must be a real element with $Z \le 89$ (MACE-MP-0 coverage). `FOLLOW_INSTABILITY` requires an imaginary mode ($f < -f_{\text{tol}}$, else `MODE_NOT_IMAGINARY`) and a phonon analysis of the current structure (else `NO_PHONON_ANALYSIS`). The commensurate supercell becomes the active structure.
* `ERROR.code` values are the `ErrorCode` enum in `backend/app/schemas/messages.py`: `INVALID_MESSAGE`, `UNKNOWN_COMMAND`, `INVALID_PAYLOAD`, `INDEX_OUT_OF_RANGE`, `INVALID_ELEMENT`, `INVALID_OPERATION`, `NO_ACTIVE_STRUCTURE`, `NO_PHONON_ANALYSIS`, `MODE_NOT_IMAGINARY`, `UNKNOWN_PRESET`, `INVALID_STRUCTURE`, `DISORDERED_STRUCTURE`, `STRUCTURE_TOO_LARGE`, `GENERATION_FAILED`, `RELAXATION_DIVERGED`, `COMPUTE_FAILED` (plus `HULL_UNAVAILABLE`, REST only).

#### Server-to-Client Events:
* `FLOW_TRAJECTORY_FRAME`: `{ "step": int, "total_steps": int, "t": float, "crystal": CrystalStatePayload }` (`step` runs $1..$`total_steps`; $t = (\text{step}-1)/(\text{total\_steps}-1)$, so the first frame is the $t = 0$ prior and the last is exactly the $t = 1$ endpoint)
* `SINGLE_POINT_UPDATE`: `{ "crystal": CrystalStatePayload }`
* `CRYSTAL_STATE_FINAL`: `{ "crystal": CrystalStatePayload }` (terminates a flow generation; uses the full-quality bond finder and includes `bulk_modulus_gpa`)
* `RELAXATION_FRAME`: `{ "step": int, "converged": bool, "final": bool, "crystal": CrystalStatePayload }` (`final = true` on the last frame, whether converged or stopped at `max_steps`)
* `EIGENMODE_DATA`: `{ "eigenmode": { ...same fields as an `eigenmodes` element..., "q_frac": [float, float, float] } }`
* `STREAM_STOPPED`: `{ "stopped": "<command type that was cancelled>" }`
* `ERROR`: `{ "code": str, "message": str, "command": "<offending command type or null>" }` (malformed payloads, out-of-range indices, no active structure, compute failures; the connection stays open)
* `PHONON_ANALYSIS_COMPLETE`:
  ```json
  {
    "is_dynamically_stable": false,
    "min_frequency_thz": -2.84,
    "imaginary_mode_count": 3,
    "kpath_distances": [0.0, 0.1, "..."],
    "kpath_labels": [{"index": 0, "label": "\\Gamma"}, {"index": 25, "label": "X"}],
    "frequencies_thz": [[-2.84, -1.10, 0.0, "..."]],
    "eigenmodes": [
      {
        "q_index": 0,
        "q_label": "\\Gamma",
        "band_index": 0,
        "q_frac": [0.0, 0.0, 0.0],
        "frequency_thz": -2.84,
        "is_imaginary": true,
        "eigenvectors_real": [[0.12, -0.04, 0.0], "..."],
        "eigenvectors_imag": [[0.0, 0.0, 0.0], "..."]
      }
    ]
  }
  ```

  Field semantics:
  * Phonons are computed for the **active cell exactly as displayed** (no reduction to a primitive cell), so there are $3N$ branches for the $N$ displayed atoms and `eigenvectors_real/imag` are $N \times 3$ arrays indexed like `CrystalStatePayload.atoms`. The `seekpath` path is expressed in the reciprocal basis of the active cell (`q_frac`); conventional cells therefore show folded bands.
  * `frequencies_thz` has shape `[n_q][3N]`, ascending within each q-point; `band_index` indexes that sorted order. `kpath_distances` has length `n_q` and is cumulative path length in $\text{\AA}^{-1}$ with zero-length jumps between disconnected segments; a discontinuity is labelled `"X|K"`.
  * `min_frequency_thz` is the minimum over the whole path. `imaginary_mode_count` is the largest number of imaginary modes ($f < -f_{\text{tol}}$) found at any single q-point on the path.
  * Eigenvectors are mass-weighted, unit-normalised over the cell, in the `phonopy` phase convention (the Bloch phase uses atomic positions, matching §4.4 step 6), with the global phase fixed so the largest component is real and positive. Within a degenerate set of bands the reported vectors are a deterministic, axis-aligned basis of the degenerate subspace (so "Follow Instability" on a triply degenerate soft mode follows a single-axis distortion and is reproducible).
  * `eigenmodes` lists every band once for each *distinct* labelled q-point (a point revisited by the path is not repeated). At a path discontinuity the tick label is `"X|K"` while the eigenmodes carry the individual labels. `kpath_distances` are in $\text{\AA}^{-1}$ without the $2\pi$ factor. Force constants are symmetrised (acoustic sum rule and index-permutation symmetry) before the dynamical matrix is built. Numeric arrays in the payload are rounded to 6 decimal places.

### 5.3 REST Endpoints (`backend/app/api/routes.py`)
* `GET /api/presets`: Returns curated seed crystals so the user can test stable vs. unstable phases immediately:
  1. $\text{SrTiO}_3$ (Cubic Perovskite, $Pm\bar{3}m$ — exhibits the classic antiferrodistortive (octahedral-rotation) imaginary phonon mode at the zone-boundary $R$ point at $0\text{ K}$, which a $2\times2\times2$ supercell captures exactly).
  2. $\text{Li}_3\text{PS}_4$ (Solid-state battery superionic conductor).
  3. $\text{Si}$ (Diamond cubic — dynamically stable reference benchmark).
  4. $\text{BaTiO}_3$ (High-temperature cubic phase with strong soft-mode instability).
* `POST /api/export/cif`: Converts current `CrystalStatePayload` into a standard crystallographic `.cif` or `POSCAR` file for download.
* `POST /api/import/cif`: Parses an uploaded `.cif` string via `pymatgen.io.cif.CifParser`, evaluates initial MACE forces, and returns the resulting `CrystalStatePayload` (stateless validation/preview; the client then sends the same CIF via the `LOAD_STRUCTURE` WebSocket command to make it the session's active structure).
* `POST /api/hull/evaluate`: Queries `mp-api` for all competing phases in the chemical system (e.g., $\text{Li-Ti-O}$) and computes the energy above the convex hull $E_{\text{hull}}$ (in $\text{eV/atom}$).
  * **Self-consistent hull.** Mixing uMLIP energies with Materials Project DFT energies is inconsistent, so the hull is built entirely in the studio's uMLIP:
    * MP reference structures are fetched for every sub-system, keeping $E_{\text{hull}}^{\text{MP}} \le 0.1\text{ eV/atom}$ and always including elemental references; at most 40 are used.
    * Each reference is relaxed with the uMLIP (FIRE + `FrechetCellFilter`, $f_{\text{max}} = 0.05$) and cached on disk per chemical system and backend.
    * The candidate is a single point of the structure as given.
    * $E_{\text{hull}}$ is measured against the references only, so a negative value means the candidate would be newly stable.
  * Requires `MP_API_KEY`; without it the endpoint returns `503 HULL_UNAVAILABLE`.
* `GET /api/health`: backend name, device, dtype, MACE load error (if it fell back), and whether hull lookup is available.
* Request/response bodies are defined in `backend/app/schemas/api.py`. Errors use `{ "code": <ErrorCode>, "message": str }` with 422 (validation / bad structure), 502/503 (`HULL_UNAVAILABLE`), or 500 (`COMPUTE_FAILED`). `POST /api/export/cif` takes `{ "crystal": CrystalStatePayload, "format": "cif" | "poscar" }` and returns a `Content-Disposition` attachment.

---

## 6. Frontend 3D Visual Engine & UX Specification

### 6.1 Visual Direction & Atmospheric Craft
* **Theme:** High-contrast dark scientific obsidian canvas (`#07090E` radial gradient background) with subtle grid floor and atmospheric depth fog so glowing bonds, force vectors, and unstable phonon oscillations pop with clarity.
* **Post-Processing:** Use `@react-three/postprocessing` with a selective `Bloom` pass (`luminanceThreshold = 0.85`, `intensity = 0.65`) and `N8AO` ambient occlusion to give the unit cell depth and tactile physical weight.

### 6.2 Core 3D Viewport Components (`frontend/src/components/viewport/`)
1. **`InstancedAtoms.tsx`:**
   * Renders both the primary unit cell atoms ($1\times 1\times 1$) and an optional ghosted $2\times 2\times 2$ supercell boundary halo (`opacity = 0.22`) so users can clearly perceive periodic crystal continuity.
   * Uses standard CPK/Jmol colors and covalent radii scaled by $0.45\times$ (ball-and-stick representation) or $1.0\times$ (space-filling Van der Waals toggle).
   * **Interactive Raycast Dragging:** Hovering an atom highlights it with a cyan Fresnel rim shader and displays a floating badge (`#3 Ti | |F| = 0.42 eV/A`). Clicking and dragging locks `OrbitControls` and projects pointer motion onto the camera-facing plane, firing debounced `PERTURB_OR_TRANSMUTE_ATOM` WebSocket frames ($30\text{ Hz}$) so the user sees the surrounding force vectors react live as they pull the atom out of equilibrium.
2. **`InstancedBonds.tsx`:**
   * Renders dynamic cylinders connecting bonded atom pairs.
   * **Strain & Instability Shader:** Normal equilibrium bonds render in brushed metallic slate. Under tensile/compressive strain, bonds interpolate along a diverging cool-to-warm colormap (blue = compressed, amber = stretched). During the **Phonon Shake-Test**, bonds participating heavily in an imaginary mode ($\omega^2 < 0$) pulse in emissive crimson (`#FF2A54`).
3. **`ForceVectors.tsx`:**
   * Renders 3D sleek cone-arrows anchored at each atom center pointing along $\mathbf{F}_i = -\nabla_{\mathbf{r}_i} E_{\text{tot}}$, with length proportional to $\log(1 + \|\mathbf{F}_i\|)$ and color shifting from emerald ($\|\mathbf{F}_i\| < 0.05\text{ eV/\AA}$) to neon orange ($\|\mathbf{F}_i\| > 0.5\text{ eV/\AA}$).
4. **`FlowTrails.tsx`:**
   * Maintains a ring buffer of the last $15$ fractional-to-Cartesian coordinate frames during `START_FLOW_GENERATION` and renders fading luminescent ribbons tracing the geodesic path each atom took from Gaussian noise into its final symmetric Wyckoff site.
5. **`PhononDisplacement.tsx`:**
   * When a phonon mode $(\mathbf{q}, \nu)$ is active, hooks into R3F's `useFrame(({ clock }) => ...)` loop to update instance matrices at $60\text{ FPS}$ directly on the GPU/Float32Array using the complex eigenvector formula from Section 4.4 without React state overhead.

### 6.3 Interactive Scientific HUD Layout
* **Left Panel (`GeneratorSidebar.tsx`):**
  * **Chemical System Selector:** Interactive compact Periodic Table modal or tag-input for allowed elements + stoichiometry slider ($4$ to $32$ atoms/cell).
  * **Symmetry & Property Conditioning:** Space group family selector (`Triclinic`, `Monoclinic`, `Orthorhombic`, `Tetragonal`, `Hexagonal`, `Cubic` or specific space group $1\text{--}230$), Target Bulk Modulus slider ($10\text{--}400\text{ GPa}$), and Physics Guidance weight slider.
  * **Primary CTA:** `"Synthesize Crystal (Riemannian Flow)"` button + `"Load Preset Phase"` selector.
* **Top Telemetry Bar (`TelemetryHeader.tsx`):**
  * Live numeric readouts formatted with clean monospace typography:
    * **Formula & Symmetry:** e.g., `SrTiO3 | Pm-3m (#221)`
    * **uMLIP Potential Energy:** e.g., `-7.842 eV/atom`
    * **Max Atomic Force:** e.g., `0.014 eV/Å` (with green `"RELAXED"` or amber `"UNRELAXED"` badge)
    * **Bulk Modulus $K$:** e.g., `174.2 GPa`
    * **Dynamical Stability Badge:** `"STABLE (All ω² > 0)"` in emerald OR `"UNSTABLE (3 Imaginary Modes: -2.84 THz)"` in pulsing red.
* **Floating Contextual Toolbar (`TransmuteToolbar.tsx`):**
  * Appears when an atom is clicked in 3D:
    * `"Transmute Element"` quick-buttons (swaps the clicked atom or all symmetry-equivalent Wyckoff sites to a neighboring element, e.g., $\text{Li} \leftrightarrow \text{Na} \leftrightarrow \text{K}$).
    * `"Create Vacancy Defect"` button (removes the selected atom and re-evaluates forces).
    * `"FIRE Relax Structure"` toggle button (triggers live neural relaxation).
    * `"Run Phonon Shake-Test"` button.
* **Bottom Drawer (`Charts Dock`):**
  * Collapsible split-dock displaying:
    1. **Phonon Dispersion Band Structure ($\omega$ vs. $\mathbf{q}$):** Plots all $3N$ phonon branches across the Brillouin zone. Any branch dipping below $0\text{ THz}$ is shaded in translucent red. **Clicking any point $(\mathbf{q}, \nu)$ on the dispersion curve immediately switches the 3D viewport to animate that exact vibrational eigenmode.**
    2. **Live Radial Distribution Function $g(r)$:** Updates in real time as atoms are dragged, relaxed, or denoised, visually proving bond-length order and coordination shells.

---

## 7. Phased Implementation Roadmap for the Coding Agent

Execute the build in the following strict 5-phase sequence. Verify each phase with its corresponding test command before proceeding to the next.

### Phase 1: Backend Physics Core & uMLIP Engine
1. Set up `backend/` virtual environment (`backend/.venv`, Python $3.11$) and install `fastapi`, `uvicorn`, `websockets`, `pydantic>=2`, `numpy`, `ase`, `pymatgen`, `spglib`, `phonopy`, `seekpath`, `torch`, `mace-torch`, `mp-api`, `pytest`, `pytest-asyncio`, and `httpx`.
2. Implement `backend/app/schemas/crystal.py` (§5.1), `backend/app/config.py`, `backend/app/core/geometry_analysis.py`, and `backend/app/core/umlip_engine.py`:
   * Create a thread-safe singleton `UMLIPEngine` that loads `mace_mp(model="small", device=device, default_dtype=dtype)` on startup. `dtype` defaults to `"float64"` (required for the $10^{-5}\text{ eV}$ invariance gate and for finite-displacement phonons with $\Delta u = 0.01\text{ \AA}$); `"float32"` is an opt-in speed setting via `LATTICEFLOW_DTYPE`.
   * Implement a deterministic analytical fallback calculator (smoothly truncated Lennard-Jones with per-element radii, plus a damped/shifted real-space Coulomb term with fixed formal charges applied only when the cell is charge-neutral) used when the MACE checkpoint cannot be loaded (e.g., CI without internet) or when `LATTICEFLOW_UMLIP=fallback` is set, ensuring tests never hang. The engine exposes `backend_name` (`"mace-mp-0-small"` or `"lj-fallback"`).
   * Implement `evaluate(atoms: ase.Atoms, bond_mode="fast") -> CrystalStatePayload` computing energy, forces, stress tensor, `spglib` space group, bonds, and RDF $g(r)$. `bond_mode="fast"` is the default distance-based finder (must run in a few ms and must not report cation–cation bonds in ionic compounds); `bond_mode="crystalnn"` uses `CrystalNN` and is reserved for non-streaming calls because it is too slow for per-frame use.
   * Expose `forces_batch(list[ase.Atoms]) -> list[np.ndarray]` for the Phase 2 phonon engine.
3. Implement `backend/app/presets/` with exact crystallographic structures for cubic $\text{SrTiO}_3$ ($a = 3.905\text{ \AA}$), diamond $\text{Si}$ ($a = 5.431\text{ \AA}$), cubic $\text{BaTiO}_3$ ($a = 4.00\text{ \AA}$), and $\beta\text{-Li}_3\text{PS}_4$ (the experimental $Pnma$ phase has partially occupied Li sites; use a fully ordered 32-atom $Pnma$ approximant and record its provenance).
4. **Verification Gate 1:** Run `pytest backend/tests/test_umlip_invariance.py` to verify that rigidly rotating the whole crystal (cell and positions together), translating all atoms, or re-wrapping atoms across periodic boundaries leaves $E_{\text{tot}}$ invariant to within $10^{-5}\text{ eV}$ and rotates $\mathbf{F}_i$ equivariantly (and $\sigma \rightarrow R\sigma R^T$). The gate must pass with both the MACE backend (float64) and the fallback calculator; MACE-only tests are skipped, not failed, when the checkpoint is unavailable.

### Phase 2: Riemannian Flow Generator, FIRE Relaxer & Phonon Engine
1. Implement `backend/app/core/flow_generator.py`:
   * Build `RiemannianFlowGenerator` supporting step-by-step async generation (`async for frame in generator.sample(...)`).
   * Initialize fractional coordinates $\mathbf{S}_0 \sim \mathcal{U}(0, 1)^{N \times 3}$ and lattice lengths around covalent atomic volume estimates.
   * Apply space-group symmetry projection via `pymatgen.symmetry` / `spglib` and integrate geodesic flow steps blended with MACE force-field Langevin guidance so atoms smoothly assemble into a low-energy symmetric crystal over $60$ frames.
2. Implement `backend/app/core/relaxer.py`:
   * Wrap `ase.optimize.FIRE` with `FrechetCellFilter` as an async generator yielding `CrystalStatePayload` on every step.
   * Implement the §4.3 Birch-Murnaghan bulk modulus estimator here (`estimate_bulk_modulus`), and real GPU batching in `UMLIPEngine.forces_batch`.
   * All three Phase 2 engines expose a synchronous, cancellable core plus a thin async wrapper, and their event payload models live in `backend/app/schemas/messages.py`.
3. Implement `backend/app/core/phonon_engine.py`:
   * Use `Phonopy(unitcell, supercell_matrix=[[2,0,0],[0,2,0],[0,0,2]])`.
   * Generate displaced supercells, evaluate forces via `UMLIPEngine`, call `phonon.produce_force_constants()`, compute automatic band structure via `seekpath`, extract eigenvalues/eigenvectors at high-symmetry points, and package `PHONON_ANALYSIS_COMPLETE`.
   * adding a `"stabilize_mode"` helper: allows the user to click `"Follow Instability"` on an imaginary phonon mode, which displaces the crystal atoms along the imaginary eigenvector $\mathbf{e}(\mathbf{q}, \nu)$ and runs FIRE relaxation to discover the true lower-symmetry ground-state phase!
4. **Verification Gate 2:** Run `pytest backend/tests/test_phonon_modes.py` to confirm that diamond $\text{Si}$ yields $\min(\omega) \ge -0.05\text{ THz}$ (stable) while high-symmetry cubic $\text{BaTiO}_3$ or cubic $\text{SrTiO}_3$ yields a clear imaginary soft mode ($\min(\omega) < -1.0\text{ THz}$). These physical assertions require the MACE backend and are skipped under the fallback calculator, which only checks pipeline mechanics (shapes, acoustic sum rule, Hermiticity).

### Phase 3: FastAPI REST & WebSocket Streaming Layer
1. Implement `backend/app/api/ws_stream.py` and `backend/app/api/routes.py`.
2. Ensure long-running compute tasks (flow sampling, FIRE relaxation, Phonopy supercell force evaluation) run in `asyncio.to_thread` or a non-blocking worker pool so WebSocket heartbeats and cancellation commands (`STOP_STREAM`) remain instantaneous.
3. **Verification Gate 3:** Run `pytest backend/tests/test_ws_protocol.py` using FastAPI's `TestClient` to verify full round-trip streaming for `START_FLOW_GENERATION`, `PERTURB_OR_TRANSMUTE_ATOM`, `START_RELAXATION`, `RUN_PHONON_SHAKE_TEST`, `LOAD_STRUCTURE`, `DELETE_ATOM`, `REQUEST_EIGENMODE`, `FOLLOW_INSTABILITY`, `STOP_STREAM`, and the `ERROR` path.

### Phase 4: Frontend 3D WebGL Studio & Interactive Manipulation
1. Initialize `frontend/` with Vite, React, TypeScript, Tailwind CSS, Three.js, `@react-three/fiber`, `@react-three/drei`, `@react-three/postprocessing`, and `zustand`.
2. Build `useCrystalStore.ts` to manage connection state, active `CrystalStatePayload`, active tool mode (`VIEW`, `DRAG_ATOM`, `TRANSMUTE`, `PHONON_SHAKE`), and selected phonon eigenmode.
3. Implement `CrystalCanvas.tsx`, `UnitCellBox.tsx`, `InstancedAtoms.tsx`, `InstancedBonds.tsx`, `ForceVectors.tsx`, `FlowTrails.tsx`, and `PhononDisplacement.tsx`.
4. Wire pointer-down/move/up raycasting on `InstancedAtoms.tsx` so dragging an atom sends live `PERTURB_OR_TRANSMUTE_ATOM` frames and renders real-time force vectors on all neighboring atoms.

### Phase 5: Scientific Charts, "Follow Instability" Workflow & Polish
1. Build `PhononBandChart.tsx` and `RdfChart.tsx` in the bottom dock.
2. Connect the interactive click handler on `PhononBandChart.tsx`: clicking any branch/wavevector $(\mathbf{q}, \nu)$ highlights that mode and animates its 3D harmonic oscillation in the viewport.
3. Add the **"Freeze & Relax Imaginary Mode" ("Follow Instability")** button in `PhononControlBar.tsx`: when viewing an imaginary phonon mode ($\omega < 0\text{ THz}$), clicking this button pushes the crystal along that unstable normal mode coordinate and triggers live FIRE relaxation so the user can watch the crystal spontaneously undergo a structural phase transition into a stable polymorph.
4. Implement `.cif` / `POSCAR` export and Materials Project convex hull lookup.

---

## 8. Acceptance Criteria & Definition of Done

1. **Zero-Config Startup:** Running `uvicorn app.main:app --reload` and `npm run dev` boots the full studio with no manual configuration, and once the backend has finished its one-time model warm-up (checkpoint download on first ever run, then `torch` import + MACE load on each start), opening the frontend loads the default $\text{SrTiO}_3$ perovskite preset with live MACE-MP-0 forces in under $5\text{ seconds}$.
2. **Smooth Generative Flow:** Clicking `"Synthesize Crystal"` streams a $60$-frame Riemannian flow-matching trajectory at $\ge 25\text{ FPS}$ where both the unit-cell wireframe and fractional atomic coordinates visibly denoise from chaos into a symmetric lattice with glowing particle trails.
3. **Tactile Grab-and-Transmute:** Dragging any atom in 3D or transmuting its element updates interatomic force arrows $\mathbf{F}_i$ and the Radial Distribution Function $g(r)$ in $< 50\text{ ms}$, and clicking `"FIRE Relax"` smoothly animates the lattice back to $\max \|\mathbf{F}_i\| < 0.02\text{ eV/\AA}$.
4. **Phonon Shake-Test & Phase Transition:** Running the Phonon Shake-Test on cubic $\text{SrTiO}_3$ or $\text{BaTiO}_3$ detects the imaginary soft mode ($\omega < 0\text{ THz}$), plots the negative dispersion branch in crimson, animates the unstable 3D atomic oscillation at $60\text{ FPS}$, and allows the user to trigger `"Follow Instability"` to relax the crystal into its stable lower-symmetry ground state.

---

## 9. Changelog: v1.0 → v1.1 Consistency Amendments

The v1.0 text contained contradictions and gaps that would have blocked or misdirected implementation. Each was resolved in place; this list records what changed and why.

**Mathematics & physics**
1. §4.1 — The lattice matrix was called "upper-triangular" but is lower-triangular; the row-vector convention is now stated explicitly.
2. §4.2 — The "Langevin" corrector had no noise term and mixed Cartesian and fractional updates without a mapping; both fixed. The built-in sampler had an undefined $\mathbf{v}_\theta$ and endpoint $\mathbf{s}_1$ (there is no trained network); now defined.
3. §1, §4.2, §3 — Band gap $E_g$ was listed as a conditioning target, but no payload field, UI control, or model in the default stack can supply it. It is now scoped to the optional MatterGen adapter.
4. §4.3 — The stress formula had a spurious $\sum_k$; "volumetric strain" was ambiguous (linear vs. volume); units/sign convention added.
5. §4.4 — The instability threshold ($10^{-3}\text{ THz}^2$ on $\omega^2$) disagreed with Gate 2's $-0.05\text{ THz}$ and mixed $\omega$ with $f$; unified to one tolerance $f_{\text{tol}} = 0.05\text{ THz}$. The animation amplitude $\sqrt{k_BT/m}$ was a velocity, not a length; divided by $\omega$ with a floor.
6. §4.5 — The RDF sum ignored periodic images, which gives wrong $g(r)$ whenever the cell is smaller than the $8\text{ \AA}$ cutoff (true for 3 of 4 presets); grid resolution was unspecified.
7. §1, §5.3 — The "$0\text{ K}$ trap" narrative said imaginary modes cause collapse *at 300 K*, yet the demo presets (cubic SrTiO₃/BaTiO₃) are the high-temperature phases that are unstable *at 0 K*; reworded. SrTiO₃'s instability is antiferrodistortive at $R$, not "ferrodistortive". Gate 2 said "distorted SrTiO₃", contradicting the cubic preset.

**Protocol & contracts**
8. §5.1 — `BondEdge` had no periodic-image offset, so bonds crossing the cell boundary (most bonds in a 5-atom perovskite cell) could not be drawn or disambiguated; `image_offset` added.
9. §5.2 — Features required elsewhere had no command: `STOP_STREAM` (Phase 3), vacancy creation (§6.3), "Follow Instability" (Phases 2 & 5), loading a preset/CIF into the WebSocket session (REST had no way to reach session state), and arbitrary-$\mathbf{q}$ eigenmodes (§6.3 requires clicking *any* point, but Phase 2 only extracts high-symmetry points). Added `LOAD_STRUCTURE`, `DELETE_ATOM`, `REQUEST_EIGENMODE`, `FOLLOW_INSTABILITY`, `STOP_STREAM`.
10. §5.2 — `CRYSTAL_STATE_FINAL` was referenced but never defined; there was no error event. Added `CRYSTAL_STATE_FINAL`, `EIGENMODE_DATA`, `STREAM_STOPPED`, `ERROR`.
11. §5.2 — Flow streaming at "20–30 Hz" contradicted the $\ge 25$ FPS acceptance criterion; the crystal-family selector and "all Wyckoff-equivalent sites" transmutation had no payload fields.

**Implementation feasibility**
12. §7 Phase 1 — `float32` MACE cannot reliably meet a $10^{-5}\text{ eV}$ invariance gate and degrades $0.01\text{ \AA}$ finite-displacement phonons; default is now `float64`. `CrystalNN` (hundreds of ms) is incompatible with the $<35\text{ ms}$ and 25 FPS targets; a fast bond finder is the default. Missing dependencies (`pydantic`, `pytest`, …) added. The "LJ + Coulomb" fallback is now specified well enough to be periodic-safe, and gates state what they assert under it. β-Li₃PS₄ has partially occupied sites, so "exact structure" now names an ordered approximant.
13. §8.1 — "Full boot in under 5 s" is not achievable including `torch` import and model load; rescoped to time-to-first-structure after backend warm-up.

**Phase 2 kickoff additions**
14. §5.2 — `START_FLOW_GENERATION` gave elements and an atom count but no stoichiometry and no seed; added optional `composition` and `seed` and defined the default behaviour.
15. §5.2 — `PHONON_ANALYSIS_COMPLETE` left undefined which cell the eigenvectors refer to, the shape and ordering of `frequencies_thz`, how `imaginary_mode_count` is counted, and the eigenvector convention; all now defined.
16. §7 Phase 2 — the §4.3 bulk-modulus estimator had no owning module; assigned to `relaxer.py`.

**Phase 2 implementation notes (built-in flow sampler, recorded as built)**
17. §4.2 — The built-in sampler's endpoint comes from a seeded, budgeted search:
    * up to 24 Wyckoff-site assignments of the target group (via `pyxtal`'s Wyckoff tables, now a dependency) are pre-screened with the analytic potential;
    * the best are scored with MACE, and the winner is relaxed under the target symmetry;
    * the Wyckoff projection is a symmetry-averaged endpoint refinement during the corrector window.
    
    Deviations from a literal reading of §4.2:
    * the prior uses hard-core rejection sampling rather than a bare $\mathcal{U}(0,1)$, because unconstrained overlaps give MACE energies of $-10^4$ eV/atom;
    * the velocity gain ramps from 1 to 3 inside the corrector window so the last frame lands exactly on the endpoint;
    * $\|\mathbf{F}\|$ in the corrector is the largest per-atom force, with $\epsilon = 3\text{ eV/\AA}$;
    * $K_{\text{target}}$ is steered by relaxing the endpoint to a target pressure (a heuristic, not a property prediction).
    
    Quality (MACE, 60 steps): SrTiO₃ #221, Si #227 and NaCl #225 come out as perovskite, diamond and rock salt at $\max|\mathbf{F}| \approx 0$; low-symmetry targets (P1, many free parameters) give locally relaxed, not globally optimal, structures. The per-frame cost is about 27 ms on an uncontended GPU and 40–50 ms on CPU.

**Degenerate-mode basis.** Degeneracy grouping is relative ($|\Delta\omega^2| < 10^{-11}\max|\omega^2|$ per q-point). Each discrete decision (grouping, axis tie-break, phase anchor) uses a threshold that sits at least 1.1 decades from every value observed on the presets, whether a symmetry-exact tie or a physical splitting. As a result, the reported basis is stable under $10^{-8}$ relative force noise. One case is inherent to the "largest component real-positive" rule: two components equal to within noise can flip the global phase between runs. The vector itself is unchanged.

**Phase 3 kickoff additions**
18. §5.2 — Defined the session and streaming rules: implicit cancellation, one terminal message per stream, the `ErrorCode` enum, the unstable-actor flags, and size and element limits. Defined the `t` semantics of flow frames.
19. §5.3 — The convex hull is computed self-consistently in the uMLIP rather than against raw MP DFT energies, which would mix two different energy scales. Added `GET /api/health`, the request/response bodies, and the error shape.

**Open items (not blocking; flagged for the product owner)**
* The "$40\%$–$60\%$ of AI-generated crystals" figure in §1 is uncited.
* MACE-MP-0 *small* reproducing a $< -1.0\text{ THz}$ soft mode in cubic BaTiO₃/SrTiO₃ at the fixed preset lattice constants is expected but unverified until Gate 2 runs; if it fails, the fix is the `medium` model or relaxing the cell first, not loosening the gate.
---

## 10. Phase A (post-v1.1) contract additions

Source: `IMPROVEMENT_SUGGESTIONS.md` (2026-11-12 review), Phase A "trust and quick wins". All changes are additive and optional; v1.1 clients keep working.

**§5.1 `CrystalStatePayload` / `AtomSite`.** These fields are filled for every payload:
* `pressure_gpa` (−tr σ / 3, + = compressed);
* `density_g_cm3`;
* `crystal_system`;
* `formula_units` (Z);
* `symprec` (1e-2);
* `AtomSite.wyckoff` (e.g. `"3c"`).

**§5.2 commands.**
* `COMPUTE_BULK_MODULUS {strain_window = 0.02, n_points = 5}` → `BULK_MODULUS_RESULT`. It reports K at the given volume, the stress cross-check, V_eq, B0 and B0′ at V_eq, an `extrapolated` flag and the raw E(V) / P(V) data. It is instant and cancels nothing.
* `REFINE_STRUCTURE {symprec = 0.01, cell = "refined" | "conventional" | "primitive"}` → `SINGLE_POINT_UPDATE`. It changes state.
* `UNDO` / `REDO` → `SINGLE_POINT_UPDATE` then `HISTORY_STATE`. They change state and cancel a live stream. Undoing at the start of the history, or redoing at the end, gives `INVALID_OPERATION`.
* `REQUEST_PHONON_DOS {mesh?, t_min = 0, t_max = 1000, t_step = 10}` → `PHONON_DOS_DATA`. It returns the total and element-projected DOS and the harmonic F, S and C_v per atom plus the ZPE, with imaginary modes excluded. It requires a valid phonon analysis.
* `START_RELAXATION` gains:
  * `fix_symmetry` (default false);
  * `pressure_gpa` (default 0);
  * `stress_tol_gpa` (when set, convergence also requires max |σ_ij + P δ_ij| < tol);
  * `optimizer` (FIRE, BFGS or LBFGS).
* `LOAD_STRUCTURE` takes exactly one of:
  * `preset_id`;
  * `cif`;
  * `text` plus `text_format` ("auto", "cif", "poscar" or "extxyz");
  * `crystal` (a `CrystalStatePayload`, used to restore a session after a reconnect).

**Undo history.** Each session keeps a bounded history of 50 entries.
* An entry is pushed for each of these commits:
  * a load, including the initial preset;
  * a transmute or delete;
  * a refine;
  * a drag gesture (consecutive drag commits of the same atom replace the top entry);
  * a stream outcome: a terminal relaxation or Follow frame, `CRYSTAL_STATE_FINAL`, or a stream cancelled after at least one applied frame.
* A `HISTORY_STATE {can_undo, can_redo, position, length, label}` follows every history change in wire order.

**§5.3 REST.**
* `POST /api/export/cif` gains:
  * `format "extxyz"` (energy, forces and stress from the payload; the cell must be "as_is");
  * `symmetrize` (default **true**: space group plus asymmetric unit);
  * `symprec`;
  * `cell` ("as_is", "conventional" or "primitive").
* `POST /api/hull/evaluate` gains `relax` (default **true**: the candidate is relaxed with the reference settings before scoring). The response adds `candidate_relaxed` and `energy_per_atom_as_given`.
