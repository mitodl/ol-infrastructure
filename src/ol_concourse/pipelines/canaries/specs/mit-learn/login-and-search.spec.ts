import { expect, test } from "./helpers/signed-in-test"

// Broad enough that a working index cannot plausibly return nothing for it, and
// specific enough that a result matching it means the query was actually applied.
// Recorded in ../README.md's content-dependency table.
const SEARCH_QUERY = "mathematics"

test.describe("MIT Learn signed-in journey", () => {
  test("signs in and reaches a page anonymous visitors cannot", async ({ page }) => {
    await page.goto("/dashboard")

    // Anonymous, this URL redirects to Keycloak, so arriving at the dashboard's
    // own heading is proof of an authenticated session rather than proof that a
    // redirect completed.
    await expect(page).toHaveURL(/\/dashboard/)
    await expect(page.getByRole("heading", { name: "Your MIT Learning Journey" })).toBeVisible()
    await expect(page.getByRole("button", { name: "User Menu" })).toBeVisible()
    await expect(page.getByRole("link", { name: "Log In" })).toHaveCount(0)
  })

  test("searches for courses and gets results relevant to the query", async ({ page }) => {
    await page.goto("/")

    // Retried as a unit because the header search box is in the server-rendered
    // markup before React attaches a handler to it, so a keystroke can land in a
    // box that is not listening yet and is silently dropped. Measured in WebKit;
    // it is a race rather than a browser difference, and Chromium only wins it by
    // being faster — which is the shape of a canary that passes until the day the
    // target is slow, then pages someone at 3am.
    const searchBox = page.getByRole("textbox", { name: "Search for" })
    await expect(async () => {
      await searchBox.fill(SEARCH_QUERY)
      await searchBox.press("Enter")
      await expect(page).toHaveURL(new RegExp(`/search\\?.*q=${SEARCH_QUERY}`), {
        timeout: 5_000,
      })
    }).toPass({ timeout: 30_000 })

    await expect(page.getByRole("heading", { name: "Search Results" })).toBeVisible()
    await expect(page.getByRole("article").first()).toBeVisible()

    // The tab set present but reading "Courses (0)" is what an emptied or
    // half-rebuilt index looks like from a user's seat, so assert the count is
    // non-zero separately from asserting the tab rendered at all.
    const coursesTab = page.getByRole("tab", { name: /^Courses/ })
    await expect(coursesTab).toBeVisible()
    await expect(page.getByRole("tab", { name: /^Courses \([1-9]\d*\)/ })).toBeVisible()

    await coursesTab.click()
    // Relevance, not an exact count: any specific number is a false page waiting
    // for the next content sync.
    await expect(
      page.getByRole("article", { name: new RegExp(`^Course:.*${SEARCH_QUERY}`, "i") }).first(),
    ).toBeVisible()
  })
})
