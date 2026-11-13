"""RiemannianFlowGenerator (PROJECT_SPEC.md §4.2, §5.2 START_FLOW_GENERATION, §7 Phase 2).

Structural contract (frame count, t schedule, fixed species, symmetry of the last
frame, smoothness, determinism, cancellation, async wrapper) runs on the
fallback backend, and a reduced version on MACE. Physical-quality assertions
(perovskite / diamond / rock salt, energies, contacts) are ``requires_mace``.
MACE runs use small step counts to keep the file fast on a busy GPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import numpy as np
import pytest
from ase import Atoms
from ase.data import covalent_radii
from ase.neighborlist import neighbor_list

import app.core.flow_generator as fg
from app.core.flow_generator import FlowCancelled, RiemannianFlowGenerator
from app.core.geometry_analysis import get_spacegroup
from app.schemas.messages import FlowTrajectoryFrame, StartFlowGenerationPayload

SRTIO3 = dict(elements=["Sr", "Ti", "O"], composition={"Sr": 1, "Ti": 1, "O": 3}, target_spacegroup=221)
SI = dict(elements=["Si"], num_atoms=8, target_spacegroup=227)
NACL = dict(elements=["Na", "Cl"], num_atoms=8, target_spacegroup=225)
LITIO = dict(elements=["Li", "Ti", "O"], num_atoms=12, target_crystal_system="cubic")
P1 = dict(elements=["Si", "O"], num_atoms=9, target_spacegroup=1)
SCENARIOS = {"SrTiO3": SRTIO3, "Si": SI, "NaCl": NACL, "LiTiO": LITIO, "P1": P1}


def req(steps=12, seed=7, **kw) -> StartFlowGenerationPayload:
    return StartFlowGenerationPayload(steps=steps, seed=seed, **kw)


def run(gen: RiemannianFlowGenerator, **kw) -> list[FlowTrajectoryFrame]:
    return list(gen.iter_frames(req(**kw)))


def frame_atoms(frame: FlowTrajectoryFrame) -> Atoms:
    c = frame.crystal
    return Atoms(numbers=[a.atomic_number for a in c.atoms],
                 scaled_positions=[a.frac_coords for a in c.atoms],
                 cell=c.lattice.matrix, pbc=True)


def has_target_symmetry(gen: RiemannianFlowGenerator, atoms: Atoms) -> bool:
    """Every op of the target group (pyxtal setting, origin shifted) maps the structure onto itself."""
    info = gen.last_run_info
    w, tr, _ = fg._group_ops(info["spacegroup"])
    frac = atoms.get_scaled_positions() - np.asarray(info["origin_shift"])
    try:
        fg._perm_tables(frac % 1.0, atoms.numbers, w, tr, tol=1e-4)
        return True
    except RuntimeError:
        return False


def min_contact_ratio(atoms: Atoms) -> float:
    i, j, d = neighbor_list("ijd", atoms, 4.0)
    r = covalent_radii[atoms.numbers]
    return float((d / (r[i] + r[j])).min())


def assert_contract(frames, steps):
    assert len(frames) == steps
    assert [f.step for f in frames] == list(range(1, steps + 1))
    assert all(f.total_steps == steps for f in frames)
    ts = [f.t for f in frames]
    assert ts[0] == 0.0 and ts[-1] == 1.0
    assert all(b > a for a, b in zip(ts, ts[1:]))
    species = [a.element for a in frames[0].crystal.atoms]
    for f in frames:
        assert [a.element for a in f.crystal.atoms] == species
        assert [a.index for a in f.crystal.atoms] == list(range(len(species)))
        assert f.crystal.lattice.volume > 0


@pytest.fixture(scope="module")
def fb_gen(fallback_engine):
    return RiemannianFlowGenerator(fallback_engine)


@pytest.fixture(scope="module")
def mace_gen(mace_engine):
    return RiemannianFlowGenerator(mace_engine)


# --------------------------------------------------------------------------
# Contract (both backends)
# --------------------------------------------------------------------------
def test_frame_contract_both_backends(engine):
    gen = RiemannianFlowGenerator(engine)
    frames = run(gen, steps=6, **SRTIO3)
    assert_contract(frames, 6)
    last = frames[-1]
    assert gen.final_atoms is not None and gen.current_atoms is not None
    np.testing.assert_allclose(gen.final_atoms.cell.array, np.array(last.crystal.lattice.matrix), atol=1e-12)
    np.testing.assert_allclose(gen.final_atoms.get_scaled_positions(),
                               [a.frac_coords for a in last.crystal.atoms], atol=1e-9)
    assert last.crystal.spacegroup_number == 221
    assert last.crystal.formula == "SrTiO3"


def test_default_singleton_engine(fallback_engine):
    from app.core.umlip_engine import UMLIPEngine

    UMLIPEngine.set_instance(fallback_engine)
    try:
        assert RiemannianFlowGenerator().engine is fallback_engine
    finally:
        UMLIPEngine.reset_instance()


@pytest.mark.parametrize("steps", [2, 5, 120])
def test_other_step_counts(fb_gen, steps):
    frames = run(fb_gen, steps=steps, **NACL)
    assert_contract(frames, steps)
    assert frames[-1].crystal.spacegroup_number == 225


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_final_frame_has_target_symmetry_fallback(fb_gen, name):
    kw = SCENARIOS[name]
    frames = run(fb_gen, steps=20, **kw)
    assert_contract(frames, 20)
    info = fb_gen.last_run_info
    final = fb_gen.final_atoms
    _, detected = get_spacegroup(final, symprec=1e-2)
    if "target_spacegroup" in kw:
        assert info["spacegroup"] == kw["target_spacegroup"]
    # detected == target, or the target is a subgroup of what is detected
    assert detected == info["spacegroup"] or has_target_symmetry(fb_gen, final), (detected, info)
    assert has_target_symmetry(fb_gen, final)
    assert min_contact_ratio(final) > fg.X_TRUST - 1e-9


def test_crystal_system_resolution_and_charge_neutrality(fb_gen):
    frames = run(fb_gen, steps=8, **LITIO)
    info = fb_gen.last_run_info
    assert info["spacegroup"] in fg.REPRESENTATIVE_GROUPS["cubic"]
    comp = info["composition"]
    assert sum(comp.values()) == len(frames[-1].crystal.atoms)
    assert comp["Li"] + 4 * comp["Ti"] == 2 * comp["O"]          # Li+ Ti4+ O2-
    assert abs(sum(comp.values()) - 12) <= 1
    lat = frames[-1].crystal.lattice
    assert lat.a == pytest.approx(lat.b, abs=1e-8) and lat.a == pytest.approx(lat.c, abs=1e-8)
    assert (lat.alpha, lat.beta, lat.gamma) == pytest.approx((90, 90, 90), abs=1e-8)


def test_composition_chooser_examples():
    plan = fg._resolve_plan(req(**NACL))
    assert dict(zip(plan.elements, plan.counts)) == {"Na": 4, "Cl": 4}
    plan = fg._resolve_plan(req(**SI))
    assert plan.counts == [8] and plan.spacegroup == 227
    plan = fg._resolve_plan(req(elements=["Cu"], num_atoms=4))      # no target: highest symmetry first
    assert plan.counts == [4] and plan.system == "cubic"
    plan = fg._resolve_plan(req(elements=["Zn", "O"], num_atoms=4, target_crystal_system="hexagonal"))
    assert plan.system in ("hexagonal", "trigonal")
    assert plan.counts[0] == plan.counts[1]


# --------------------------------------------------------------------------
# Smoothness
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,steps", [("SrTiO3", 60), ("P1", 60), ("LiTiO", 5), ("NaCl", 120)])
def test_smoothness_bounds(fb_gen, name, steps):
    frames = run(fb_gen, steps=steps, **SCENARIOS[name])
    h = 1.0 / (steps - 1)
    max_disp = 0.0
    for a, b in zip(frames, frames[1:]):
        la, lb = np.array(a.crystal.lattice.matrix), np.array(b.crystal.lattice.matrix)
        sa = np.array([x.frac_coords for x in a.crystal.atoms])
        sb = np.array([x.frac_coords for x in b.crystal.atoms])
        ds = sb - sa
        ds -= np.round(ds)
        disp = np.linalg.norm(sb @ lb - (sb - ds) @ la, axis=1).max()      # minimum-image motion
        assert disp <= fg.displacement_bound(la, lb, steps, a.t, 1.5) + 1e-9, (a.step, disp)
        max_disp = max(max_disp, disp)
        rel = (np.linalg.norm(lb - la, axis=1) / np.linalg.norm(la, axis=1)).max()
        assert rel <= 2.0 * h + 1.2 * fg.MAX_STRAIN_STEP, (a.step, rel)
        lat = b.crystal.lattice
        assert lat.volume > 0
        assert all(45.0 < x < 135.0 for x in (lat.alpha, lat.beta, lat.gamma))
    if steps >= 60:
        assert max_disp < 0.75      # Å per frame for 4-7 Å cells at >= 60 frames


# --------------------------------------------------------------------------
# Determinism, seeds, guidance, bulk modulus
# --------------------------------------------------------------------------
def test_deterministic_bitwise_on_fallback(fb_gen):
    a = [f.model_dump() for f in run(fb_gen, steps=15, seed=11, **LITIO)]
    b = [f.model_dump() for f in run(fb_gen, steps=15, seed=11, **LITIO)]
    assert a == b


def test_seed_sensitivity(fb_gen):
    a = run(fb_gen, steps=10, seed=1, **NACL)
    b = run(fb_gen, steps=10, seed=2, **NACL)
    fa = np.array([x.frac_coords for x in a[0].crystal.atoms])
    fb = np.array([x.frac_coords for x in b[0].crystal.atoms])
    assert not np.allclose(fa, fb)
    assert a[0].crystal.lattice.matrix != b[0].crystal.lattice.matrix


def test_guidance_zero_is_pure_geometric_flow(fb_gen):
    g0 = run(fb_gen, steps=30, physics_guidance_scale=0.0, **LITIO)
    g1 = run(fb_gen, steps=30, physics_guidance_scale=1.5, **LITIO)
    # identical before the corrector window, different after it
    for a, b in zip(g0, g1):
        if a.t <= fg.T_CORRECTOR:
            assert a.model_dump() == b.model_dump()
    assert g0[-1].model_dump() != g1[-1].model_dump()
    assert has_target_symmetry(fb_gen, frame_atoms(g0[-1]))


def test_stiffer_target_gives_denser_cell(fb_gen):
    soft = run(fb_gen, steps=12, target_bulk_modulus_gpa=20.0, **NACL)
    stiff = run(fb_gen, steps=12, target_bulk_modulus_gpa=400.0, **NACL)
    assert stiff[-1].crystal.lattice.volume < soft[-1].crystal.lattice.volume
    assert stiff[0].crystal.lattice.volume < soft[0].crystal.lattice.volume


# --------------------------------------------------------------------------
# Invalid requests (raised eagerly by iter_frames)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    dict(elements=["Xx"]),
    dict(elements=["Sr", "Ti", "O"], composition={"Sr": 1, "Ti": 1, "O": 3}, target_spacegroup=225),
    dict(elements=["Sr", "O"], composition={"Sr": 1, "Ti": 1, "O": 3}),
    dict(elements=["Na", "Cl"], target_crystal_system="decagonal"),
    dict(elements=["Na", "Cl"], composition={"Na": 40, "Cl": 40}),
    dict(elements=["Na", "Cl"], composition={"Na": 0, "Cl": 0}),
    dict(elements=["Pu"]),
])
def test_invalid_requests_raise(fb_gen, kw):
    with pytest.raises(ValueError):
        fb_gen.iter_frames(req(**kw))


def test_empty_elements_raise(fb_gen):
    bad = StartFlowGenerationPayload.model_construct(
        elements=[], num_atoms=8, composition=None, target_spacegroup=None, target_crystal_system=None,
        target_bulk_modulus_gpa=None, steps=10, physics_guidance_scale=1.5, seed=1)
    with pytest.raises(ValueError):
        fb_gen.iter_frames(bad)


# --------------------------------------------------------------------------
# Cancellation (sync core)
# --------------------------------------------------------------------------
def test_should_cancel_stops_mid_stream(fb_gen):
    seen = []
    gen_iter = fb_gen.iter_frames(req(steps=30, **SRTIO3), should_cancel=lambda: len(seen) >= 3)
    with pytest.raises(FlowCancelled):
        for f in gen_iter:
            seen.append(f)
    assert len(seen) == 3
    assert fb_gen.final_atoms is None
    np.testing.assert_allclose(fb_gen.current_atoms.get_scaled_positions(),
                               [a.frac_coords for a in seen[-1].crystal.atoms], atol=1e-9)


def test_should_cancel_during_endpoint_search(fb_gen):
    with pytest.raises(FlowCancelled):
        next(fb_gen.iter_frames(req(steps=10, **LITIO), should_cancel=lambda: True))
    assert fb_gen.current_atoms is None


# --------------------------------------------------------------------------
# Async wrapper
# --------------------------------------------------------------------------
def _flow_threads():
    return [t for t in threading.enumerate() if t.name == "flow-generator" and t.is_alive()]


async def test_async_sample_matches_sync_and_keeps_loop_responsive(fb_gen):
    sync = [f.model_dump() for f in run(fb_gen, steps=40, **LITIO)]
    gaps = []
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    frames = []
    async with contextlib.aclosing(fb_gen.sample(req(steps=40, **LITIO))) as agen:
        async for f in agen:
            frames.append(f.model_dump())
    stop.set()
    await tick
    assert frames == sync
    assert fb_gen.final_atoms is not None
    assert max(gaps) < 0.15, max(gaps)            # the loop never stalls on compute
    assert not _flow_threads()


async def test_async_early_exit_cleans_up(fb_gen):
    got = []
    async with contextlib.aclosing(fb_gen.sample(req(steps=60, **NACL))) as agen:
        async for f in agen:
            got.append(f)
            if len(got) == 2:
                break
    assert len(got) == 2
    assert fb_gen.final_atoms is None
    np.testing.assert_allclose(fb_gen.current_atoms.get_scaled_positions(),
                               [a.frac_coords for a in got[-1].crystal.atoms], atol=1e-9)
    assert not _flow_threads()                     # aclose waited for the worker


async def test_async_task_cancellation(fb_gen):
    first = asyncio.Event()

    async def consume():
        async with contextlib.aclosing(fb_gen.sample(req(steps=120, **NACL))) as agen:
            async for _ in agen:
                first.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first.wait(), 10)
    t0 = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.perf_counter() - t0 < 1.0
    deadline = time.perf_counter() + 2.0
    while _flow_threads() and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)
    assert not _flow_threads()
    assert fb_gen.final_atoms is None


async def test_async_propagates_validation_errors(fb_gen):
    with pytest.raises(ValueError):
        async for _ in fb_gen.sample(req(elements=["Xx"])):
            pass


# --------------------------------------------------------------------------
# MACE: physical quality
# --------------------------------------------------------------------------
def _neighbours(atoms: Atoms, i: int, cutoff: float):
    ii, jj, dd = neighbor_list("ijd", atoms, cutoff)
    sel = ii == i
    return jj[sel], dd[sel]


def _physical_checks(frames, gen):
    final = gen.final_atoms
    e = [f.crystal.energy_per_atom for f in frames]
    k65 = min(range(len(frames)), key=lambda k: abs(frames[k].t - 0.65))
    assert e[-1] < e[k65]
    assert e[-1] < e[0] - 0.5
    assert min_contact_ratio(final) >= 0.75
    return final


@pytest.mark.requires_mace
def test_mace_srtio3_becomes_perovskite(mace_gen):
    frames = run(mace_gen, steps=30, **SRTIO3)
    final = _physical_checks(frames, mace_gen)
    c = frames[-1].crystal
    assert c.spacegroup_number == 221 and c.formula == "SrTiO3"
    assert 3.8 < c.lattice.a < 4.05
    ti = int(np.flatnonzero(final.numbers == 22)[0])
    sr = int(np.flatnonzero(final.numbers == 38)[0])
    j, d = _neighbours(final, ti, 2.3)
    assert len(j) == 6 and np.all(final.numbers[j] == 8)                # TiO6 octahedron
    np.testing.assert_allclose(d, c.lattice.a / 2, atol=1e-6)
    j, d = _neighbours(final, sr, 3.0)
    assert len(j) == 12 and np.all(final.numbers[j] == 8)               # SrO12 cuboctahedron


@pytest.mark.requires_mace
def test_mace_silicon_becomes_diamond(mace_gen):
    frames = run(mace_gen, steps=30, **SI)
    final = _physical_checks(frames, mace_gen)
    assert frames[-1].crystal.spacegroup_number == 227
    a = frames[-1].crystal.lattice.a
    assert 5.3 < a < 5.6
    for i in range(len(final)):
        j, d = _neighbours(final, i, 2.6)
        assert len(j) == 4
        np.testing.assert_allclose(d, a * np.sqrt(3) / 4, atol=1e-6)


@pytest.mark.requires_mace
def test_mace_nacl_becomes_rock_salt(mace_gen):
    frames = run(mace_gen, steps=30, **NACL)
    final = _physical_checks(frames, mace_gen)
    c = frames[-1].crystal
    assert c.spacegroup_number == 225 and c.formula == "NaCl" and len(c.atoms) == 8
    for i in np.flatnonzero(final.numbers == 11):
        j, d = _neighbours(final, int(i), 3.3)
        assert len(j) == 6 and np.all(final.numbers[j] == 17)
        np.testing.assert_allclose(d, c.lattice.a / 2, atol=1e-6)


@pytest.mark.requires_mace
def test_mace_litio_cubic_is_neutral_and_symmetric(mace_gen):
    frames = run(mace_gen, steps=30, **LITIO)
    final = _physical_checks(frames, mace_gen)
    info = mace_gen.last_run_info
    comp = info["composition"]
    assert comp["Li"] + 4 * comp["Ti"] == 2 * comp["O"]
    assert has_target_symmetry(mace_gen, final)
    assert get_spacegroup(final)[1] in range(195, 231)                 # cubic (target or supergroup)
    assert frames[-1].crystal.max_force < 1.0


@pytest.mark.requires_mace
def test_mace_p1_works(mace_gen):
    frames = run(mace_gen, steps=30, **P1)
    _physical_checks(frames, mace_gen)
    assert mace_gen.last_run_info["spacegroup"] == 1
    assert frames[-1].crystal.energy_per_atom < -4.0


@pytest.mark.requires_mace
def test_mace_deterministic_and_guidance_zero(mace_gen):
    a = run(mace_gen, steps=8, seed=3, **NACL)
    b = run(mace_gen, steps=8, seed=3, **NACL)
    for fa, fb in zip(a, b):
        pa = np.array([x.cart_coords for x in fa.crystal.atoms])
        pb = np.array([x.cart_coords for x in fb.crystal.atoms])
        np.testing.assert_allclose(pa, pb, atol=1e-8)
        assert fa.crystal.energy_per_atom == pytest.approx(fb.crystal.energy_per_atom, abs=1e-8)
    g0 = run(mace_gen, steps=8, seed=3, physics_guidance_scale=0.0, **NACL)
    assert g0[-1].crystal.spacegroup_number == 225


@pytest.mark.requires_mace
def test_mace_time_before_first_frame_is_bounded(mace_gen):
    run(mace_gen, steps=2, **SRTIO3)                                   # warm caches
    run(mace_gen, steps=2, **LITIO)
    info = mace_gen.last_run_info
    assert info["engine_calls_before_first_frame"] <= fg.PRESTREAM_CALLS
    # generous: the GPU may be shared; the call budget above is the real bound
    assert info["seconds_before_first_frame"] < 15.0
