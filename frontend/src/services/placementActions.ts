/**
 * Phase C2 click-to-place for ADD_ATOM (§7.3): the Add-atom dialog hides
 * while the viewport picks a point on the cell plane s_axis = depth
 * (components/viewport/PlacementPlane.tsx); a pick or a cancel reopens it.
 */
import { useUi } from "../store/uiStore";
import type { Vec3 } from "../utils/latticeMath";

/** Depth of a placement plane: 1 stays 1 (the far face), everything else wraps into [0, 1). */
export function wrapDepth(d: number): number {
  if (d === 1) return 1;
  const w = d - Math.floor(d);
  return w >= 1 ? 0 : w;
}

export function startPlacement(axis: 0 | 1 | 2, depth: number): void {
  const ui = useUi.getState();
  ui.setPlacement({ axis, depth: wrapDepth(depth), hover: null });
  ui.closeDialog();
}

export function cancelPlacement(): void {
  const ui = useUi.getState();
  ui.setPlacement(null);
  ui.openDialog("add-atom");
}

/** A viewport click on the plane: take the coordinates and reopen the dialog. */
export function commitPlacement(frac: Vec3): void {
  const ui = useUi.getState();
  ui.setAddAtomDraft({ frac: frac.map((x) => x.toFixed(4)) as [string, string, string] });
  ui.setPlacement(null);
  ui.openDialog("add-atom");
}
