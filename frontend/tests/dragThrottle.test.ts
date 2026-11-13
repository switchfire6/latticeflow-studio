import { describe, expect, it } from "vitest";
import { DragController } from "../src/services/dragController";
import { FrameBuffers } from "../src/store/frameBuffers";
import { throttle, type Clock } from "../src/utils/throttle";
import { makeCrystal } from "./fixtures";

/** Manual clock: timers fire only when `advance` passes their deadline. */
class FakeClock implements Clock {
  t = 0;
  private timers: { at: number; fn: () => void; id: number }[] = [];
  private nextId = 1;
  now = () => this.t;
  setTimeout = (fn: () => void, ms: number) => {
    const id = this.nextId++;
    this.timers.push({ at: this.t + ms, fn, id });
    return id;
  };
  clearTimeout = (h: unknown) => {
    this.timers = this.timers.filter((x) => x.id !== h);
  };
  advance(ms: number) {
    const end = this.t + ms;
    for (;;) {
      this.timers.sort((a, b) => a.at - b.at);
      const next = this.timers[0];
      if (!next || next.at > end) break;
      this.timers.shift();
      this.t = next.at;
      next.fn();
    }
    this.t = end;
  }
}

describe("throttle", () => {
  it("fires the leading call, coalesces, and fires the latest args on the trailing edge", () => {
    const clock = new FakeClock();
    const calls: number[] = [];
    const th = throttle((x: number) => calls.push(x), 100, clock);
    th(1);
    th(2);
    th(3);
    expect(calls).toEqual([1]);
    clock.advance(99);
    expect(calls).toEqual([1]);
    clock.advance(1);
    expect(calls).toEqual([1, 3]);
    clock.advance(500);
    th(4);
    expect(calls).toEqual([1, 3, 4]);
  });

  it("flush fires the pending call now and cancel drops it", () => {
    const clock = new FakeClock();
    const calls: number[] = [];
    const th = throttle((x: number) => calls.push(x), 100, clock);
    th(1);
    th(2);
    th.flush();
    expect(calls).toEqual([1, 2]);
    th(3);
    expect(th.pending).toBe(true);
    th.cancel();
    clock.advance(1000);
    expect(calls).toEqual([1, 2]);
  });
});

describe("DragController", () => {
  function setup() {
    const clock = new FakeClock();
    const frame = new FrameBuffers();
    frame.ingest(makeCrystal(5), "single_point");
    const sent: { i: number; p: [number, number, number]; t: number }[] = [];
    const drag = new DragController(frame, (i, p) => sent.push({ i, p, t: clock.t }), clock);
    return { clock, frame, sent, drag };
  }

  it("sends at most 30 Hz while dragging and always sends the final position on release", () => {
    const { clock, frame, sent, drag } = setup();
    drag.begin(3, [1, 1, 1]);
    // 120 Hz pointer events for 500 ms
    for (let k = 1; k <= 60; k++) {
      clock.advance(1000 / 120);
      drag.move([1 + k * 0.01, 1, 1]);
      // the optimistic override follows every pointer event immediately
      expect(frame.drag!.pos[0]).toBeCloseTo(1 + k * 0.01, 12);
    }
    const duringDrag = sent.length;
    expect(duringDrag).toBeGreaterThanOrEqual(14);
    expect(duringDrag).toBeLessThanOrEqual(16); // 500 ms at 30 Hz (+ leading edge)
    for (let k = 1; k < sent.length; k++) expect(sent[k]!.t - sent[k - 1]!.t).toBeGreaterThanOrEqual(1000 / 30 - 1e-9);

    drag.move([2.5, 1.2, 0.9]); // inside the throttle window: would be dropped by a pure throttle
    expect(drag.end()).toBe(true);
    const last = sent[sent.length - 1]!;
    expect(last.i).toBe(3);
    expect(last.p).toEqual([2.5, 1.2, 0.9]);
    // no stale trailing frame after the final one
    const count = sent.length;
    clock.advance(1000);
    expect(sent.length).toBe(count);
    // override is kept (released) until the server echoes the final position
    expect(frame.drag).not.toBeNull();
    expect(frame.drag!.active).toBe(false);
  });

  it("treats press-and-release without motion as a click (nothing sent, override removed)", () => {
    const { sent, drag, frame } = setup();
    drag.begin(1, [0, 0, 0]);
    expect(drag.end()).toBe(false);
    expect(sent).toHaveLength(0);
    expect(frame.drag).toBeNull();
  });

  it("reconciles: keeps the override against older echoes and drops it once the final position comes back", () => {
    const { frame, drag } = setup();
    drag.begin(2, [0.5, 0.5, 0.5]);
    drag.move([0.7, 0.5, 0.5]);
    drag.move([0.9, 0.5, 0.5]);
    drag.end();
    // an older echo (atom 2 at 0.7) must not snap the atom back
    frame.ingest(makeCrystal(5, { move: { index: 2, cart: [0.7, 0.5, 0.5] } }), "single_point");
    expect(frame.drag).not.toBeNull();
    const p: [number, number, number] = [0, 0, 0];
    frame.basePosition(2, p);
    expect(p[0]).toBeCloseTo(0.9, 12);
    // the echo of the final position clears it
    frame.ingest(makeCrystal(5, { move: { index: 2, cart: [0.9, 0.5, 0.5] } }), "single_point");
    expect(frame.drag).toBeNull();
  });

  it("reconciles across the periodic boundary (server wraps the position into the cell)", () => {
    const { frame, drag } = setup();
    const a = 4; // makeCrystal uses a cubic a = 4 Å cell
    drag.begin(0, [0.1, 0.1, 0.1]);
    drag.move([-0.3, 0.1, 0.1]);
    drag.end();
    frame.ingest(makeCrystal(5, { move: { index: 0, cart: [a - 0.3, 0.1, 0.1] } }), "single_point");
    expect(frame.drag).toBeNull();
  });
});
