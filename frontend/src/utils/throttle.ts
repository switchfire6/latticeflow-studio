/**
 * Leading + trailing throttle with an injectable clock (for deterministic tests).
 *
 * The first call fires immediately; calls inside the interval are coalesced and
 * the latest arguments fire once the interval has elapsed. `flush()` fires a
 * pending call now, `cancel()` drops it.
 */
export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const realClock: Clock = {
  now: () => (typeof performance !== "undefined" ? performance.now() : Date.now()),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
};

export interface Throttled<A extends unknown[]> {
  (...args: A): void;
  flush(): void;
  cancel(): void;
  readonly pending: boolean;
}

export function throttle<A extends unknown[]>(fn: (...args: A) => void, intervalMs: number, clock: Clock = realClock): Throttled<A> {
  let last = -Infinity;
  let pendingArgs: A | null = null;
  let timer: unknown = null;

  const fire = () => {
    timer = null;
    if (pendingArgs) {
      const args = pendingArgs;
      pendingArgs = null;
      last = clock.now();
      fn(...args);
    }
  };

  const throttled = ((...args: A) => {
    const now = clock.now();
    const elapsed = now - last;
    if (timer === null && elapsed >= intervalMs) {
      last = now;
      fn(...args);
      return;
    }
    pendingArgs = args;
    if (timer === null) timer = clock.setTimeout(fire, Math.max(0, intervalMs - elapsed));
  }) as Throttled<A>;

  throttled.flush = () => {
    if (timer !== null) clock.clearTimeout(timer);
    fire();
  };
  throttled.cancel = () => {
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
    pendingArgs = null;
  };
  Object.defineProperty(throttled, "pending", { get: () => pendingArgs !== null });
  return throttled;
}
