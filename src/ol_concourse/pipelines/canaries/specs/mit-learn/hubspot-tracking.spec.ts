import { expect, test } from "@playwright/test"

const HUBSPOT_SCRIPT = 'script[src^="https://js.hs-scripts.com/"]'

test.describe("MIT Learn HubSpot tracking", () => {
  test("loads the HubSpot tracking script for an anonymous visitor", async ({ page }) => {
    await page.goto("/")

    // afterInteractive injects the tag after hydration, not in the initial
    // HTML; toHaveCount auto-retries until it appears.
    await expect(page.locator(HUBSPOT_SCRIPT)).toHaveCount(1)
  })
})
