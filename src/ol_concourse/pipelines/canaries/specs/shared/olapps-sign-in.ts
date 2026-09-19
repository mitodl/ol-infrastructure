import { type Page } from "@playwright/test"
import { existsSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

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
        "falls back to a default credential — see ../../AGENTS.md.",
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

export type OlappsSignInOptions = {
  /**
   * File name, under tmpdir, recording that Keycloak rejected the credential in
   * this run. One per property so each keeps its own name for the refusal.
   */
  rejectedCredentialMarker: string
  /**
   * Take the page from the application to the start of its login, and return
   * the application origin Keycloak is expected to hand the browser back to.
   * Step one of the journey belongs to the property: a way in that has
   * disappeared is a user-facing outage a direct hop to the IdP would miss.
   */
  startLogin: (page: Page) => Promise<string>
}

/**
 * Drive the real identity-first login on the `olapps` realm and return to the
 * application.
 *
 * Fails with the actual cause rather than the visible symptom. The realm's flow
 * is identity-first, so an account it does not recognise is never answered with
 * a login error — the browser is simply sent somewhere else. See the login flow
 * and lockout notes in ../mit-learn/README.md; they are properties of the realm,
 * not of any one property's canary.
 */
export async function signInThroughOlapps(
  page: Page,
  { rejectedCredentialMarker, startLogin }: OlappsSignInOptions,
): Promise<void> {
  // Realm `olapps` is permanentLockout=true, failureFactor=10, with a 12h counter
  // reset — so a credential that has drifted between Keycloak and Vault does not
  // settle into a harmless recurring failure, it disables the account for good in
  // about two hours at a 10-minute cadence. Playwright starts a fresh worker for a
  // retry, so refusing the second attempt has to be recorded somewhere outside the
  // process. Not under canary-results/, which is published as build artifacts;
  // tmpdir is per-container, so the refusal covers the run and nothing beyond it.
  const markerPath = join(tmpdir(), rejectedCredentialMarker)
  if (existsSync(markerPath)) {
    throw new Error(
      "Refusing to re-submit a credential Keycloak already rejected in this run. " +
        "Ten consecutive failures lock the canary account, and the next ten disable " +
        "it permanently. Fix the credential in Keycloak and SOPS together.",
    )
  }
  const { email, password } = canaryCredentials()

  const applicationOrigin = await startLogin(page)
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
      writeFileSync(markerPath, rejection)
      throw new Error(
        `Keycloak rejected the canary credential: "${rejection}". Not retrying — ` +
          "see the lockout note in specs/mit-learn/README.md. Rotation must change " +
          "Keycloak and the SOPS/Vault value together; the gap between the two is " +
          "itself enough to disable the account.",
      )
    }
    throw error
  }
}
