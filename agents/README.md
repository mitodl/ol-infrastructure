# agents/

This directory contains resources for AI coding agents working in this repository.

| Directory | Purpose |
|-----------|---------|
| [`skills/`](skills/) | Skills — reusable, step-by-step guides agents load on demand |

**Planned locations** (add as needed):

- `instructions/` — Fragments of agent context/instructions (e.g. project conventions, runbook snippets) that can be composed into agent prompts
- `definitions/` — Custom agent definitions (system prompts, tool configs, persona files)

## Skills quick-reference

Skills are loaded by agents when the task matches the skill description.

### Setup (new clone)

```bash
# Restore external skill packages from the lock file, then register local skills
npx skills experimental_install && npx skills add ./agents/skills --all -y --full-depth
```

### Adding a local skill

```bash
npx skills init agents/skills/<skill-name>   # scaffold SKILL.md
# edit agents/skills/<skill-name>/SKILL.md
npx skills add ./agents/skills/<skill-name> --all -y
```

### Adding an external skill package

```bash
npx skills add <owner>/<repo> --all -y
# commit the updated skills-lock.json
```

See [`skills/README.md`](skills/README.md) for the full skill index.
