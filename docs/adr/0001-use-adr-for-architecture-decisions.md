# 0001. Use ADR for Architecture Decisions

**Status:** Accepted
**Date:** 2025-10-29
**Deciders:** Platform Team
**Technical Story:** Agentic coding session analysis of Gateway API migration

## Context

### Current Situation

The ol-infrastructure project contains complex infrastructure-as-code with 467 Python files managing AWS resources, Kubernetes clusters, and 38+ applications. Important architectural decisions have been made over time, but the reasoning behind these decisions is not consistently documented.

### Problem Statement

When team members (or AI agents) need to understand why certain architectural choices were made, they must:
- Search through commit messages and PR discussions
- Ask team members who may have left the project
- Reverse-engineer decisions from code
- Risk repeating mistakes or breaking assumptions

Recent example: During analysis of external-dns domain management, we discovered:
- A code comment from Dec 2024 stating Gateway API support was experimental
- Official documentation showing Gateway API is actually production-ready (v1 APIs)
- No clear record of why `enableGatewayAPI: False` was chosen
- Difficulty determining if this was a permanent decision or temporary constraint

### Business/Technical Drivers

- **Knowledge Preservation:** Capture institutional knowledge as team members change
- **Decision Quality:** Improve decisions through structured thinking and review
- **Agentic Coding:** Enable AI agents to understand and respect architectural decisions
- **Onboarding:** Help new team members understand the "why" behind architecture
- **Avoid Rework:** Prevent revisiting settled questions or repeating mistakes

### Constraints

- Must be lightweight (team won't adopt complex processes)
- Must integrate with existing Git/PR workflow
- Must be searchable and discoverable
- Must work with both human and AI authorship

### Assumptions

- Team values understanding "why" over just "what"
- Markdown in Git is acceptable documentation format
- ADRs will be reviewed during PR process

## Decision

**Adopt Architecture Decision Records (ADRs) using the Michael Nygard template format.**

### Implementation Details

1. **Location:** `docs/adr/` directory in repository
2. **Format:** Markdown files with standard structure (Status, Context, Decision, Consequences)
3. **Naming:** Sequential numbering `NNNN-title-with-dashes.md`
4. **Process:**
   - Create ADR when making significant architectural decisions
   - Include ADR in PR for the related change
   - Review ADR as part of PR review process
   - Mark as "Accepted" when PR merges
5. **Tool Support:** Optional use of adr-tools CLI, but not required
6. **AI Integration:** Agents create ADRs during coding sessions, humans review

### Rationale

**Why ADRs over alternatives:**

- **vs. Wiki:** ADRs live with code, versioned in Git, part of PR workflow
- **vs. Confluence:** Searchable, lightweight, no separate system
- **vs. Code Comments:** More structured, easier to find, captures alternatives
- **vs. Issue Tracker:** Permanent record, not lost when issues close
- **vs. Design Docs:** Lightweight, decision-focused, immutable

**Why Michael Nygard template:**
- Industry standard (most popular ADR format)
- Simple (4 sections: Status, Context, Decision, Consequences)
- Flexible (can be extended as needed)
- Well-documented with many examples

## Consequences

### Positive Consequences

- **Knowledge Capture:** Preserve reasoning for future developers
- **Better Decisions:** Structured thinking forces consideration of alternatives
- **Reduced Debates:** Settled decisions are documented, no need to rehash
- **Onboarding:** New team members can understand architecture faster
- **AI-Friendly:** Agents can read ADRs to understand constraints and patterns
- **Searchable:** Git history + text search makes decisions discoverable

### Negative Consequences

- **Overhead:** Requires time to write ADRs (estimated 30-60 minutes each)
- **Discipline Required:** Team must remember to create ADRs
- **Maintenance:** ADR index needs updating
- **Storage:** More files in repository (minimal impact)

### Neutral Consequences

- **Process Change:** Team needs to learn ADR format and when to use it
- **Backfill Needed:** Existing major decisions not documented (optional to backfill)
- **Review Burden:** PRs with ADRs may take longer to review (but better quality)

## Implementation Notes

- **Effort Estimate:** 1 hour to set up directory and templates
- **Risk Level:** Low (documentation-only change)
- **Dependencies:** None (optional adr-tools CLI for convenience)
- **Migration Path:** N/A (net new process)

### Initial ADRs to Create

1. ✅ This ADR (use ADR for architecture decisions)
2. Gateway API migration decision
3. (Backfill others as discovered or as new decisions arise)

## Related Decisions

- No prior ADRs (this is the first)
- Related to Gateway API Migration Plan (docs/gateway-api-migration-plan.md)

## References

- [ADR GitHub Organization](https://adr.github.io/)
- [Michael Nygard's Original Article](https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [Architecture Decision Records: A Primer](https://github.com/joelparkerhenderson/architecture-decision-record)
- [AWS Prescriptive Guidance on ADRs](https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/welcome.html)

## Notes

This ADR was created during an agentic coding session that analyzed the Gateway API migration decision. The need for ADRs became apparent when historical decisions were difficult to trace.

**When to create an ADR** (see docs/adr/README.md for full criteria):
- Infrastructure pattern changes (e.g., ingress controller migration)
- New core technologies or platforms
- Decisions affecting multiple apps or teams
- Decisions requiring significant effort (>8 hours) to implement or reverse
- Decisions that will affect future developers' understanding

**When NOT to create an ADR:**
- Trivial or standard practice changes
- Temporary workarounds or experiments
- Fully reversible decisions (<1 hour to undo)
- Implementation details that don't affect architecture

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2025-10-29 | GitHub Copilot | Proposed | Created during agentic session |
| _TBD_ | _Human Reviewer_ | _Pending_ | Awaiting human approval |

**Last Updated:** 2025-10-29
