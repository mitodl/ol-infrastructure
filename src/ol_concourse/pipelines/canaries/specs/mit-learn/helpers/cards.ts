import type { Locator } from "@playwright/test"

/**
 * The learning-resource cards in a listing, whichever card component drew them.
 *
 * mit-learn is mid-way through a card redesign behind the
 * `resource-card-redesign` PostHog flag (mitodl/mit-learn#4170), and a page can
 * show both cards in one visit: the server renders the legacy card, because the
 * flag is unresolved there, and the client swaps in the new one once PostHog
 * answers. The two share almost no markup. The legacy card is an `article` named
 * "Course: {title}" with a `link > heading` title; the new one has no landmark
 * role or name at all, and its title is `heading > link`. Pinning either one
 * makes the canary a race against PostHog — in Concourse against RC the swap
 * usually won, and `getByRole("article")` came back empty on a healthy page.
 *
 * What both keep is the listing itself: every card is a `listitem` with a title
 * heading inside it. That is also what a screen-reader user navigates by, so it
 * is the affordance worth asserting on. The heading filter keeps out any list
 * items that are not cards.
 */
export function resourceCards(listing: Locator): Locator {
  return listing.getByRole("listitem").filter({ has: listing.page().getByRole("heading") })
}

/**
 * The subset of {@link resourceCards} for courses.
 *
 * Both cards print the resource type ("Course") as their first text, so this
 * anchors on that rather than on either card's accessible name.
 */
export function courseCards(listing: Locator): Locator {
  return resourceCards(listing).filter({ hasText: /^Course/ })
}
