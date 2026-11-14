"""Project library: SQLite persistence of projects, structures, results and jobs.

One database file ``library.db`` in ``LATTICEFLOW_DATA_DIR`` (default
``~/.latticeflow``), stdlib ``sqlite3`` in WAL mode. Every call opens its own
short-lived connection, and every multi-statement write runs under one
process-wide ``RLock``, so a :class:`Library` is safe to share between the
request threadpool and the job worker.

Rows are plain ``dict`` objects (JSON columns decoded); the REST layer turns
them into the pydantic models of ``app.schemas.library``.

* **Projects**: a default project "Scratch" (``is_default``) always exists and
  cannot be deleted.
* **Structures**: ``content_hash`` (:func:`content_hash`) is unique per project;
  :meth:`add_structure` of an already stored structure returns the stored row
  with ``existing = True``.
* **Results**: ``data`` is the JSON of the matching event / response, zlib
  compressed above ``COMPRESS_ABOVE`` bytes; ``summary`` comes from
  :func:`summarize`. One row per (structure, kind, model_id, dispersion,
  settings): a newer result *updates that row in place* (same id, new data,
  summary and ``created_at``), so job ``result_id`` references stay valid.
* **Jobs**: see ``app.core.jobs`` (queue logic); this module only stores them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from app import __version__ as CODE_VERSION

DEFAULT_PROJECT_NAME = "Scratch"
RESULT_KINDS = ("relax", "phonons", "elastic", "eos", "hull", "md", "diffraction", "superconductivity")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled", "skipped")
ACTIVE_STATUSES = ("queued", "running")
FINISHED_STATUSES = ("done", "failed", "cancelled", "skipped")
#: ``data`` JSON larger than this (bytes) is stored zlib-compressed.
COMPRESS_ABOVE = 32 * 1024
#: Export bundle identification.
BUNDLE_FORMAT = "latticeflow-project"
BUNDLE_VERSION = 1
#: Rounding of the content hash: lattice in Angstrom, fractional coordinates.
HASH_LATTICE_DECIMALS = 3
HASH_FRAC_DECIMALS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS structures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    formula TEXT NOT NULL,
    n_atoms INTEGER NOT NULL,
    spacegroup_symbol TEXT NOT NULL,
    spacegroup_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    parent_id INTEGER REFERENCES structures(id) ON DELETE SET NULL,
    origin TEXT NOT NULL DEFAULT 'import',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    crystal TEXT NOT NULL,
    UNIQUE (project_id, content_hash)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id INTEGER NOT NULL REFERENCES structures(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dispersion INTEGER NOT NULL DEFAULT 0,
    settings TEXT NOT NULL,
    settings_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    data BLOB NOT NULL,
    data_encoding TEXT NOT NULL DEFAULT 'json',
    created_at TEXT NOT NULL,
    code_version TEXT NOT NULL,
    UNIQUE (structure_id, kind, model_id, dispersion, settings_key)
);
CREATE INDEX IF NOT EXISTS results_by_structure ON results(structure_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    structure_id INTEGER REFERENCES structures(id) ON DELETE SET NULL,
    chain_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    n_steps INTEGER NOT NULL,
    kind TEXT NOT NULL,
    settings TEXT NOT NULL,
    only_if_unstable INTEGER NOT NULL DEFAULT 0,
    model_id TEXT,
    dispersion INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    result_id INTEGER REFERENCES results(id) ON DELETE SET NULL,
    output_structure_id INTEGER REFERENCES structures(id) ON DELETE SET NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, position);
CREATE INDEX IF NOT EXISTS jobs_by_chain ON jobs(chain_id, step_index);
"""


class LibraryError(Exception):
    """Base class; ``status`` / ``code`` are used by the REST layer."""

    status = 422
    code = "INVALID_PAYLOAD"


class NotFound(LibraryError):
    status = 404
    code = "NOT_FOUND"


class Conflict(LibraryError):
    status = 409
    code = "INVALID_OPERATION"


