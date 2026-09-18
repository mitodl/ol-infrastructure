import { expect, test } from "./helpers/signed-in-test"

// NOT ON THE SCHEDULE YET. pipeline.py narrows this property's spec_paths to the
// anonymous journeys, because this one needs an RC account with is_staff on MITx
// Online and nobody has decided to grant one. See ../README.md.
//
// Read-only on purpose: it navigates and never clicks Create, Save or a status
// change. A canary runs every 10 minutes against a shared environment and must
// not write state, and it must never hold superuser, so the discount journeys
// are not here at all.

// The Flexible Pricing list shows learners' names, email addresses and incomes,
// and failure artifacts are published to S3. A screenshot, video or trace (which
// also carries the API responses) of this page would copy that into the bucket,
// so this journey reports through its assertion messages alone.
test.use({ trace: "off", screenshot: "off", video: "off" })

test.describe("MITx Online staff dashboard, signed in as staff", () => {
  test("reaches the dashboard and the Flexible Pricing list", async ({ page }) => {
    await page.goto("/staff-dashboard/")

    // Anonymous, this URL redirects to Keycloak, and a non-staff session is
    // turned away by the app's auth check, so the heading is proof of a staff
    // session rather than of a completed redirect.
    await expect(page.getByRole("heading", { name: "MITx Online Staff Dashboard" })).toBeVisible()

    const flexiblePricing = page.getByRole("menuitem", { name: "Flexible Pricing" })
    await expect(flexiblePricing).toBeVisible()
    await flexiblePricing.click()

    await expect(page).toHaveURL(/\/staff-dashboard\/flexible_pricing/)
    await expect(page.getByRole("heading", { name: "Flexible Pricing Requests" })).toBeVisible()
    // The table rendering at all, not its rows: how many requests RC holds is
    // content, and an empty list is a legitimate state.
    await expect(page.getByRole("table")).toBeVisible()
  })
})
