# Superconductivity screening, Tier 0 (contract)

Tier 0 ranks candidates for conventional (phonon-mediated) superconductivity from what the
studio already computes. It adds new features and changes no existing behaviour: existing
messages only gain optional fields, and existing job chains and result kinds run exactly as before.

**What it is not.** MACE has no electrons, so Tier 0 cannot compute the electron–phonon coupling λ.
It reports the phonon energy scale, Allen–Dynes T_c *as a function of an assumed λ and μ\**, and a
metallicity gate. Every UI and doc surface must say "proxy" / "assumed λ" and never present a T_c
as a prediction. Tier 2 (DFT e-ph handoff) is future work.

## 1. Physics

Grid: the existing phonon DOS `PhononDosData.frequencies_thz` / `total_dos` (Gaussian-smeared,
states/THz/cell). Only use points with f > F_CUT = 0.05 THz (`F_TOL_THZ`). Constant-coupling
proxy: α²F(ω) ∝ F(ω). With w(ω) = F(ω)/ω, using trapezoid integration:

- ω_log = exp(∫ w ln ω dω / ∫ w dω)
- ω̄₂ = sqrt(∫ F ω dω / ∫ F/ω dω)            (Allen–Dynes ω̄₂ under the proxy)
- ⟨ω⟩ = ∫ F ω dω / ∫ F dω
- ω_max = highest f with F(f) > 1e-3 · max F
- Convert THz → K with 47.99243 K/THz.

Allen–Dynes (ω in K):

```
den = λ − μ*(1 + 0.62 λ);  T_c = 0 if den ≤ 0
Λ1 = 2.46 (1 + 3.8 μ*);    Λ2 = 1.82 (1 + 6.3 μ*) (ω̄₂/ω_log)
f1 = [1 + (λ/Λ1)^1.5]^(1/3)
f2 = 1 + (ω̄₂/ω_log − 1) λ² / (λ² + Λ2²)
T_c = f1 f2 (ω_log / 1.2) exp(−1.04 (1 + λ) / den)
```

Reference values (both the Python and the TypeScript implementation test these, to 1e-6 relative):

| λ | μ* | ω_log K | ω̄₂ K | T_c K |
|---|---|---|---|---|
| 1.0 | 0.10 | 300 | 300 | 21.950675143220174 |
| 1.0 | 0.10 | 300 | 450 | 22.478294763352174 |
| 0.5 | 0.13 | 300 | 400 | 2.248854551769687 |
| 0.1 | 0.13 | 300 | 300 | 0.0 (den ≤ 0) |

- `LAMBDA_GRID = (0.3, 0.5, 0.75, 1.0, 1.5, 2.0)`, `MU_STAR_GRID = (0.10, 0.13)`.
- `tc_ref_k` = T_c at λ = 1.0, μ* = 0.10. It is a **ranking scale** (it orders candidates by their
  phonon energy scale), not a prediction.
- Light elements: `{H, Li, Be, B, C, N}`. `light_element_fraction` is the share of ∫F dω (f > F_CUT)
  carried by those elements' projected DOS.
- `reliable = False` when `imaginary_fraction > 0.01` or there are no frequencies above F_CUT. In that
  case `warnings` says why.

## 2. Schemas

`backend/app/schemas/messages.py` (the TypeScript mirror is in `frontend/src/types/protocol.ts`):

```python
class TcGridPoint(BaseModel):
    lam: float
    mu_star: float
    tc_k: float

class EphProxyDescriptors(BaseModel):
    omega_log_k: Optional[float]
    omega_2_k: Optional[float]
    omega_mean_k: Optional[float]
    omega_max_k: Optional[float]
    light_element_fraction: float
    tc_grid: List[TcGridPoint]          # LAMBDA_GRID × MU_STAR_GRID, λ-major
    tc_ref_k: Optional[float]           # None when not reliable
    reliable: bool
    warnings: List[str]
    method: str                         # one human-readable line, e.g. "constant-coupling proxy α²F ∝ F(ω); Allen–Dynes"

class PhononDosData(BaseModel):         # existing; one optional field added
    ...
    eph_proxy: Optional[EphProxyDescriptors] = None
```

`PhononAnalysis.dos_and_thermal` fills `eph_proxy`. A failure there must never break the DOS: it
logs the error and leaves the field `None`.

`backend/app/schemas/api.py`:

```python
MetallicityVerdict = Literal["metal", "likely_metal", "likely_insulator", "insulator", "unknown"]

class MetallicityRequest(BaseModel):
    crystal: CrystalStatePayload
    use_mp: bool = True

class MetallicityResponse(BaseModel):
    verdict: MetallicityVerdict
    source: Literal["materials_project", "heuristic"]
    band_gap_ev: Optional[float]        # MP (GGA/GGA+U) gap of the best StructureMatcher match
    mp_id: Optional[str]
    rms_displacement: Optional[float]
    reason: str                         # one sentence shown to the user

class MPEntry(BaseModel):               # existing; optional fields added
    ...
    band_gap: Optional[float] = None
    is_metal: Optional[bool] = None
```

