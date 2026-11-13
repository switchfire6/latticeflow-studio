"""Self-consistent uMLIP convex hull (app/core/hull_analyzer.py, PROJECT_SPEC.md §5.3).

All Materials Project access is replaced by fake fetchers returning simple
hand-built structures; nothing here touches the network.
"""

from __future__ import annotations

import json
import time
from typing import List

import numpy as np
import pytest
from ase import Atoms
from pymatgen.core import Composition, Lattice, Structure
from pymatgen.io.ase import AseAtomsAdaptor

from app.core import hull_analyzer as ha
from app.core.umlip_engine import UMLIPEngine
from app.presets import load_preset


# --------------------------------------------------------------------------
# fake reference structures
# --------------------------------------------------------------------------
def fcc(el: str, a: float) -> Structure:
    return Structure.from_spacegroup("Fm-3m", Lattice.cubic(a), [el], [[0, 0, 0]]).get_primitive_structure()


def bcc(el: str, a: float) -> Structure:
    return Structure.from_spacegroup("Im-3m", Lattice.cubic(a), [el], [[0, 0, 0]]).get_primitive_structure()


def hcp(el: str, a: float, c: float) -> Structure:
    return Structure.from_spacegroup("P6_3/mmc", Lattice.hexagonal(a, c), [el], [[1 / 3, 2 / 3, 0.25]])


def dimer_in_box(el: str, bond: float, box: float = 8.0) -> Structure:
    return Structure(Lattice.cubic(box), [el, el], [[0, 0, 0], [bond, 0, 0]], coords_are_cartesian=True)


def rocksalt(a_el: str, b_el: str, a: float) -> Structure:
    return Structure.from_spacegroup(
        "Fm-3m", Lattice.cubic(a), [a_el, b_el], [[0, 0, 0], [0.5, 0.5, 0.5]]
    ).get_primitive_structure()


def cscl(a_el: str, b_el: str, a: float) -> Structure:
    return Structure(Lattice.cubic(a), [a_el, b_el], [[0, 0, 0], [0.5, 0.5, 0.5]])


def rutile(a_el: str, b_el: str, a: float, c: float, u: float = 0.305) -> Structure:
    return Structure.from_spacegroup("P4_2/mnm", Lattice.tetragonal(a, c), [a_el, b_el], [[0, 0, 0], [u, u, 0]])


def preset_structure(preset_id: str) -> Structure:
    return AseAtomsAdaptor.get_structure(load_preset(preset_id))


def sr_ti_o_refs():
    return [
        ha.MPReference("mp-sr-fcc", fcc("Sr", 6.08), 0.0),
        ha.MPReference("mp-sr-bcc", bcc("Sr", 4.85), 0.005),
        ha.MPReference("mp-ti-hcp", hcp("Ti", 2.95, 4.68), 0.0),
        ha.MPReference("mp-o2", dimer_in_box("O", 1.21), 0.0),
        ha.MPReference("mp-sro", rocksalt("Sr", "O", 5.16), 0.0),
        ha.MPReference("mp-tio2", rutile("Ti", "O", 4.594, 2.959), 0.0),
        ha.MPReference("mp-srtio3", preset_structure("srtio3"), 0.0),
    ]


def na_cl_refs():
    return [
        ha.MPReference("mp-na", bcc("Na", 4.23), 0.0),
        ha.MPReference("mp-cl2", dimer_in_box("Cl", 1.99), 0.0),
        ha.MPReference("mp-nacl", rocksalt("Na", "Cl", 5.64), 0.0),
    ]


class FakeFetcher:
    """Records every call; returns ``refs`` restricted to the requested chemical systems."""

    def __init__(self, refs, error: Exception | None = None):
        self.refs = list(refs)
        self.error = error
        self.calls: List[List[str]] = []

    def __call__(self, chemsys_list, api_key):
        self.calls.append(list(chemsys_list))
        if self.error is not None:
            raise self.error
        wanted = set(chemsys_list)
        return [r for r in self.refs if r.structure.composition.chemical_system in wanted]


