import { expect, type Page } from "@playwright/test"
import { signInThroughOlapps } from "../../shared/olapps-sign-in"

/**
 * Drive the real MIT Learn login, from the homepage through Keycloak.
 *
 * The Keycloak screens are shared with every property on the `olapps` realm;
 * see ../../shared/olapps-sign-in.ts and the login flow notes in ../README.md.
 */
export async function signIn(page: Page): Promise<void> {
  await signInThroughOlapps(page, {
    rejectedCredentialMarker: "mit-learn-canary-credential-rejected",
    startLogin: async (page) => {
      await page.goto("/")
      // Step one of the journey, and the reason login starts here rather than at
      // a deep link: a homepage that no longer offers a way in is a user-facing
      // outage that a direct hop to the IdP would sail straight past.
      await expect(page.locator("main")).toBeVisible()
      const applicationOrigin = new URL(page.url()).origin
      await page.getByRole("link", { name: "Log In" }).click()
      return applicationOrigin
    },
  })

  if (new URL(page.url()).pathname.startsWith("/onboarding")) {
    throw new Error(
      "Login landed on onboarding, which only a canary account that has never " +
        "completed it does. Log the account in by hand once, then re-run.",
    )
  }
}
