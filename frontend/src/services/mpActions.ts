/**
 * Materials Project import (§4.3): search by formula, chemical system or
 * mp-id (GET /api/mp/search), fetch the structure (GET /api/mp/structure/{id})
 * and load its CIF through LOAD_STRUCTURE {cif}. Errors are mapped to
 * actionable messages (503: no MP_API_KEY on the backend).
 */
import { ApiError } from "./apiClient";

export type MpQueryKind = "mp-id" | "chemsys" | "formula" | "invalid";

/** How the backend will read the query (display hint; the server does the parsing). */
export function classifyMpQuery(q: string): MpQueryKind {
  const t = q.trim();
  if (/^mp-\d+$/i.test(t) || /^\d+$/.test(t)) return "mp-id";
  if (/^[A-Z][a-z]?(-[A-Z][a-z]?)+$/.test(t)) return "chemsys";
  if (/^([A-Z][a-z]?\d*(\.\d+)?)+$/.test(t)) return "formula";
  return "invalid";
}

export const MP_KEY_HINT = "Set MP_API_KEY in the backend's .env (free key from materialsproject.org → Dashboard → API key) and restart the server.";

export interface MpErrorView {
  title: string;
  message: string;
  tone: "amber" | "red" | "slate";
}

export function mpErrorView(err: unknown): MpErrorView {
  if (err instanceof ApiError) {
    if (err.code === "MP_UNAVAILABLE" && err.status === 503) return { title: "Materials Project not configured", message: `${MP_KEY_HINT} (${err.message})`, tone: "amber" };
    if (err.code === "MP_NOT_FOUND" || err.status === 404) return { title: "Nothing found", message: err.message || "No Materials Project entry matches this query.", tone: "slate" };
    if (err.code === "MP_UNAVAILABLE" || err.status === 502) return { title: "Materials Project unreachable", message: `The backend could not reach the Materials Project API; try again later (${err.message}).`, tone: "red" };
    return { title: err.code, message: err.message, tone: "red" };
  }
  return { title: "Backend unreachable", message: err instanceof Error ? err.message : String(err), tone: "red" };
}
