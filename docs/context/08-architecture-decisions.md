# Architecture Decision Records (ADR)

## What are ADRs?

**Architecture Decision Records** (ADRs) document important architectural decisions along with their context and consequences. They capture the "why" behind decisions, not just the "what."

**Key characteristics:**
- Lightweight markdown documents in `docs/adr/`
- Numbered sequentially (`NNNN-title-with-dashes.md`)
- Immutable once accepted (supersede with new ADRs, don't edit)
- Include: Status, Context, Decision, Consequences

## When to Create an ADR

### ALWAYS create an ADR when you:

1. **Make infrastructure architecture changes:**
   - ✅ Change ingress controllers, service mesh, or routing patterns
   - ✅ Introduce new core technologies (databases, caching, messaging)
   - ✅ Modify deployment strategies or blue-green patterns
   - ✅ Change monitoring, logging, or observability approaches
   - ✅ Alter authentication or security mechanisms

2. **Make decisions affecting multiple systems:**
   - ✅ Changes impacting 5+ applications or stacks
   - ✅ Cross-team coordination required
   - ✅ Breaking changes to existing patterns

3. **Evaluate multiple options:**
   - ✅ You compared 2+ approaches during the session
   - ✅ Trade-offs were analyzed
   - ✅ The decision isn't obvious or standard practice

4. **Create significant technical debt or constraints:**
   - ✅ Decision limits future options
   - ✅ Temporary workarounds that will persist
   - ✅ Compromises made due to time/resource constraints

5. **Spend significant effort (>8 hours):**
   - ✅ Large refactoring or migration projects
   - ✅ Multi-phase implementations
   - ✅ Changes that are difficult or expensive to reverse

### When NOT to Create an ADR

**Skip ADRs for:**
- ❌ Trivial changes following established patterns
- ❌ Bug fixes that don't change architecture
- ❌ Code refactoring without architectural impact
- ❌ Configuration value changes (non-architectural)
- ❌ Single-file or single-function changes
- ❌ Obvious choices with no alternatives
- ❌ Temporary experiments or POCs that won't persist

**Rule of thumb:** If a developer 6 months from now would ask "Why did we do it this way?", create an ADR.

## ADR Creation Process

When you determine an ADR is needed:

### 1. Copy Template

```bash
# Check next sequence number
ls docs/adr/ | grep -E "^[0-9]{4}" | sort -n | tail -1

# Copy template with next number
cp docs/adr/template.md docs/adr/NNNN-your-title.md
```

### 2. Fill in the ADR Sections

**Status:** Always start with "Proposed"

**Context:** Explain:
- The problem statement
- Business/technical drivers
- Constraints and limitations
- Options considered (2+ options)
- Criteria for evaluation

**Decision:** State:
- Chosen option
- Primary rationale
- Why other options were rejected
- Key factors in the decision

**Consequences:** List:
- Positive outcomes (benefits)
- Negative outcomes (trade-offs)
- Neutral outcomes (costs, timeline)
- Risk mitigation strategies

### 3. Reference Analysis

Include in the ADR:
- Links to comparison tables or analysis docs
- Effort estimates and risk assessments
- Timeline for implementation
- Known alternatives or future evolution

### 4. Mark for Review

- Leave status as "Proposed"
- Add note: "Created by AI agent, pending human approval"
- Human reviewer will update to "Accepted" or "Rejected"

## ADR Template Quick Reference

```markdown
# NNNN. {Title}

**Status:** Proposed
**Date:** {YYYY-MM-DD}
**Deciders:** {AI Agent + Human Reviewer}
**Technical Story:** {Link to PR/Issue}

## Context
{Problem statement, drivers, constraints, options considered}

## Decision
{Chosen option and rationale}

## Consequences
{Positive, negative, and neutral outcomes}

## Review History
- Created by AI agent, pending human approval
```

**Full template:** See `docs/adr/template.md`
**ADR Guide:** See `docs/adr/README.md`

## Examples from This Repository

- **ADR-0001:** Use ADR for Architecture Decisions (meta-ADR)
- **ADR-0002:** Migrate to Gateway API HTTPRoute (example from agentic session)

## ADR Best Practices for AI Agents

### DO:
- ✅ Create ADR during the session, not after
- ✅ Document all options you evaluated (not just the chosen one)
- ✅ Be honest about negative consequences and trade-offs
- ✅ Include effort estimates and risk levels
- ✅ Link to related planning docs or analysis
- ✅ Write clearly for future developers (assume they lack your context)
- ✅ Reference specific metrics or criteria used in decision
- ✅ Document assumptions that were made

### DON'T:
- ❌ Mark ADR as "Accepted" (only humans can accept)
- ❌ Edit existing ADRs (create new ADR to supersede)
- ❌ Write ADRs for trivial or standard changes
- ❌ Skip ADRs for architectural decisions (err on side of documenting)
- ❌ Forget to update `docs/adr/README.md` index
- ❌ Use future tense ("will implement") — focus on the decision
- ❌ Include implementation details that belong in design docs

## Integration with Workflow

### When making Pulumi changes:
1. Determine if ADR needed (see criteria above)
2. If yes, create ADR alongside code changes
3. Include ADR in same PR as code changes
4. Human reviews both code and ADR
5. ADR status updated during PR merge

### When exploring options:
- If you create comparison tables or analysis docs during a session
- AND you make a recommendation
- THEN create an ADR documenting the decision

### When migrating or refactoring:
- Multi-phase projects (like Gateway API migration) → Create ADR
- Include link to detailed migration plan
- ADR captures the "why" and high-level approach
- Plan captures the "how" and detailed steps

## ADR Numbering

Check existing ADRs to determine next number:
```bash
ls docs/adr/ | grep -E "^[0-9]{4}" | sort -n | tail -1
# Increment the number for your ADR
```

Current sequence: 0002 (use 0003 for next ADR)

## FAQs

### "Is this architectural?"
If it affects how systems connect, deploy, or operate → Yes, create an ADR

### "Do I need approval?"
ADRs need human review; mark as "Proposed" and let humans accept/reject

### "Can I skip this?"
When in doubt, create ADR. 15 minutes now saves hours of confusion later

### "Where do I learn more?"
Read `docs/adr/README.md` and existing ADRs as examples

## Updating Existing Decisions

When you need to change a previous architectural decision:

1. **Don't edit the original ADR** — ADRs are immutable once accepted
2. **Create a new ADR** that supersedes the old one
3. **Reference the superseded ADR** in the "Related" section
4. **Mark old ADR as "Superseded"** with pointer to new ADR
5. **Explain rationale** for why the decision changed

Example:
```markdown
# 0002. Migrate to Gateway API HTTPRoute

**Status:** Superseded by ADR-0003
**Superseded By:** ADR-0003 (Return to Ingress Controller)
```

## Archiving Decisions

Rejected ADRs are archived but preserved for historical context:
- Status: "Rejected"
- Include explanation of why rejected
- Keep in `docs/adr/` for reference
- May be revisited in future with new information
