import { test as base, type BrowserContextOptions, type Page } from "@playwright/test"

type SignedInWorkerFixtures = {
  /** Session established once per worker, reused by every test in it. */
  canarySession: BrowserContextOptions["storageState"]
}

/**
 * Build the `test` a property's signed-in journeys import.
 *
 * The ordinary `page` fixture of the returned `test` arrives authenticated, so a
 * second journey reuses the login rather than re-implementing or re-running it.
 * Anonymous journeys keep importing `@playwright/test` directly.
 *
 * The session is held in memory and never written to disk: a storageState file
 * carries live tokens, and this project's failure artifacts get published.
 *
 * @param signIn The property's sign-in, driven on a fresh page.
 */
export function signedInTest(signIn: (page: Page) => Promise<void>) {
  return base.extend<{}, SignedInWorkerFixtures>({
    canarySession: [
      async ({ browser }, use) => {
        // A context built straight off `browser` inherits nothing from the
        // project's `use`, so the config's required base URL has to be handed
        // over explicitly or every `page.goto("/")` below is an invalid URL. Read
        // from the project rather than the environment to keep
        // playwright.config.ts the one place that decides what a canary targets.
        const context = await browser.newContext({
          baseURL: base.info().project.use.baseURL,
        })
        // Deliberately untraced, unlike the fixture-provided page: a trace of the
        // login screens would embed the canary account's address, and traces are
        // published as build artifacts. signIn() reports the cause in its message.
        const page = await context.newPage()
        await signIn(page)
        const session = await context.storageState()
        await context.close()
        await use(session)
      },
      { scope: "worker" },
    ],

    storageState: ({ canarySession }, use) => use(canarySession),
  })
}
