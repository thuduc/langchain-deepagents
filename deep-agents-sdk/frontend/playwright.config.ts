import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
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
  },
});
