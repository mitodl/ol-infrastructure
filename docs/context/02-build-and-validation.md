# Build and Validation Commands

## Environment Setup (One-Time)

```bash
# Install dependencies (ALWAYS run first after cloning)
uv sync

# Login to Pulumi state backend (required for Pulumi operations)
pulumi login s3://mitol-pulumi-state
```

## Linting & Formatting (Fast: <1 second)

```bash
# Format code (auto-fixes formatting issues)
uv run ruff format src/

# Check code style and quality (completes in <0.1s)
uv run ruff check src/

# Check code with auto-fixes
uv run ruff check --fix src/

# Format Packer HCL files
packer fmt -recursive src/bilder/
```

## Type Checking (Slow: ~75 seconds)

```bash
# Run mypy type checking (this is SLOW, budget 75+ seconds)
uv run mypy src/
```

**Note:** Expect ~1316 type errors and ~809 ruff errors in the codebase. Your changes should not introduce NEW errors.

## Pre-commit Hooks

```bash
# Run all pre-commit hooks (includes ruff, mypy, shellcheck, yamllint, etc.)
# Note: hadolint may fail on existing Docker issues—ignore if unrelated to your changes
uv run pre-commit run --all-files
```

## Testing

```bash
# Run all tests (currently minimal test coverage)
uv run pytest tests/

# Run tests with verbose output
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py

# Run tests with coverage report
uv run pytest tests/ --cov=src/ol_infrastructure --cov-report=term-missing
```

## Validation Checklist

Before submitting changes:

1. ✅ `uv sync` — Ensure dependencies are installed
2. ✅ `uv run ruff format src/` — Auto-format code
3. ✅ `uv run ruff check src/` — Check for new linting errors
4. ✅ `uv run mypy src/` — Verify no new type errors (75s runtime)
5. ✅ `packer fmt -recursive src/bilder/` — Format Packer files (if modified)
6. ✅ `pulumi preview` — Validate Pulumi changes (if applicable)
7. ✅ `uv run pytest tests/` — Run tests (if applicable)

**Optional but recommended:**
- `uv run pre-commit run --all-files` — Run all hooks (may take 2+ minutes)

## Common Build Issues

### Issue: `mypy` reports 1316 errors
**Solution:** Expected. Only fix new errors introduced by your changes. Many legacy type issues exist.

### Issue: `ruff check` reports 809 errors
**Solution:** Expected. Focus on not introducing NEW errors. Consider using `--fix` for auto-fixable issues.

### Issue: pre-commit hook `hadolint-docker` fails with existing Docker warnings
**Solution:** Ignore hadolint failures if your changes don't affect Dockerfiles. These are pre-existing issues.

### Issue: Timeout running commands
**Solution:**
- `mypy src/` takes 75+ seconds—increase timeout to 120s
- `uv run pytest` may need 60s timeout depending on test scope

### Issue: `uv` warns about `~=3.12` in `requires-python`
**Solution:** This is a warning, not an error. Ignore it—fixing requires changing project config.

### Issue: Pulumi operation requires AWS credentials
**Solution:** Ensure AWS CLI is configured (`aws configure`) or environment variables are set.

### Issue: README mentions Poetry but `poetry.lock` doesn't exist
**Solution:** Repository has migrated to `uv`. Always use `uv sync` and `uv run <command>`.
