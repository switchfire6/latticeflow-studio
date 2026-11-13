/**
 * Backend endpoints. By default the app talks to its own origin and the Vite
 * dev/preview server proxies /api and /ws to http://127.0.0.1:8000.
 * Set VITE_BACKEND_URL (e.g. http://127.0.0.1:8000) to talk to a backend
 * directly (the backend allows CORS from :5173).
 */
function trimSlash(s: string): string {
  return s.replace(/\/+$/, "");
}

export function backendOrigin(): string | null {
  const raw = import.meta.env?.VITE_BACKEND_URL;
  return raw && raw.trim() ? trimSlash(raw.trim()) : null;
}

export function apiBaseUrl(): string {
  const origin = backendOrigin();
  return origin ? `${origin}/api` : "/api";
}

export function wsUrl(): string {
  const origin = backendOrigin();
  if (origin) return `${origin.replace(/^http/i, "ws")}/ws/studio`;
  const loc = globalThis.location;
  const proto = loc?.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${loc?.host ?? "localhost:5173"}/ws/studio`;
}
