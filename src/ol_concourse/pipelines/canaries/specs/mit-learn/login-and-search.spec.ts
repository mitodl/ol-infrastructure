import { courseCards, resourceCards } from "./helpers/cards"
import { SEARCH_QUERY } from "./helpers/fixtures"
import { expect, test } from "./helpers/signed-in-test"

// The query itself lives in helpers/fixtures.ts, shared with
// search-direct-url.spec.ts so the property has one content dependency to
// re-check rather than one per journey. Recorded in ../README.md's
// content-dependency table.

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

    // Wait for this query's results, not the last one's. The listing uses
    // keepPreviousData, so while a query fetches the page goes on rendering
    // whatever it showed before, dimmed and inert, and every assertion below
    // would pass against it. Seen signed in on RC, intermittently: a frame
    // taken once the URL had changed still held the unfiltered catalogue,
    // "All (45067)", whose listing happens to include a mathematics course,
    // so the relevance check below passed on results that were not this
    // query's. The visually hidden
    // live region is empty until the fetch settles and then announces the
    // count, so it is the one signal that the results on screen are this
    // query's.
    await expect(page.getByText(/^\d+ results$/)).toBeAttached()

    await expect(page.getByRole("heading", { name: "Search Results" })).toBeVisible()
    await expect(resourceCards(page.getByRole("tabpanel")).first()).toBeVisible()

    // The tab set present but reading "Courses (0)" is what an emptied or
    // half-rebuilt index looks like from a user's seat, so assert the count is
    // non-zero separately from asserting the tab rendered at all.
    const coursesTab = page.getByRole("tab", { name: /^Courses/ })
    await expect(coursesTab).toBeVisible()
    await expect(page.getByRole("tab", { name: /^Courses \([1-9]\d*\)/ })).toBeVisible()
    const coursesCount = (await coursesTab.innerText()).match(/\((\d+)\)/)?.[1]

    await coursesTab.click()
    // The same wait again, keyed to the tab's own count so the announcement
    // left over from the All tab cannot satisfy it.
    await expect(page).toHaveURL(/[?&]resource_type_group=course/)
    await expect(page.getByText(new RegExp(`^${coursesCount} results$`))).toBeAttached()
    // Relevance, not an exact count: any specific number is a false page waiting
    // for the next content sync.
    await expect(
      courseCards(page.getByRole("tabpanel"))
        .filter({ has: page.getByRole("heading", { name: new RegExp(SEARCH_QUERY, "i") }) })
        .first(),
    ).toBeVisible()
  })
})
