/**
 * Design tokens for the Reptile brand identity.
 * Mirrors the CSS custom properties shipped in static/login.html so the
 * Remotion render and the live (dependency-free) port stay visually identical.
 */
export const theme = {
  bg: "#0d1117", // canvas
  surface: "#161b22", // card / panel
  border: "#30363d",
  text: "#f0f6fc", // display
  muted: "#8b949e",
  accent: "#58a6ff", // edges / links — knowledge connections
  node: "#3fb950", // active node / success
  synth: "#bc8cff", // weave highlight / synthesis layer
} as const;

/** Canvas is authored in a 1000×1000 logical space, centred at (500,500). */
export const VIEW = 1000;
export const CENTER = { x: 500, y: 500 };