@pytest.fixture
def cache(tmp_path) -> ha.HullCache:
    return ha.HullCache(tmp_path / "hull")


def relaxed_atoms(refset: ha.ReferenceSet, material_id: str) -> Atoms:
    return next(r for r in refset.references if r.material_id == material_id).atoms()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_chemsys_helpers():
    assert ha.chemsys_of(["Ti", "O", "Sr", "O"]) == "O-Sr-Ti"
    assert ha.sub_chemsys(["Ti", "Sr", "O"]) == ["O", "Sr", "Ti", "O-Sr", "O-Ti", "Sr-Ti", "O-Sr-Ti"]
    assert len(ha.sub_chemsys(["Li", "Fe", "P", "O"])) == 15


# --------------------------------------------------------------------------
# hull mechanics
# --------------------------------------------------------------------------
def test_reference_phases_as_candidates(fallback_engine, cache):
    fetcher = FakeFetcher(sr_ti_o_refs())
    refset = ha.get_reference_set(["Sr", "Ti", "O"], fallback_engine, fetcher=fetcher, cache=cache)
    assert refset.chemsys == "O-Sr-Ti"
    pd = refset.phase_diagram()
    stable_ids = {e.attribute for e in pd.stable_entries}
    assert {"mp-ti-hcp", "mp-o2"} <= stable_ids

    for ref in refset.references:
        res = ha.evaluate_hull(ref.atoms(), engine=fallback_engine, fetcher=fetcher, cache=cache)
        entry = next(e for e in pd.all_entries if e.attribute == ref.material_id)
        # A relaxed reference re-scored as the candidate reproduces its own hull distance.
        assert res.e_hull_ev == pytest.approx(pd.get_e_above_hull(entry), abs=1e-6)
        assert res.energy_per_atom == pytest.approx(ref.energy_per_atom, abs=1e-8)
        if ref.material_id in stable_ids:
            assert abs(res.e_hull_ev) < 1e-6
            assert res.decomposition == pytest.approx({ref.formula: 1.0})
    # Each candidate is scored in its *own* chemical system (TiO2 -> O-Ti, ...), one fetch each;
    # the same structure relaxes to the same energy in every system (checked above).
    systems = {ha.chemsys_of(r.composition) for r in refset.references} | {"O-Sr-Ti"}
    assert len(fetcher.calls) == len(systems)
    for ref in refset.references:  # all cached now
        ha.evaluate_hull(ref.atoms(), engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert len(fetcher.calls) == len(systems)


def test_strained_candidate_is_above_hull(fallback_engine, cache):
    fetcher = FakeFetcher(sr_ti_o_refs())
    refset = ha.get_reference_set(["Sr", "Ti", "O"], fallback_engine, fetcher=fetcher, cache=cache)
    stable_ids = {e.attribute for e in refset.phase_diagram().stable_entries}
    compound = "mp-tio2" if "mp-tio2" in stable_ids else next(i for i in stable_ids if i in ("mp-sro", "mp-srtio3"))
    atoms = relaxed_atoms(refset, compound)
    base = ha.evaluate_hull(atoms, engine=fallback_engine, fetcher=fetcher, cache=cache, relax_candidate=False)
    assert abs(base.e_hull_ev) < 1e-6

    strained = atoms.copy()
    strained.set_cell(atoms.cell.array * 1.06, scale_atoms=True)  # 6 % linear strain
    res = ha.evaluate_hull(strained, engine=fallback_engine, fetcher=fetcher, cache=cache, relax_candidate=False)
    assert res.e_hull_ev > 0.01
    assert res.energy_per_atom > base.energy_per_atom
    assert len(fetcher.calls) == 2  # O-Sr-Ti, then the compound's own system (cached for the strained one)


def test_decomposition_off_stoichiometry(fallback_engine, cache):
    fetcher = FakeFetcher(sr_ti_o_refs())
    # Sr2TiO4-like composition (no such reference): rock salt SrO 2x1x1 supercell + TiO2 is
    # awkward to build, so use a crude Sr2TiO4 box; only the composition matters here.
    cand = Atoms(
        "Sr2TiO4",
        scaled_positions=[[0, 0, 0], [0.5, 0.5, 0.5], [0.5, 0.5, 0.0], [0.25, 0, 0], [0.75, 0, 0], [0, 0.25, 0.5], [0, 0.75, 0.5]],
        cell=[6.0, 6.0, 6.0],
        pbc=True,
    )
    res = ha.evaluate_hull(cand, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert res.chemsys == "O-Sr-Ti"
    assert sum(res.decomposition.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v > 0 for v in res.decomposition.values())
    # The atom-fraction-weighted decomposition reproduces the candidate composition.
    target = Composition("Sr2TiO4").fractional_composition
    mix = {el.symbol: 0.0 for el in target}
    for formula, frac in res.decomposition.items():
        for el, x in Composition(formula).fractional_composition.items():
            mix[el.symbol] += frac * x
    for el, x in target.items():
        assert mix[el.symbol] == pytest.approx(x, abs=1e-9)
    assert res.formation_energy_per_atom is not None and np.isfinite(res.formation_energy_per_atom)
    assert res.umlip_backend == fallback_engine.backend_name
    assert "lj-fallback" in res.method and "0.1 eV/atom" in res.method


def test_below_hull_is_negative(fallback_engine, cache):
    """Without the NaCl reference, the Na + Cl2 hull is far above rock-salt NaCl."""
    fetcher = FakeFetcher([r for r in na_cl_refs() if r.material_id != "mp-nacl"])
    cand = load_rocksalt_nacl()
    res = ha.evaluate_hull(cand, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert res.e_hull_ev < 0
    assert res.e_hull_ev == pytest.approx(res.formation_energy_per_atom, abs=1e-9)  # hull = elements only
    assert res.decomposition == pytest.approx({"Na": 0.5, "Cl2": 0.5})


def load_rocksalt_nacl(a: float = 5.64) -> Atoms:
    return AseAtomsAdaptor.get_atoms(rocksalt("Na", "Cl", a))


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------
def test_cache_memory_and_disk(fallback_engine, tmp_path):
    directory = tmp_path / "hull"
    cache = ha.HullCache(directory)
    fetcher = FakeFetcher(na_cl_refs())
    cand = load_rocksalt_nacl()
    first = ha.evaluate_hull(cand, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert len(fetcher.calls) == 1
    assert sorted(fetcher.calls[0]) == sorted(["Cl", "Na", "Cl-Na"])

    second = ha.evaluate_hull(cand, engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert len(fetcher.calls) == 1
    assert second == first

    # On disk: one atomically written JSON file, no temp leftovers.
    files = sorted(p.name for p in directory.iterdir())
    assert files == ["lj-fallback__Cl-Na.json"]
    data = json.loads((directory / files[0]).read_text(encoding="utf-8"))
    assert data["version"] == ha.CACHE_VERSION and data["chemsys"] == "Cl-Na"

    # A fresh in-memory cache on the same directory needs neither a fetch nor an API key.
    def no_fetch(chemsys_list, api_key):  # pragma: no cover - must not be called
        raise AssertionError("fetch on a disk-cache hit")

    third = ha.evaluate_hull(cand, engine=fallback_engine, fetcher=no_fetch, cache=ha.HullCache(directory))
    assert third == first

    # Another backend is another key.
    other = UMLIPEngine(backend="fallback")
    other.backend_name = "other-backend"
    ha.evaluate_hull(cand, engine=other, fetcher=fetcher, cache=cache)
    assert len(fetcher.calls) == 2
    assert (directory / "other-backend__Cl-Na.json").is_file()


def test_cache_ignores_corrupt_or_stale_files(fallback_engine, tmp_path):
    directory = tmp_path / "hull"
    directory.mkdir()
    cache = ha.HullCache(directory)
    path = cache.path_for(fallback_engine.backend_name, "Cl-Na")
    path.write_text("{not json", encoding="utf-8")
    fetcher = FakeFetcher(na_cl_refs())
    ha.evaluate_hull(load_rocksalt_nacl(), engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert len(fetcher.calls) == 1
    json.loads(path.read_text(encoding="utf-8"))  # rewritten, valid

    # Different selection parameters (here: the cap) invalidate the cached set.
    ha.evaluate_hull(load_rocksalt_nacl(), engine=fallback_engine, fetcher=fetcher,
                     cache=ha.HullCache(directory), max_references=2)
    assert len(fetcher.calls) == 2


def test_default_cache_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LATTICEFLOW_HULL_CACHE_DIR", str(tmp_path / "x"))
    assert ha.default_cache_dir() == tmp_path / "x"
    monkeypatch.delenv("LATTICEFLOW_HULL_CACHE_DIR")
    assert ha.default_cache_dir().parts[-3:] == (".cache", "latticeflow", "hull")


# --------------------------------------------------------------------------
# selection, cap, errors
# --------------------------------------------------------------------------
def test_missing_elemental_reference(fallback_engine, cache):
    fetcher = FakeFetcher([r for r in sr_ti_o_refs() if r.material_id != "mp-o2"])
    with pytest.raises(ha.HullMissingElementalReferenceError) as info:
        ha.evaluate_hull(load_preset("srtio3"), engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert info.value.elements == ["O"]


def test_large_disordered_and_unstable_references_are_skipped(fallback_engine, cache):
    big = preset_structure("si") * (3, 1, 1) * (1, 3, 1)  # 72 atoms
    disordered = Structure(Lattice.cubic(5.43), [{"Si": 0.5}], [[0, 0, 0]])
    refs = [
        ha.MPReference("mp-si", preset_structure("si"), 0.0),
        ha.MPReference("mp-si-big", big, 0.02),
        ha.MPReference("mp-si-dis", disordered, 0.0),
        ha.MPReference("mp-si-fcc", fcc("Si", 3.85), 0.5),  # above the 0.1 eV/atom cutoff: ignored
        ha.MPReference("mp-si", preset_structure("si"), 0.0),  # duplicate id: ignored
    ]
    refset = ha.get_reference_set(["Si"], fallback_engine, fetcher=FakeFetcher(refs), cache=cache)
    assert [r.material_id for r in refset.references] == ["mp-si"]
    reasons = {s.material_id: s.reason for s in refset.skipped}
    assert set(reasons) == {"mp-si-big", "mp-si-dis"}
    assert "72 atoms > 64" in reasons["mp-si-big"]
    res = ha.evaluate_hull(load_preset("si"), engine=fallback_engine, fetcher=FakeFetcher(refs), cache=cache)
    assert "skipped 2" in res.method and "mp-si-big" in res.method


def test_reference_cap_keeps_elements_and_lowest_ehull(fallback_engine, cache, caplog):
    refs = [
        ha.MPReference("mp-na", bcc("Na", 4.23), 0.08),       # elemental phases always kept,
        ha.MPReference("mp-na-fcc", fcc("Na", 5.30), 0.09),   # but only the lowest-E_hull one is mandatory
        ha.MPReference("mp-cl2", dimer_in_box("Cl", 1.99), 0.0),
    ]
    ehulls = [0.05, 0.0, 0.03, 0.01, 0.07, 0.02]
    for k, e in enumerate(ehulls):
        refs.append(ha.MPReference(f"mp-nacl-{k}", rocksalt("Na", "Cl", 5.5 + 0.05 * k), e))
    caplog.set_level("INFO", logger="app.core.hull_analyzer")
    refset = ha.get_reference_set(["Na", "Cl"], fallback_engine, fetcher=FakeFetcher(refs), cache=cache,
                                  max_references=4)
    ids = [r.material_id for r in refset.references]
    assert len(ids) == 4
    assert {"mp-na", "mp-cl2"} <= set(ids)
    assert set(ids) - {"mp-na", "mp-cl2"} == {"mp-nacl-1", "mp-nacl-3"}  # E_hull 0.0 and 0.01
    dropped = [d.material_id for d in refset.dropped]
    assert dropped == ["mp-nacl-5", "mp-nacl-2", "mp-nacl-0", "mp-nacl-4", "mp-na-fcc"]
    assert "dropped 5 references over the cap" in caplog.text
    res = ha.evaluate_hull(load_rocksalt_nacl(), engine=fallback_engine, fetcher=FakeFetcher(refs), cache=cache,
                           max_references=4)
    assert "dropped 5" in res.method
    assert len(res.references) == 4


def test_default_cap_is_40():
    assert ha.MAX_REFERENCES == 40 and ha.MAX_REFERENCE_ATOMS == 64


def test_fetch_failure_is_wrapped(fallback_engine, cache):
    fetcher = FakeFetcher([], error=ConnectionError("unreachable"))
    with pytest.raises(ha.HullFetchError, match="unreachable"):
        ha.evaluate_hull(load_preset("si"), engine=fallback_engine, fetcher=fetcher, cache=cache)
    bad_items = lambda chemsys, key: [object()]  # noqa: E731
    with pytest.raises(ha.HullFetchError):
        ha.evaluate_hull(load_preset("si"), engine=fallback_engine, fetcher=bad_items, cache=cache)


def test_tuple_items_are_accepted(fallback_engine, cache):
    fetcher = lambda chemsys, key: [("mp-149", preset_structure("si"))]  # noqa: E731
    res = ha.evaluate_hull(load_preset("si"), engine=fallback_engine, fetcher=fetcher, cache=cache)
    assert [r.material_id for r in res.references] == ["mp-149"]
    assert res.e_hull_ev >= -1e-9  # the unrelaxed preset cannot beat its own relaxed structure


# --------------------------------------------------------------------------
# default (Materials Project) fetcher, without any network
# --------------------------------------------------------------------------
def test_default_fetcher_requires_valid_key(fallback_engine, cache):
    with pytest.raises(ha.HullNoApiKeyError):
        ha.fetch_reference_structures(["Si"], None)
    with pytest.raises(ha.HullNoApiKeyError):
        ha.fetch_reference_structures(["Si"], "   ")
    with pytest.raises(ha.HullNoApiKeyError, match="malformed"):
        ha.fetch_reference_structures(["Si"], "too-short")
    with pytest.raises(ha.HullNoApiKeyError):  # through evaluate_hull with the default fetcher
        ha.evaluate_hull(load_preset("si"), engine=fallback_engine, api_key=None, cache=cache)


class _FakeDoc:
    def __init__(self, material_id, structure, energy_above_hull):
        self.material_id = material_id
        self.structure = structure
        self.energy_above_hull = energy_above_hull


def test_default_fetcher_query_shape(monkeypatch):
    import mp_api.client

    seen = {}

    class FakeSummary:
        def search(self, **kwargs):
            seen["search"] = kwargs
            return [
                _FakeDoc("mp-149", preset_structure("si"), 0.0),
                {"material_id": "mp-x", "structure": preset_structure("si").as_dict(), "energy_above_hull": 0.05},
                _FakeDoc("mp-none", None, 0.0),
            ]

    class FakeRester:
        def __init__(self, **kwargs):
            seen["init"] = kwargs
            self.session = type("S", (), {"headers": {}})()
            self.materials = type("M", (), {"summary": FakeSummary()})()

        def __enter__(self):
            seen["rester"] = self
            return self

        def __exit__(self, *exc):
            seen["closed"] = True

    monkeypatch.setattr(mp_api.client, "MPRester", FakeRester)
    out = ha.fetch_reference_structures(["Si"], "k" * 32)
    assert [(r.material_id, r.mp_e_above_hull) for r in out] == [("mp-149", 0.0), ("mp-x", 0.05)]
    assert all(isinstance(r.structure, Structure) for r in out)
    assert seen["init"] == {"api_key": "k" * 32, "mute_progress_bars": True, "include_user_agent": False}
    # MP answers 403 to a request with no User-Agent; send a generic library-only one.
    ua = seen["rester"].session.headers["user-agent"]
    assert ua.startswith("mp-api") and "Windows" not in ua and "Python" not in ua and "atticeflow" not in ua
    assert seen["search"] == {
        "chemsys": ["Si"],
        "energy_above_hull": (0.0, ha.MP_E_HULL_CUTOFF),
        "deprecated": False,
        "include_gnome": False,
        "fields": ha.MP_SUMMARY_FIELDS,
    }
    assert seen["closed"]


def test_default_fetcher_wraps_client_errors(monkeypatch):
    import mp_api.client
    from mp_api.client.core.exceptions import MPRestError

    class FailingRester:
        def __init__(self, **kwargs):
            raise MPRestError("REST query returned with error status code 403 on URL ...")

    monkeypatch.setattr(mp_api.client, "MPRester", FailingRester)
    with pytest.raises(ha.HullFetchError, match="rejected"):
        ha.fetch_reference_structures(["Si"], "k" * 32)


# --------------------------------------------------------------------------
# performance (fake 3-element system, fallback engine)
# --------------------------------------------------------------------------
def test_timing_three_element_fallback(fallback_engine, tmp_path, capsys):
    fetcher = FakeFetcher(sr_ti_o_refs())
    cache = ha.HullCache(tmp_path / "hull")
    t0 = time.perf_counter()
    ha.evaluate_hull(load_preset("srtio3"), engine=fallback_engine, fetcher=fetcher, cache=cache)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    ha.evaluate_hull(load_preset("srtio3"), engine=fallback_engine, fetcher=fetcher, cache=cache)
    warm = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[hull timing] Sr-Ti-O, 7 fake refs, lj-fallback: cold {cold:.2f} s, cached {warm * 1e3:.1f} ms")
    assert warm < cold
    assert cold < 60.0


# --------------------------------------------------------------------------
# MACE: physically sensible signs
# --------------------------------------------------------------------------
@pytest.mark.requires_mace
def test_mace_nacl_hull_signs(mace_engine, tmp_path, capsys):
    cand = load_rocksalt_nacl()

    # Hull of the elements only: rock-salt NaCl is far below Na + Cl2.
    t0 = time.perf_counter()
    elements_only = FakeFetcher([r for r in na_cl_refs() if r.material_id != "mp-nacl"])
    res = ha.evaluate_hull(cand, engine=mace_engine, fetcher=elements_only, cache=ha.HullCache(tmp_path / "a"))
    cold = time.perf_counter() - t0
    assert res.umlip_backend.startswith("mace")
    assert res.formation_energy_per_atom < -1.0      # DFT/expt. about -2.1 eV/atom
    assert res.e_hull_ev < -1.0
    assert res.decomposition == pytest.approx({"Na": 0.5, "Cl2": 0.5})

    # With rock salt and a CsCl-type polymorph as references: rock salt is on the hull and
    # the given (unrelaxed, experimental-a) rock salt is at most slightly above it.
    fetcher = FakeFetcher(na_cl_refs() + [ha.MPReference("mp-nacl-cscl", cscl("Na", "Cl", 3.36), 0.03)])
    cache = ha.HullCache(tmp_path / "b")
    refset = ha.get_reference_set(["Na", "Cl"], mace_engine, fetcher=fetcher, cache=cache)
    stable = {e.attribute for e in refset.phase_diagram().stable_entries}
    assert "mp-nacl" in stable
    assert "mp-nacl-cscl" not in stable
    t0 = time.perf_counter()
    res2 = ha.evaluate_hull(cand, engine=mace_engine, fetcher=fetcher, cache=cache)
    warm = time.perf_counter() - t0
    assert 0.0 <= res2.e_hull_ev < 0.02
    assert res2.decomposition == pytest.approx({"NaCl": 1.0})
    relaxed = ha.evaluate_hull(relaxed_atoms(refset, "mp-nacl"), engine=mace_engine, fetcher=fetcher, cache=cache)
    assert abs(relaxed.e_hull_ev) < 1e-6
    with capsys.disabled():
        print(f"\n[hull timing] Na-Cl, {res2.umlip_backend}: cold {cold:.2f} s (2 refs), cached {warm * 1e3:.1f} ms")
