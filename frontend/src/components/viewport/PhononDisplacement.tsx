/**
 * Real-time harmonic eigenmode animator (§4.4 step 6, §6.2.5).
 *
 * Runs first in every frame (useFrame priority -2) and refreshes the per-atom
 * complex displacement field in the transient frame buffers. InstancedAtoms,
 * InstancedBonds and ForceVectors then place every atom of every periodic image
 * at base + n·L + Re[c_κ e^{2πi q·n}] while writing their instance matrices,
 * so the animation never touches React state. It runs only in the PHONON_SHAKE
 * tool mode (switching to Drag puts the atoms back at equilibrium).
 */
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import { useCrystalStore } from "../../store/useCrystalStore";
import { computeModeField } from "../../utils/phonon";

export function PhononDisplacement() {
  // animation time advances only while playing (pause freezes the displaced snapshot)
  const tAnim = useRef(0);
  useFrame((_, dt) => {
    const st = useCrystalStore.getState();
    if (st.phononPlaying) tAnim.current += Math.min(dt, 0.1);
    const f = st.frame;
    const mode = st.selectedEigenmode;
    if (!mode || !st.phonon || f.drag || st.toolMode !== "PHONON_SHAKE") {
      f.phonon.active = false;
      return;
    }
    const ok = computeModeField(mode, f.frac, f.masses, f.n, tAnim.current, st.phononParams, f.phonon.re, f.phonon.im);
    f.phonon.active = ok;
    if (ok) {
      f.phonon.q[0] = mode.q_frac[0];
      f.phonon.q[1] = mode.q_frac[1];
      f.phonon.q[2] = mode.q_frac[2];
    }
  }, -2);
  return null;
}
