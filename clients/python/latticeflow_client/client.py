"""Synchronous client for a LatticeFlow Studio backend.

>>> from latticeflow_client import Studio
>>> with Studio("http://localhost:8000") as studio:      # connects; the server loads SrTiO3
...     studio.relax(fmax=0.01)
...     ph = studio.phonons(cell_mode="primitive", dos=True)
...     print(ph["min_frequency_thz"], ph["dos"]["thermal"]["zero_point_energy_ev_per_atom"])

One :class:`Studio` = one ``/ws/studio`` session (its own active structure, undo
history, force field and phonon cache) plus an ``httpx`` client for ``/api/*``.
A background thread reads the socket and keeps :attr:`crystal`, :attr:`history`,
:attr:`model` and :attr:`phonon` current; each command method sends one envelope
and blocks until the command's documented terminal sequence has arrived
(:mod:`latticeflow_client.protocol`). Calls are serialised (one command at a time
per session); :meth:`stop` may be called from another thread or from an
``on_frame`` / ``on_progress`` callback.

The project library and the job queue are plain REST (no session needed; works
with ``connect=False``)::

    pid = studio.create_project("Perovskites")["id"]
    sid = studio.save_structure(pid)["id"]                       # the active structure
    chain = studio.submit_jobs(pid, [sid], ["relax", "phonons"])
    studio.wait_jobs(chain)
    print(studio.structures(pid)[-1]["latest"])                   # the relaxed child's summaries
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from latticeflow_client import protocol as P
from latticeflow_client.errors import StreamStopped, StudioConnectionError, StudioError, StudioTimeout
from latticeflow_client.structures import (
    Crystal,
    ase_to_cif,
    ase_to_extxyz,
    crystal_to_ase,
    crystal_to_pymatgen,
    parse_formula,
    pymatgen_to_cif,
    pymatgen_to_extxyz,
)

Callback = Optional[Callable[[Dict[str, Any]], Any]]
_CLOSED = object()


class Reply:
    """Events received for one command (in wire order), keyed access by event type."""

    def __init__(self, command: Optional[str], request_id: Optional[str] = None) -> None:
        self.command = command
        self.request_id = request_id
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    def add(self, event: str, payload: Dict[str, Any]) -> None:
        self.events.append((event, payload))

    def get(self, event: str, default: Any = None) -> Any:
        """Payload of the last ``event`` received for this command."""
        for etype, payload in reversed(self.events):
            if etype == event:
                return payload
        return default

    def all(self, event: str) -> List[Dict[str, Any]]:
        return [p for e, p in self.events if e == event]

    def __getitem__(self, event: str) -> Dict[str, Any]:
        value = self.get(event)
        if value is None:
            raise KeyError(event)
        return value

    def __contains__(self, event: str) -> bool:
        return any(e == event for e, _ in self.events)

    def __repr__(self) -> str:
        return f"Reply({self.command}, {[e for e, _ in self.events]})"


def _urls(url: str) -> Tuple[str, str]:
    url = url.rstrip("/")
    if url.startswith(("ws://", "wss://")):
        http = "http" + url[2:]
        if http.endswith("/ws/studio"):
            http = http[: -len("/ws/studio")]
        return http, url if url.endswith("/ws/studio") else url + "/ws/studio"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url, "ws" + url[4:] + "/ws/studio"


class Studio:
    """A LatticeFlow Studio session.

    Parameters
    ----------
    url:
        Backend base URL (``http://host:8000``; ``ws://host:8000/ws/studio`` also accepted).
    timeout:
        Default per-command wait in seconds (``None`` = wait for the terminal message forever).
    rest_timeout:
        ``httpx`` timeout for REST calls (the hull relaxes reference phases and can take minutes).
    connect:
        Open the WebSocket now (else call :meth:`connect`).
    on_event:
        Called with ``(event_type, payload)`` for every server message (from the reader thread).
    """

    def __init__(self, url: str = "http://localhost:8000", *, timeout: Optional[float] = None,
                 rest_timeout: Optional[float] = 600.0, connect: bool = True,
                 on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> None:
        self.base_url, self.ws_url = _urls(url)
        self.timeout = timeout
        self.on_event = on_event
        self.http = httpx.Client(base_url=self.base_url, timeout=rest_timeout)
        self._ws = None
        self._reader: Optional[threading.Thread] = None
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._call_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed_reason: Optional[str] = None
        #: attach a ``request_id`` to every command (False = behave like a pre-Phase D client)
        self.send_request_ids = True
        #: the server echoes ``request_id`` (Phase D); learnt from the first echoed reply
        self.server_echoes_request_ids = False
        #: latest active structure (CrystalStatePayload dict)
        self.crystal: Optional[Crystal] = None
        #: latest HISTORY_STATE payload (``entries`` = the whole undo history)
        self.history: Optional[Dict[str, Any]] = None
        #: latest MODEL_CHANGED payload (``None`` until SET_MODEL: the process default model)
        self.model: Optional[Dict[str, Any]] = None
        #: latest PHONON_ANALYSIS_COMPLETE payload
        self.phonon: Optional[Dict[str, Any]] = None
        #: Reply of the last command
        self.last_reply: Optional[Reply] = None
        if connect:
            self.connect()

    # ================================================================ connection
    def connect(self, open_timeout: float = 60.0) -> "Studio":
        """Open the session and wait for the initial SINGLE_POINT_UPDATE + HISTORY_STATE."""
        if self._ws is not None:
            return self
        self._inbox = queue.Queue()
        self._closed_reason = None
        self.server_echoes_request_ids = False
        try:
            # max_size=None: no client-side message size limit, on purpose. The server bounds what
            # it sends (atom limits, HISTORY_LIMIT) and its largest replies are legitimately big:
            # PHONON_ANALYSIS_COMPLETE of a large supercell (every eigenvector on the band path) or
            # a top-k GENERATION_CANDIDATES easily exceed the websockets default of 1 MiB, which
            # would close the session (1009 "message too big") in the middle of a run. The client
            # only talks to a backend its user chose, so a limit would protect against nothing.
            self._ws = ws_connect(self.ws_url, open_timeout=open_timeout, max_size=None,
                                  ping_interval=20, ping_timeout=120)
            if hasattr(self._ws, "pending_legacy_warning"):   # websockets >= 17.1: we close it explicitly
                self._ws.pending_legacy_warning = False
        except Exception as exc:  # noqa: BLE001
            raise StudioConnectionError(f"cannot connect to {self.ws_url}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="latticeflow-reader", daemon=True)
        self._reader.start()
        with self._call_lock:
            self.last_reply = self._wait(None, P.CONNECT_SPEC, timeout=open_timeout)
        return self

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            self._reader.join(timeout=5)
            self._reader = None
        self.http.close()

    def __enter__(self) -> "Studio":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._closed_reason is None

    def _read_loop(self) -> None:
        ws = self._ws
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                    event, payload = str(msg["type"]), msg.get("payload") or {}
                    rid = msg.get("request_id")
                except (ValueError, KeyError, TypeError):
                    continue
                self._track(event, payload)
                if self.on_event is not None:
                    try:
                        self.on_event(event, payload)
                    except Exception:  # noqa: BLE001 - never kill the reader
                        pass
                self._inbox.put((event, payload, rid if isinstance(rid, str) else None))
            self._closed_reason = "connection closed by the server"
        except ConnectionClosed as exc:
            self._closed_reason = f"connection closed: {exc}"
        except Exception as exc:  # noqa: BLE001
            self._closed_reason = f"reader failed: {type(exc).__name__}: {exc}"
        finally:
            self._inbox.put(_CLOSED)

    def _track(self, event: str, payload: Dict[str, Any]) -> None:
        with self._state_lock:
            if event in P.CRYSTAL_EVENTS and isinstance(payload.get("crystal"), dict):
                self.crystal = payload["crystal"]
            elif event == P.HISTORY_STATE:
                self.history = payload
            elif event == P.MODEL_CHANGED:
                self.model = payload
            elif event == P.PHONON_ANALYSIS_COMPLETE:
                self.phonon = payload

    def _send(self, message: Dict[str, Any]) -> None:
        if self._ws is None:
            raise StudioConnectionError("not connected; call connect() first")
        with self._send_lock:
            try:
                self._ws.send(json.dumps(message))
            except ConnectionClosed as exc:
                raise StudioConnectionError(f"connection closed: {exc}") from exc

    # ================================================================ generic command
    def send_command(self, command: str, payload: Optional[Dict[str, Any]] = None, *,
                     spec: Optional[P.CommandSpec] = None, on_frame: Callback = None,
                     on_progress: Callback = None, timeout: Optional[float] = None) -> Reply:
        """Send ``{"type": command, "payload": payload}`` and wait for its terminal sequence.

        ``payload`` keys whose value is ``None`` are dropped (server defaults apply).
        ``on_frame`` gets every intermediate frame payload (relaxation / flow / MD frames),
        ``on_progress`` every ``PROGRESS`` payload of this command. Raises :class:`StudioError`
        on ``ERROR``, :class:`StreamStopped` when the stream is cancelled.
        """
        spec = spec or P.COMMANDS.get(command)
        if spec is None:
            raise ValueError(f"unknown command {command!r}; pass spec=CommandSpec(...) for a new one")
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        with self._call_lock:
            self._drain()
            rid = self._new_request_id()
            message: Dict[str, Any] = {"type": command, "payload": body}
            if rid is not None:
                message["request_id"] = rid
            self._send(message)
            reply = self._wait(command, spec, request_id=rid, on_frame=on_frame, on_progress=on_progress,
                               timeout=self.timeout if timeout is None else timeout)
            self.last_reply = reply
            return reply

    def stop(self) -> None:
        """Cancel the running stream (``STOP_STREAM``); the waiting call raises :class:`StreamStopped`."""
        message: Dict[str, Any] = {"type": "STOP_STREAM", "payload": {}}
        rid = self._new_request_id()
        if rid is not None:
            message["request_id"] = rid   # the stream's STREAM_STOPPED carries the *stream's* id
        self._send(message)

    def _new_request_id(self) -> Optional[str]:
        return uuid.uuid4().hex if self.send_request_ids else None

    def _drain(self) -> None:
        """Drop already-tracked messages left over from earlier commands."""
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                return
            if item is _CLOSED:
                self._inbox.put(_CLOSED)
                return

    def _wait(self, command: Optional[str], spec: P.CommandSpec, *, request_id: Optional[str] = None,
              on_frame: Callback = None, on_progress: Callback = None,
              timeout: Optional[float] = None) -> Reply:
        reply = Reply(command, request_id)
        deadline = None if timeout is None else time.monotonic() + timeout

        def next_item() -> Tuple[str, Dict[str, Any], bool]:
            """Next message that may belong to this command, and whether its request_id says so."""
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    item = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise StudioTimeout(command, timeout or 0.0) from None
                if item is _CLOSED:
                    self._inbox.put(_CLOSED)
                    raise StudioConnectionError(self._closed_reason or "connection closed")
                event, payload, rid = item
                if request_id is not None and rid == request_id:
                    self.server_echoes_request_ids = True
                    return event, payload, True
                if rid is not None:
                    continue      # caused by another (earlier) command
                if request_id is not None and self.server_echoes_request_ids and event != P.ERROR:
                    continue      # unsolicited (e.g. the connect-time load): not ours
                return event, payload, False   # pre-Phase D server: ordering heuristics below

        def fail(exc: StudioError, payload: Dict[str, Any]) -> None:
            # A stream that had changed the structure records "<label> (stopped|failed)" in the undo
            # history right after its STREAM_STOPPED / ERROR: consume that HISTORY_STATE first so
            # `history` is current when the exception reaches the caller. Phase D servers say so
            # (`history_pushed`); for older ones infer it from "this command applied a frame".
            if "history_pushed" in payload:
                pushed = bool(payload["history_pushed"])
            else:
                pushed = applied and any(s.event == P.HISTORY_STATE for s in spec.steps)
            if pushed:
                while True:
                    ev, pl, _ = next_item()
                    if ev == P.HISTORY_STATE:
                        reply.add(ev, pl)
                        break
            raise exc

        step = 0
        applied = False   # this command has sent a structure-carrying frame (pre-Phase D fallback)
        while step < len(spec.steps):
            event, payload, ours = next_item()
            if event == P.ERROR:
                if ours or payload.get("command") in (command, None):
                    fail(StudioError(str(payload.get("code")), str(payload.get("message")),
                                     command=payload.get("command"), reply=reply,
                                     after_terminal=bool(payload.get("after_terminal"))), payload)
                continue
            if event == P.STREAM_STOPPED:
                if spec.streaming and command is not None and (ours or payload.get("stopped") == command):
                    fail(StreamStopped(command, reply), payload)
                continue
            if spec.streaming and event in P.CRYSTAL_EVENTS and event != P.SINGLE_POINT_UPDATE:
                applied = True
            if event == P.PROGRESS:
                if on_progress is not None and (ours or payload.get("command") == command):
                    on_progress(payload)
                continue
            if spec.steps[step].matches(event, payload):
                reply.add(event, payload)
                step += 1
                continue
            if event in spec.frames:
                if on_frame is not None:
                    on_frame(payload)
            elif event in spec.collect:
                reply.add(event, payload)
            # anything else: stale message of an earlier command or an unknown event -> ignore
        return reply

    # ================================================================ structures in / out
    def load(self, preset: Optional[str] = None, *, cif: Optional[str] = None, text: Optional[str] = None,
             text_format: str = "auto", crystal: Optional[Crystal] = None, atoms: Any = None,
             structure: Any = None, mp_id: Optional[str] = None, path: Optional[str] = None) -> Crystal:
        """Make a structure the session's active one (exactly one source).

        ``preset`` ("srtio3", "batio3", "si", "li3ps4"), ``cif`` text, ``text`` (CIF / POSCAR / extxyz,
        ``text_format``), ``crystal`` (a payload dict, e.g. a generator or prototype candidate),
        ``atoms`` (``ase.Atoms``), ``structure`` (pymatgen ``Structure``), ``mp_id`` (fetched with
        ``GET /api/mp/structure``, needs MP_API_KEY on the server) or ``path`` (a local file).
        """
        given = [x is not None for x in (preset, cif, text, crystal, atoms, structure, mp_id, path)]
        if sum(given) != 1:
            raise ValueError("give exactly one of preset, cif, text, crystal, atoms, structure, mp_id, path")
        if atoms is not None:
            text, text_format = ase_to_extxyz(atoms), "extxyz"
        elif structure is not None:
            text, text_format = pymatgen_to_extxyz(structure), "extxyz"
        elif mp_id is not None:
            cif = self.mp_structure(mp_id)["cif"]
        elif path is not None:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        payload: Dict[str, Any] = {"preset_id": preset, "cif": cif, "crystal": crystal}
        if text is not None:
            payload.update(text=text, text_format=text_format)
        return self.send_command("LOAD_STRUCTURE", payload)[P.SINGLE_POINT_UPDATE]["crystal"]

    def to_ase(self, crystal: Optional[Crystal] = None) -> Any:
        """Active (or given) structure as ``ase.Atoms`` (energy / forces / stress attached)."""
        return crystal_to_ase(crystal or self._active())

    def to_pymatgen(self, crystal: Optional[Crystal] = None) -> Any:
        return crystal_to_pymatgen(crystal or self._active())

    def _active(self) -> Crystal:
        if self.crystal is None:
            raise StudioError("NO_ACTIVE_STRUCTURE", "no structure received yet")
        return self.crystal

    # ================================================================ editing (state-changing, instant)
    def perturb(self, atom_index: int, *, new_cart_coords: Optional[Sequence[float]] = None,
                new_element: Optional[str] = None, apply_to_equivalent_sites: bool = False,
                auto_relax: bool = False, on_frame: Callback = None) -> Crystal:
        """Move and/or transmute an atom (``PERTURB_OR_TRANSMUTE_ATOM``); ``auto_relax`` then relaxes."""
        payload = dict(atom_index=atom_index, new_cart_coords=list(new_cart_coords) if new_cart_coords else None,
                       new_element=new_element, apply_to_equivalent_sites=apply_to_equivalent_sites,
                       auto_relax=auto_relax)
        spec = P.AUTO_RELAX_SPEC if auto_relax else None
        self.send_command("PERTURB_OR_TRANSMUTE_ATOM", payload, spec=spec, on_frame=on_frame)
        return self._active()

    def transmute(self, atom_index: int, element: str, *, equivalent_sites: bool = False) -> Crystal:
        return self.perturb(atom_index, new_element=element, apply_to_equivalent_sites=equivalent_sites)

    def delete_atom(self, atom_index: int) -> Crystal:
        """Remove an atom (vacancy)."""
        return self.send_command("DELETE_ATOM", {"atom_index": atom_index})[P.SINGLE_POINT_UPDATE]["crystal"]

    def add_atom(self, element: str, frac_coords: Sequence[float]) -> Crystal:
        """Insert an atom (interstitial); INVALID_OPERATION closer than 0.5 A to another atom."""
        reply = self.send_command("ADD_ATOM", {"element": element, "frac_coords": list(frac_coords)})
        return reply[P.SINGLE_POINT_UPDATE]["crystal"]

    def make_supercell(self, matrix: Union[int, Sequence[int], Sequence[Sequence[int]]]) -> Crystal:
        """``2`` -> 2x2x2, ``[2, 2, 1]`` -> diagonal, or a full 3x3 integer matrix (rows = new vectors)."""
        if isinstance(matrix, int):
            matrix = [matrix] * 3
        m = [list(r) for r in matrix] if isinstance(matrix[0], (list, tuple)) else \
            [[int(matrix[i]) if i == j else 0 for j in range(3)] for i in range(3)]
        return self.send_command("MAKE_SUPERCELL", {"matrix": m})[P.SINGLE_POINT_UPDATE]["crystal"]

    def refine(self, symprec: float = 1e-2, cell: str = "refined") -> Crystal:
        """Snap to the detected space group (``cell``: refined | conventional | primitive)."""
        reply = self.send_command("REFINE_STRUCTURE", {"symprec": symprec, "cell": cell})
        return reply[P.SINGLE_POINT_UPDATE]["crystal"]

    def undo(self) -> Crystal:
        return self.send_command("UNDO")[P.SINGLE_POINT_UPDATE]["crystal"]

    def redo(self) -> Crystal:
        return self.send_command("REDO")[P.SINGLE_POINT_UPDATE]["crystal"]

    def substitute(self, from_element: str, to_element: str, fraction: float, *, n_candidates: int = 12,
                   seed: Optional[int] = None, on_progress: Callback = None) -> Dict[str, Any]:
        """Replace a fraction of one species, keep the lowest-energy configuration -> SUBSTITUTION_RESULT."""
        reply = self.send_command("SUBSTITUTE_FRACTION", dict(from_element=from_element, to_element=to_element,
                                                              fraction=fraction, n_candidates=n_candidates,
                                                              seed=seed), on_progress=on_progress)
        return reply[P.SUBSTITUTION_RESULT]

    # ================================================================ streams
    def relax(self, *, on_frame: Callback = None, timeout: Optional[float] = None, **opts: Any) -> Dict[str, Any]:
        """``START_RELAXATION`` (fmax, max_steps, relax_cell, fix_symmetry, pressure_gpa, stress_tol_gpa,
        optimizer) -> the final RELAXATION_FRAME ``{step, converged, final, crystal}``."""
        return self.send_command("START_RELAXATION", opts, on_frame=on_frame, timeout=timeout)[P.RELAXATION_FRAME]

    def generate(self, composition: Union[str, Dict[str, int], None] = None, *, elements: Optional[List[str]] = None,
                 top_k: int = 1, on_frame: Callback = None, timeout: Optional[float] = None,
                 **opts: Any) -> Dict[str, Any]:
        """Symmetric-candidate search (``START_FLOW_GENERATION``).

        ``composition`` is atoms per cell (``"SrTiO3"`` or ``{"Sr": 1, "Ti": 1, "O": 3}``); without it give
        ``elements`` and ``num_atoms``. Other options: target_spacegroup, target_crystal_system,
        target_bulk_modulus_gpa, steps, physics_guidance_scale, seed.
        Returns ``{"crystal": <final>, "candidates": [GenerationCandidate...], "summary": {...} | None}``;
        ``candidates`` is filled when ``top_k > 1`` (rank 1 is the active structure).
        """
        comp = parse_formula(composition) if isinstance(composition, str) else composition
        if elements is None:
            if not comp:
                raise ValueError("give a composition or elements")
            elements = list(comp)
        payload = dict(opts, elements=elements, composition=comp, top_k=top_k)
        reply = self.send_command("START_FLOW_GENERATION", payload, on_frame=on_frame, timeout=timeout)
        candidates = reply.get(P.GENERATION_CANDIDATES) or {}
        return {"crystal": reply[P.CRYSTAL_STATE_FINAL]["crystal"],
                "candidates": candidates.get("candidates", []), "summary": candidates.get("summary")}

    def phonons(self, *, dos: bool = False, dos_options: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None, **opts: Any) -> Dict[str, Any]:
        """``RUN_PHONON_SHAKE_TEST`` (supercell_dim, displacement_distance, cell_mode, min_supercell_length)
        -> PHONON_ANALYSIS_COMPLETE payload; with ``dos=True`` also ``["dos"]`` (PHONON_DOS_DATA)."""
        result = dict(self.send_command("RUN_PHONON_SHAKE_TEST", opts, timeout=timeout)[P.PHONON_ANALYSIS_COMPLETE])
        if dos:
            result["dos"] = self.phonon_dos(**(dos_options or {}))
        return result

    def phonon_dos(self, **opts: Any) -> Dict[str, Any]:
        """``REQUEST_PHONON_DOS`` (mesh, t_min, t_max, t_step): DOS, projected DOS, harmonic F/S/Cv, ZPE."""
        return self.send_command("REQUEST_PHONON_DOS", opts)[P.PHONON_DOS_DATA]

    def eigenmode(self, q_index: int, band_index: int) -> Dict[str, Any]:
        return self.send_command("REQUEST_EIGENMODE", {"q_index": q_index, "band_index": band_index})[
            P.EIGENMODE_DATA]["eigenmode"]

    def phonon_convergence(self, supercell_dim: Optional[Sequence[int]] = None,
                           on_progress: Callback = None) -> Dict[str, Any]:
        """Rerun in a larger supercell -> ``{"convergence": PHONON_CONVERGENCE_RESULT, "phonons": ...}``."""
        reply = self.send_command("CHECK_PHONON_CONVERGENCE",
                                  {"supercell_dim": list(supercell_dim) if supercell_dim else None},
                                  on_progress=on_progress)
        return {"convergence": reply[P.PHONON_CONVERGENCE_RESULT], "phonons": reply[P.PHONON_ANALYSIS_COMPLETE]}

    def export_phonopy(self, path: Optional[str] = None) -> Dict[str, Any]:
        """``EXPORT_PHONON_DATA`` -> ``{filename, phonopy_yaml}`` (written to ``path`` if given)."""
        data = self.send_command("EXPORT_PHONON_DATA")[P.PHONON_EXPORT]
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data["phonopy_yaml"])
        return data

    def soft_mode(self, label: Optional[str] = None) -> Tuple[int, int, float]:
        """``(q_index, band_index, frequency_thz)`` of the softest mode of the last phonon analysis,
        overall or at the labelled q-point ``label`` (e.g. ``"R"``, ``"Gamma"``)."""
        ph = self.phonon
        if ph is None:
            raise StudioError("NO_PHONON_ANALYSIS", "run phonons() first")
        if label is None:
            freqs = ph["frequencies_thz"]
            q = min(range(len(freqs)), key=lambda i: freqs[i][0])
            return q, 0, float(freqs[q][0])
        want = _norm_label(label)
        modes = [m for m in ph["eigenmodes"] if _norm_label(m.get("q_label") or "") == want]
        if not modes:
            labels = sorted({m.get("q_label") for m in ph["eigenmodes"]})
            raise StudioError("INDEX_OUT_OF_RANGE", f"no labelled q-point {label!r}; have {labels}")
        m = min(modes, key=lambda m: m["frequency_thz"])
        return int(m["q_index"]), int(m["band_index"]), float(m["frequency_thz"])

    def follow_instability(self, q_index: Optional[int] = None, band_index: Optional[int] = None,
                           amplitude: float = 0.1, *, label: Optional[str] = None, on_frame: Callback = None,
                           timeout: Optional[float] = None, **opts: Any) -> Dict[str, Any]:
        """Displace along an imaginary mode in its commensurate supercell and relax -> final RELAXATION_FRAME.

        Without ``q_index`` the softest mode (at ``label`` if given) of the last analysis is followed.
        """
        if q_index is None:
            q_index, band, _ = self.soft_mode(label)
            band_index = band if band_index is None else band_index
        payload = dict(opts, q_index=q_index, band_index=band_index or 0, amplitude=amplitude)
        return self.send_command("FOLLOW_INSTABILITY", payload, on_frame=on_frame, timeout=timeout)[P.RELAXATION_FRAME]

    def md(self, *, on_frame: Callback = None, timeout: Optional[float] = None, **opts: Any) -> Dict[str, Any]:
        """``START_MD`` (temperature_k, ensemble NVE|NVT|NPT, timestep_fs, n_steps, friction_per_fs,
        pressure_gpa, frame_interval, seed) -> MD_COMPLETE (MSD, diffusion coefficients, final crystal).
        ``on_frame`` receives every MD_FRAME; a :meth:`stop` raises :class:`StreamStopped`."""
        return self.send_command("START_MD", opts, on_frame=on_frame, timeout=timeout)[P.MD_COMPLETE]

    def bulk_modulus(self, **opts: Any) -> Dict[str, Any]:
        """``COMPUTE_BULK_MODULUS`` (strain_window, n_points) -> BULK_MODULUS_RESULT (clamped-ion E(V) fit)."""
        return self.send_command("COMPUTE_BULK_MODULUS", opts)[P.BULK_MODULUS_RESULT]

    def elastic_tensor(self, *, on_progress: Callback = None, timeout: Optional[float] = None,
                       **opts: Any) -> Dict[str, Any]:
        """``COMPUTE_ELASTIC_TENSOR`` (max_strain, n_strains, relax_ions, fmax) -> ELASTIC_TENSOR_RESULT."""
        return self.send_command("COMPUTE_ELASTIC_TENSOR", opts, on_progress=on_progress,
                                 timeout=timeout)[P.ELASTIC_TENSOR_RESULT]

    def set_model(self, model_id: str, dispersion: bool = False, *, on_progress: Callback = None) -> Dict[str, Any]:
        """Switch this session's force field (ids from :meth:`models`) -> MODEL_CHANGED."""
        return self.send_command("SET_MODEL", {"model_id": model_id, "dispersion": dispersion},
                                 on_progress=on_progress)[P.MODEL_CHANGED]

    def committee(self, models: Sequence[str], *, relax: bool = True, dispersion: bool = False,
                  on_progress: Callback = None) -> Dict[str, Any]:
        """Evaluate the active structure with 2-4 models -> COMMITTEE_RESULT (entries + spreads)."""
        return self.send_command("COMMITTEE_EVALUATE", {"model_ids": list(models), "relax": relax,
                                                        "dispersion": dispersion},
                                 on_progress=on_progress)[P.COMMITTEE_RESULT]

    def local_analysis(self, include_partial_rdf: bool = True) -> Dict[str, Any]:
        """Coordination, polyhedra, bond valence, octahedral tilts (Glazer) -> LOCAL_ANALYSIS_DATA."""
        return self.send_command("REQUEST_LOCAL_ANALYSIS",
                                 {"include_partial_rdf": include_partial_rdf})[P.LOCAL_ANALYSIS_DATA]

    def formation_energy(self, bulk_position: int, *, on_progress: Callback = None) -> Dict[str, Any]:
        """Neutral defect formation energy of the active cell vs history entry ``bulk_position``."""
        return self.send_command("COMPUTE_FORMATION_ENERGY", {"bulk_position": bulk_position},
                                 on_progress=on_progress)[P.FORMATION_ENERGY_RESULT]

    def history_entry(self, position: int) -> Dict[str, Any]:
        """``REQUEST_HISTORY_ENTRY``: the stored structure of undo-history entry ``position`` (see
        ``history["entries"]``) without changing the session -> ``{position, label, crystal}``."""
        return self.send_command("REQUEST_HISTORY_ENTRY", {"position": int(position)})[P.HISTORY_ENTRY]

    def prototype_search(self, composition: Union[str, Dict[str, int]], *, on_progress: Callback = None,
                         timeout: Optional[float] = None, **opts: Any) -> Dict[str, Any]:
        """Ionic substitution into MP prototypes (max_candidates, probability_threshold, relax, with_e_hull)
        -> PROTOTYPE_CANDIDATES. Load one with ``load(crystal=result["candidates"][0]["crystal"])``."""
        comp = parse_formula(composition) if isinstance(composition, str) else dict(composition)
        return self.send_command("PROTOTYPE_SEARCH", dict(opts, composition=comp), on_progress=on_progress,
                                 timeout=timeout)[P.PROTOTYPE_CANDIDATES]

    # ================================================================ REST
    def _rest(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise StudioConnectionError(f"{method} {path}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
                code, message = body.get("code", "HTTP_ERROR"), body.get("message", resp.text)
            except ValueError:
                code, message = "HTTP_ERROR", resp.text
            raise StudioError(str(code), str(message), status=resp.status_code, command=f"{method} {path}")
        return resp

    def _crystal_arg(self, crystal: Optional[Crystal]) -> Crystal:
        return crystal if crystal is not None else self._active()

    def _model_fields(self) -> Dict[str, Any]:
        if self.model is None:
            return {}
        return {"model_id": self.model["model_id"], "dispersion": bool(self.model.get("dispersion"))}

    def health(self) -> Dict[str, Any]:
        return self._rest("GET", "/api/health").json()

    def presets(self) -> List[Dict[str, Any]]:
        return self._rest("GET", "/api/presets").json()

    def models(self) -> Dict[str, Any]:
        """``GET /api/models`` -> ``{models: [...], default_model_id}``."""
        return self._rest("GET", "/api/models").json()

    def hull(self, crystal: Optional[Crystal] = None, *, relax: bool = True) -> Dict[str, Any]:
        """``POST /api/hull/evaluate`` of the active (or given) structure with the session's model.
        Needs MP_API_KEY on the server (``StudioError`` HULL_UNAVAILABLE otherwise)."""
        body = dict(crystal=self._crystal_arg(crystal), relax=relax, **self._model_fields())
        return self._rest("POST", "/api/hull/evaluate", json=body).json()

    def diffraction(self, crystal: Optional[Crystal] = None, **opts: Any) -> Dict[str, Any]:
        """``POST /api/diffraction`` (kind xray|neutron, radiation, wavelength_angstrom, two_theta_min/max)."""
        return self._rest("POST", "/api/diffraction", json=dict(opts, crystal=self._crystal_arg(crystal))).json()

    def mp_search(self, query: str) -> List[Dict[str, Any]]:
        """``GET /api/mp/search?q=`` (formula, chemsys "Li-Fe-O" or mp-id) -> list of MPEntry."""
        return self._rest("GET", "/api/mp/search", params={"q": query}).json()["results"]

    def mp_structure(self, material_id: str) -> Dict[str, Any]:
        """``GET /api/mp/structure/{id}`` -> ``{entry, cif}`` (``load(mp_id=...)`` does both steps)."""
        return self._rest("GET", f"/api/mp/structure/{material_id}").json()

    def mp_match(self, crystal: Optional[Crystal] = None) -> Dict[str, Any]:
        """``POST /api/mp/match``: Materials Project entries matching the structure."""
        return self._rest("POST", "/api/mp/match", json={"crystal": self._crystal_arg(crystal)}).json()

    def metallicity(self, crystal: Optional[Crystal] = None, use_mp: bool = True) -> Dict[str, Any]:
        """``POST /api/superconductivity/metallicity``: the superconductivity pre-screen's metallicity gate
        (``verdict``, ``source``, ``band_gap_ev``, ``mp_id``, ``reason``). Uses the Materials Project gap of the
        best structure match when the server has an MP key, else a composition heuristic."""
        body = {"crystal": self._crystal_arg(crystal), "use_mp": bool(use_mp)}
        return self._rest("POST", "/api/superconductivity/metallicity", json=body).json()

    def compare(self, a: Union[Crystal, int], b: Union[Crystal, int, None] = None) -> Dict[str, Any]:
        """``POST /api/compare`` of two structures, each a payload or an undo-history position
        (fetched with :meth:`history_entry`); ``b`` defaults to the active structure."""
        a = self.history_entry(a)["crystal"] if isinstance(a, int) else a
        b = self.history_entry(b)["crystal"] if isinstance(b, int) else self._crystal_arg(b)
        return self._rest("POST", "/api/compare", json={"crystal_a": a, "crystal_b": b}).json()

    def ordering(self, cif: str, **opts: Any) -> Dict[str, Any]:
        """``POST /api/ordering``: ordered approximants of a disordered CIF, ranked by energy."""
        return self._rest("POST", "/api/ordering", json=dict(self._model_fields(), **opts, cif=cif)).json()

    def import_cif(self, cif: str) -> Crystal:
        """``POST /api/import/cif``: stateless preview (does not change the session)."""
        return self._rest("POST", "/api/import/cif", json={"cif": cif}).json()

    def export(self, fmt: str = "cif", crystal: Optional[Crystal] = None, *, path: Optional[str] = None,
               **opts: Any) -> str:
        """``POST /api/export/cif`` (fmt cif|poscar|extxyz; symmetrize, symprec, cell) -> file text."""
        text = self._rest("POST", "/api/export/cif",
                          json=dict(opts, crystal=self._crystal_arg(crystal), format=fmt)).text
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return text

    # ================================================================ project library (REST, protocol §6)
    # Shared by every session and the job queue; nothing here changes the session
    # (except load_structure). Ids are ints; dicts follow docs/protocol.md §6.
    def projects(self) -> List[Dict[str, Any]]:
        """``GET /api/projects`` -> ``Project[]`` (the default project "Scratch" first)."""
        return self._rest("GET", "/api/projects").json()

    def create_project(self, name: str) -> Dict[str, Any]:
        """``POST /api/projects`` -> the new ``Project`` (names need not be unique)."""
        return self._rest("POST", "/api/projects", json={"name": name}).json()

    def delete_project(self, project_id: int) -> None:
        """``DELETE /api/projects/{id}`` with its structures, results and finished jobs.
        ``StudioError`` INVALID_OPERATION (409) for the default project or while it has active jobs."""
        self._rest("DELETE", f"/api/projects/{int(project_id)}")

    def save_structure(self, project_id: int, crystal: Optional[Crystal] = None, *, atoms: Any = None,
                       structure: Any = None, name: Optional[str] = None, parent_id: Optional[int] = None,
                       origin: Optional[str] = None, notes: Optional[str] = None) -> Dict[str, Any]:
        """``POST /api/projects/{id}/structures`` -> ``Structure``.

        Saves the session's active structure, or ``crystal`` (a payload dict), ``atoms`` (``ase.Atoms``) or
        ``structure`` (pymatgen ``Structure``); objects are converted with the stateless ``POST
        /api/import/cif``, so the session is not touched. ``name`` defaults to the formula, ``origin`` to
        ``"import"``. If the project already holds the same structure, that one is returned with
        ``existing: True`` and nothing changes.
        """
        if sum(x is not None for x in (crystal, atoms, structure)) > 1:
            raise ValueError("give at most one of crystal, atoms, structure")
        if atoms is not None:
            crystal = self.import_cif(ase_to_cif(atoms))
        elif structure is not None:
            crystal = self.import_cif(pymatgen_to_cif(structure))
        body = {"crystal": self._crystal_arg(crystal), "name": name, "parent_id": parent_id, "origin": origin,
                "notes": notes}
        return self._rest("POST", f"/api/projects/{int(project_id)}/structures",
                          json={k: v for k, v in body.items() if v is not None}).json()

    def structures(self, project_id: int) -> List[Dict[str, Any]]:
        """``GET /api/projects/{id}/structures`` -> ``StructureListRow[]`` (no ``crystal``). Each row's
        ``latest`` merges the newest summary of every result kind (``energy_per_atom_ev``,
        ``min_frequency_thz``, ``k_vrh_gpa``...); ``latest_result_ids`` maps kind -> result id."""
        return self._rest("GET", f"/api/projects/{int(project_id)}/structures").json()

    def structure(self, structure_id: int) -> Dict[str, Any]:
        """``GET /api/structures/{id}`` -> ``Structure`` (with ``crystal``)."""
        return self._rest("GET", f"/api/structures/{int(structure_id)}").json()

    def rename_structure(self, structure_id: int, name: Optional[str] = None, *,
                         notes: Optional[str] = None) -> Dict[str, Any]:
        """``PATCH /api/structures/{id}`` (``name`` and / or ``notes``) -> ``Structure``."""
        body = {k: v for k, v in (("name", name), ("notes", notes)) if v is not None}
        return self._rest("PATCH", f"/api/structures/{int(structure_id)}", json=body).json()

    def delete_structure(self, structure_id: int) -> None:
        """``DELETE /api/structures/{id}`` with its results (children keep existing, ``parent_id: None``)."""
        self._rest("DELETE", f"/api/structures/{int(structure_id)}")

    def load_structure(self, structure_id: int) -> Crystal:
        """Make a saved structure the session's active one (``LOAD_STRUCTURE``) -> the new active crystal."""
        return self.load(crystal=self.structure(structure_id)["crystal"])

    def results(self, structure_id: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """``GET /api/structures/{id}/results`` -> ``ResultRow[]``, newest first (``summary``, no ``data``)."""
        params = {"kind": kind} if kind is not None else None
        return self._rest("GET", f"/api/structures/{int(structure_id)}/results", params=params).json()

    def result(self, result_id: int) -> Dict[str, Any]:
        """``GET /api/results/{id}`` -> ``Result``; ``data`` is the payload of the matching event
        (final RELAXATION_FRAME, PHONON_ANALYSIS_COMPLETE, the hull response...)."""
        return self._rest("GET", f"/api/results/{int(result_id)}").json()

    def export_project(self, project_id: int, path: str) -> Dict[str, Any]:
        """``GET /api/projects/{id}/export``: write the self-contained bundle (JSON) to ``path``; returns it."""
        bundle = self._rest("GET", f"/api/projects/{int(project_id)}/export").json()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh)
        return bundle

    def import_project(self, path: str, name: Optional[str] = None) -> Dict[str, Any]:
        """``POST /api/projects/import`` of a bundle file -> the **new** ``Project`` (``name`` overrides
        the bundle's)."""
        with open(path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        params = {"name": name} if name is not None else None
        return self._rest("POST", "/api/projects/import", json=bundle, params=params).json()

    # ================================================================ job queue (REST, protocol §7)
    def submit_jobs(self, project_id: int, structure_ids: Union[int, Sequence[int]],
                    steps: Sequence[Union[str, P.JobStep]], *, model_id: Optional[str] = None,
                    dispersion: Optional[bool] = None) -> List[Dict[str, Any]]:
        """``POST /api/jobs``: one chain per structure -> every created ``Job``, chain by chain in step order.

        ``steps`` are step dicts (``{"kind", "settings", "only_if_unstable"}``, see :func:`job_step`) or bare
        kinds (``"relax"``, ``"phonons"``, ``"elastic"``, ``"eos"``, ``"hull"``, ``"follow"``,
        ``"superconductivity"``); omitted settings take the UI defaults. ``follow`` and ``superconductivity``
        need a ``phonons`` step earlier in the chain (no relax / follow in between). Without ``model_id`` the session's model (:meth:`set_model`) is used,
        else the server default. Each later step runs on the output of the last relax / follow before it.
        """
        ids = [int(structure_ids)] if isinstance(structure_ids, int) else [int(s) for s in structure_ids]
        body: Dict[str, Any] = {"project_id": int(project_id), "structure_ids": ids,
                                "steps": [{"kind": s} if isinstance(s, str) else dict(s) for s in steps]}
        model = self._model_fields() if model_id is None else {"model_id": model_id}
        if dispersion is not None:
            model["dispersion"] = bool(dispersion)
        body.update(model)
        return self._rest("POST", "/api/jobs", json=body).json()

    def jobs(self, project_id: Optional[int] = None, *, active: Optional[bool] = None) -> List[Dict[str, Any]]:
        """``GET /api/jobs`` -> ``Job[]`` in queue order (``active=True``: queued + running, ``False``:
        finished). A job paused for interactive work says so in its ``message``."""
        params = {k: v for k, v in (("project_id", project_id),
                                    ("active", None if active is None else str(bool(active)).lower()))
                  if v is not None}
        return self._rest("GET", "/api/jobs", params=params).json()["jobs"]

    def job(self, job_id: int) -> Dict[str, Any]:
        """``GET /api/jobs/{id}`` -> ``Job``."""
        return self._rest("GET", f"/api/jobs/{int(job_id)}").json()

    def cancel_job(self, job_id: int) -> Dict[str, Any]:
        """``POST /api/jobs/{id}/cancel`` -> the ``Job`` (a running one says ``"cancelling"`` until its next
        checkpoint; the rest of its chain is cancelled). INVALID_OPERATION (409) if already finished."""
        return self._rest("POST", f"/api/jobs/{int(job_id)}/cancel").json()

    def wait_jobs(self, jobs: Union[int, str, Dict[str, Any], Iterable[Union[int, str, Dict[str, Any]]]],
                  poll: float = 1.5, on_progress: Callback = None, *,
                  timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """Block until every given job is finished (done / failed / cancelled / skipped); returns them.

        ``jobs``: job ids, ``Job`` dicts (e.g. what :meth:`submit_jobs` returned), chain ids (32-hex
        strings: every job of the chain), or a mix. ``on_progress`` gets each ``Job`` whose status,
        progress or message changed since the last poll. Raises :class:`StudioTimeout` after ``timeout``
        seconds (the jobs keep running) and ``StudioError`` NOT_FOUND for an unknown job or chain.
        """
        items = [jobs] if isinstance(jobs, (int, str, dict)) else list(jobs)
        ids: List[int] = []
        chains: List[str] = []
        for item in items:
            if isinstance(item, str):
                chains.append(item)
            else:
                ids.append(int(item["id"]) if isinstance(item, dict) else int(item))
        deadline = None if timeout is None else time.monotonic() + timeout
        seen: Dict[int, Tuple[Any, Any, Any]] = {}
        while True:
            listed = self.jobs()
            by_id = {j["id"]: j for j in listed}
            current = [by_id[i] if i in by_id else self.job(i) for i in ids]   # job(): 404 for unknown ids
            for chain in chains:
                members = sorted((j for j in listed if j["chain_id"] == chain), key=lambda j: j["step_index"])
                if not members:
                    raise StudioError("NOT_FOUND", f"no jobs in chain {chain}", command="wait_jobs")
                current += [j for j in members if j["id"] not in ids]
            for j in current:
                state = (j["status"], j.get("progress"), j.get("message"))
                if seen.get(j["id"]) != state:
                    seen[j["id"]] = state
                    if on_progress is not None:
                        on_progress(j)
            if all(j["status"] in P.JOB_FINISHED for j in current):
                return current
            if deadline is not None and time.monotonic() >= deadline:
                raise StudioTimeout("wait_jobs", timeout or 0.0)
            time.sleep(poll if deadline is None else max(0.0, min(poll, deadline - time.monotonic())))


def _norm_label(label: str) -> str:
    s = label.replace("\\", "").replace("$", "").strip().lower()
    return {"γ": "gamma", "g": "gamma"}.get(s, s)


def connect(url: str = "http://localhost:8000", **kw: Any) -> Studio:
    """``latticeflow_client.connect()`` -> a connected :class:`Studio`."""
    return Studio(url, **kw)
