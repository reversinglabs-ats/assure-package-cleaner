# CLAUDE.md

## What this project is

A Python CLI/Docker tool that automatically deletes stale packages from the ReversingLabs Spectra Assure portal. It walks the groups/projects/packages in an organization — the whole org by default, or a subset when scoped via `SPECTRA_ASSURE_GROUP` / `SPECTRA_ASSURE_PROJECT` — and deletes packages where **every** version has an analysis timestamp older than a configurable threshold. Designed to run as a long-lived Docker container on a schedule, or as a one-shot invocation.

## Quick reference

```bash
# Install (uses a .venv with Python 3.14 locally)
.venv/bin/python -m pip install -e ".[dev]"

# Lint and format
.venv/bin/ruff format --check .
.venv/bin/ruff check --no-fix .

# Type check
.venv/bin/mypy src tests

# Run tests (207 tests, should complete in <1s)
.venv/bin/pytest

# Run the app locally (requires env vars — see below)
.venv/bin/python -m assure_package_cleaner
```

There is no system-level pip — always use `.venv/bin/` prefixed commands.

## Project layout

```
src/assure_package_cleaner/
  __init__.py        # empty package marker
  __main__.py        # entrypoint: config → client → cleaner → loop
  config.py          # Config dataclass parsed from env vars
  client.py          # SpectraClient: thin HTTP wrapper over the portal API
  cleaner.py         # Cleaner.run_cycle(): the group→project→package→version walk
tests/
  test_config.py     # 75 tests — env var parsing, validation, defaults, scoping
  test_client.py     # 43 tests — API methods, errors, auth, delay
  test_cleaner.py    # 82 tests — staleness logic, short-circuit, fail-safe, dry-run, scoping
  test_main.py       # 7 tests  — config → cleaner/client wiring (dry_run and scope must survive it)
Dockerfile           # Multi-stage Chainguard build
```

## Architecture decisions

- **No SDK dependency.** The Spectra Assure API is called directly with `requests`. The API is simple (6 endpoints, no pagination, no auth refresh).
- **Stateless.** No database, no files, no persistent state. Every cycle walks the tree (full org, or the configured group/project scope) from scratch.
- **Fail-safe deletion rule.** A package is only deleted when ALL versions are confirmed stale. If any `/status/` call fails, that package is skipped entirely — never delete what you can't fully evaluate.
- **Short-circuit.** When evaluating versions, the first fresh version found causes the package to be skipped immediately (no further `/status/` calls).
- **DRY_RUN defaults to true.** This is intentional and must stay this way — the tool deletes things permanently.

## Environment variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `SPECTRA_ASSURE_BASE_URL` | Yes | — | e.g. `https://my.secure.software/acme-corp` — org resolution: `SPECTRA_ASSURE_ORG` override > URL path segment > subdomain (capitalized) |
| `SPECTRA_ASSURE_ORG` | No | — | Explicit org override. Required for hosts like `localhost` with no path or subdomain |
| `SPECTRA_ASSURE_GROUP` | No | — | Comma-separated group names to restrict the walk. Set-but-empty warns to stderr and means all groups |
| `SPECTRA_ASSURE_PROJECT` | No | — | Comma-separated project names to restrict the walk, matched within each walked group. Set-but-empty warns to stderr and means all projects |
| `SPECTRA_API_TOKEN` | Yes | — | Bearer token (PAT) |
| `STALE_THRESHOLD_DAYS` | No | `180` | Minimum 1 |
| `CLEANUP_INTERVAL_HOURS` | No | `24` | `0` = single run and exit |
| `DRY_RUN` | No | `true` | Only `false`, `0`, `no` disable it |
| `REQUEST_DELAY_SECONDS` | No | `0.5` | Delay between API calls |
| `LOG_LEVEL` | No | `INFO` | Standard Python levels |

## Code style and CI

- **Ruff** for formatting (double quotes, spaces, 100-char lines) and linting (isort, pyupgrade, bugbear).
- **mypy** with `check_untyped_defs = true` but `disallow_untyped_defs = false`.
- **pytest** with `pythonpath = ["src"]` — tests import from `assure_package_cleaner` directly.
- All tests use `unittest.mock` — no additional test dependencies.
- CI runs on push/PR to main: ruff format → ruff lint → mypy → pytest.

## Testing conventions

- Tests mock the `SpectraClient` (or `requests` for client tests) — no real API calls.
- The cleaner tests construct a `MagicMock` for the client and wire it into a `Cleaner` instance directly.
- Client tests patch `requests.get`/`requests.delete` and set `request_delay=0` to avoid sleeps.
- Config tests use `@patch.dict(os.environ, ...)` to set env vars.

## API reference

The full OpenAPI spec is at `spectra-assure-portal-api-openapi.yaml`. The endpoints used are:

- `GET /list/{org}` — list groups
- `GET /list/{org}/{group}` — list projects
- `GET /list/{org}/{group}/pkg:rl/{project}` — list packages
- `GET /list/{org}/{group}/pkg:rl/{project}/{package}` — list versions
- `GET /status/{org}/{group}/pkg:rl/{project}/{package}@{version}` — analysis status (has the timestamp)
- `DELETE /delete/{org}/{group}/pkg:rl/{project}/{package}` — delete package + all versions

No pagination. Auth is `Authorization: Bearer <token>`.

## Things to watch out for

- The `analysis.timestamp` field is the last-analyzed time, not upload time. It changes on rescan. This is the only timestamp available.
- Never delete individual versions — only whole packages via the DELETE endpoint.
- The `pkg:rl/` prefix in URL paths is literal and required by the API.
- Token masking in `config.py` assumes the token is at least 8 characters (shows first 4 + last 4).
- Every listing entry goes through `_entry_name()` in `cleaner.py`. A malformed entry must **skip**, never raise — an exception mid-walk aborts the cycle after earlier packages have already been deleted. It rejects non-dicts, missing keys, non-string values, and blank names (which would build a URL with an empty path segment).
- Duplicate entries are skipped at the group, project, and package levels, but the cost differs. Groups and projects re-list from the API on each pass, so a repeat there costs wasted requests and inflated `projects_processed` — measure this against a stateful fake, not a `MagicMock`, whose `return_value` hands back the same list forever and makes it look like a double deletion. A package listing is iterated in memory with no re-list between deletes, so a repeat there is a genuine second DELETE of something already gone, 404ing into `errors`. Duplicate versions cost one extra `/status/` call and nothing else.
- The project de-dupe is per group, never global — the same project name legitimately appears in many groups (`test_project_filter_spans_all_groups`).
- `tests/test_main.py` exists because `__main__` is where `DRY_RUN` and the scope filters meet the `Cleaner`. A wiring slip there is the one class of bug that **over**-deletes, and type checking can't catch it — `target_groups`/`target_projects` are both `frozenset[str]`. Assert kwargs there, not just in `test_config.py`.
