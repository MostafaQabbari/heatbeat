// What this file does: browser tests. They start the dev server and replace the API with
// recorded answers (page.route), so they test the portal alone, fast and repeatably.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:5173", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: { command: "npx vite --port 5173 --strictPort", url: "http://127.0.0.1:5173", reuseExistingServer: true },
});
