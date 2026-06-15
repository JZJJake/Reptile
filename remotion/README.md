# Reptile — Remotion Brand Motion

The **source of truth** for Reptile's brand identity: *the self-weaving
knowledge constellation*. Scattered crawled sources are discovered by a crawl
sweep, then woven into a connected knowledge graph — the product's thesis as
motion.

The live FastAPI app does **not** depend on this project. The same motion is
ported, dependency-free, into `static/login.html` (inline SVG + CSS) so the app
ships with zero new build tooling. This project is where the motion is
authored, previewed, and rendered to video when a real `.mp4` is needed.

## Run

```bash
cd remotion
npm install
npm run dev          # opens Remotion Studio to preview/scrub the composition
npm run render       # renders out/constellation.mp4 (needs Node + Chromium)
```

## Structure

```
src/
  index.ts                         registerRoot
  Root.tsx                         <Composition> — 240f @ 30fps, 1080×1080
  theme.ts                         design tokens (mirror of login.html CSS vars)
  graph.ts                         the hand-composed node/edge layout (shared spec)
  components/
    Node.tsx                       spring pop-in + crawl-flare + ambient bob
    Edge.tsx                       stroke-dashoffset weave + opacity pulse
    CrawlSweep.tsx                 the L→R scan bar
    Wordmark.tsx                   🦎 + title assemble; tracking-tighten signature
  compositions/
    KnowledgeConstellation.tsx     orchestrates the timeline
```

## Timeline (30fps)

| Frames   | Beat         | What happens                                   |
|----------|--------------|------------------------------------------------|
| 0–60     | NODES_IN     | nodes spring-pop in, staggered                 |
| 30–110   | CRAWL_SWEEP  | scan arc sweeps L→R; nodes flare on contact    |
| 50–150   | EDGES_WEAVE  | edges draw themselves; the graph self-assembles|
| 80–125   | WORDMARK     | 🦎 + title spring up; letter-spacing tightens  |
| 120–240  | AMBIENT      | nodes bob, edges pulse — loop-safe seam        |

## Keeping the port in sync

`graph.ts` (`NODES` / `EDGES`) and `theme.ts` are duplicated as inline constants
in `static/login.html`. If you change the layout or tokens here, mirror them
there so the rendered video and the live hero stay identical.
