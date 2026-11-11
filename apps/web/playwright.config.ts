import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

// The browser test runs the REAL stack (API, worker, FFmpeg, Remotion) with the TEST adapters
// replacing OpenRouter and Whisper, on its own ports and in a throw-away data directory.
const dataDir = process.env.CLIPFACTORY_E2E_DATA ?? mkdtempSync(path.join(tmpdir(), "clipfactory-e2e-"));
process.env.CLIPFACTORY_E2E_DATA = dataDir;

export default defineConfig({
  testDir: "./e2e",
  timeout: 300_000,
  expect: { timeout: 20_000 },
  use: { baseURL: "http://localhost:3100", viewport: { width: 1440, height: 900 }, actionTimeout: 15_000 },
  webServer: {
    command: "../../scripts/dev.sh",
    url: "http://localhost:3100",
    timeout: 180_000,
    reuseExistingServer: false,
    env: {
      CLIPFACTORY_DATA_DIR: dataDir,
      CLIPFACTORY_AI_ADAPTER: "fake",
      CLIPFACTORY_TRANSCRIBER: "fake",
      CLIPFACTORY_API_PORT: "8100",
      PORT: "3100",
      NEXT_DIST_DIR: ".next-e2e",
    },
  },
});
