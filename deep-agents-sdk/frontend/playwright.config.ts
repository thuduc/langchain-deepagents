import { defineConfig } from "@playwright/test";
import { cpSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(frontendDir, "../..");
const e2eRoot = mkdtempSync(join(tmpdir(), "deep-agents-e2e-"));
const e2eProjects = join(e2eRoot, "projects");

cpSync(join(repositoryRoot, "projects"), e2eProjects, { recursive: true });
writeFileSync(
  join(e2eProjects, "hpi-analytics/data/browser-preview-fixture.png"),
  Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mP8z8Dwn4GBgYGJAQoAHgQCAa2FY3AAAAAASUVORK5CYII=", "base64"),
);
process.env.DEEP_AGENTS_E2E_ROOT = e2eRoot;

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:9020",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../venv/bin/python -m uvicorn server:app --app-dir .. --host 127.0.0.1 --port 9020",
    url: "http://127.0.0.1:9020/api/auth/config",
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      ...process.env,
      PROJECTS_DIR: e2eProjects,
      DEEP_AGENTS_DB_DIR: join(e2eRoot, "databases"),
      DEEP_AGENTS_GENERATED_DIR: join(e2eRoot, "generated"),
      DEEP_AGENTS_DEV_LOGIN_ENABLED: "true",
    },
  },
});
