import { expect, test } from "@playwright/test"
import { SEARCH_QUERY } from "./helpers/fixtures"

// `/search` is the second most-rendered route in production (21,470 non-bot
// renders/day), and the traces show it is reached overwhelmingly as a URL
// carrying its query parameters — a shared link, a bookmark, a search engine
// result — not by typing into the header box. login-and-search.spec.ts covers
// the header box, signed in; this covers the entry that actually dominates, and
// it is anonymous because that traffic is.
//
// The two are deliberately not merged. They break for different reasons: the
// header-box journey fails when the homepage's search affordance breaks, this
// one fails when the server cannot turn query parameters into results.

test.describe("MIT Learn search by URL", () => {
  test("returns filtered results for a query arriving in the URL", async ({ page }) => {
    // resource_type is carried as well as q, because a bare query would not
    // show whether the facet was read at all. Everything asserted below is
    // about the parameters being honoured, which is the part of this route that
    // a URL entry exercises and a keystroke entry does not.
    await page.goto(`/search?q=${SEARCH_QUERY}&resource_type=course`)

    // The parameters survived the round trip. A redirect that drops them
    // renders a perfectly healthy-looking unfiltered search page.
    await expect(page).toHaveURL(new RegExp(`/search\\?.*q=${SEARCH_QUERY}`))
    await expect(page).toHaveURL(/[?&]resource_type=course/)
    await expect(page.getByRole("heading", { name: "Search Results" })).toBeVisible()

    // Behind this is vector_learning_resources_search with hybrid_search=true,
    // measured from production traces — not the older
    // learning_resources_search. So a green run here is a statement that the
    // hybrid/vector path is up, which is worth knowing when reading a failure.
    await expect(page.getByRole("tab", { name: /^Courses \([1-9]\d*\)/ })).toBeVisible()

    // The filter was applied, not merely accepted. Measured on RC: the same
    // query unfiltered returns All (32) / Courses (21) / Programs (2) /
    // Learning Materials (9); with resource_type=course the two non-course
    // tabs go to zero. A backend that silently ignored the facet would leave
    // them populated, and no other assertion on the page would notice —
    // results would still be present and still look right.
    await expect(page.getByRole("tab", { name: /^Programs \(0\)/ })).toBeVisible()
    await expect(page.getByRole("tab", { name: /^Learning Materials \(0\)/ })).toBeVisible()

    // And the results themselves agree with the facet. Scoped to the tabpanel
    // so it is the search listing being asserted on. Matching on the card's
    // "Course:" name prefix rather than on any course's title keeps this true
    // of every environment and every content sync.
    const results = page.getByRole("tabpanel")
    await expect(results.getByRole("article", { name: /^Course:/ }).first()).toBeVisible()
  })
})
