import { interpolate, useCurrentFrame } from "remotion";
import { theme } from "../theme";
import { VIEW } from "../theme";

/**
 * CRAWL_SWEEP (f30–110): a soft vertical scan bar travels left→right — the
 * literal "crawl" reading scattered sources. Fades out as the weave takes over.
 */
export const CrawlSweep: React.FC = () => {
  const frame = useCurrentFrame();

  const x = interpolate(frame, [30, 110], [120, 900], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const opacity = interpolate(frame, [28, 40, 100, 118], [0, 0.5, 0.5, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <g opacity={opacity}>
      <defs>
        <linearGradient id="sweepGrad" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor={theme.accent} stopOpacity={0} />
          <stop offset="50%" stopColor={theme.accent} stopOpacity={0.9} />
          <stop offset="100%" stopColor={theme.synth} stopOpacity={0} />
        </linearGradient>
      </defs>
      <rect x={x - 40} y={0} width={80} height={VIEW} fill="url(#sweepGrad)" />
      <line x1={x} y1={0} x2={x} y2={VIEW} stroke={theme.accent} strokeWidth={1.5} opacity={0.7} />
    </g>
  );
};
