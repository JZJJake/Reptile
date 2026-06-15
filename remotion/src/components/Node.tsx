import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colorFor, GNode } from "../graph";

const SWEEP_START = 30;
const SWEEP_END = 110;

/**
 * NODES_IN (f0–60): spring pop-in, staggered by id.
 * CRAWL_SWEEP (f30–110): as the scan arc's x passes a node, it flares bright
 *   then settles — the "discovery" beat.
 * AMBIENT (f120+): a small sine bob keeps the field breathing, loop-safe.
 */
export const Node: React.FC<{ node: GNode }> = ({ node }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const appear = spring({
    frame,
    fps,
    delay: node.id * 4,
    config: { damping: 13, stiffness: 200, mass: 0.6 },
  });

  // sweep x travels across the canvas; flare peaks when it reaches node.x
  const sweepX = interpolate(frame, [SWEEP_START, SWEEP_END], [120, 900], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const dist = Math.abs(sweepX - node.x);
  const flare = interpolate(dist, [0, 110], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const bob = Math.sin((frame + node.id * 30) / 22) * 3.5;
  const color = colorFor(node.kind);
  const r = node.r * appear * (1 + flare * 0.5);

  return (
    <g transform={`translate(${node.x}, ${node.y + bob})`} opacity={appear}>
      {/* glow */}
      <circle r={r * 2.6} fill={color} opacity={0.1 + flare * 0.22} />
      {/* core */}
      <circle r={r} fill={color} opacity={0.55 + flare * 0.45} />
      {/* discovery ring on flare */}
      {flare > 0.02 && (
        <circle
          r={r + 6 + flare * 10}
          fill="none"
          stroke={color}
          strokeWidth={1.4}
          opacity={flare * 0.6}
        />
      )}
    </g>
  );
};