def now_iso() -> str:
    """UTC timestamp, ISO 8601 with milliseconds and a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_data_dir() -> Path:
    raw = os.environ.get("LATTICEFLOW_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".latticeflow"


# --------------------------------------------------------------------------- #
# content hash and summaries
# --------------------------------------------------------------------------- #
def _r(x: float, decimals: int) -> float:
    v = round(float(x), decimals)
    return 0.0 if v == 0 else v  # no "-0.0"


def content_hash(crystal: Dict[str, Any]) -> str:
    """SHA-256 of species + lattice matrix + wrapped fractional coordinates.

    Lattice rows are rounded to 1e-3 Angstrom, fractional coordinates are
    wrapped into [0, 1) and rounded to 1e-3 (a coordinate that rounds to 1.0
    counts as 0.0); the (element, u, v, w) tuples are sorted, so atom order does
    not matter. Energies, forces and bonds are ignored.
    """
    matrix = [[_r(x, HASH_LATTICE_DECIMALS) for x in row] for row in crystal["lattice"]["matrix"]]
    sites = []
    for atom in crystal["atoms"]:
        frac = []
        for x in atom["frac_coords"]:
            w = float(x) - math.floor(float(x))
            v = _r(w, HASH_FRAC_DECIMALS)
            frac.append(0.0 if v >= 1.0 else v)
        sites.append([str(atom["element"]), *frac])
    sites.sort()
    blob = json.dumps({"lattice": matrix, "sites": sites}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def summarize(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Small flat summary of one result (keys are unique across kinds, see docs/protocol.md §6)."""
    if kind == "relax":
        c = data["crystal"]
        return {
            "energy_per_atom_ev": _num(c.get("energy_per_atom")),
            "max_force_ev_a": _num(c.get("max_force")),
            "pressure_gpa": _num(c.get("pressure_gpa")),
            "relax_converged": bool(data.get("converged")),
            "steps": int(data.get("step", 0)),
        }
    if kind == "phonons":
        dim = data.get("supercell_dim")
        return {
            "min_frequency_thz": _num(data.get("min_frequency_thz")),
            "imaginary_mode_count": int(data.get("imaginary_mode_count", 0)),
            "dynamically_stable": bool(data.get("is_dynamically_stable")),
            "supercell": "x".join(str(int(n)) for n in dim) if dim else None,
        }
    if kind == "elastic":
        return {
            "k_vrh_gpa": _num(data.get("k_vrh_gpa")),
            "g_vrh_gpa": _num(data.get("g_vrh_gpa")),
            "born_stable": bool(data.get("born_stable")),
        }
    if kind == "eos":
        return {"k_gpa": _num(data.get("k_gpa")), "v0": _num(data.get("v0"))}
    if kind == "hull":
        return {
            "e_hull_ev": _num(data.get("e_hull_ev")),
            "formation_energy_ev": _num(data.get("formation_energy_per_atom")),
        }
    if kind == "md":
        out: Dict[str, Any] = {"temperature_k": _num(data.get("mean_temperature_k"))}
        for el, d in sorted((data.get("diffusion_coefficients_cm2_s") or {}).items()):
            out[f"diffusion_{el}_cm2_s"] = _num(d)
        return out
    if kind == "diffraction":
        peaks = data.get("peaks") or []
        top = max(peaks, key=lambda p: p.get("intensity", 0.0)) if peaks else None
        return {"n_peaks": len(peaks), "strongest_peak_two_theta": _num(top["two_theta"]) if top else None}
    if kind == "superconductivity":   # Tier 0 proxies: tc_ref_k is a ranking scale at an assumed λ, not a T_c
        d = data.get("descriptors") or {}
        verdict = (data.get("metallicity") or {}).get("verdict")
        return {
            "omega_log_k": _num(d.get("omega_log_k")),
            "omega_2_k": _num(d.get("omega_2_k")),
            "tc_ref_k": _num(d.get("tc_ref_k")),
            "light_element_fraction": _num(d.get("light_element_fraction")),
            "metallicity": verdict,
            "sc_flags": ",".join(str(f) for f in (data.get("flags") or [])),
            "sc_gate_pass": bool(d.get("reliable")) and verdict not in ("insulator", "likely_insulator"),
        }
    raise LibraryError(f"unknown result kind {kind!r}")


