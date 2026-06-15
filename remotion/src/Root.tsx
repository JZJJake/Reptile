import { Composition } from "remotion";
import { KnowledgeConstellation } from "./compositions/KnowledgeConstellation";

/**
 * 8-second loop at 30fps. 1080×1080 square reads well as an ambient hero behind
 * a centred card and crops cleanly to any aspect ratio in @remotion/player.
 */
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="KnowledgeConstellation"
      component={KnowledgeConstellation}
      durationInFrames={240}
      fps={30}
      width={1080}
      height={1080}
    />
  );
};
