import { defineConfig, devices } from "@playwright/test";

const frontendOrigin = "https://127.0.0.1:3443";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "real-bff.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: frontendOrigin,
    ignoreHTTPSErrors: true,
    trace: "on-first-retry",
  },
  webServer: {
    command: "npm run start -- --hostname 127.0.0.1 --port 3100",
    url: "http://127.0.0.1:3100/login",
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      NIDS_BACKEND_INTERNAL_URL: "http://127.0.0.1:18090",
      NIDS_FRONTEND_PUBLIC_ORIGIN: frontendOrigin,
    },
  },
});
