import { defineConfig, devices } from "@playwright/test"

// A canary with a default target silently tests the wrong thing forever, so an
// unset base URL is a configuration error rather than something to paper over.
const baseURL = process.env.CANARY_BASE_URL
if (!baseURL) {
  throw new Error(
    "CANARY_BASE_URL is required (e.g. https://rc.learn.mit.edu). " +
      "Canaries never default to a target — see AGENTS.md.",
  )
}

// Which release this run was checking. The pipeline sets it from the deploy
// marker that triggered the run (or that was current when the schedule fired);
// a local run leaves it unset. It is recorded below rather than asserted on —
// nothing here should branch on the release.
const releaseRef = process.env.CANARY_RELEASE_REF || "unknown"

export default defineConfig({
  testDir: "./specs",
  // Lands in results.json as `config.metadata`, which is uploaded on every run
  // — so the retained per-run record says which release it covered, and a flake
  // found later can be attributed to a deploy. Concourse's build page shows the
  // same version as the deploy-marker resource, but the build is reaped long
  // before the bucket is.
  metadata: { releaseRef },
  // Real networks and real logins against a live property, not a local stub.
  timeout: Number(process.env.CANARY_TIMEOUT) || 90_000,
  expect: { timeout: Number(process.env.CANARY_EXPECT_TIMEOUT) || 15_000 },
  // Unconditional, unlike an app suite that only forbids `.only` under CI. A
  // stray `.only` here stops every other journey for that property from being
  // checked at all, and nothing would report the gap.
  forbidOnly: true,
  // One retry: a genuine outage fails twice, a single network blip does not page
  // anyone. More retries would start hiding real intermittent breakage.
  retries: 1,
  // Canaries run against live properties, including production. Serial keeps the
  // load we add predictable and the failure attribution unambiguous.
  workers: 1,
  fullyParallel: false,
  reporter: [
    ["list"],
    ["json", { outputFile: "canary-results/results.json" }],
    ["html", { outputFolder: "canary-results/html", open: "never" }],
  ],
  outputDir: "canary-results/artifacts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  // One project per browser the image ships. `pipeline.py`'s CanaryParams.browsers
  // selects between them with --project, and defaults to chromium alone: every
  // extra browser multiplies the load a canary puts on a live property.
  // A name added there must exist here.
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          // Concourse gives a task container the 64MB default /dev/shm, which
          // is where Chromium puts its renderer shared memory; without this it
          // dies with a bare tab crash partway through a journey. Chromium-only
          // — Firefox and WebKit reject the flag.
          args: ["--disable-dev-shm-usage"],
        },
      },
    },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
})
