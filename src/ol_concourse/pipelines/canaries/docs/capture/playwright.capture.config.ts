import { defineConfig, devices } from "@playwright/test"

// Regenerates the screenshots in ../images/ for ../journeys.md. Not a canary:
// the scheduled pipeline runs `playwright test specs/<property>` against the
// root config and never loads this file. Kept separate on purpose — a canary
// that wrote a full screenshot set on every green run would be a different cost
// and a different artifact story.
const baseURL = process.env.CANARY_BASE_URL
if (!baseURL) {
  throw new Error("CANARY_BASE_URL is required (e.g. https://rc.learn.mit.edu).")
}

export default defineConfig({
  testDir: ".",
  testMatch: "*.capture.ts",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  forbidOnly: true,
  // No retries. A capture run is a real login against the same account the
  // pipeline uses, and a second attempt at a refused credential spends lockout
  // budget for nothing.
  retries: 0,
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  // Outside canary-results/ and never traced: a trace of the login screens would
  // carry the canary address unmasked.
  outputDir: "../../canary-results/capture",
  use: {
    baseURL,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 800 },
        launchOptions: { args: ["--disable-dev-shm-usage"] },
      },
    },
  ],
})
