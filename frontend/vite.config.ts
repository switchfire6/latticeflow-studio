/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api and /ws to the FastAPI backend so `npm run dev`
// works with `uvicorn app.main:app --port 8000` and zero configuration.
// VITE_BACKEND_URL (e.g. http://127.0.0.1:8000) overrides both the proxy target
// and, in the browser, makes the client talk to the backend directly (CORS).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = (env.VITE_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target, changeOrigin: true },
        "/ws": { target, ws: true, changeOrigin: true },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        "/api": { target, changeOrigin: true },
        "/ws": { target, ws: true, changeOrigin: true },
      },
    },
    build: {
      chunkSizeWarningLimit: 2500,
    },
    test: {
      environment: "node",
      include: ["tests/**/*.test.ts"],
    },
  };
});