def settings_key(settings: Dict[str, Any]) -> str:
    return json.dumps(settings or {}, sort_keys=True, separators=(",", ":"))


def _encode(data: Dict[str, Any]) -> tuple:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    if len(raw) > COMPRESS_ABOVE:
        return zlib.compress(raw, 6), "zlib"
    return raw, "json"


def _decode(blob: bytes, encoding: str) -> Dict[str, Any]:
    raw = zlib.decompress(blob) if encoding == "zlib" else blob
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
_STRUCT_COLS = ("id, project_id, name, formula, n_atoms, spacegroup_symbol, spacegroup_number, content_hash, "
                "parent_id, origin, notes, created_at, updated_at")
_RESULT_COLS = "id, structure_id, kind, model_id, dispersion, settings, summary, created_at, code_version"
_JOB_JSON = ("settings",)


class Library:
    """SQLite-backed project library (thread-safe; see module docstring)."""

    def __init__(self, directory: Optional[os.PathLike] = None) -> None:
        self.directory = Path(directory) if directory is not None else default_data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "library.db"
        self.lock = threading.RLock()
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
        self.default_project_id()

    # ------------------------------------------------------------ plumbing
    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Locked write transaction."""
        with self.lock, self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._conn() as db:
            return db.execute(sql, tuple(args)).fetchone()

    def _all(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._conn() as db:
            return db.execute(sql, tuple(args)).fetchall()

    # ------------------------------------------------------------ projects
    def default_project_id(self) -> int:
        with self.tx() as db:
            row = db.execute("SELECT id FROM projects WHERE is_default = 1").fetchone()
            if row is not None:
                return int(row["id"])
            t = now_iso()
            cur = db.execute("INSERT INTO projects (name, is_default, created_at, updated_at) VALUES (?, 1, ?, ?)",
                             (DEFAULT_PROJECT_NAME, t, t))
            return int(cur.lastrowid)

    _PROJECT_SQL = ("SELECT p.id, p.name, p.is_default, p.created_at, p.updated_at, "
                    "(SELECT COUNT(*) FROM structures s WHERE s.project_id = p.id) AS n_structures FROM projects p")

    @staticmethod
    def _project(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["is_default"] = bool(d["is_default"])
        return d

    def list_projects(self) -> List[Dict[str, Any]]:
        return [self._project(r) for r in self._all(self._PROJECT_SQL + " ORDER BY p.is_default DESC, p.id")]

    def get_project(self, pid: int) -> Dict[str, Any]:
        row = self._one(self._PROJECT_SQL + " WHERE p.id = ?", (pid,))
        if row is None:
            raise NotFound(f"project {pid} not found")
        return self._project(row)

    def create_project(self, name: str) -> Dict[str, Any]:
        t = now_iso()
        with self.tx() as db:
            cur = db.execute("INSERT INTO projects (name, is_default, created_at, updated_at) VALUES (?, 0, ?, ?)",
                             (name, t, t))
        return self.get_project(int(cur.lastrowid))

    def rename_project(self, pid: int, name: str) -> Dict[str, Any]:
        with self.tx() as db:
            cur = db.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), pid))
            if cur.rowcount == 0:
                raise NotFound(f"project {pid} not found")
        return self.get_project(pid)

    def delete_project(self, pid: int) -> None:
        project = self.get_project(pid)
        if project["is_default"]:
            raise Conflict("the default project cannot be deleted")
        with self.tx() as db:
            busy = db.execute("SELECT COUNT(*) FROM jobs WHERE project_id = ? AND status IN ('queued', 'running')",
                              (pid,)).fetchone()[0]
            if busy:
                raise Conflict(f"project {pid} has {busy} queued or running job(s); cancel them first")
            db.execute("DELETE FROM jobs WHERE project_id = ?", (pid,))
            db.execute("UPDATE structures SET parent_id = NULL WHERE project_id = ?", (pid,))
            db.execute("DELETE FROM projects WHERE id = ?", (pid,))

    def _touch(self, db: sqlite3.Connection, pid: int) -> None:
        db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), pid))

    # ------------------------------------------------------------ structures
    @staticmethod
    def _structure(row: sqlite3.Row, with_crystal: bool = True) -> Dict[str, Any]:
        d = dict(row)
        if "crystal" in d:
            if with_crystal:
                d["crystal"] = json.loads(d["crystal"])
            else:
                del d["crystal"]
        return d

    def add_structure(self, pid: int, crystal: Dict[str, Any], *, name: Optional[str] = None,
                      parent_id: Optional[int] = None, origin: str = "import", notes: str = "") -> Dict[str, Any]:
        """Store ``crystal`` (a ``CrystalStatePayload`` dict); dedup by content hash (``existing``)."""
        self.get_project(pid)
        h = content_hash(crystal)
        with self.tx() as db:
            row = db.execute(f"SELECT {_STRUCT_COLS}, crystal FROM structures WHERE project_id = ? AND content_hash = ?",
                             (pid, h)).fetchone()
            if row is not None:
                out = self._structure(row)
                out["existing"] = True
                return out
            if parent_id is not None:
                p = db.execute("SELECT project_id FROM structures WHERE id = ?", (parent_id,)).fetchone()
                if p is None or int(p["project_id"]) != pid:
                    raise LibraryError(f"parent_id {parent_id} is not a structure of project {pid}")
            t = now_iso()
            cur = db.execute(
                "INSERT INTO structures (project_id, name, formula, n_atoms, spacegroup_symbol, spacegroup_number, "
                "content_hash, parent_id, origin, notes, created_at, updated_at, crystal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, name or crystal["formula"], crystal["formula"], len(crystal["atoms"]),
                 crystal["spacegroup_symbol"], int(crystal["spacegroup_number"]), h, parent_id, origin, notes,
                 t, t, json.dumps(crystal, separators=(",", ":"))),
            )
            self._touch(db, pid)
        out = self.get_structure(int(cur.lastrowid))
        out["existing"] = False
        return out

    def get_structure(self, sid: int, with_crystal: bool = True) -> Dict[str, Any]:
        row = self._one(f"SELECT {_STRUCT_COLS}, crystal FROM structures WHERE id = ?", (sid,))
        if row is None:
            raise NotFound(f"structure {sid} not found")
        return self._structure(row, with_crystal)

    def list_structures(self, pid: int) -> List[Dict[str, Any]]:
        """Rows without ``crystal``, with ``latest`` (flattened newest summary per kind) and ``latest_result_ids``."""
        self.get_project(pid)
        rows = [dict(r) for r in self._all(f"SELECT {_STRUCT_COLS} FROM structures WHERE project_id = ? ORDER BY id",
                                           (pid,))]
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            r["latest"], r["latest_result_ids"] = {}, {}
        seen: set = set()
        for res in self._all(
            "SELECT r.id, r.structure_id, r.kind, r.summary FROM results r JOIN structures s ON s.id = r.structure_id "
            "WHERE s.project_id = ? ORDER BY r.created_at DESC, r.id DESC", (pid,)
        ):
            key = (res["structure_id"], res["kind"])
            if key in seen:
                continue
            seen.add(key)
            target = by_id[res["structure_id"]]
            target["latest"].update(json.loads(res["summary"]))
            target["latest_result_ids"][res["kind"]] = res["id"]
        return rows

    def update_structure(self, sid: int, *, name: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
        s = self.get_structure(sid, with_crystal=False)
        with self.tx() as db:
            db.execute("UPDATE structures SET name = ?, notes = ?, updated_at = ? WHERE id = ?",
                       (s["name"] if name is None else name, s["notes"] if notes is None else notes, now_iso(), sid))
            self._touch(db, s["project_id"])
        return self.get_structure(sid)

    def delete_structure(self, sid: int) -> None:
        s = self.get_structure(sid, with_crystal=False)
        with self.tx() as db:
            busy = db.execute("SELECT COUNT(*) FROM jobs WHERE structure_id = ? AND status IN ('queued', 'running')",
                              (sid,)).fetchone()[0]
            if busy:
                raise Conflict(f"structure {sid} has {busy} queued or running job(s); cancel them first")
            db.execute("DELETE FROM structures WHERE id = ?", (sid,))
            self._touch(db, s["project_id"])

    # ------------------------------------------------------------ results
    @staticmethod
    def _result(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["dispersion"] = bool(d["dispersion"])
        d["settings"] = json.loads(d["settings"])
        d["summary"] = json.loads(d["summary"])
        if "data" in d:
            d["data"] = _decode(d["data"], d.pop("data_encoding"))
        return d

    def add_result(self, sid: int, kind: str, data: Dict[str, Any], *, model_id: str, dispersion: bool = False,
                   settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Store a result (summary computed here); replaces the row with the same key in place (``replaced``)."""
        if kind not in RESULT_KINDS:
            raise LibraryError(f"unknown result kind {kind!r}; one of {', '.join(RESULT_KINDS)}")
        s = self.get_structure(sid, with_crystal=False)
        settings = dict(settings or {})
        summary = summarize(kind, data)
        blob, enc = _encode(data)
        key = settings_key(settings)
        t = now_iso()
        with self.tx() as db:
            old = db.execute("SELECT id FROM results WHERE structure_id = ? AND kind = ? AND model_id = ? "
                             "AND dispersion = ? AND settings_key = ?",
                             (sid, kind, model_id, int(dispersion), key)).fetchone()
            values = (json.dumps(summary), sqlite3.Binary(blob), enc, t, CODE_VERSION)
            if old is not None:
                rid = int(old["id"])
                db.execute("UPDATE results SET summary = ?, data = ?, data_encoding = ?, created_at = ?, "
                           "code_version = ? WHERE id = ?", (*values, rid))
            else:
                cur = db.execute(
                    "INSERT INTO results (structure_id, kind, model_id, dispersion, settings, settings_key, summary, "
                    "data, data_encoding, created_at, code_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, kind, model_id, int(dispersion), json.dumps(settings), key, *values),
                )
                rid = int(cur.lastrowid)
            self._touch(db, s["project_id"])
        out = self.get_result(rid, with_data=False)
        out["replaced"] = old is not None
        return out

    def get_result(self, rid: int, with_data: bool = True) -> Dict[str, Any]:
        cols = _RESULT_COLS + (", data, data_encoding" if with_data else "")
        row = self._one(f"SELECT {cols} FROM results WHERE id = ?", (rid,))
        if row is None:
            raise NotFound(f"result {rid} not found")
        return self._result(row)

    def list_results(self, sid: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_structure(sid, with_crystal=False)
        sql = f"SELECT {_RESULT_COLS} FROM results WHERE structure_id = ?"
        args: List[Any] = [sid]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [self._result(r) for r in self._all(sql + " ORDER BY created_at DESC, id DESC", args)]

    def delete_result(self, rid: int) -> None:
        with self.tx() as db:
            if db.execute("DELETE FROM results WHERE id = ?", (rid,)).rowcount == 0:
                raise NotFound(f"result {rid} not found")

    # ------------------------------------------------------------ export / import
    def export_project(self, pid: int) -> Dict[str, Any]:
        project = self.get_project(pid)
        structures = [self._structure(r) for r in self._all(
            f"SELECT {_STRUCT_COLS}, crystal FROM structures WHERE project_id = ? ORDER BY id", (pid,))]
        results = [self._result(r) for r in self._all(
            f"SELECT r.{_RESULT_COLS.replace(', ', ', r.')}, r.data, r.data_encoding FROM results r "
            "JOIN structures s ON s.id = r.structure_id WHERE s.project_id = ? ORDER BY r.id", (pid,))]
        keep_s = ("id", "name", "formula", "content_hash", "parent_id", "origin", "notes", "created_at", "crystal")
        keep_r = ("structure_id", "kind", "model_id", "dispersion", "settings", "summary", "data", "created_at",
                  "code_version")
        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "exported_at": now_iso(),
            "code_version": CODE_VERSION,
            "project": {"name": project["name"], "created_at": project["created_at"]},
            "structures": [{k: s[k] for k in keep_s} for s in structures],
            "results": [{k: r[k] for k in keep_r} for r in results],
        }

    def import_project(self, bundle: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
        """Create a new project from an export bundle (ids are remapped; hashes recomputed)."""
        if bundle.get("format") != BUNDLE_FORMAT:
            raise LibraryError(f"not a {BUNDLE_FORMAT} bundle")
        if int(bundle.get("version", 0)) > BUNDLE_VERSION:
            raise LibraryError(f"bundle version {bundle.get('version')} is newer than supported ({BUNDLE_VERSION})")
        project = self.create_project(name or (bundle.get("project") or {}).get("name") or "Imported project")
        pid = project["id"]
        id_map: Dict[int, int] = {}
        pending_parents: List[tuple] = []
        for s in bundle.get("structures", []):
            row = self.add_structure(pid, s["crystal"], name=s.get("name"), origin=s.get("origin") or "import",
                                     notes=s.get("notes") or "")
            id_map[int(s["id"])] = row["id"]
            if s.get("parent_id") is not None:
                pending_parents.append((row["id"], int(s["parent_id"])))
        with self.tx() as db:
            for child, old_parent in pending_parents:
                parent = id_map.get(old_parent)
                if parent is not None and parent != child:
                    db.execute("UPDATE structures SET parent_id = ? WHERE id = ?", (parent, child))
        for r in bundle.get("results", []):
            sid = id_map.get(int(r["structure_id"]))
            if sid is None:
                continue
            self.add_result(sid, r["kind"], r["data"], model_id=r["model_id"], dispersion=bool(r.get("dispersion")),
                            settings=r.get("settings") or {})
        return self.get_project(pid)

    # ------------------------------------------------------------ jobs (storage only)
    @staticmethod
    def _job(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["settings"] = json.loads(d["settings"])
        d["only_if_unstable"] = bool(d["only_if_unstable"])
        d["dispersion"] = bool(d["dispersion"])
        return d

    def insert_chain(self, pid: int, sid: int, steps: Iterable[Dict[str, Any]], model_id: Optional[str],
                     dispersion: bool) -> List[int]:
        steps = list(steps)
        chain = uuid.uuid4().hex
        t = now_iso()
        ids = []
        with self.tx() as db:
            pos = int(db.execute("SELECT COALESCE(MAX(position), -1) FROM jobs").fetchone()[0]) + 1
            for i, step in enumerate(steps):
                cur = db.execute(
                    "INSERT INTO jobs (project_id, structure_id, chain_id, step_index, n_steps, kind, settings, "
                    "only_if_unstable, model_id, dispersion, status, position, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (pid, sid, chain, i, len(steps), step["kind"], json.dumps(step["settings"]),
                     int(bool(step.get("only_if_unstable"))), model_id, int(dispersion), pos + i, t),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def get_job(self, jid: int) -> Dict[str, Any]:
        row = self._one("SELECT * FROM jobs WHERE id = ?", (jid,))
        if row is None:
            raise NotFound(f"job {jid} not found")
        return self._job(row)

    def list_jobs(self, project_id: Optional[int] = None, active: Optional[bool] = None) -> List[Dict[str, Any]]:
        sql, args, where = "SELECT * FROM jobs", [], []
        if project_id is not None:
            where.append("project_id = ?")
            args.append(project_id)
        if active is True:
            where.append("status IN ('queued', 'running')")
        elif active is False:
            where.append("status NOT IN ('queued', 'running')")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [self._job(r) for r in self._all(sql + " ORDER BY position, id", args)]

    def chain_jobs(self, chain_id: str) -> List[Dict[str, Any]]:
        return [self._job(r) for r in self._all("SELECT * FROM jobs WHERE chain_id = ? ORDER BY step_index",
                                                (chain_id,))]

    def update_job(self, jid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), jid))

    def requeue_running(self) -> int:
        """Startup: jobs left ``running`` by a previous process go back to ``queued``."""
        with self.tx() as db:
            return db.execute("UPDATE jobs SET status = 'queued', progress = 0, started_at = NULL, "
                              "message = 'requeued after restart' WHERE status = 'running'").rowcount


_default: Optional[Library] = None
_default_lock = threading.Lock()


def get_library() -> Library:
    """Process-wide library in ``LATTICEFLOW_DATA_DIR`` (created on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Library()
        return _default
