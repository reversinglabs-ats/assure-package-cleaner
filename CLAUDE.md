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

# Run tests (335 tests, should complete in <1s)
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
  test_config.py     # 122 tests — env var parsing, validation, defaults, scoping
  test_client.py     # 86 tests — API methods, errors, auth, delay
  test_cleaner.py    # 120 tests — staleness logic, short-circuit, fail-safe, dry-run, scoping
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

- **Ruff** for formatting (double quotes, spaces, 100-char lines) and linting. The lint rules are an explicit `select` (`E4`, `E7`, `E9`, `F`, `I`, `UP`, `B`), not `extend-select` — a ruff release that widens its defaults must not widen our surface, which is what forced the old `<0.16` pin. Adopting a rule family is a deliberate edit to `select`. Markdown is in `extend-exclude` for the same reason: 0.16 began formatting Python blocks inside it.
- **mypy** with `check_untyped_defs = true` but `disallow_untyped_defs = false`.
- **pytest** with `pythonpath = ["src"]` — tests import from `assure_package_cleaner` directly.
- All tests use `unittest.mock` — no additional test dependencies. The one exception is `TestRedirectsAreNotFollowed`, which runs a stdlib `http.server` on localhost because module-level patching of `requests` cannot exercise redirect behaviour. Still no new dependencies.
- CI runs on push/PR to main: ruff format → ruff lint → mypy → pytest.

## Testing conventions

- Tests mock the `SpectraClient` (or `requests` for client tests) — no real API calls. Where the behaviour under test belongs to `requests` rather than to us, mock the network instead: a localhost `http.server` is not a real API call.
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
- The same rule applies to the **container**, at *two* layers, and missing either one aborts the walk with an uncaught `AttributeError` — after earlier packages have already been deleted, with later groups never walked, so their stale packages survive every future cycle.
  - **In `client.py`, `_get` rejects a non-dict body.** A bare `null`, `[]`, `"x"` or `42` is valid JSON: it decodes cleanly and then explodes on the `.get()` in all four `list_*` methods, inside the client, where `run_cycle`'s `except APIError` cannot see it. Raising `APIError` routes it into the channel that already works. This is the layer that matters most, because it is the only one that covers a body that is not a dict at all.
  - **In `cleaner.py`, each of the four loops type-checks its listing.** This covers the *other* case — key present, value `null` — which `_get` cannot catch, since the body is a perfectly good dict. The consequences differ by level and are not uniform: the group guard aborts the cycle, the project guard also returns `False` to mark the listing incomplete (which suppresses phantom scope warnings), and the package and version guards simply skip their subtree — they have no incompleteness signal because nothing downstream needs one.
