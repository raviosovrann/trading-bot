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

## Security scanning

Three blocking, version-pinned scanners run on PRs, `push` to `main`, and a
weekly schedule:

| Scanner | Workflow | Policy |
|---------|----------|--------|
| Bandit `1.9.4` | `bandit.yml` | Static analysis of `src/tradingbot` at medium severity **and** confidence; a finding fails the build (no `exit_zero`). |
| pip-audit `2.10.1` | `security.yml` | Audits `constraints.txt`; a new advisory fails the build. |
| npm audit | `security.yml` | Fails on `high`+ advisories in `ui/`. |

**Dependabot** (`.github/dependabot.yml`) opens weekly update PRs for pip, npm,
and GitHub Actions, so surfaced advisories become actionable upgrades.

### Suppression policy (no blanket ignores)

- **Bandit**: suppress a specific false positive inline with
  `# nosec <test-id>  # rationale` — never disable a rule globally.
- **pip-audit**: suppress a specific advisory with `--ignore-vuln <ID>` in
  `security.yml`, each with a comment. A blanket success-on-findings is never
  used, so any *unlisted* advisory still fails.

Current pip-audit exceptions (transitive via `fastapi`/`starlette` or dev/build
tooling with no in-range fix; tracked by Dependabot) — **review by 2026-10-01**,
owner: repo maintainers:

`PYSEC-2026-161`, `-248`, `-249`, `-1941`, `-1942`, `-2280`, `-2281` (starlette);
`PYSEC-2026-1845` (pytest); `PYSEC-2026-3447` (setuptools).
