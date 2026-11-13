import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StudioClient, backoffDelay, dispatchServerMessage, parseServerMessage, type WebSocketLike } from "../src/services/wsClient";
import { createCrystalStore, type CrystalStore } from "../src/store/useCrystalStore";
import type { ClientCommand, ServerMessage } from "../src/types/protocol";
import { error, flowFinal, flowFrame, makeCrystal, makeMode, makePhonon, phononDone, relaxFrame, spu, stopped } from "./fixtures";

class FakeSocket implements WebSocketLike {
  readyState = 0;
  sent: { type: ClientCommand; payload: unknown }[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  constructor(readonly url: string) {}
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
    this.onclose?.({});
  }
  // test helpers
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  deliver(msg: ServerMessage) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  drop() {
    this.readyState = 3;
    this.onclose?.({});
  }
}

function connected() {
  const store = createCrystalStore();
  const sockets: FakeSocket[] = [];
  const client = new StudioClient({
    url: "ws://test/ws/studio",
    store,
    createSocket: (u) => {
      const s = new FakeSocket(u);
      sockets.push(s);
      return s;
    },
    random: () => 1,
  });
  client.connect();
  sockets[0]!.open();
  const sock = () => sockets[sockets.length - 1]!;
  const deliver = (...msgs: ServerMessage[]) => {
    for (const m of msgs) sock().deliver(m);
    store.getState().flushHud();
  };
  return { store, client, sockets, sock, deliver };
}

const streamCommands = (store: CrystalStore) => store.getState().streams.map((s) => s.command);

describe("parseServerMessage", () => {
  it("accepts known envelopes and rejects junk", () => {
    expect(parseServerMessage(JSON.stringify(spu(makeCrystal(2))))?.type).toBe("SINGLE_POINT_UPDATE");
    expect(parseServerMessage("not json")).toBeNull();
    expect(parseServerMessage(JSON.stringify({ type: "NOPE", payload: {} }))).toBeNull();
    expect(parseServerMessage(JSON.stringify({ type: "ERROR" }))).toBeNull();
  });
});

describe("connection + auto-loaded structure", () => {
  it("opens, then ingests the auto-loaded SrTiO3 single point", () => {
    const { store, deliver } = connected();
    expect(store.getState().connection).toBe("open");
    deliver(spu(makeCrystal(5)));
    const s = store.getState();
    expect(s.crystal?.formula).toBe("SrTiO3");
    expect(s.frame.n).toBe(5);
    expect(s.frame.positions).toBeInstanceOf(Float32Array);
    expect(s.topologyVersion).toBe(1);
  });

  it("reconnects with exponential backoff and resets the session", () => {
    vi.useFakeTimers();
    try {
      const { store, sockets, client } = connected();
      client.send("START_RELAXATION", {});
      expect(store.getState().streams).toHaveLength(1);
      sockets[0]!.drop();
      expect(store.getState().connection).toBe("closed");
      expect(store.getState().streams).toHaveLength(0);
      expect(sockets).toHaveLength(1);
      vi.advanceTimersByTime(backoffDelay(0, 500, 10_000, () => 1));
      expect(sockets).toHaveLength(2);
      expect(store.getState().connection).toBe("connecting");
      sockets[1]!.drop(); // fails again: the delay doubles
      vi.advanceTimersByTime(999);
      expect(sockets).toHaveLength(2);
      vi.advanceTimersByTime(1);
      expect(sockets).toHaveLength(3);
      sockets[2]!.open();
      expect(store.getState().connection).toBe("open");
      client.disconnect();
    } finally {
      vi.useRealTimers();
    }
  });

  it("backoff grows exponentially and is capped", () => {
    const one = () => 1;
    expect([0, 1, 2, 3, 10].map((a) => backoffDelay(a, 500, 10_000, one))).toEqual([500, 1000, 2000, 4000, 10_000]);
    expect(backoffDelay(3, 500, 10_000, () => 0)).toBe(2000); // jitter floor = 50 %
  });

  it("refuses to send while the socket is not open", () => {
    const store = createCrystalStore();
    const client = new StudioClient({ store, createSocket: (u) => new FakeSocket(u) });
    client.connect();
    expect(client.send("START_RELAXATION", {})).toBe(false);
    expect(store.getState().streams).toHaveLength(0);
  });
});

