# Spectra Assure Package Cleaner

Automatically deletes stale packages from the [ReversingLabs Spectra Assure](https://www.reversinglabs.com/products/software-supply-chain-security) portal. Designed to run as a long-lived Docker container on a schedule, or as a one-shot invocation.

## Disclaimer of Warranty

This application is provided "as is" and "as available" without any warranties of any kind, either express or implied.

Reversing Labs make no representations or warranties of any kind, including but not limited to:

- The accuracy, completeness, or timeliness of the information submitted or received via this application;
- The functionality, availability, or performance of the application;
- The security, integrity, or confidentiality of submitted files or user data; or
- The fitness of this application for any particular purpose.

Use of this application is at your own risk. By using this application, you acknowledge that any data submitted to third-party services (e.g., ReversingLabs Spectra Assure) may be subject to their own terms and conditions.

In no event shall the developer be liable for any direct, indirect, incidental, special, exemplary, or consequential damages arising out of or in any way connected with the use or misuse of this application.

## How it works

Each cleanup cycle walks the organization tree: **groups > projects > packages > versions** — the full org by default, or a subset when scoped with `SPECTRA_ASSURE_GROUP` / `SPECTRA_ASSURE_PROJECT` (see [Scoping](#scoping)). For every package, it fetches the analysis timestamp of each version and applies the following rules:

- **A package is deleted only when every version's analysis timestamp is older than the threshold.** This is the core safety rule — if even one version is recent, the entire package is kept.
- **If any API call fails while evaluating a package, that package is skipped entirely.** The tool never deletes what it cannot fully evaluate.
- **Evaluation short-circuits on the first fresh version found.** Once a recent version is detected, remaining versions are not checked — the package is immediately marked as kept.
- **Only whole packages are deleted**, never individual versions. This matches the Spectra Assure API, which only supports package-level deletion.

The timestamp used is the `analysis.timestamp` field from the version status endpoint. This reflects the last time the version was analyzed (not when it was uploaded), and changes on rescan.

### Safety defaults

- **`DRY_RUN` defaults to `true`.** Out of the box, the tool only logs what it *would* delete. You must explicitly set `DRY_RUN=false` to enable actual deletions.
- **Deletions are permanent.** There is no undo. Always run in dry-run mode first to verify behavior.

## Configuration

All configuration is via environment variables. No config files are needed.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SPECTRA_ASSURE_BASE_URL` | Yes | — | Portal URL, e.g. `https://my.secure.software/acme-corp`. The org can be in the path, derived from the subdomain, or set explicitly via `SPECTRA_ASSURE_ORG` |
| `SPECTRA_ASSURE_ORG` | No | — | Override the organization name. When set, the org is not parsed from the URL. Useful for instances like `https://example.secure.software` |
| `SPECTRA_ASSURE_GROUP` | No | — (all groups) | Comma-separated list of group names to clean. When set, only these groups are walked. See [Scoping](#scoping) |
| `SPECTRA_ASSURE_PROJECT` | No | — (all projects) | Comma-separated list of project names to clean, matched within each walked group. See [Scoping](#scoping) |
| `SPECTRA_API_TOKEN` | Yes | — | Personal access token (PAT) for Bearer auth |
| `STALE_THRESHOLD_DAYS` | No | `180` | Minimum age in days. Packages where every version was last analyzed more than this many days ago are eligible for deletion |
| `CLEANUP_INTERVAL_HOURS` | No | `24` | Hours between cleanup cycles. Set to `0` for a single run then exit |
| `DRY_RUN` | No | `true` | Set to `false`, `0`, or `no` to enable actual deletions. Any other value (including typos) keeps dry-run enabled |
| `REQUEST_DELAY_SECONDS` | No | `0.5` | Delay in seconds between API calls to avoid overwhelming the portal |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Scoping

By default the tool walks every group and project in the organization. Two
optional variables narrow that walk. They are **independent filters**:

- `SPECTRA_ASSURE_GROUP` restricts which **groups** are walked. Unset = all groups.
- `SPECTRA_ASSURE_PROJECT` restricts which **projects** are walked, by name,
  within each walked group. Unset = all projects.

| Env | Meaning |
|-----|---------|
| `SPECTRA_ASSURE_GROUP=foo` | Everything in group `foo` |
| `SPECTRA_ASSURE_GROUP=foo,baz` | Everything in groups `foo` and `baz` |
| `SPECTRA_ASSURE_GROUP=foo` + `SPECTRA_ASSURE_PROJECT=bar` | Only project `bar` in group `foo` |
| `SPECTRA_ASSURE_PROJECT=bar` (no group) | Project `bar` in every group that has one |
| `SPECTRA_ASSURE_GROUP=foo,baz` + `SPECTRA_ASSURE_PROJECT=bar,qux` | Projects `bar`/`qux` wherever they appear in `foo`/`baz` |

Each variable is a comma-separated list; whitespace around each name is
ignored. Setting `SPECTRA_ASSURE_PROJECT` without `SPECTRA_ASSURE_GROUP`
matches that project name across all groups.

Names are matched **exactly** against what the API returns — case-sensitive,
never substring, and sensitive to whitespace and Unicode normalization on the
API's side (the value you set is stripped, the API's name is not). So
`SPECTRA_ASSURE_PROJECT=api` scopes to a project named exactly `api`, never to
`api-legacy` or `internal-api`. A near-miss matches nothing and is reported as
a typo: **a name that does not match deletes nothing**. Two cases below are the
exceptions to that — a value that parses to no name at all, and a name
containing a comma. Both widen the walk rather than narrowing it.

**A name containing a comma cannot be expressed, and the failure is silent.**
The list is split on `,` with no escape, and the split cannot be told apart
from an ordinary two-name list. If the halves happen to be real names, the walk
**widens into groups you never named** — with no warning, because every parsed
name matched something:

```
SPECTRA_ASSURE_GROUP="a,b"   # meaning the single group literally named "a,b"
parsed scope -> a, b         # walks and deletes in groups `a` and `b`
                             # the group `a,b` is never touched
```

This is the one case where a filter that is in effect does not fail closed. Such
a group or project can only be reached by leaving the variable unset and letting
the walk cover it.

At the end of a cycle, any filter value that matched no group or project is
logged as a warning, so typos surface quickly. These warnings are per cycle and
are suppressed where the walk could not establish what exists, to avoid false
alarms. An interrupted cycle suppresses both levels. A group listing that failed,
or an unreadable entry in one, also suppresses both — the group a filter names
may have been the one that could not be read, and its projects were never listed.
A failed or partly unreadable *project* listing suppresses only the project
warnings; the group warnings still fire, correctly. So does an unmatched group
filter, which would otherwise make every project warning meaningless.

An absent warning therefore means "matched" *or* "could not tell", and the two
are not always distinguishable from the summary line. A group typo plus a
project typo reports only the group one, with `errors=0`; an interrupted cycle
reports neither, also with `errors=0`. What tells them apart is the status word
(`Cycle interrupted`), the presence of a group warning, and the error count
together — not the error count alone.

Note also that an unmatched group filter suppresses the project warnings for
*every* group, including ones that were fully enumerated. With groups
`{team-a, tema-b}` and project `biling`, only the group typo is reported, even
though `team-a`'s projects were listed and `biling` genuinely matched nothing in
them. Fix reported typos one at a time and re-run; a clean cycle is the only
reliable all-clear.

If a scope variable is set but contains no usable name — empty, or only
whitespace and commas — the tool treats it as unset, meaning **no scope, i.e.
the whole org**. It keeps the documented "unset = all" default rather than
inventing a narrower one, so the walk widens instead of narrowing. Unlike the
comma case above, it is never silent — the tool prints a
warning to stderr at startup, before logging is configured, so it appears
regardless of `LOG_LEVEL`. This covers `-e VAR=` and a Compose `${VAR}` that
interpolates to nothing. Under `DRY_RUN=false`, treat that warning as a reason
to stop the run.

## Usage

### Docker (recommended)

```bash
# Build the image
docker build -t assure-package-cleaner .

# Dry run (default) — logs what would be deleted, deletes nothing
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  assure-package-cleaner

# Live run — actually deletes stale packages
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e DRY_RUN=false \
  assure-package-cleaner

# Single run then exit (no periodic loop)
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e CLEANUP_INTERVAL_HOURS=0 \
  assure-package-cleaner

# Org-less URL — org derived from subdomain (becomes "Example")
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://example.secure.software \
  -e SPECTRA_API_TOKEN=your-token-here \
  assure-package-cleaner

# Custom threshold — delete packages not analyzed in the last year
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e STALE_THRESHOLD_DAYS=365 \
  -e DRY_RUN=false \
  assure-package-cleaner

# Scope to a single group — walk only group "acme-team"
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e SPECTRA_ASSURE_GROUP=acme-team \
  assure-package-cleaner

# Scope to one project in one group
docker run --rm \
  -e SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e SPECTRA_ASSURE_GROUP=acme-team \
  -e SPECTRA_ASSURE_PROJECT=billing-service \
  assure-package-cleaner
```

The container handles `SIGTERM` and `SIGINT` gracefully — it finishes the current operation and then exits cleanly, and it never abandons a package half-evaluated.

One case is slower than `docker stop`'s default 10-second grace period: the signal handler only sets a flag, which is checked between operations, so a shutdown that arrives while the client is sleeping off a rate-limit (429) backoff is not noticed until that sleep ends. With three retries at the 60-second default that is up to 180 seconds, and Docker will `SIGKILL` first. That is safe — a kill mid-walk cannot leave a package partly deleted, since deletion is a single API call — but if you stop the container during a rate-limit storm, either pass `docker stop -t 200` or expect the kill. Tracked in [#19](https://github.com/reversinglabs-ats/assure-package-cleaner/issues/19).

### Running directly (without Docker)

Requires Python 3.12 or newer.

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the package
pip install -e .

# Set required environment variables
export SPECTRA_ASSURE_BASE_URL=https://my.secure.software/acme-corp
export SPECTRA_API_TOKEN=your-token-here

# Run (dry-run mode by default)
python -m assure_package_cleaner
```

## Development

Requires Python 3.12+ and a virtual environment.

```bash
# Create venv and install with dev dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run the full check suite (same as CI)
.venv/bin/ruff format --check .   # check formatting
.venv/bin/ruff check --no-fix .   # lint
.venv/bin/mypy src tests          # type check
.venv/bin/pytest                  # run tests (233 tests, <1s)
```

### Project layout

```
src/assure_package_cleaner/
  __init__.py        # package marker
  __main__.py        # entrypoint: config, client, cleaner, loop
  config.py          # Config dataclass parsed from env vars
  client.py          # SpectraClient: thin HTTP wrapper over the portal API
  cleaner.py         # Cleaner.run_cycle(): the group/project/package/version walk
tests/
  test_config.py     # env var parsing, validation, defaults
  test_client.py     # API methods, errors, auth, delays, network exceptions
  test_cleaner.py    # staleness logic, short-circuit, fail-safe, dry-run
  test_main.py       # config to cleaner wiring
Dockerfile           # multi-stage Chainguard build
```

## License

MIT — see [LICENSE](LICENSE).
