/**
 * Replays a real session recorded from the backend through the Vite proxy
 * (scripts/proxy-smoke.mjs --record): auto-load, drag, relaxation, preset load,
 * phonon shake-test + flagged update, REQUEST_EIGENMODE, a 20-step flow,
 * STOP_STREAM and an ERROR. Client commands are replayed through StudioClient
 * so stream registration is exercised exactly as in the app.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { StudioClient, type WebSocketLike } from "../src/services/wsClient";
import { createCrystalStore } from "../src/store/useCrystalStore";
import type { ClientCommand, ClientCommandMap, ServerMessage } from "../src/types/protocol";

type Recorded = (ServerMessage & { _sent?: undefined }) | { _sent: true; type: ClientCommand; payload: unknown };

const recorded: Recorded[] = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/recorded-session.json", import.meta.url)), "utf8"),
);

class ReplaySocket implements WebSocketLike {
  readyState = 1;
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  send() {}
  close() {}
}

describe("recorded backend session", () => {
  it("drives the store through the whole sequence consistently", () => {
    const store = createCrystalStore();
    let sock!: ReplaySocket;
    const client = new StudioClient({ store, createSocket: () => (sock = new ReplaySocket()) });
    client.connect();
    sock.onopen?.({});

    let sawPhonon = false;
    let sawFlags = false;
    let maxStreams = 0;

    for (let i = 0; i < recorded.length; i++) {
      const m = recorded[i]!;
      if (m._sent) {
        client.send(m.type, m.payload as ClientCommandMap[typeof m.type]);
        continue;
      }
      sock.onmessage?.({ data: JSON.stringify(m) });
      const s = store.getState();
      maxStreams = Math.max(maxStreams, s.session.streams.length);

      if (m.type === "PHONON_ANALYSIS_COMPLETE") {
        sawPhonon = true;
        expect(s.phonon?.is_dynamically_stable).toBe(false);
        expect(s.selectedEigenmode?.is_imaginary).toBe(true);
        expect(s.toolMode).toBe("PHONON_SHAKE");
        expect(s.session.streams).toHaveLength(0);
      }
      if (m.type === "SINGLE_POINT_UPDATE" && sawPhonon && !sawFlags) {
        sawFlags = true;
        // flagged update must keep the analysis and carry the actor flags
        expect(s.phonon).not.toBeNull();
        const flagged = m.payload.crystal.atoms.filter((a) => a.is_unstable_actor).map((a) => a.index);
        expect(flagged.length).toBeGreaterThan(0);
        expect(Array.from(s.frame.unstable).flatMap((v, k) => (v ? [k] : []))).toEqual(flagged);
      }
      if (m.type === "EIGENMODE_DATA") {
        expect(s.selectedEigenmode?.q_frac).toEqual(m.payload.eigenmode.q_frac);
      }
      if (m.type === "FLOW_TRAJECTORY_FRAME") {
        expect(s.phonon).toBeNull(); // the flow invalidated the analysis
        expect(s.session.streams[0]?.kind).toBe("flow");
        expect(s.frame.trails.count).toBe(Math.min(m.payload.step, 15));
        expect(s.frame.n).toBe(m.payload.crystal.atoms.length);
      }
      if (m.type === "CRYSTAL_STATE_FINAL") {
        expect(s.session.streams).toHaveLength(0);
        store.getState().flushHud();
        expect(store.getState().crystal?.bulk_modulus_gpa).toBe(m.payload.crystal.bulk_modulus_gpa);
      }
      if (m.type === "RELAXATION_FRAME" && m.payload.final) {
        expect(s.session.streams.some((x) => x.kind === "relaxation")).toBe(false);
      }
    }

    const s = store.getState();
    s.flushHud();
    expect(sawPhonon && sawFlags).toBe(true);
    expect(maxStreams).toBeGreaterThan(0);
    expect(store.getState().streams).toHaveLength(0);
    expect(store.getState().lastError?.code).toBe("INDEX_OUT_OF_RANGE");
    expect(store.getState().relaxHistory.length).toBeGreaterThan(0);
    // frame buffers agree with the last structure the server sent
    const lastCrystal = [...recorded].reverse().find((m): m is Extract<ServerMessage, { payload: { crystal: unknown } }> => !m._sent && "crystal" in (m.payload as object));
    expect(s.frame.n).toBe(lastCrystal!.payload.crystal.atoms.length);
    expect(s.frame.positions[0]).toBeCloseTo(lastCrystal!.payload.crystal.atoms[0]!.cart_coords[0], 5);
  });
});
