/**
 * The knowledge constellation layout — a deliberately hand-composed graph,
 * NOT a random blob (random scatter reads as AI-generated noise).
 *
 * A clear outer ring of crawled "source" nodes surrounds an inner lattice of
 * "distilled" nodes, leaving the centre clear for the wordmark. Edges form a
 * connected web: ring → spokes → inner lattice → a couple of chords.
 *
 * These exact constants are duplicated in static/login.html's inline JS so the
 * live CSS/SVG port matches this spec node-for-node and edge-for-edge.
 */
import { theme } from "./theme";

export type NodeKind = "accent" | "node" | "synth" | "muted";

export interface GNode {
  id: number;
  x: number;
  y: number;
  r: number;
  kind: NodeKind;
}

export interface GEdge {
  a: number;
  b: number;
  /** precomputed length in logical units — used to drive the draw animation */
  len: number;
}

const RAW_NODES: Omit<GNode, "len">[] = [
  // outer ring (crawled sources)
  { id: 0, x: 500, y: 180, r: 13, kind: "accent" },
  { id: 1, x: 700, y: 250, r: 10, kind: "muted" },
  { id: 2, x: 820, y: 420, r: 12, kind: "node" },
  { id: 3, x: 815, y: 610, r: 9, kind: "muted" },
  { id: 4, x: 690, y: 760, r: 12, kind: "accent" },
  { id: 5, x: 500, y: 820, r: 10, kind: "muted" },
  { id: 6, x: 310, y: 765, r: 13, kind: "synth" },
  { id: 7, x: 185, y: 610, r: 9, kind: "muted" },
  { id: 8, x: 180, y: 420, r: 12, kind: "node" },
  { id: 9, x: 300, y: 250, r: 10, kind: "muted" },
  // inner lattice (distilled concepts)
  { id: 10, x: 430, y: 360, r: 9, kind: "accent" },
  { id: 11, x: 615, y: 360, r: 9, kind: "muted" },
  { id: 12, x: 680, y: 540, r: 10, kind: "node" },
  { id: 13, x: 560, y: 660, r: 9, kind: "muted" },
  { id: 14, x: 400, y: 620, r: 10, kind: "synth" },
  { id: 15, x: 335, y: 500, r: 9, kind: "accent" },
];

const RAW_EDGES: Array<[number, number]> = [
  // outer ring
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9], [9, 0],
  // inner lattice
  [10, 11], [11, 12], [12, 13], [13, 14], [14, 15], [15, 10],
  // spokes: outer → inner
  [9, 10], [1, 11], [2, 12], [4, 13], [6, 14], [7, 15], [0, 10],
  // chords for depth
  [10, 12], [15, 13],
];

export const NODES: GNode[] = RAW_NODES.map((n) => ({ ...n }));

export const EDGES: GEdge[] = RAW_EDGES.map(([a, b]) => {
  const na = RAW_NODES[a];
  const nb = RAW_NODES[b];
  return { a, b, len: Math.hypot(nb.x - na.x, nb.y - na.y) };
});

export function colorFor(kind: NodeKind): string {
  switch (kind) {
    case "accent":
      return theme.accent;
    case "node":
      return theme.node;
    case "synth":
      return theme.synth;
    default:
      return theme.muted;
  }
}
