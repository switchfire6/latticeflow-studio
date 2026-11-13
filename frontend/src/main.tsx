import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { commands, studio } from "./services/wsClient";
import { useCrystalStore } from "./store/useCrystalStore";
import "./index.css";

// Dev-only handle for debugging and scripted screenshots (scripts/cdp-shot.mjs).
if (import.meta.env.DEV) {
  (globalThis as unknown as { __latticeflow: unknown }).__latticeflow = { useCrystalStore, studio, commands };
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
