/**
 * Atom-drag state machine, independent of three.js and the DOM (unit-tested).
 *
 * * begin(): installs an optimistic override in the frame buffers, so the
 *   dragged atom follows the pointer at once while server echoes update
 *   everything else.
 * * move(): updates the override every pointer event and sends
 *   PERTURB_OR_TRANSMUTE_ATOM {new_cart_coords} throttled to 30 Hz
 *   (leading + trailing; the server additionally coalesces queued drag frames).
 * * end(): drops any pending throttled frame and always sends the final
 *   position. The override stays until a server update echoes that position
 *   (FrameBuffers.reconcileDrag) so the atom does not snap back to an older echo.
 */
import type { FrameBuffers } from "../store/frameBuffers";
import { throttle, realClock, type Clock, type Throttled } from "../utils/throttle";

export const DRAG_SEND_HZ = 30;

export type DragSend = (atomIndex: number, cart: [number, number, number]) => void;

export class DragController {
  private readonly throttled: Throttled<[number, [number, number, number]]>;
  private index: number | null = null;
  private moved = false;

  constructor(
    private readonly frame: FrameBuffers,
    private readonly send: DragSend,
    clock: Clock = realClock,
    intervalMs = 1000 / DRAG_SEND_HZ,
  ) {
    this.throttled = throttle((i: number, p: [number, number, number]) => this.send(i, p), intervalMs, clock);
  }

  get active(): boolean {
    return this.index !== null;
  }

  get atomIndex(): number | null {
    return this.index;
  }

  /** Whether the pointer moved the atom since begin() (a click otherwise). */
  get hasMoved(): boolean {
    return this.moved;
  }

  begin(atomIndex: number, start: ArrayLike<number>): void {
    this.throttled.cancel();
    this.index = atomIndex;
    this.moved = false;
    this.frame.drag = {
      index: atomIndex,
      pos: new Float64Array([start[0]!, start[1]!, start[2]!]),
      active: true,
      releasedAt: null,
    };
  }

  move(cart: ArrayLike<number>): void {
    if (this.index === null || !this.frame.drag) return;
    this.moved = true;
    this.frame.drag.pos[0] = cart[0]!;
    this.frame.drag.pos[1] = cart[1]!;
    this.frame.drag.pos[2] = cart[2]!;
    this.throttled(this.index, [cart[0]!, cart[1]!, cart[2]!]);
  }

  /** Release. Returns true if this was a drag (final position sent), false for a click. */
  end(): boolean {
    const index = this.index;
    const drag = this.frame.drag;
    this.index = null;
    this.throttled.cancel();
    if (index === null || !drag) return false;
    if (!this.moved) {
      this.frame.drag = null;
      return false;
    }
    drag.active = false;
    drag.releasedAt = Date.now();
    this.send(index, [drag.pos[0]!, drag.pos[1]!, drag.pos[2]!]);
    return true;
  }
}