describe("phonon invalidation rules", () => {
  function withPhonon() {
    const ctx = connected();
    ctx.deliver(spu(makeCrystal(5)));
    ctx.client.send("RUN_PHONON_SHAKE_TEST", {});
    ctx.deliver(phononDone(makePhonon(5)), spu(makeCrystal(5, { actors: [2, 3] })));
    return ctx;
  }

  it("keeps the analysis through the flagged SINGLE_POINT_UPDATE that follows PHONON_ANALYSIS_COMPLETE", () => {
    const { store } = withPhonon();
    const s = store.getState();
    expect(s.phonon).not.toBeNull();
    expect(s.selectedEigenmode?.frequency_thz).toBe(-2.5); // softest imaginary mode auto-selected
    expect(s.toolMode).toBe("PHONON_SHAKE");
    expect(s.streams).toHaveLength(0);
    expect(s.crystalSource).toBe("phonon_flags");
    expect(Array.from(s.frame.unstable)).toEqual([0, 0, 1, 1, 0]);
    expect(s.crystal?.bonds[1]?.is_unstable).toBe(true);
  });

  it("clears it on the next SINGLE_POINT_UPDATE (perturb / delete / load)", () => {
    const { store, deliver } = withPhonon();
    deliver(spu(makeCrystal(5, { move: { index: 1, cart: [1, 1, 1] } })));
    const s = store.getState();
    expect(s.phonon).toBeNull();
    expect(s.selectedEigenmode).toBeNull();
    expect(s.toolMode).toBe("DRAG_ATOM");
    expect(s.frame.phonon.active).toBe(false);
  });

  it.each([
    ["RELAXATION_FRAME", relaxFrame(0, makeCrystal(5))],
    ["FLOW_TRAJECTORY_FRAME", flowFrame(1, 10, makeCrystal(5))],
    ["CRYSTAL_STATE_FINAL", flowFinal(makeCrystal(5))],
  ])("clears it on %s", (_name, msg) => {
    const { store, deliver } = withPhonon();
    deliver(msg);
    expect(store.getState().phonon).toBeNull();
  });

  it("a second SINGLE_POINT_UPDATE after the flagged one is treated as a state change", () => {
    const { store, deliver } = withPhonon();
    deliver(spu(makeCrystal(5)), spu(makeCrystal(5)));
    expect(store.getState().phonon).toBeNull();
  });

  it("does not clear on STREAM_STOPPED or EIGENMODE_DATA, and EIGENMODE_DATA selects the mode", () => {
    const { store, deliver } = withPhonon();
    deliver(stopped(null));
    const mode = makeMode(5, { q_index: 7, band_index: 4, q_label: null, frequency_thz: 1.7, is_imaginary: false });
    deliver({ type: "EIGENMODE_DATA", payload: { eigenmode: mode } });
    const s = store.getState();
    expect(s.phonon).not.toBeNull();
    expect(s.selectedEigenmode?.q_index).toBe(7);
  });

  it("picks the softest optical mode for a stable structure", () => {
    const { store, deliver, client } = connected();
    deliver(spu(makeCrystal(2, { elements: ["Si", "Si"], formula: "Si" })));
    client.send("RUN_PHONON_SHAKE_TEST", {});
    deliver(phononDone(makePhonon(2, true)), spu(makeCrystal(2, { elements: ["Si", "Si"], formula: "Si" })));
    const s = store.getState();
    expect(s.phonon?.is_dynamically_stable).toBe(true);
    expect(s.selectedEigenmode?.frequency_thz).toBe(4.2);
  });
});

