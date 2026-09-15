import { expect, test } from "@playwright/test"

// Presence, not the portal ID: the ID differs per environment (RC/QA vs
// production), so asserting a specific number would be keying the canary on the
// environment. Any js.hs-scripts.com loader means tracking is wired up, which is
// the regression this guards — the script silently vanished when its portal ID
// stopped being set in the deployed env (mitodl/hq#13258).
const HUBSPOT_SCRIPT = 'script[src^="https://js.hs-scripts.com/"]'

test.describe("MIT Learn HubSpot tracking", () => {
  test("loads the HubSpot tracking script for an anonymous visitor", async ({ page }) => {
    await page.goto("/")

    // Next renders this <Script strategy="afterInteractive"> as a preload +
    // flight descriptor server-side and injects the executable tag only after
    // hydration, so the tag is not in the initial HTML. toHaveCount auto-retries
    // until it appears rather than reading a count once and racing hydration.
    await expect(page.locator(HUBSPOT_SCRIPT)).toHaveCount(1)
  })
})
