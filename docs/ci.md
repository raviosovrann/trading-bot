# Continuous Integration

## Required checks (branch protection)

`main` is protected. Configure these as **required status checks** so a merge
commit on `main` is revalidated by the same gates as its PR (all workflows run on
both `pull_request` to `main` **and** `push` to `main`):

| Check | Workflow | Enforces |
|-------|----------|----------|
| `tests (3.11)`, `tests (3.12)`, `tests (3.13)` | `tests.yml` | pytest + branch-coverage floor |
| `typecheck` | `typecheck.yml` | pyright over `src/tradingbot` + `tests` |
| `ui` | `ui.yml` | UI typecheck, lint, format, vitest |
| `analyze`, `Analyze (javascript-typescript)` | `codeql.yml` | CodeQL (Python + JS/TS) |
| `bandit` / `Bandit` | `bandit.yml` | Python security scan |

Enable **"Require branches to be up to date before merging"** so a stale branch
is rebuilt against the latest `main` before it can merge.

## Reproducible dependencies

`requirements.txt` declares the direct dependencies (with compatible ranges);
`constraints.txt` is a full `pip freeze` lock of the resolved environment. CI
installs with `pip install -r requirements.txt -c constraints.txt`, so
re-running any historical commit resolves the **same** versions. Regenerate the
lock after intentionally changing a dependency:

```bash
pip install -r requirements.txt && pip freeze --exclude-editable \
  | grep -viE '^(pip|setuptools|wheel)==' > constraints.txt
```

The install step doubles as the freshness check: an out-of-date lock that cannot
satisfy `requirements.txt` fails the resolution and therefore the build.

## Coverage floor

`tests.yml` runs `pytest --cov=tradingbot --cov-branch --cov-fail-under=85`, so a
drop below 85% branch coverage fails CI with a per-file diff (`term-missing`).
The floor is a **ratchet**: when coverage rises comfortably above it, raise the
number in `tests.yml` to lock in the gain. `coverage.xml` and JUnit
`test-results-*.xml` are uploaded as artifacts for each Python version.

## Trigger coverage (CI self-test)

The gates carry **no `paths` filters**, so every workflow triggers for backend,
UI, and shared changes alike — there is no path-filtered gap where a change can
merge without its relevant gate running. Adding a `paths` filter to any workflow
requires re-checking that shared-code changes still trigger every affected gate.
