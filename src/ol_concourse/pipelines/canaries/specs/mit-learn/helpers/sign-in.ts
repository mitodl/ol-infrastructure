import { expect, type Page } from "@playwright/test"
import { existsSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

// Realm `olapps` is permanentLockout=true, failureFactor=10, with a 12h counter
// reset — so a credential that has drifted between Keycloak and Vault does not
// settle into a harmless recurring failure, it disables the account for good in
// about two hours at a 10-minute cadence. Playwright starts a fresh worker for a
// retry, so refusing the second attempt has to be recorded somewhere outside the
// process. Not under canary-results/, which is published as build artifacts;
// tmpdir is per-container, so the refusal covers the run and nothing beyond it.
const REJECTED_CREDENTIAL_MARKER = join(tmpdir(), "mit-learn-canary-credential-rejected")

// Bounded so a rejected credential surfaces as itself. Left to the 90s test
// timeout it would instead report as "test timeout", and the diagnosis below
// would never run.
const REDIRECT_TIMEOUT = 30_000

function canaryCredentials(): { email: string; password: string } {
  const email = process.env.CANARY_USER_EMAIL
  const password = process.env.CANARY_USER_PASSWORD
  if (!email || !password) {
    throw new Error(
      "CANARY_USER_EMAIL and CANARY_USER_PASSWORD are required. The pipeline " +
        "sources them from Vault; locally, export them from SOPS. A canary never " +
        "falls back to a default credential — see ../../../AGENTS.md.",
    )
  }
  return { email, password }
}

// The realm ships a custom theme, so Keycloak's stock alert markup is not there
// to key on — a refusal is ordinary visible text, which is also exactly what the
// user is shown. Listed as whole phrases rather than as loose keywords so that
// unrelated page copy cannot be read as a rejection and stop the canary.
const REJECTION_MESSAGE =
  /invalid username or password|invalid user credentials|account is (temporarily )?disabled|account is locked/i

async function rejectionMessage(page: Page): Promise<string | null> {
  const message = page.getByText(REJECTION_MESSAGE).first()
  if (!(await message.isVisible().catch(() => false))) {
    return null
  }
  return (await message.innerText()).trim()
}

/**
 * Drive the real MIT Learn login, from the homepage through Keycloak.
 *
 * Fails with the actual cause rather than the visible symptom. The realm's flow
 * is identity-first, so an account it does not recognise is never answered with
 * a login error — the browser is simply sent somewhere else. See ../README.md.
 */
export async function signIn(page: Page): Promise<void> {
  if (existsSync(REJECTED_CREDENTIAL_MARKER)) {
    throw new Error(
      "Refusing to re-submit a credential Keycloak already rejected in this run. " +
        "Ten consecutive failures lock the canary account, and the next ten disable " +
        "it permanently. Fix the credential in Keycloak and SOPS together.",
    )
  }
  const { email, password } = canaryCredentials()

  await page.goto("/")
  // Step one of the journey, and the reason login starts here rather than at a
  // deep link: a homepage that no longer offers a way in is a user-facing outage
  // that a direct hop to the IdP would sail straight past.
  await expect(page.locator("main")).toBeVisible()
  const applicationOrigin = new URL(page.url()).origin

  await page.getByRole("link", { name: "Log In" }).click()
  await page.waitForURL((url) => url.origin !== applicationOrigin, {
    timeout: REDIRECT_TIMEOUT,
  })

  const identityProvider = new URL(page.url()).origin
  const emailScreen = page.url()
  await page.getByLabel("Email", { exact: true }).fill(email)
  await page.getByRole("button", { name: "Next", exact: true }).click()
  await page.waitForURL((url) => url.href !== emailScreen, { timeout: REDIRECT_TIMEOUT })

  // Assert the local password screen positively, rather than testing for the
  // known wrong destinations. An unrecognised account goes to whichever of them
  // fits the address — measured: an @mit.edu one is handed off to Touchstone,
  // any other domain gets the captcha'd signup form — and both mean the account
  // is gone, not that login has grown a captcha or an SSO requirement. Guessing
  // wrong here is not just a bad message: it would type the canary's password
  // into whatever page happened to be showing.
  const reached = new URL(page.url())
  const onPasswordScreen =
    reached.origin === identityProvider &&
    reached.pathname.endsWith("/login-actions/authenticate")
  if (!onPasswordScreen) {
    throw new Error(
      `After submitting the canary address, the flow reached ${reached.origin}` +
        `${reached.pathname} instead of the password screen, so the account does ` +
        "not exist in this realm. This is an account problem — not a captcha " +
        "problem and not an SSO problem, whichever of those the page says.",
    )
  }

  await page.getByLabel("Password", { exact: true }).fill(password)
  await page.getByRole("button", { name: "Next", exact: true }).click()
  try {
    await page.waitForURL((url) => url.origin === applicationOrigin, {
      timeout: REDIRECT_TIMEOUT,
    })
  } catch (error) {
    const rejection = await rejectionMessage(page)
    if (rejection) {
      writeFileSync(REJECTED_CREDENTIAL_MARKER, rejection)
      throw new Error(
        `Keycloak rejected the canary credential: "${rejection}". Not retrying — ` +
          "see the lockout note in ../README.md. Rotation must change Keycloak and " +
          "the SOPS/Vault value together; the gap between the two is itself enough " +
          "to disable the account.",
      )
    }
    throw error
  }

  if (new URL(page.url()).pathname.startsWith("/onboarding")) {
    throw new Error(
      "Login landed on onboarding, which only a canary account that has never " +
        "completed it does. Log the account in by hand once, then re-run.",
    )
  }
}
