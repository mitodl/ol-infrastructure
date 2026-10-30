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
 * makes the canary a race against PostHog — on RC the swap won consistently,
 * and every `getByRole("article")` came back empty on a perfectly healthy page.
 *
 * What both keep is the listing itself: every card is a `listitem` with a title
 * heading inside it. That is also what a screen-reader user navigates by, so it
 * is the affordance worth asserting on. The heading filter keeps out list items
 * that are not cards, such as pagination.
 */
export function resourceCards(listing: Locator): Locator {
  return listing.getByRole("listitem").filter({ has: listing.page().getByRole("heading") })
}

/**
 * The subset of {@link resourceCards} for courses.
 *
 * Both cards print the resource type as their first text — "Course" above the
 * title — so this anchors on that rather than on either card's accessible name.
 */
export function courseCards(listing: Locator): Locator {
  return resourceCards(listing).filter({ hasText: /^Course/ })
}
