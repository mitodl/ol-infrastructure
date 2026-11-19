import type { Locator } from "@playwright/test"

/**
 * The resource cards in a listing, in either card design. mit-learn renders a
 * redesigned card when the PostHog flag `resource-card-redesign` is on for the
 * visitor (mitodl/mit-learn#4170). The legacy card is an `article` named
 * "<category>: <title>"; the redesigned one has no role or name of its own.
 * Both sit in a list item and title the resource with a heading, so a card here
 * is a list item holding a heading. The heading also leaves out loading
 * skeletons and pagination, which share the listing's list items.
 */
export function resourceCards(listing: Locator): Locator {
  return listing.getByRole("listitem").filter({ has: listing.page().getByRole("heading") })
}

/**
 * The cards in a listing that name `type` ("Course", "Program", ...) as their
 * resource type. Both designs show the type as a text element of its own.
 */
export function resourceCardsOfType(listing: Locator, type: string): Locator {
  return resourceCards(listing).filter({
    has: listing.page().getByText(type, { exact: true }),
  })
}
