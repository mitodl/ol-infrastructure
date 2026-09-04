import { expect, test } from "@playwright/test"

// Deliberately content-free: it asserts the page rendered in a real browser, not
// what it says. Anything keyed to CMS-authored copy pages someone when an editor
// changes a word. See ../../AGENTS.md.
test.describe("MIT Learn homepage", () => {
  test("renders for an anonymous visitor", async ({ page }) => {
    await page.goto("/")
    await expect(page.locator("main")).toBeVisible()
  })
})
