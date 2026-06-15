import { interpolate, useCurrentFrame } from "remotion";
import { GEdge, NODES } from "../graph";
import { theme } from "../theme";

/**
 * EDGES_WEAVE (f50–150): each edge draws itself from node A to node B via
 * stroke-dashoffset, staggered by index. AMBIENT (f120+): a slow opacity pulse
 * keeps the lattice alive without re-drawing.
 */
export const Edge: React.FC<{ edge: GEdge; index: number }> = ({ edge, index }) => {
  const frame = useCurrentFrame();
  const a = NODES[edge.a];
  const b = NODES[edge.b];

  const start = 50 + index * 3.2; // distance-independent stagger
  const draw = interpolate(frame, [start, start + 26], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // gentle breathing once drawn
  const pulse = 0.22 + 0.12 * Math.sin((frame + index * 12) / 24);
  const opacity = draw * pulse;

  return (
    <line
      x1={a.x}
      y1={a.y}
      x2={b.x}
      y2={b.y}
      stroke={theme.accent}
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeDasharray={edge.len}
      strokeDashoffset={edge.len * (1 - draw)}
      opacity={opacity}
    />
  );
};
