/**
 * Last committed structure, kept in memory and (debounced) in localStorage so a
 * reconnect or a page reload can restore it (§4.4). Storage access never
 * throws (private windows, quota, Node tests).
 */
import type { CrystalStatePayload } from "../types/protocol";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export interface SessionMemoryOptions {
  /** localStorage key; null = memory only. */
  storageKey?: string | null;
  /** Debounce of storage writes (drag echoes arrive at ~20 Hz). */
  saveDelayMs?: number;
  storage?: StorageLike | null;
}

const VERSION = 1;

function isCrystal(x: unknown): x is CrystalStatePayload {
  if (!x || typeof x !== "object") return false;
  const c = x as Partial<CrystalStatePayload>;
  return Array.isArray(c.atoms) && c.atoms.length > 0 && !!c.lattice && Array.isArray(c.lattice.matrix) && typeof c.formula === "string";
}

/** Phase C1: the session's model, restored with SET_MODEL after a reconnect. */
export interface RememberedModel {
  model_id: string;
  dispersion: boolean;
}

export class SessionMemory {
  crystal: CrystalStatePayload | null = null;
  model: RememberedModel | null = null;
  private readonly key: string | null;
  private readonly delay: number;
  private readonly storageOverride: StorageLike | null | undefined;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(opts: SessionMemoryOptions = {}) {
    this.key = opts.storageKey ?? null;
    this.delay = opts.saveDelayMs ?? 1000;
    this.storageOverride = opts.storage;
    this.crystal = this.load();
    this.model = this.loadModel();
  }

  private loadModel(): RememberedModel | null {
    if (!this.key) return null;
    try {
      const raw = this.storage()?.getItem(`${this.key}.model`);
      if (!raw) return null;
      const m = JSON.parse(raw) as Partial<RememberedModel>;
      return typeof m?.model_id === "string" ? { model_id: m.model_id, dispersion: !!m.dispersion } : null;
    } catch {
      return null;
    }
  }

  /** Remember the session's model (written at once; it changes rarely). */
  rememberModel(m: RememberedModel | null): void {
    this.model = m ? { model_id: m.model_id, dispersion: !!m.dispersion } : null;
    if (!this.key) return;
    try {
      const st = this.storage();
      if (m) st?.setItem(`${this.key}.model`, JSON.stringify(this.model));
      else if (st?.removeItem) st.removeItem(`${this.key}.model`);
    } catch {
      /* ignore */
    }
  }

  private storage(): StorageLike | null {
    if (this.storageOverride !== undefined) return this.storageOverride;
    try {
      return (globalThis.localStorage as StorageLike | undefined) ?? null;
    } catch {
      return null;
    }
  }

  private load(): CrystalStatePayload | null {
    if (!this.key) return null;
    try {
      const raw = this.storage()?.getItem(this.key);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as { v?: number; crystal?: unknown };
      return parsed?.v === VERSION && isCrystal(parsed.crystal) ? parsed.crystal : null;
    } catch {
      return null;
    }
  }

  remember(c: CrystalStatePayload | null): void {
    if (!c) return;
    this.crystal = c;
    this.schedule();
  }

  forget(): void {
    this.crystal = null;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (!this.key) return;
    try {
      const st = this.storage();
      if (st?.removeItem) st.removeItem(this.key);
      else st?.setItem(this.key, "");
    } catch {
      /* ignore */
    }
  }

  private schedule(): void {
    if (!this.key || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, this.delay);
  }

  /** Write the remembered structure now. */
  flush(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (!this.key || !this.crystal) return;
    try {
      this.storage()?.setItem(this.key, JSON.stringify({ v: VERSION, crystal: this.crystal }));
    } catch {
      /* storage unavailable or full: restore after a page reload just won't work */
    }
  }
}