Add `band_gap` and `is_metal` to `MP_SEARCH_FIELDS`.

## 3. Metallicity gate (`app/core/superconductivity.py: assess_metallicity(atoms, client | None, use_mp)`)

1. **Materials Project.** This step runs when `use_mp` is set and a client is available, and uses
   `compare.match_mp`. If the best match has `is_metal` set, or `band_gap ≤ 0.0`, the verdict is
   `metal`; if `band_gap > 0`, the verdict is `insulator`. The reason notes that GGA gaps are
   underestimated. An MP error or no match falls through to step 2, and the reason says why.
2. **Composition heuristic** (pymatgen, on the reduced formula). The rules apply in order:
   - every element a metal (a metallic element or an intermetallic) → `likely_metal`;
   - a single non-metallic element (Si, Ge, diamond) → `likely_insulator`;
   - a transition metal, lanthanide or actinide is present and the only non-metals are in
     {B, C, N, H, Si, P, As} → `likely_metal` (carbides, nitrides, borides, hydrides and silicides
     such as NbN, NbC, MgB₂-type compounds and LaH₁₀);
   - `Composition.oxi_state_guesses()` is non-empty → `likely_insulator`;
   - otherwise → `likely_metal` (the formula cannot be charge-balanced).

   If the composition cannot be parsed, the verdict is `unknown`.

REST: `POST /api/superconductivity/metallicity` takes a `MetallicityRequest` and returns a
`MetallicityResponse`. It works without an MP key, in which case it uses the heuristic and
`reason` says so.

## 4. Job step `superconductivity`

- `STEP_KINDS` gains `"superconductivity"`. Settings: `{"mesh": null | [i, j, k], "use_mp": true}`.
- Validation is the same as for `follow`: it needs a `phonons` step earlier in the chain with no
  `relax` / `follow` between them. It does **not** reset phonons readiness, so
  `phonons → superconductivity → follow` is valid.
- Execution:
  - reuse the chain's cached `PhononAnalysis`, or rerun the last phonons settings exactly as
    `follow` does;
  - run `dos_and_thermal(mesh=...)` and take `eph_proxy`;
  - run `assess_metallicity` using the hull context's API key. MP errors never fail the step.
- Saved result kind `"superconductivity"`. Its data is
  `{"descriptors": EphProxyDescriptors, "metallicity": MetallicityResponse, "flags": [str], "dos_mesh": [3], "imaginary_fraction": float}`.
- Flags (strings): `light_elements` (fraction ≥ 0.05), `high_omega_log` (ω_log ≥ 400 K),
  `soft_mode` (stable phonons but, at a labelled high-symmetry q-point other than Γ, the lowest
  frequency is < 0.1 · ω_max; an earlier "any q with |q_frac| > 0.1" rule flagged acoustic branches in
  MgO and Al), `unstable` (not reliable), `insulating` (verdict insulator / likely_insulator).
- The step message reads like `"ω_log 312 K · T_c,ref 21.9 K · metal (MP)"`.
- `library.summarize("superconductivity")` gives these keys: `omega_log_k`, `omega_2_k`, `tc_ref_k`,
  `light_element_fraction`, `metallicity` (the verdict), `sc_flags` (comma-joined, "" if none), and
  `sc_gate_pass`. `sc_gate_pass` = reliable AND verdict not in {insulator, likely_insulator}.

## 5. Clients and docs

- Python client: `metallicity(crystal=None, use_mp=True)` REST helper (it defaults to the session
  structure, like the other REST helpers). `"superconductivity"` is accepted in `submit_jobs` steps.
  `latticeflow-screen --sc` adds the step after phonons and the summary columns, and sorts the output
  by `sc_gate_pass` desc, then `tc_ref_k` desc.
- `docs/protocol.md`: the new field, the endpoint, the step kind and the summary keys.
  `docs/workflows.md`: a short "Superconductivity pre-screen" recipe. README: one row in the
  "What you can do" table and one bullet in the scope and limits.
- Frontend:
  - the DosPane gets a "Superconductivity proxies" section: descriptors, λ and μ* sliders driving a
    live TS Allen–Dynes, a T_c(λ) chart for both μ* values, warnings, and a "Check metallicity"
    button that calls the REST endpoint;
  - the JobDrawer gets an "SC proxies" step after phonons;
  - `SUMMARY_COLUMNS` gets the new keys;
  - the methods text gets one sentence.
