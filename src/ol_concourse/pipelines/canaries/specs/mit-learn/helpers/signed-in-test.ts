import { signedInTest } from "../../shared/signed-in-test"
import { signIn } from "./sign-in"

/**
 * `test` for MIT Learn journeys that need to be signed in. See
 * ../../shared/signed-in-test.ts.
 */
export const test = signedInTest(signIn)

export { expect } from "@playwright/test"
