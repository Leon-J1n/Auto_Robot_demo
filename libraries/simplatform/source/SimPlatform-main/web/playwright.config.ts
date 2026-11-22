import { defineConfig, devices } from "@playwright/test";

const repositoryRoot = process.env.ROBOT_SIM_ROOT ?? "..";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "desktop-1280", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "mobile-390", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } } },
  ],
  webServer: [
    {
      command: "python -m web.gateway.server --port 8080",
      cwd: repositoryRoot,
      url: "http://127.0.0.1:8080/api/health",
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: "node node_modules/vite/bin/vite.js --config editor/vite.config.ts",
      cwd: ".",
      url: "http://127.0.0.1:5173",
      reuseExistingServer: true,
      timeout: 60_000,
    }
  ]
});
