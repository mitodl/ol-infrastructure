# Skills

Skills give agents step-by-step, repo-specific guidance for common tasks. They
are loaded on demand — an agent reads the skill description and decides whether
it is relevant to the current task.

`agents/skills/` is the source of truth for **locally-authored** skills.
External skills (from `pulumi/agent-skills` and other packages) are fetched on
setup and pinned in `skills-lock.json` at the repo root. Agent-specific
directories (`.claude/skills/`, `.copilot/skills/`) contain symlinks that point
into the npx-managed `.agents/skills/` installation directory.

## Local skills

| Skill | Description |
|-------|-------------|
| [new-pulumi-project](new-pulumi-project/SKILL.md) | Scaffold a new Pulumi infrastructure, application, or substructure project |

## External skills (pinned in skills-lock.json)

| Skill | Source |
|-------|--------|
| `pulumi-best-practices` | `pulumi/agent-skills` |
| `pulumi-component` | `pulumi/agent-skills` |
| `pulumi-automation-api` | `pulumi/agent-skills` |
| `pulumi-terraform-to-pulumi` | `pulumi/agent-skills` |
| `cloudformation-to-pulumi` | `pulumi/agent-skills` |
| `pulumi-cdk-to-pulumi` | `pulumi/agent-skills` |
| `pulumi-arm-to-pulumi` | `pulumi/agent-skills` |
| `pulumi-esc` | `pulumi/agent-skills` |

## Adding a local skill

```bash
# 1. Scaffold
npx skills init agents/skills/<skill-name>

# 2. Edit agents/skills/<skill-name>/SKILL.md

# 3. Register with all agents
npx skills add ./agents/skills/<skill-name> --all -y
```

The `SKILL.md` frontmatter controls how agents discover the skill:

```yaml
---
name: <skill-name>
description: >
  One or two sentences. Agents use this to decide whether to load
  the skill — make it specific and actionable.
---
```

## Notes

- `.agents/skills/` is the npx-managed installation directory and is gitignored.
  Do not author skills there directly.
- `skills/` at the repo root is a generated symlink view and is gitignored.
- Skills run with full agent permissions; review content before committing.