- `_extract_timestamp` guards its whole payload for the same reason. That guard is not *redundant* with `_get`'s — it is **superseded, with a different counter outcome**, and that is the part to remember. A bare-`null` `/status/` body used to reach `_extract_timestamp`, return `None` and count as `skipped`; it now raises `APIError` in `_get` and counts as `errors`. Same fail-safe direction, different bucket. Keep the guard as the backstop for a caller that ever bypasses `_get`, but do not write a test that hands the cleaner a raw non-dict status and asserts `skipped` — production can no longer produce that.
- A `{"analysis": null}` body is a *different* case and must stay in `skipped`: the OpenAPI spec marks `analysis.timestamp` nullable alongside `status: PROCESSING`, so a routine in-progress version would otherwise land in `errors`.
- Duplicate entries are gated at all four levels, but only three of them **skip**. **In dry-run — the default — a duplicate group, project or package double-counts `deleted`, identically.** That matters most: the dry-run report is what an operator reads to decide whether to set `DRY_RUN=false`. Only under `DRY_RUN=false` do those three diverge, because `_delete_package` returns before touching the API in dry-run and the package therefore never disappears. There: groups and projects re-list between passes, so a repeat costs wasted requests and inflated `groups_processed` / `projects_processed` / `packages_evaluated`; a package listing is iterated in memory with no re-list, so the repeat reaches a package that is already gone and 404s at `list_versions` into `errors` — one call short of DELETE, which is never issued twice.
- **The version gate warns but deliberately does not `continue`** — it is the one level that behaves identically in both modes. A duplicate version cannot double-count `deleted` (that counter is per package), so gating it would buy only saved `/status/` calls and a tidier log count, at the price of letting a repeat with a *divergent* status through unchecked: `all_stale` would stay `True` and a fresh version would lose its veto. Skipping on doubt is the posture everywhere else in this walk, and it applies here too. `walked_versions` still exists — `len()` of it is the count in the DELETED line. Pinned by `test_duplicate_version_is_still_status_checked`.
- Two consequences of that choice, both deliberate. **The cost is N−1 redundant `/status/` calls, not one, and it is unbounded** — 50 identical version entries make 50 calls, each preceded by `_delay()`, so ~25s of stall at the 0.5s default where the old gate spent 0.5s. And **the version level is the one gate that can add to `errors`**: a transient 500 on a redundant call now skips the package (`deleted=0 errors=1`) where the old `continue` deleted it. Both are the fail-safe direction, so neither is worth "fixing" by restoring the skip.
- When measuring any of this, use the **stateful fake** (`_StatefulPortal` in `tests/test_cleaner.py`), where a deleted package actually disappears, and run **both** `DRY_RUN` modes. A `MagicMock`'s `return_value` hands back the same list forever, which models dry-run faithfully but not live deletion — under `DRY_RUN=false` it reports a duplicate package as two *successful* deletes and no error, which is not what the portal does.
- The de-dupe gates are scoped one level up: projects per group, packages per project, versions per package. Never global — the same project name legitimately appears in many groups, the same package name in many projects, and the same version name in many packages. All three are pinned (`test_same_project_name_in_two_groups_is_not_deduped`, `test_same_package_name_in_two_projects_is_not_deduped`, `test_same_version_in_two_packages_is_not_deduped`).
- Each gate sits **above** its scope filter so a misbehaving server's duplicates surface even for entries that would not be walked, and the gate itself never touches `errors` — nothing failed to be evaluated. (At the version level the *re-check* that follows the warning can still raise an error of its own; the gate does not.) Both choices are pinned by tests.
- A scope-warning test whose group filter contains only a typo proves nothing below the filter: the one real group is skipped by the `target_groups` check in `_walk`, `_process_group` is never called, and any `list_projects` fixture goes unread. To exercise the walk *and* leave a typo unmatched, the filter needs both — `target_groups={"grp1", "grp-typo"}`. Assert `stats.errors` too, so the test fails if it stops reaching the malformed entry. (Cite constructs, not line numbers, in this file — the earlier version of this bullet pointed at `cleaner.py:76`, which a five-line insertion in the same series had already turned into a comment.)
- The end-of-cycle "filter matched no group/project" warnings are suppressed whenever the walk could not enumerate what the filter names — an interrupt, a failed project listing, **or a malformed entry**, which leaves exactly the same doubt as a listing that failed. A malformed *group* entry suppresses both levels, since that group's projects were never listed either. Do not replace this channel with a blanket `stats.errors == 0` check: it passes the suite but silences genuine typo warnings whenever any unrelated delete fails in the same cycle.
- **Every request sets `allow_redirects=False`.** The portal API has fixed, fully-specified paths and never redirects, and following one is unsafe in both directions. On a DELETE, `requests` rewrites the method to `GET` for 302/303, so the package is never removed while the final 200 reads as success — `deleted=1 errors=0` logged with nothing deleted, and no line distinguishing it from a working run. On a GET, a redirect onto another JSON endpoint feeds `get_version_status` a foreign body, and a stale-looking `analysis.timestamp` there authorizes a real delete of a package that was never evaluated. The `Authorization` header also survives a same-host hop (`requests` only strips it when the hostname changes). Pinned by `TestRedirectsAreNotFollowed`.
- **That test class runs a real `http.server`, and it is the only one that does.** Every other client test patches `requests.get`/`.delete` at module level, which is structurally incapable of catching redirect behaviour — the mock returns the final response and no redirect logic ever runs. When a defect lives in the HTTP library's behaviour rather than ours, module-level patching cannot see it. Keep the server fixture class-scoped: `HTTPServer.shutdown()` waits out `serve_forever`'s poll interval, and a per-test server costs ~0.5s each.
- The summary line's **status word is a contract**, not decoration: `Cycle complete` only when the walk enumerated groups and finished, `Cycle interrupted` for a shutdown, `Cycle ABORTED` when an exception escaped or `_walk` returned early. `README.md` tells operators to read it, and a cycle that walked nothing must never say `complete` — that is worse than the no-line-at-all it replaced, because a scheduler watching for a missing line would stop seeing one. An exception outranks the interrupt flag. All three arms are pinned; before this they were not, and deleting the `interrupted` arm passed the whole suite.
- Mutating a shared helper's body tests the helper, not its call sites. `_brief` has ten call sites; mutating its body is killed by any one of them. **Every site needs its own test**, and the rule was written down after three sites were unpinned — then three *more* shipped unpinned in the very next commit, because the rule was recorded without auditing the existing sites against it. When adding a call, add its test in the same edit.
- A constant compared against itself pins nothing. `assert _parse_retry_after(r) == _MAX_RETRY_AFTER` passes for any value of the constant, so raising a ceiling stays green while the README publishes the old number. Assert the **literal**, and pin the boundary from both sides (the ceiling accepted, ceiling+1 rejected).
- **Validation must not be stricter than the thing it stands in for.** `_VALID_LOG_LEVELS` was hand-written and omitted `WARN` and `FATAL`, which `logging` accepts — turning `-e LOG_LEVEL=WARN` into an exit-1 crashloop on upgrade, the exact failure the validation was added to prevent. It now derives from `logging.getLevelNamesMapping()`. Where a value is a proxy for another library's accepted set, take the set from that library.
- Config that *rejects* something previously accepted is a breaking change and needs a README upgrade note. Three exist: redirects, credentials in the base URL, and an empty-but-set scope var under `DRY_RUN=false`. Each was justified by the old behaviour being broken (silent non-deletes, an already-401ing deployment) or silently unsafe (an unrequested org-wide delete) — that bar is what makes the break acceptable, and a change that cannot clear it does not belong.
- `tests/test_main.py` exists because `__main__` is where `DRY_RUN` and the scope filters meet the `Cleaner`. A wiring slip there is the one class of bug that **over**-deletes, and type checking can't catch it — `target_groups`/`target_projects` are both `frozenset[str]`. Assert kwargs there, not just in `test_config.py`.
