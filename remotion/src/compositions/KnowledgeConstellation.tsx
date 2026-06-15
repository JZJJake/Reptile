import { AbsoluteFill } from "remotion";
import { CrawlSweep } from "../components/CrawlSweep";
import { Edge } from "../components/Edge";
import { Node } from "../components/Node";
import { Wordmark } from "../components/Wordmark";
import { EDGES, NODES } from "../graph";
import { theme, VIEW } from "../theme";

/**
 * Reptile brand hero — the self-weaving knowledge constellation.
 *
 *   f0–60    NODES_IN      nodes spring-pop, staggered
 *   f30–110  CRAWL_SWEEP   scan arc sweeps L→R, nodes flare on contact
 *   f50–150  EDGES_WEAVE   edges draw, staggered → the graph weaves itself
 *   f80–125  WORDMARK      🦎 + title assemble; tracking tightens (signature)
 *   f120–240 AMBIENT       nodes bob, edges pulse — loop-safe
 *
 * The product's thesis in motion: scattered crawled sources, discovered by a
 * crawl, distilled and woven into a connected knowledge graph.
 */
export const KnowledgeConstellation: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: theme.bg }}>
      {/* radial vignette so the centre reads behind the wordmark */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 50% 50%, ${theme.surface} 0%, ${theme.bg} 70%)`,
        }}
      />
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <svg
          width="100%"
          height="100%"
          viewBox={`0 0 ${VIEW} ${VIEW}`}
          style={{ position: "absolute", inset: 0 }}
        >
          {/* edges under nodes */}
          {EDGES.map((edge, i) => (
            <Edge key={`e${i}`} edge={edge} index={i} />
          ))}
          <CrawlSweep />
          {NODES.map((node) => (
            <Node key={`n${node.id}`} node={node} />
          ))}
        </svg>
      </AbsoluteFill>
      <Wordmark />
    </AbsoluteFill>
  );
};
