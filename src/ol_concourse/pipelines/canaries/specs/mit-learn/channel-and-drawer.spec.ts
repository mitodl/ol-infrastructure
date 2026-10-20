import { expect, test } from "@playwright/test"
import { CHANNEL_PATH, CHANNEL_TITLE } from "./helpers/fixtures"

// The most-rendered route on the property: `/c/[channelType]/[name]` at 39,086
// non-bot renders/day in production, ahead of `/search`, and until this spec it
// had no canary at all. Anonymous on purpose — production channel traffic is
// overwhelmingly not signed in, and a signed-in run would exercise a code path
// almost nobody takes to get here. See ../README.md for the trace derivation.

/** Escape a literal for embedding in a RegExp. */
function literal(text: string): RegExp {
  return new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
}

test.describe("MIT Learn channel browse", () => {
  test("renders the channel and its indexed resource listing", async ({ page }) => {
    await page.goto(CHANNEL_PATH)

    // `level: 1` is doing real work. Without it the name also matches the
    // "Search within MIT OpenCourseWare" h2 further down the page and the
    // locator is ambiguous; and `exact: true` would match nothing at all,
    // because the h1 pairs the title text with a logo whose alt repeats it, so
    // its accessible name is the title twice over.
    await expect(page.getByRole("heading", { level: 1, name: CHANNEL_TITLE })).toBeVisible()

    // Scoped to the tabpanel rather than the page, which is the difference
    // between asserting on the search index and asserting on a hand-curated
    // carousel. The page carries both: a "Featured Courses" strip whose
    // contents are configured on the channel, and the tabpanel below it fed by
    // vector_learning_resources_search. Only the second one goes dark when
    // search breaks, and an unscoped getByRole("article") would match the
    // carousel first and pass straight through an empty index.
    const results = page.getByRole("tabpanel")
    await expect(results.getByRole("article").first()).toBeVisible()

    // Non-zero, asserted through the tab's own accessible name so it keeps
    // auto-retrying. "All (0)" is what a channel looks like when the index has
    // been emptied or is half-rebuilt, and it renders with every affordance in
    // place — the tabs, the filters, the layout — which is exactly why the
    // count has to be asserted rather than the tab's presence.
    await expect(page.getByRole("tab", { name: /^All \([1-9]\d*\)/ })).toBeVisible()
  })

  test("opens a resource drawer from a card, and the same link re-enters server-side", async ({
    page,
  }) => {
    await page.goto(CHANNEL_PATH)

    const card = page.getByRole("tabpanel").getByRole("article").first()
    const cardLink = card.getByRole("link").first()
    await expect(cardLink).toBeVisible()
    // Read the resource's name off the card instead of pinning one. Everything
    // below is checked against whatever the index returned first, so this
    // journey has no resource of its own to go stale: a pinned id that is
    // unpublished or reindexed produces a red build about the catalogue rather
    // than about the property, and the trace data shows a large share of
    // production `/c/...` and `/search` renders already carrying exactly that
    // error from stale shared links.
    const resourceName = (await cardLink.innerText()).trim()

    // Retried as a unit: the card and its href are in the server-rendered
    // markup, but opening the drawer is a client-side route change, so a click
    // that lands before React attaches is swallowed and leaves the URL alone.
    // Same hydration race as the header search box in login-and-search.spec.ts
    // — see ../../AGENTS.md.
    await expect(async () => {
      await cardLink.click()
      await expect(page).toHaveURL(/[?&]resource=\d+/, { timeout: 5_000 })
    }).toPass({ timeout: 30_000 })

    const drawer = page.getByRole("dialog")
    await expect(drawer).toBeVisible()
    await expect(drawer.getByRole("heading", { name: literal(resourceName) })).toBeVisible()

    // Re-enter the identical URL as a cold navigation. This is not a repeat of
    // what just happened: the click was client-side routing with the app
    // already loaded, while `?resource=` arriving in a fresh request makes the
    // server fetch the resource inside generateMetadata before anything
    // renders. That is the measured production path — the drawer link is what
    // gets shared and indexed — and it has its own failure mode. When the
    // server-side fetch 404s, Next answers **HTTP 200** with the not-found
    // page: no drawer, and the channel's own content still on screen.
    const deepLink = page.url()
    await page.goto(deepLink)

    // Server-rendered, so this is the half of the assertion that can only pass
    // if generateMetadata resolved the resource. Split from the drawer check
    // below on purpose: together they say whether a failure was the server
    // fetch or the client render, which is the first question triage asks.
    await expect(page).toHaveTitle(literal(resourceName))
    await expect(page.getByRole("dialog").getByRole("heading", { name: literal(resourceName) })).toBeVisible()
  })
})
