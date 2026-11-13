/**
 * Element data for Z = 1..89 (the MACE-MP-0 coverage, PROJECT_SPEC.md §5.2).
 *
 * Values are taken verbatim from ASE (`ase.data`), i.e. the same tables the
 * backend uses:
 *   colour  - Jmol CPK colours (`ase.data.colors.jmol_colors`)
 *   radius  - covalent radii in Å, Cordero et al. 2008 (`ase.data.covalent_radii`)
 *   mass    - standard atomic weights in amu (`ase.data.atomic_masses`)
 */

export interface ElementInfo {
  z: number;
  symbol: string;
  color: number; // 0xRRGGBB
  covalentRadius: number; // Å
  mass: number; // amu
}

export const MAX_Z = 89;

// [symbol, jmol colour, covalent radius (Å), mass (amu)] indexed by Z - 1
const TABLE: ReadonlyArray<readonly [string, number, number, number]> = [
  ['H', 0xFFFFFF, 0.31, 1.0080],
  ['He', 0xD9FFFF, 0.28, 4.0026],
  ['Li', 0xCC80FF, 1.28, 6.9400],
  ['Be', 0xC2FF00, 0.96, 9.0122],
  ['B', 0xFFB5B5, 0.84, 10.8100],
  ['C', 0x909090, 0.76, 12.0110],
  ['N', 0x3050F8, 0.71, 14.0070],
  ['O', 0xFF0D0D, 0.66, 15.9990],
  ['F', 0x90E050, 0.57, 18.9984],
  ['Ne', 0xB3E3F5, 0.58, 20.1797],
  ['Na', 0xAB5CF2, 1.66, 22.9898],
  ['Mg', 0x8AFF00, 1.41, 24.3050],
  ['Al', 0xBFA6A6, 1.21, 26.9815],
  ['Si', 0xF0C8A0, 1.11, 28.0850],
  ['P', 0xFF8000, 1.07, 30.9738],
  ['S', 0xFFFF30, 1.05, 32.0600],
  ['Cl', 0x1FF01F, 1.02, 35.4500],
  ['Ar', 0x80D1E3, 1.06, 39.9480],
  ['K', 0x8F40D4, 2.03, 39.0983],
  ['Ca', 0x3DFF00, 1.76, 40.0780],
  ['Sc', 0xE6E6E6, 1.70, 44.9559],
  ['Ti', 0xBFC2C7, 1.60, 47.8670],
  ['V', 0xA6A6AB, 1.53, 50.9415],
  ['Cr', 0x8A99C7, 1.39, 51.9961],
  ['Mn', 0x9C7AC7, 1.39, 54.9380],
  ['Fe', 0xE06633, 1.32, 55.8450],
  ['Co', 0xF090A0, 1.26, 58.9332],
  ['Ni', 0x50D050, 1.24, 58.6934],
  ['Cu', 0xC88033, 1.32, 63.5460],
  ['Zn', 0x7D80B0, 1.22, 65.3800],
  ['Ga', 0xC28F8F, 1.22, 69.7230],
  ['Ge', 0x668F8F, 1.20, 72.6300],
  ['As', 0xBD80E3, 1.19, 74.9216],
  ['Se', 0xFFA100, 1.20, 78.9710],
  ['Br', 0xA62929, 1.20, 79.9040],
  ['Kr', 0x5CB8D1, 1.16, 83.7980],
  ['Rb', 0x702EB0, 2.20, 85.4678],
  ['Sr', 0x00FF00, 1.95, 87.6200],
  ['Y', 0x94FFFF, 1.90, 88.9058],
  ['Zr', 0x94E0E0, 1.75, 91.2240],
  ['Nb', 0x73C2C9, 1.64, 92.9064],
  ['Mo', 0x54B5B5, 1.54, 95.9500],
  ['Tc', 0x3B9E9E, 1.47, 97.9072],
  ['Ru', 0x248F8F, 1.46, 101.0700],
  ['Rh', 0x0A7D8C, 1.42, 102.9055],
  ['Pd', 0x006985, 1.39, 106.4200],
  ['Ag', 0xC0C0C0, 1.45, 107.8682],
  ['Cd', 0xFFD98F, 1.44, 112.4140],
  ['In', 0xA67573, 1.42, 114.8180],
  ['Sn', 0x668080, 1.39, 118.7100],
  ['Sb', 0x9E63B5, 1.39, 121.7600],
  ['Te', 0xD47A00, 1.38, 127.6000],
  ['I', 0x940094, 1.39, 126.9045],
  ['Xe', 0x429EB0, 1.40, 131.2930],
  ['Cs', 0x57178F, 2.44, 132.9055],
  ['Ba', 0x00C900, 2.15, 137.3270],
  ['La', 0x70D4FF, 2.07, 138.9055],
  ['Ce', 0xFFFFC7, 2.04, 140.1160],
  ['Pr', 0xD9FFC7, 2.03, 140.9077],
  ['Nd', 0xC7FFC7, 2.01, 144.2420],
  ['Pm', 0xA3FFC7, 1.99, 144.9128],
  ['Sm', 0x8FFFC7, 1.98, 150.3600],
  ['Eu', 0x61FFC7, 1.98, 151.9640],
  ['Gd', 0x45FFC7, 1.96, 157.2500],
  ['Tb', 0x30FFC7, 1.94, 158.9254],
  ['Dy', 0x1FFFC7, 1.92, 162.5000],
  ['Ho', 0x00FF9C, 1.92, 164.9303],
  ['Er', 0x00E675, 1.89, 167.2590],
  ['Tm', 0x00D452, 1.90, 168.9342],
  ['Yb', 0x00BF38, 1.87, 173.0540],
  ['Lu', 0x00AB24, 1.87, 174.9668],
  ['Hf', 0x4DC2FF, 1.75, 178.4900],
  ['Ta', 0x4DA6FF, 1.70, 180.9479],
  ['W', 0x2194D6, 1.62, 183.8400],
  ['Re', 0x267DAB, 1.51, 186.2070],
  ['Os', 0x266696, 1.44, 190.2300],
  ['Ir', 0x175487, 1.41, 192.2170],
  ['Pt', 0xD0D0E0, 1.36, 195.0840],
  ['Au', 0xFFD123, 1.36, 196.9666],
  ['Hg', 0xB8B8D0, 1.32, 200.5920],
  ['Tl', 0xA6544D, 1.45, 204.3800],
  ['Pb', 0x575961, 1.46, 207.2000],
  ['Bi', 0x9E4FB5, 1.48, 208.9804],
  ['Po', 0xAB5C00, 1.40, 208.9824],
  ['At', 0x754F45, 1.50, 209.9872],
  ['Rn', 0x428296, 1.50, 222.0176],
  ['Fr', 0x420066, 2.60, 223.0197],
  ['Ra', 0x007D00, 2.21, 226.0254],
  ['Ac', 0x70ABFA, 2.15, 227.0277],
];

