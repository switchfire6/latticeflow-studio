/** Text for the Follow Instability outcome ("old → new space group, ΔE"). */
import type { FollowResult } from "../store/useCrystalStore";
import { formatEnergyDelta } from "./units";

/** "Pm-3m (#221) → P4mm (#99) · −14.4 meV/atom · −72.0 meV/f.u." */
export function transitionText(r: FollowResult): string {
  const from = `${r.from.spacegroupSymbol} (#${r.from.spacegroupNumber})`;
  if (!r.to) return `${from} → …`;
  const dE = r.to.energyPerAtom - r.from.energyPerAtom;
  return `${from} → ${r.to.spacegroupSymbol} (#${r.to.spacegroupNumber}) · ${formatEnergyDelta(dE, r.from.atomsPerFormulaUnit ?? null)}`;
}

export function followStatusText(r: FollowResult): string {
  switch (r.status) {
    case "running":
      return "relaxing along the frozen-in mode…";
    case "converged":
      return `converged in ${r.steps} steps (max |F| ${r.to?.maxForce.toFixed(3)} eV/Å)`;
    case "max_steps":
      return `stopped at max_steps (${r.steps}), max |F| ${r.to?.maxForce.toFixed(3)} eV/Å`;
    case "stopped":
      return "stopped by the user";
    case "failed":
      return "failed (see the error)";
  }
}