describe("stream lifecycle", () => {
  it("flow: progress from frames, CRYSTAL_STATE_FINAL ends it", () => {
    const { store, client, deliver } = connected();
    deliver(spu(makeCrystal(5)));
    client.send("START_FLOW_GENERATION", { elements: ["Sr", "Ti", "O"], num_atoms: 5 });
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
    for (let k = 1; k <= 4; k++) deliver(flowFrame(k, 4, makeCrystal(5)));
    const head = store.getState().streams[0]!;
    expect(head.progress.step).toBe(4);
    expect(head.progress.totalSteps).toBe(4);
    expect(store.getState().frame.trails.count).toBe(4);
    const final = makeCrystal(5);
    final.bulk_modulus_gpa = 174.2;
    deliver(flowFinal(final));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().crystal?.bulk_modulus_gpa).toBe(174.2);
    expect(store.getState().crystalSource).toBe("flow_final");
  });

  it("relaxation: history per stream, RELAXATION_FRAME.final ends it", () => {
    const { store, client, deliver } = connected();
    deliver(spu(makeCrystal(5)));
    client.send("START_RELAXATION", { fmax: 0.02 });
    deliver(relaxFrame(0, makeCrystal(5, { maxForce: 0.5, energy: -7.7 })), relaxFrame(1, makeCrystal(5, { maxForce: 0.1, energy: -7.8 })));
    let s = store.getState();
    expect(s.streams[0]?.kind).toBe("relaxation");
    expect(s.streams[0]?.progress.maxForce).toBe(0.1);
    expect(s.relaxHistory.map((p) => p.maxForce)).toEqual([0.5, 0.1]);
    deliver(relaxFrame(2, makeCrystal(5, { maxForce: 0.01, energy: -7.81 }), true, true));
    s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.relaxHistory).toHaveLength(3);
    expect(s.toasts).toHaveLength(0); // converged: no "ended without converging" toast
    // a new relaxation starts a new history
    client.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)));
    expect(store.getState().relaxHistory).toHaveLength(1);
  });

  it("restarting the same stream: STREAM_STOPPED for the old one keeps the new one active", () => {
    const { store, client, deliver } = connected();
    client.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)));
    client.send("START_RELAXATION", { fmax: 0.01 });
    expect(streamCommands(store)).toEqual(["START_RELAXATION", "START_RELAXATION"]);
    const second = store.getState().streams[1]!.id;
    deliver(stopped("START_RELAXATION"));
    expect(store.getState().streams.map((s) => s.id)).toEqual([second]);
    deliver(relaxFrame(0, makeCrystal(5)), relaxFrame(1, makeCrystal(5), true));
    expect(store.getState().streams).toHaveLength(0);
  });

  it("the old stream may finish on its own before the new one starts (no STREAM_STOPPED)", () => {
    const { store, client, deliver } = connected();
    client.send("START_RELAXATION", {});
    client.send("START_FLOW_GENERATION", { elements: ["Si"] });
    deliver(relaxFrame(3, makeCrystal(5), true, true));
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
    deliver(flowFrame(1, 2, makeCrystal(5)), flowFrame(2, 2, makeCrystal(5)), flowFinal(makeCrystal(5)));
    expect(store.getState().streams).toHaveLength(0);
  });

  it("an instant state-changing command cancels the stream (STREAM_STOPPED then SINGLE_POINT_UPDATE)", () => {
    const { store, client, deliver } = connected();
    client.send("START_FLOW_GENERATION", { elements: ["Si"] });
    deliver(flowFrame(1, 60, makeCrystal(8)));
    client.send("DELETE_ATOM", { atom_index: 0 });
    deliver(stopped("START_FLOW_GENERATION"), spu(makeCrystal(7)));
    const s = store.getState();
    expect(s.streams).toHaveLength(0);
    expect(s.frame.n).toBe(7);
    expect(s.crystalSource).toBe("single_point");
  });

  it("STOP_STREAM marks the stream stopping; STREAM_STOPPED ends it", () => {
    const { store, client, deliver } = connected();
    client.send("RUN_PHONON_SHAKE_TEST", {});
    client.send("STOP_STREAM", {});
    expect(store.getState().streams[0]?.stopping).toBe(true);
    deliver(stopped("RUN_PHONON_SHAKE_TEST"));
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().phonon).toBeNull();
  });

  it("STREAM_STOPPED {stopped: null} drops only streams sent before the STOP", () => {
    const { store, client, deliver } = connected();
    client.send("START_RELAXATION", {});
    client.send("STOP_STREAM", {});
    client.send("START_FLOW_GENERATION", { elements: ["Si"] });
    // the relaxation had already finished silently (lost) and STOP found nothing running
    deliver(stopped(null));
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
  });

  it("a refused streaming command removes only itself (the running stream continues)", () => {
    const { store, client, deliver } = connected();
    client.send("START_RELAXATION", {});
    deliver(relaxFrame(0, makeCrystal(5)));
    client.send("RUN_PHONON_SHAKE_TEST", {});
    deliver(error("STRUCTURE_TOO_LARGE", "RUN_PHONON_SHAKE_TEST", "too many atoms"));
    const s = store.getState();
    expect(streamCommands(store)).toEqual(["START_RELAXATION"]);
    expect(s.toasts.at(-1)).toMatchObject({ level: "error", title: "Structure too large for phonons" });
    expect(s.toasts.at(-1)?.message).toContain("too many atoms");
    expect(s.lastError?.code).toBe("STRUCTURE_TOO_LARGE");
  });

  it("ERROR ends a failed flow", () => {
    const { store, client, deliver } = connected();
    client.send("START_FLOW_GENERATION", { elements: ["Xx"] });
    deliver(error("GENERATION_FAILED", "START_FLOW_GENERATION"));
    expect(store.getState().streams).toHaveLength(0);
  });

  it("RELAXATION_DIVERGED arrives after the final frame and must not pop the next stream", () => {
    const { store, client, deliver } = connected();
    client.send("START_RELAXATION", {});
    client.send("START_RELAXATION", {});
    deliver(stopped("START_RELAXATION"));
    deliver(relaxFrame(5, makeCrystal(5), true, false), error("RELAXATION_DIVERGED", "START_RELAXATION"));
    expect(store.getState().streams).toHaveLength(0);
    client.send("START_RELAXATION", {});
    deliver(relaxFrame(2, makeCrystal(5), true, false), error("RELAXATION_DIVERGED", "START_RELAXATION"));
    client.send("START_FLOW_GENERATION", { elements: ["Si"] });
    deliver(error("RELAXATION_DIVERGED", "START_RELAXATION"));
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
  });

  it("errors from instant commands do not touch the streams", () => {
    const { store, client, deliver } = connected();
    client.send("START_FLOW_GENERATION", { elements: ["Si"] });
    deliver(error("INDEX_OUT_OF_RANGE", "DELETE_ATOM"), error("INVALID_MESSAGE", null));
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
    expect(store.getState().toasts).toHaveLength(2);
  });

  it("PERTURB with auto_relax is a stream: SINGLE_POINT_UPDATE, then relaxation frames", () => {
    const { store, client, deliver } = connected();
    deliver(spu(makeCrystal(5)));
    client.send("PERTURB_OR_TRANSMUTE_ATOM", { atom_index: 1, new_element: "Na", auto_relax: true });
    client.send("PERTURB_OR_TRANSMUTE_ATOM", { atom_index: 1, new_cart_coords: [1, 1, 1] }); // plain drag: not a stream
    expect(streamCommands(store)).toEqual(["PERTURB_OR_TRANSMUTE_ATOM"]);
    deliver(spu(makeCrystal(5)), relaxFrame(0, makeCrystal(5)));
    expect(store.getState().streams[0]?.kind).toBe("relaxation");
    deliver(relaxFrame(1, makeCrystal(5), true, true));
    expect(store.getState().streams).toHaveLength(0);
  });

  it("FOLLOW_INSTABILITY streams relaxation frames into the follow entry", () => {
    const { store, client, deliver } = connected();
    client.send("FOLLOW_INSTABILITY", { q_index: 0, band_index: 0 });
    deliver(relaxFrame(0, makeCrystal(8)));
    expect(store.getState().streams[0]?.command).toBe("FOLLOW_INSTABILITY");
    deliver(relaxFrame(4, makeCrystal(8), true, true));
    expect(store.getState().streams).toHaveLength(0);
  });

  it("adopts a stream for frames it did not start (e.g. after a client reload)", () => {
    const { store, deliver } = connected();
    deliver(flowFrame(3, 10, makeCrystal(5)));
    expect(streamCommands(store)).toEqual(["START_FLOW_GENERATION"]);
    deliver(flowFinal(makeCrystal(5)));
    expect(store.getState().streams).toHaveLength(0);
  });
});

