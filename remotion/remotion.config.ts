import { Config } from "@remotion/cli/config";

// Transparent renders are not needed — the constellation owns a solid canvas.
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setConcurrency(4);
