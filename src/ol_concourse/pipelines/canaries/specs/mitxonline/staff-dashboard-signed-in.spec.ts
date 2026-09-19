import { expect, test } from "./helpers/signed-in-test"

// Runs as the mit-learn canary account, which holds is_staff (not superuser) on
// RC MITx Online. See ../README.md for why it is shared and what that costs.
//
// Read-only on purpose: it navigates and never clicks Create, Save or a status
// change. A canary runs every 10 minutes against a shared environment and must
// not write state, and it must never hold superuser, so the discount journeys
// are not here at all.

// The Flexible Pricing list shows learners' names, email addresses and incomes,
// and failure artifacts are published to S3. Turning off trace, screenshot and
// video is not enough on its own: on any failure Playwright 1.63 also writes an
// ARIA snapshot of the page into error-context.md, from two places. A failed
// web-first assertion carries one in its matcher result, which `step` below
// drops by rethrowing a plain error, and the page fixture's teardown takes one
// unless PLAYWRIGHT_NO_COPY_PROMPT is set, which the hooks below scope to this
// file. Measured with a sentinel learner row: without both, the row is in
// error-context.md; with both, it is in no file under canary-results/.
test.use({ trace: "off", screenshot: "off", video: "off" })

let copyPromptSetting: string | undefined
test.beforeAll(() => {
  copyPromptSetting = process.env.PLAYWRIGHT_NO_COPY_PROMPT
  process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1"
})
test.afterAll(() => {
  if (copyPromptSetting === undefined) {
    delete process.env.PLAYWRIGHT_NO_COPY_PROMPT
  } else {
    process.env.PLAYWRIGHT_NO_COPY_PROMPT = copyPromptSetting
  }
})

/**
 * Run an assertion and, if it fails, report only which step failed and the
 * matcher's first line. The full message can quote matched elements (e.g. a
 * strict-mode violation lists them), and the original error carries the page
 * snapshot described above.
 */
async function step(name: string, assertion: () => Promise<void>): Promise<void> {
  try {
    await assertion()
  } catch (error) {
    const summary = error instanceof Error ? error.message.split("\n")[0] : String(error)
    throw new Error(`${name}: ${summary}`)
  }
}

test.describe("MITx Online staff dashboard, signed in as staff", () => {
  test("reaches the dashboard and the Flexible Pricing list", async ({ page }) => {
    await page.goto("/staff-dashboard/")

    // Anonymous, this URL redirects to Keycloak, and a non-staff session is
    // turned away by the app's auth check, so the heading is proof of a staff
    // session rather than of a completed redirect.
    await step("dashboard heading", () =>
      expect(page.getByRole("heading", { name: "MITx Online Staff Dashboard" })).toBeVisible(),
    )

    const flexiblePricing = page.getByRole("menuitem", { name: "Flexible Pricing" })
    await step("Flexible Pricing menu item", () => expect(flexiblePricing).toBeVisible())
    await flexiblePricing.click()

    await step("Flexible Pricing URL", () =>
      expect(page).toHaveURL(/\/staff-dashboard\/flexible_pricing/),
    )
    await step("Flexible Pricing heading", () =>
      expect(page.getByRole("heading", { name: "Flexible Pricing Requests" })).toBeVisible(),
    )
    // The table rendering at all, not its rows: how many requests RC holds is
    // content, and an empty list is a legitimate state.
    await step("Flexible Pricing table", () => expect(page.getByRole("table")).toBeVisible())
  })
})
