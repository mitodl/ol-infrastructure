import { expect, test, type Locator, type Page } from "@playwright/test"
import { mkdirSync } from "node:fs"
import { dirname, join } from "node:path"
import {
  CHANNEL_PATH,
  CHANNEL_TITLE,
  SEARCH_QUERY,
} from "../../specs/mit-learn/helpers/fixtures"
import { signIn } from "../../specs/mit-learn/helpers/sign-in"

// Walks each mit-learn journey step for step and saves a screenshot at every
// boundary ../journeys.md describes. It mirrors the specs rather than running
// them, so that the specs stay free of documentation hooks; the price is that a
// spec change needs the matching change here. Every spec assertion is repeated,
// so a capture of a broken page fails instead of committing a picture of it.
// The login itself is not mirrored: it is sign-in.ts's own signIn(), with its
// lockout guard intact, and a hook that screenshots each Keycloak screen.

const IMAGES = join(__dirname, "..", "images", "mit-learn")

async function shot(page: Page, file: string, mask: Locator[] = []): Promise<void> {
  const path = join(IMAGES, `${file}.jpg`)
  mkdirSync(dirname(path), { recursive: true })
  // Viewport only, not fullPage, and JPEG rather than PNG: the step being
  // described is what is on screen, and these are binaries entering git. PNG
  // ran to ~400KB a frame on the image-heavy pages; this is a tenth of that.
  await page.screenshot({ path, mask, type: "jpeg", quality: 60, animations: "disabled" })
}

/** Escape a literal for embedding in a RegExp. */
function literal(text: string): RegExp {
  return new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
}

test("homepage", async ({ page }) => {
  await page.goto("/")
  await expect(page.locator("main")).toBeVisible()
  await shot(page, "homepage/01-homepage")
})

test("channel listing", async ({ page }) => {
  await page.goto(CHANNEL_PATH)
  await expect(page.getByRole("heading", { level: 1, name: CHANNEL_TITLE })).toBeVisible()
  await shot(page, "channel-and-drawer/01-channel-heading")

  const results = page.getByRole("tabpanel")
  await expect(results.getByRole("article").first()).toBeVisible()
  await expect(page.getByRole("tab", { name: /^All \([1-9]\d*\)/ })).toBeVisible()
  await page.getByRole("tab", { name: /^All \(/ }).scrollIntoViewIfNeeded()
  await shot(page, "channel-and-drawer/02-indexed-listing")
})

test("resource drawer", async ({ page }) => {
  await page.goto(CHANNEL_PATH)
  const cardLink = page.getByRole("tabpanel").getByRole("article").first().getByRole("link").first()
  await expect(cardLink).toBeVisible()
  const resourceName = (await cardLink.innerText()).trim()

  await expect(async () => {
    await cardLink.click()
    await expect(page).toHaveURL(/[?&]resource=\d+/, { timeout: 5_000 })
  }).toPass({ timeout: 30_000 })
  const drawer = page.getByRole("dialog")
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole("heading", { name: literal(resourceName) })).toBeVisible()
  await shot(page, "channel-and-drawer/03-drawer-client-side")

  await page.goto(page.url())
  await expect(page).toHaveTitle(literal(resourceName))
  await expect(page.getByRole("dialog").getByRole("heading", { name: literal(resourceName) })).toBeVisible()
  await shot(page, "channel-and-drawer/04-drawer-cold-load")
})

test("search by URL", async ({ page }) => {
  await page.goto(`/search?q=${SEARCH_QUERY}&resource_type=course`)
  await expect(page).toHaveURL(new RegExp(`/search\\?.*q=${SEARCH_QUERY}`))
  await expect(page).toHaveURL(/[?&]resource_type=course/)
  await expect(page.getByRole("heading", { name: "Search Results" })).toBeVisible()
  await expect(page.getByRole("tab", { name: /^Courses \([1-9]\d*\)/ })).toBeVisible()
  await expect(page.getByRole("tab", { name: /^Programs \(0\)/ })).toBeVisible()
  await expect(page.getByRole("tab", { name: /^Learning Materials \(0\)/ })).toBeVisible()
  await expect(page.getByRole("tabpanel").getByRole("article", { name: /^Course:/ }).first()).toBeVisible()
  await shot(page, "search-direct-url/01-filtered-results")
})

// Step 1 of the signed-in journey is anonymous, so it is captured on its own and
// a re-capture of it needs no credential. Waiting for the link rather than for
// `main` alone is what gets a hydrated header into the frame: the link is not
// in the server-rendered markup.
test("login entry point", async ({ page }) => {
  await page.goto("/")
  await expect(page.locator("main")).toBeVisible()
  await expect(page.getByRole("link", { name: "Log In" })).toBeVisible()
  await shot(page, "login-and-search/01-homepage-log-in-link")
})

test("signed in: login, dashboard, header search", async ({ page }) => {
  // No retries in this config, and signIn() keeps its own rejected-credential
  // marker, so a refused password is submitted once and never again.
  await signIn(page, (screen, sensitive) =>
    shot(page, `login-and-search/0${screen === "email" ? 2 : 3}-keycloak-${screen}`, sensitive),
  )

  await page.goto("/dashboard")
  await expect(page).toHaveURL(/\/dashboard/)
  await expect(page.getByRole("heading", { name: "Your MIT Learning Journey" })).toBeVisible()
  await expect(page.getByRole("button", { name: "User Menu" })).toBeVisible()
  await expect(page.getByRole("link", { name: "Log In" })).toHaveCount(0)
  await shot(page, "login-and-search/04-dashboard")

  await page.goto("/")
  const searchBox = page.getByRole("textbox", { name: "Search for" })
  await expect(async () => {
    await searchBox.fill(SEARCH_QUERY)
    await searchBox.press("Enter")
    await expect(page).toHaveURL(new RegExp(`/search\\?.*q=${SEARCH_QUERY}`), { timeout: 5_000 })
  }).toPass({ timeout: 30_000 })
  await expect(page.getByRole("heading", { name: "Search Results" })).toBeVisible()
  await expect(page.getByRole("article").first()).toBeVisible()
  const coursesTab = page.getByRole("tab", { name: /^Courses/ })
  await expect(coursesTab).toBeVisible()
  await expect(page.getByRole("tab", { name: /^Courses \([1-9]\d*\)/ })).toBeVisible()
  await shot(page, "login-and-search/05-header-search-results")

  await coursesTab.click()
  await expect(
    page.getByRole("article", { name: new RegExp(`^Course:.*${SEARCH_QUERY}`, "i") }).first(),
  ).toBeVisible()
  await shot(page, "login-and-search/06-courses-tab")
})
