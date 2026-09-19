import { expect, test, type Page } from "@playwright/test"

// The dashboard's two ways of sending a visitor to sign in both end at the
// Keycloak email screen with MITx Online's own /login/ as the return address.
// Checking that return address is what makes this a MITx Online sign-in and not
// merely some page on the IdP.
async function expectMitxOnlineSignIn(page: Page, applicationOrigin: string) {
  await expect(page).toHaveURL(
    (url) =>
      url.origin !== applicationOrigin &&
      url.pathname.endsWith("/protocol/openid-connect/auth") &&
      (url.searchParams.get("redirect_uri") ?? "").startsWith(`${applicationOrigin}/login/`),
  )
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible()
}

test.describe("MITx Online staff dashboard, anonymous", () => {
  test("sends a visitor at the dashboard root to sign in", async ({ page, baseURL }) => {
    const applicationOrigin = new URL(baseURL!).origin
    // Redirected server-side by mitxonline's staff-dashboard-login view, so this
    // passes even when the dashboard's JavaScript does not load. The next journey
    // is the one that needs the bundle.
    await page.goto("/staff-dashboard/")
    await expectMitxOnlineSignIn(page, applicationOrigin)
  })

  test("loads the dashboard app, which sends a visitor to sign in", async ({ page, baseURL }) => {
    const applicationOrigin = new URL(baseURL!).origin
    // A deep route is served the app shell, and only the app's own auth check
    // -- the profile request below -- sends an anonymous visitor onward. A
    // bundle that fails to load or crashes before routing leaves the browser on
    // a blank page here, which is the regression this journey exists to catch.
    // Bounded so a missing request reports as itself rather than as a whole-test
    // timeout. Measured: with the bundle's scripts blocked, this is what fails.
    const profileRequest = page.waitForRequest(
      (request) => new URL(request.url()).pathname.startsWith("/api/v0/users/me"),
      { timeout: 30_000 },
    )
    await page.goto("/staff-dashboard/flexible_pricing")
    await profileRequest
    await expectMitxOnlineSignIn(page, applicationOrigin)
  })
})
