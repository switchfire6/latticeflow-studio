/**
 * Structure-file import helpers (§4.3): map a file name (and, for pasted text,
 * the content) to the LOAD_STRUCTURE text_format. "auto" lets the server sniff.
 */
import type { StructureTextFormat } from "../types/protocol";

/** Extensions / names the import dialogs accept. */
export const STRUCTURE_FILE_ACCEPT = ".cif,.vasp,.poscar,.contcar,.xyz,.extxyz,POSCAR,CONTCAR,text/plain,chemical/x-cif";

/** Format implied by a file name: .cif → cif; POSCAR/CONTCAR/.vasp → poscar; .xyz/.extxyz → extxyz; else auto. */
export function formatFromFilename(name: string): StructureTextFormat {
  const base = name.split(/[\\/]/).pop() ?? name;
  const lower = base.toLowerCase();
  if (lower.endsWith(".cif") || lower.endsWith(".mcif")) return "cif";
  if (lower.endsWith(".extxyz") || lower.endsWith(".xyz")) return "extxyz";
  if (lower.endsWith(".vasp") || lower.endsWith(".poscar") || lower.endsWith(".contcar")) return "poscar";
  if (/^(poscar|contcar)([._-].*)?$/i.test(base)) return "poscar";
  return "auto";
}

/** Cheap content sniffing for pasted text (the server decides when unsure). */
export function sniffFormat(text: string): StructureTextFormat {
  const t = text.trimStart();
  if (/^data_/im.test(t) || /_cell_length_a/.test(t)) return "cif";
  const lines = t.split(/\r?\n/);
  if (lines.length > 1 && /^\d+\s*$/.test(lines[0]!.trim()) && /Lattice\s*=/.test(lines[1] ?? "")) return "extxyz";
  return "auto";
}

export const FORMAT_LABELS: Record<StructureTextFormat, string> = {
  auto: "auto-detect",
  cif: "CIF",
  poscar: "POSCAR / CONTCAR",
  extxyz: "extended XYZ",
};

/** True if the drag event carries files. */
export function hasFiles(types: readonly string[] | DOMStringList | null | undefined): boolean {
  if (!types) return false;
  for (let i = 0; i < types.length; i++) {
    const t = (types as ArrayLike<string>)[i];
    if (t === "Files") return true;
  }
  return false;
}
