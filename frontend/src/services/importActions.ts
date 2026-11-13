/**
 * Structure import (§4.3): files (picker or drag-and-drop on the viewport) and
 * pasted text go to the server as LOAD_STRUCTURE {text, text_format}. The
 * format comes from the file name when unambiguous, else the server sniffs it.
 */
import type { StructureTextFormat } from "../types/protocol";
import { formatFromFilename, sniffFormat } from "../utils/structureFile";

export type LoadText = (text: string, format: StructureTextFormat) => boolean;
export type Notify = (level: "warning" | "info", title: string, message: string) => void;

export const MAX_IMPORT_BYTES = 5 * 1024 * 1024;

export interface FileLike {
  name: string;
  size: number;
  text(): Promise<string>;
}

/** Read a structure file and send it. Resolves to the format used, or null when nothing was sent. */
export async function importStructureFile(file: FileLike, loadText: LoadText, notify: Notify): Promise<StructureTextFormat | null> {
  if (file.size > MAX_IMPORT_BYTES) {
    notify("warning", "File too large", `${file.name} is ${(file.size / 1e6).toFixed(1)} MB; structure files over 5 MB are not supported.`);
    return null;
  }
  let text: string;
  try {
    text = await file.text();
  } catch (e) {
    notify("warning", "Could not read file", `${file.name}: ${e instanceof Error ? e.message : String(e)}`);
    return null;
  }
  if (!text.trim()) {
    notify("warning", "Empty file", `${file.name} is empty.`);
    return null;
  }
  const byName = formatFromFilename(file.name);
  const format = byName === "auto" ? sniffFormat(text) : byName;
  if (!loadText(text, format)) return null;
  notify("info", "Loading structure", `${file.name} (${format === "auto" ? "format auto-detected by the server" : format})`);
  return format;
}

/** Send pasted text (explicit format, or sniffed when "auto"). */
export function importStructureText(text: string, format: StructureTextFormat, loadText: LoadText): StructureTextFormat | null {
  if (!text.trim()) return null;
  const f = format === "auto" ? sniffFormat(text) : format;
  return loadText(text, f) ? f : null;
}
