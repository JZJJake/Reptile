import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { theme } from "../theme";

/**
 * WORDMARK (f80–125): the brand assembles at centre — spring scale + fade-up,
 * and the signature move: letter-spacing tightens from loose to −0.035em as it
 * lands, so the title "snaps into focus".
 */
export const Wordmark: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const enter = spring({
    frame,
    fps,
    delay: 80,
    config: { damping: 14, stiffness: 170, mass: 0.7 },
  });

  const translateY = interpolate(enter, [0, 1], [26, 0]);
  const tracking = interpolate(enter, [0, 1], [0.12, -0.035]); // em
  const markScale = spring({
    frame,
    fps,
    delay: 74,
    config: { damping: 10, stiffness: 220, mass: 0.5 },
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        flexDirection: "column",
        gap: 18,
      }}
    >
      <div
        style={{
          fontSize: 120,
          lineHeight: 1,
          transform: `scale(${markScale})`,
          filter: `drop-shadow(0 0 28px ${theme.accent}55)`,
        }}
      >
        🦎
      </div>
      <div
        style={{
          opacity: enter,
          transform: `translateY(${translateY}px)`,
          fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
          fontWeight: 800,
          fontSize: 72,
          color: theme.text,
          letterSpacing: `${tracking}em`,
        }}
      >
        Reptile
      </div>
      <div
        style={{
          opacity: interpolate(enter, [0.4, 1], [0, 1], { extrapolateLeft: "clamp" }),
          transform: `translateY(${translateY}px)`,
          fontFamily: "ui-monospace, 'Consolas', monospace",
          fontSize: 22,
          letterSpacing: "0.32em",
          color: theme.muted,
          textTransform: "uppercase",
        }}
      >
        智能知识库系统
      </div>
    </AbsoluteFill>
  );
};
