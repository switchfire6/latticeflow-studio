"""UMLIPEngine.evaluate(), singleton/thread-safety, and backend-loading guards."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from ase import Atoms
from ase.filters import FrechetCellFilter
from ase.optimize import FIRE

import app.core.umlip_engine as umlip_engine
from app.config import Settings
from app.core.fallback_calculator import AnalyticFallbackCalculator, assign_formal_charges
from app.core.umlip_engine import EV_PER_A3_TO_GPA, UMLIPEngine, _download_with_deadline
from app.schemas.crystal import CrystalStatePayload

BACKEND_DIR = Path(__file__).resolve().parents[1]
A_STO = 3.905
# CUDA scatter-add (used by MACE message passing) is not bitwise deterministic:
# repeated float64 calls differ at the ~1e-15 level. CPU/fallback are bitwise exact.
REPRO_TOL = 1e-10


def assert_same_payload(a, b, path="payload"):
    """Recursive equality of model_dump() trees, floats compared to REPRO_TOL."""
    if isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for k in a:
            assert_same_payload(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for k, (x, y) in enumerate(zip(a, b)):
            assert_same_payload(x, y, f"{path}[{k}]")
    elif isinstance(a, float):
        assert abs(a - b) <= REPRO_TOL, (path, a, b)
    else:
        assert a == b, (path, a, b)


def srtio3(rattle: float = 0.0) -> Atoms:
    atoms = Atoms(
        "SrTiO3",
        scaled_positions=[[0, 0, 0], [0.5, 0.5, 0.5], [0.5, 0.5, 0], [0.5, 0, 0.5], [0, 0.5, 0.5]],
        cell=[A_STO] * 3,
        pbc=True,
    )
    if rattle:
        atoms.positions += np.random.default_rng(1).normal(0.0, rattle, atoms.positions.shape)
    return atoms


def unwrapped_srtio3() -> Atoms:
    """Rattled SrTiO3 with atoms pushed outside [0,1) (incl. one at s = -1e-17)."""
    atoms = srtio3(rattle=0.05)
    atoms.positions[0] = [-1e-17, 0.0, 0.0]
    atoms.positions[1] += [A_STO, -2 * A_STO, 0.0]
    atoms.positions[3] -= [0.0, 0.0, 3 * A_STO]
    return atoms


# -------------------------------------------------------------------- payload
def test_evaluate_payload(engine):
    atoms = unwrapped_srtio3()
    before = (atoms.positions.copy(), atoms.cell.array.copy(), atoms.numbers.copy())
    payload = engine.evaluate(atoms)

    # input not mutated, no calculator attached
    np.testing.assert_array_equal(atoms.positions, before[0])
    np.testing.assert_array_equal(atoms.cell.array, before[1])
    np.testing.assert_array_equal(atoms.numbers, before[2])
    assert atoms.calc is None

    assert isinstance(payload, CrystalStatePayload)
    CrystalStatePayload.model_validate(payload.model_dump())
    CrystalStatePayload.model_validate_json(payload.model_dump_json())

    assert payload.formula == "SrTiO3"
    assert len(payload.atoms) == 5
    assert [s.element for s in payload.atoms] == ["Sr", "Ti", "O", "O", "O"]
    assert [s.atomic_number for s in payload.atoms] == [38, 22, 8, 8, 8]
    assert [s.index for s in payload.atoms] == list(range(5))

    L = np.array(payload.lattice.matrix)
    np.testing.assert_allclose(L, atoms.cell.array, atol=1e-12)
    frac = np.array([s.frac_coords for s in payload.atoms])
    cart = np.array([s.cart_coords for s in payload.atoms])
    assert np.all(frac >= 0.0) and np.all(frac < 1.0)
    np.testing.assert_allclose(cart, frac @ L, atol=1e-10)
    # wrapped positions are lattice translates of the input positions
    dfrac = np.linalg.solve(L.T, (cart - atoms.positions).T).T
    np.testing.assert_allclose(dfrac, np.round(dfrac), atol=1e-9)

    e, f, s = engine.energy_forces_stress(atoms)
    forces = np.array([site.force for site in payload.atoms])
    norms = np.array([site.force_norm for site in payload.atoms])
    np.testing.assert_allclose(forces, f, atol=1e-6)
    np.testing.assert_allclose(norms, np.linalg.norm(f, axis=1), atol=1e-6)
    assert payload.max_force == pytest.approx(norms.max(), abs=1e-12)
    assert payload.max_force > 1e-3
    assert payload.energy_per_atom == pytest.approx(e / 5, abs=1e-6)
    assert len(payload.stress_gpa) == 6
    np.testing.assert_allclose(payload.stress_gpa, s * EV_PER_A3_TO_GPA, atol=1e-5)
    assert payload.bulk_modulus_gpa is None and payload.e_hull_ev is None
    assert len(payload.rdf_r) == len(payload.rdf_g) == 151
    assert isinstance(payload.spacegroup_symbol, str) and 1 <= payload.spacegroup_number <= 230
    assert all(0 <= b.source_index < 5 and 0 <= b.target_index < 5 for b in payload.bonds)


def test_evaluate_ideal_srtio3_symmetry(engine):
    payload = engine.evaluate(srtio3())
    assert (payload.spacegroup_symbol, payload.spacegroup_number) == ("Pm-3m", 221)
    assert payload.max_force < 1e-6  # all sites are inversion centres
    xx, yy, zz, yz, xz, xy = payload.stress_gpa
    assert xx == pytest.approx(yy, abs=1e-6) and yy == pytest.approx(zz, abs=1e-6)
    assert max(abs(yz), abs(xz), abs(xy)) < 1e-6


def test_stress_unit_conversion_constant():
    assert EV_PER_A3_TO_GPA == pytest.approx(160.21766, abs=1e-5)


# -------------------------------------------------------- calculator facade
def test_get_calculator_runs_fire_with_cell_filter(fallback_engine):
    atoms = srtio3(rattle=0.05)
    atoms.calc = fallback_engine.get_calculator()
    opt = FIRE(FrechetCellFilter(atoms), logfile=None)
    opt.run(fmax=0.02, steps=300)
    assert np.linalg.norm(atoms.get_forces(), axis=1).max() < 0.02


def test_get_calculator_shared_across_threads(engine):
    structures = [srtio3(rattle=0.02 * (k + 1)) for k in range(6)]
    reference = [engine.energy_forces_stress(a) for a in structures]

    def work(k):
        atoms = structures[k].copy()
        atoms.calc = engine.get_calculator()
        return atoms.get_potential_energy(), atoms.get_forces(), atoms.get_stress()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, list(range(6)) * 3))
    for k, (e, f, s) in enumerate(results):
        e_ref, f_ref, s_ref = reference[k % 6]
        assert abs(e - e_ref) <= REPRO_TOL
        np.testing.assert_allclose(f, f_ref, atol=REPRO_TOL, rtol=0)
        np.testing.assert_allclose(s, s_ref, atol=REPRO_TOL, rtol=0)


def test_forces_batch(engine):
    structures = [srtio3(rattle=0.03), srtio3(rattle=0.06).repeat((2, 1, 1))]
    batch = engine.forces_batch(structures)
    assert [f.shape for f in batch] == [(5, 3), (10, 3)]
    for a, f in zip(structures, batch):
        np.testing.assert_allclose(f, engine.energy_forces_stress(a)[1], atol=REPRO_TOL, rtol=0)


# --------------------------------------------------- concurrency / singleton
def test_concurrent_evaluate_identical(engine):
    atoms = srtio3(rattle=0.04)
    reference = engine.evaluate(atoms).model_dump()
    barrier = threading.Barrier(8)

    def work(_):
        barrier.wait()
        return engine.evaluate(atoms).model_dump()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8)))
    for r in results:
        assert_same_payload(r, reference)
    if not engine.is_mace or engine.device == "cpu":
        assert all(r == reference for r in results)  # bitwise on deterministic backends


@pytest.fixture
def isolated_singleton(monkeypatch):
    """Swap in a fresh fallback-mode singleton; restore the original afterwards."""
    saved = UMLIPEngine._instance
    UMLIPEngine.reset_instance()
    monkeypatch.setattr(umlip_engine, "get_settings", lambda: Settings(umlip="fallback"))
    yield
    UMLIPEngine.set_instance(saved)


def test_singleton_same_object_across_threads(isolated_singleton, monkeypatch):
    constructed = []
    original_init = UMLIPEngine.__init__

    def counting_init(self, *args, **kwargs):
        time.sleep(0.05)  # widen the race window
        constructed.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(UMLIPEngine, "__init__", counting_init)
    barrier = threading.Barrier(12)

    def get(_):
        barrier.wait()
        return UMLIPEngine.get_instance()

    with ThreadPoolExecutor(max_workers=12) as pool:
        engines = list(pool.map(get, range(12)))
    assert len(constructed) == 1
    assert all(e is engines[0] for e in engines)
    assert UMLIPEngine.get_instance() is engines[0]
    assert engines[0].backend_name == "lj-fallback"


def test_direct_construction_does_not_touch_singleton(isolated_singleton):
    UMLIPEngine(backend="fallback")
    assert UMLIPEngine._instance is None


# ------------------------------------------------------------ loading guards
def test_fallback_mode_never_imports_mace_or_torch():
    code = (
        "import sys\n"
        "from app.core.umlip_engine import UMLIPEngine\n"
        "e = UMLIPEngine(backend='fallback')\n"
        "from ase.build import bulk\n"
        "e.energy_forces_stress(bulk('Si', 'diamond', 5.431))\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('mace', 'torch', 'e3nn')]\n"
        "assert not bad, bad\n"
        "print(e.backend_name)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "lj-fallback"


def test_auto_mode_falls_back_when_checkpoint_unavailable():
    engine = UMLIPEngine(backend="auto", model="no-such-mace-model", allow_download=False)
    assert engine.backend_name == "lj-fallback"
    assert engine.load_error is not None


def test_mace_mode_raises_when_checkpoint_unavailable():
    with pytest.raises(RuntimeError, match="MACE backend could not be loaded"):
        UMLIPEngine(backend="mace", model="no-such-mace-model", allow_download=False)


def test_download_guard_is_time_bounded(tmp_path):
    # 10.255.255.1 is non-routable: connect() blocks until the socket timeout.
    t0 = time.monotonic()
    with pytest.raises((TimeoutError, RuntimeError)):
        _download_with_deadline("http://10.255.255.1/model.model", tmp_path / "m.model", timeout_s=1.0)
    assert time.monotonic() - t0 < 5.0
    assert not (tmp_path / "m.model").exists()


# ----------------------------------------------------- fallback robustness
def test_fallback_charge_assignment():
    assert assign_formal_charges(["Sr", "Ti", "O", "O", "O"]).tolist() == [2, 4, -2, -2, -2]
    assert assign_formal_charges(["Sr", "Ti", "O", "O"]) is None          # vacancy: not neutral
    assert assign_formal_charges(["Sr", "Og", "O", "O", "O"]) is None     # unknown element
    assert assign_formal_charges(["Si", "Si"]) is None                     # not neutral
    assert assign_formal_charges(["Li"] * 3 + ["P"] + ["S"] * 4) is not None


@pytest.mark.parametrize(
    "atoms",
    [
        Atoms("OgXeHO", positions=[[0, 0, 0], [0, 0, 0], [1, 1, 1], [1, 1, 1.001]], cell=[3, 3, 3], pbc=True),
        Atoms("H", positions=[[0, 0, 0]], cell=[0.9, 0.9, 0.9], pbc=True),
        Atoms("CsFr", positions=[[0, 0, 0], [2, 2, 2]], cell=[[2.5, 0, 0], [2.4, 0.4, 0], [0, 0, 7]], pbc=True),
        Atoms("Fe2O", positions=[[0, 0, 0], [1.2, 0, 0], [0, 1.5, 0]], pbc=False),
    ],
    ids=["overlapping-unusual", "tiny-cell", "skewed-cell", "molecule"],
)
def test_fallback_never_crashes(atoms):
    atoms.calc = AnalyticFallbackCalculator()
    assert np.isfinite(atoms.get_potential_energy())
    assert np.all(np.isfinite(atoms.get_forces()))
    if atoms.pbc.all():
        assert np.all(np.isfinite(atoms.get_stress()))


def test_fallback_is_fast():
    atoms = srtio3(rattle=0.03).repeat((2, 2, 2))[:32]  # 32-atom charge-neutral-or-not cell
    calc = AnalyticFallbackCalculator()
    times = []
    for _ in range(15):
        t0 = time.perf_counter()
        calc.compute(atoms)
        times.append(time.perf_counter() - t0)
    assert np.median(times) < 0.05
