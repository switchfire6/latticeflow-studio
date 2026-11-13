/**
 * Layout (§6.3): telemetry header on top, generator sidebar on the left, the
 * 3D viewport filling the rest with its overlays (Phase B: the stability
 * scorecard top right), and a collapsible charts dock.
 */
import { useEffect, useState, type ComponentType } from "react";
import { BottomDock } from "./components/hud/BottomDock";
import { GeneratorSidebar } from "./components/hud/GeneratorSidebar";
import { TelemetryHeader } from "./components/hud/TelemetryHeader";
import { Toasts } from "./components/hud/Toasts";
import { TransmuteToolbar } from "./components/hud/TransmuteToolbar";
import { ViewportDropZone } from "./components/hud/ImportExport";
import { refreshModels } from "./components/hud/ModelPicker";
import { Scorecard } from "./components/hud/Scorecard";
import { PhaseCDialogs, PlacementBar } from "./components/hud/PhaseCDialogs";
import { PhaseC2Dialogs } from "./components/hud/PhaseC2Dialogs";
import { useLocalAnalysisAutoRefresh } from "./services/localActions";
import { DisplayPanel, EigenmodePill, FollowBanner, StreamProgress, ToolModeBar } from "./components/hud/ViewportOverlays";
import { api } from "./services/apiClient";
import { commands, sessionMemory, studio } from "./services/wsClient";
import { useCrystalStore } from "./store/useCrystalStore";
import { shortcutFor } from "./utils/shortcuts";

const HEALTH_POLL_MS = 15_000;

/**
 * three / R3F / postprocessing live in their own chunk so the HUD paints first.
 * Loaded with a plain dynamic import (not React.lazy + Suspense: drei's <Html>
 * roots inside a suspended StrictMode subtree log "synchronously unmount a root" errors).
 */
function useCrystalCanvas(): ComponentType | null {
  const [Comp, setComp] = useState<ComponentType | null>(null);
  useEffect(() => {
    let alive = true;
    void import("./components/viewport/CrystalCanvas").then((m) => {
      if (alive) setComp(() => m.CrystalCanvas);
    });
    return () => {
      alive = false;
    };
  }, []);
  return Comp;
}

/** Esc stops the running stream; Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y undo and redo (unless typing in a field). */
function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const action = shortcutFor(e);
      if (!action) return;
      const st = useCrystalStore.getState();
      if (action === "stop") {
        if (st.streams.length > 0 && !st.streams[0]!.stopping) commands.stop();
        return;
      }
      e.preventDefault();
      if (action === "undo" && st.history?.can_undo) commands.undo();
      if (action === "redo" && st.history?.can_redo) commands.redo();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/** Connect the socket, poll /api/health and fetch the preset list (retrying while the backend warms up). */
function useBackend() {
  useEffect(() => {
    studio.connect();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const st = useCrystalStore.getState();
      try {
        st.setHealth(await api.health());
        if (st.presets.length === 0) st.setPresets(await api.presets());
        if (!useCrystalStore.getState().models) void refreshModels();
      } catch (e) {
        st.setHealth(null, e instanceof Error ? e.message : String(e));
      }
      if (!cancelled) timer = setTimeout(poll, useCrystalStore.getState().health ? HEALTH_POLL_MS : 3000);
    };
    void poll();
    const flush = () => sessionMemory.flush();
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("pagehide", flush);
      flush();
      cancelled = true;
      if (timer) clearTimeout(timer);
      studio.disconnect();
    };
  }, []);
}

export function App() {
  useBackend();
  useLocalAnalysisAutoRefresh();
  useShortcuts();
  const CrystalCanvas = useCrystalCanvas();
  const hasCrystal = useCrystalStore((s) => s.crystal !== null);
  const connection = useCrystalStore((s) => s.connection);
  return (
    <div className="flex h-full flex-col">
      <TelemetryHeader />
      <div className="flex min-h-0 flex-1">
        <GeneratorSidebar />
        <ViewportDropZone>
          {CrystalCanvas ? (
            <CrystalCanvas />
          ) : (
            <div className="absolute inset-0 grid place-items-center text-sm text-fg-4">loading 3D engine…</div>
          )}
          <div className="pointer-events-none absolute inset-0">
            {/* max-w keeps the column clear of the 310 px scorecard (+ margins) at 1280 px */}
            <div className="absolute top-3 left-3 flex max-w-[calc(100%-346px)] flex-col items-start gap-2">
              <ToolModeBar />
              <EigenmodePill />
              <FollowBanner />
            </div>
            <div className="absolute top-3 left-1/2 flex -translate-x-1/2 flex-col items-center gap-2">
              <StreamProgress />
            </div>
            <div className="absolute top-3 right-3">
              <Scorecard />
            </div>
            <div className="absolute bottom-3 left-3">
              <DisplayPanel />
            </div>
            {!hasCrystal && (
              <div className="absolute inset-0 grid place-items-center">
                <div className="text-center text-sm text-fg-4">
                  {connection === "open" ? "loading structure…" : "connecting to the LatticeFlow backend (uvicorn :8000)…"}
                </div>
              </div>
            )}
          </div>
          <TransmuteToolbar />
          <Toasts />
          <PhaseCDialogs />
          <PhaseC2Dialogs />
          <PlacementBar />
        </ViewportDropZone>
      </div>
      <BottomDock />
    </div>
  );
}
