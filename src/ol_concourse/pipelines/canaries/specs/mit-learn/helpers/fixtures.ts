/**
 * Every piece of live content the mit-learn journeys depend on, in one place.
 *
 * A canary that references content is making a bet on that content still being
 * there, and the cost of losing the bet is a red build that means nothing. So
 * each constant here has to justify itself against one question: **would a
 * failure of the assertion it feeds be a real outage?** If the honest answer is
 * "no, someone could have unpublished it", it does not belong here.
 *
 * Keep the table in ../README.md in step with this file. That table is what a
 * human reads during triage to decide whether a red canary is the property
 * breaking or the catalogue changing.
 */

/**
 * Channel the browse journey runs against — the most-rendered route in
 * production, `/c/[channelType]/[name]`.
 *
 * A **unit** channel, not a topic channel, and that is the whole point. Units
 * are the offerors themselves, so the channel exists as long as MIT Open
 * Learning publishes anything from OCW at all. Topic channels are curated, and
 * measured on RC they are thin enough to be fragile: `/c/topic/cybersecurity`
 * — the one that happens to appear in the production traces this journey was
 * derived from — indexes **4** resources, against **42,342** for this one. A
 * journey pinned to the topic channel would be one retagging away from a red
 * build that tells you nothing about whether MIT Learn is up.
 */
export const CHANNEL_PATH = "/c/unit/ocw"

/** Accessible name of {@link CHANNEL_PATH}'s own page heading. */
export const CHANNEL_TITLE = "MIT OpenCourseWare"

/**
 * Query the search journeys use.
 *
 * Broad enough that a working index cannot plausibly return nothing for it, and
 * specific enough that a result matching it proves the query was applied rather
 * than ignored. Shared by `login-and-search.spec.ts` (entered through the
 * header box, as a signed-in user) and `search-direct-url.spec.ts` (entered as
 * a URL, which is how production traffic actually arrives at `/search`), so
 * that the property has exactly one content dependency to re-check rather than
 * one per journey.
 *
 * MIT's catalogue not containing a single mathematics course is not a realistic
 * content change. An emptied or half-rebuilt search index, which looks
 * identical from a user's seat, is — and is worth paging on.
 */
export const SEARCH_QUERY = "mathematics"
