import { test, type Page } from "@playwright/test"
import { signInThroughOlapps } from "../../shared/olapps-sign-in"

/**
 * Drive the real MITx Online staff login: the dashboard, MITx Online's /login/,
 * then Keycloak.
 *
 * Starts at the staff dashboard rather than the homepage because that is where a
 * staff member starts, and because MITx Online's homepage now redirects to MIT
 * Learn, whose session is not MITx Online's.
 */
export async function signIn(page: Page): Promise<void> {
  await signInThroughOlapps(page, {
    rejectedCredentialMarker: "mitxonline-canary-credential-rejected",
    startLogin: async (page) => {
      // Redirected server-side straight to Keycloak, so the application origin
      // cannot be read off the page afterwards; take it from the project, the
      // one place that decides what a canary targets.
      const applicationOrigin = new URL(test.info().project.use.baseURL!).origin
      await page.goto("/staff-dashboard/")
      return applicationOrigin
    },
  })
}