describe("high-rate data path", () => {
  it("frame buffers update synchronously; the HUD copy is throttled", () => {
    let now = 0;
    const timers: (() => void)[] = [];
    const store = createCrystalStore({
      clock: { now: () => now, setTimeout: (fn) => timers.push(fn), clearTimeout: () => {} },
    });
    let crystalRenders = 0;
    store.subscribe((s, prev) => {
      if (s.crystal !== prev.crystal) crystalRenders++;
    });
    dispatchServerMessage(store, spu(makeCrystal(5))); // topology change: published at once
    expect(crystalRenders).toBe(1);
    for (let k = 1; k <= 30; k++) {
      now += 33; // ~30 Hz
      dispatchServerMessage(store, flowFrame(k, 30, makeCrystal(5, { move: { index: 0, cart: [k * 0.01, 0, 0] } })));
      // the transient buffer always has the latest frame
      expect(store.getState().frame.positions[0]).toBeCloseTo(k * 0.01, 6);
    }
    // ~1 s of frames -> about 10 HUD publishes, not 30
    expect(crystalRenders).toBeLessThanOrEqual(12);
    store.getState().flushHud();
    expect(store.getState().crystal?.atoms[0]?.cart_coords[0]).toBeCloseTo(0.3, 10);
  });

  it("flow trails unwrap across the periodic boundary", () => {
    const store = createCrystalStore();
    const a = 4;
    const xs = [3.8, 3.95, 0.1, 0.25]; // atom 0 crosses x = a
    xs.forEach((x, k) => dispatchServerMessage(store, flowFrame(k + 1, 10, makeCrystal(2, { move: { index: 0, cart: [x, 1, 1] } }))));
    const tr = store.getState().frame.trails;
    expect(tr.count).toBe(4);
    const pts = [3, 2, 1, 0].map((age) => tr.data[tr.frameOffset(age)]!);
    // continuous: 3.8, 3.95, 4.1, 4.25 (no jump of -a)
    expect(pts[0]).toBeCloseTo(3.8, 5);
    expect(pts[3]).toBeCloseTo(4.25, 5);
    for (let k = 1; k < 4; k++) expect(Math.abs(pts[k]! - pts[k - 1]!)).toBeLessThan(0.2);
    // the newest trail point is a lattice translate of the displayed atom
    expect(pts[3]! - store.getState().frame.positions[0]!).toBeCloseTo(a, 5);
  });

  it("a new flow resets the trail; a single point clears it", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, flowFrame(1, 3, makeCrystal(2)));
    dispatchServerMessage(store, flowFrame(2, 3, makeCrystal(2)));
    expect(store.getState().frame.trails.count).toBe(2);
    dispatchServerMessage(store, flowFrame(1, 3, makeCrystal(2)));
    expect(store.getState().frame.trails.count).toBe(1);
    dispatchServerMessage(store, spu(makeCrystal(2)));
    expect(store.getState().frame.trails.count).toBe(0);
  });

  it("clamps the selection when atoms disappear", () => {
    const store = createCrystalStore();
    dispatchServerMessage(store, spu(makeCrystal(5)));
    store.getState().selectAtom(4);
    expect(store.getState().selectedAtom).toBe(4);
    dispatchServerMessage(store, spu(makeCrystal(4)));
    expect(store.getState().selectedAtom).toBeNull();
  });
});

describe("store defaults", () => {
  let store: CrystalStore;
  beforeEach(() => {
    store = createCrystalStore();
  });
  afterEach(() => store.getState().resetSession());
  it("has the Phase 4 display defaults and phonon animation parameters", () => {
    const s = store.getState();
    expect(s.toolMode).toBe("DRAG_ATOM");
    expect(s.display.representation).toBe("ball-and-stick");
    expect(s.phononParams).toEqual({ amplitude: 1, temperatureK: 300, slowMotionPsPerSecond: 0.3 });
  });
});