export const ELEMENTS: readonly ElementInfo[] = TABLE.map(([symbol, color, covalentRadius, mass], i) => ({
  z: i + 1,
  symbol,
  color,
  covalentRadius,
  mass,
}));

const BY_SYMBOL = new Map<string, ElementInfo>(ELEMENTS.map((e) => [e.symbol, e]));

const FALLBACK: ElementInfo = { z: 0, symbol: "X", color: 0xff1493, covalentRadius: 1.0, mass: 1.0 };

export function elementByZ(z: number): ElementInfo {
  return ELEMENTS[z - 1] ?? FALLBACK;
}

export function elementBySymbol(symbol: string): ElementInfo | undefined {
  return BY_SYMBOL.get(symbol);
}

/** Normalise user input ("ti", "TI") to a known symbol ("Ti"), or null. */
export function normalizeSymbol(input: string): string | null {
  const s = input.trim();
  if (!s) return null;
  const cased = s[0]!.toUpperCase() + s.slice(1).toLowerCase();
  return BY_SYMBOL.has(cased) ? cased : null;
}

export function cpkColor(z: number): number {
  return elementByZ(z).color;
}

export function covalentRadius(z: number): number {
  return elementByZ(z).covalentRadius;
}

export function atomicMass(z: number): number {
  return elementByZ(z).mass;
}

export function cpkHex(z: number): string {
  return `#${cpkColor(z).toString(16).padStart(6, "0")}`;
}

/** Display radius: ball-and-stick = 0.45 x covalent, space-filling = 1.0 x covalent (§6.2). */
export const BALL_AND_STICK_SCALE = 0.45;
export const SPACE_FILLING_SCALE = 1.0;
