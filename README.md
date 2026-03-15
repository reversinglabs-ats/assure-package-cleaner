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

Each cleanup cycle walks the full organization tree: **groups > projects > packages > versions**. For every package, it fetches the analysis timestamp of each version and applies the following rules:

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
| `SPECTRA_BASE_URL` | Yes | — | Full portal URL including your org name, e.g. `https://my.secure.software/acme-corp` |
| `SPECTRA_API_TOKEN` | Yes | — | Personal access token (PAT) for Bearer auth |
| `STALE_THRESHOLD_DAYS` | No | `180` | Minimum age in days. Packages where every version was last analyzed more than this many days ago are eligible for deletion |
| `CLEANUP_INTERVAL_HOURS` | No | `24` | Hours between cleanup cycles. Set to `0` for a single run then exit |
| `DRY_RUN` | No | `true` | Set to `false`, `0`, or `no` to enable actual deletions. Any other value (including typos) keeps dry-run enabled |
| `REQUEST_DELAY_SECONDS` | No | `0.5` | Delay in seconds between API calls to avoid overwhelming the portal |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Usage

### Docker (recommended)

```bash
# Build the image
docker build -t assure-package-cleaner .

# Dry run (default) — logs what would be deleted, deletes nothing
docker run --rm \
  -e SPECTRA_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  assure-package-cleaner

# Live run — actually deletes stale packages
docker run --rm \
  -e SPECTRA_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e DRY_RUN=false \
  assure-package-cleaner

# Single run then exit (no periodic loop)
docker run --rm \
  -e SPECTRA_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e CLEANUP_INTERVAL_HOURS=0 \
  assure-package-cleaner

# Custom threshold — delete packages not analyzed in the last year
docker run --rm \
  -e SPECTRA_BASE_URL=https://my.secure.software/acme-corp \
  -e SPECTRA_API_TOKEN=your-token-here \
  -e STALE_THRESHOLD_DAYS=365 \
  -e DRY_RUN=false \
  assure-package-cleaner
```

The container handles `SIGTERM` and `SIGINT` gracefully — it will finish the current operation and then exit cleanly. This means `docker stop` works without forcing a kill.

### Running directly (without Docker)

Requires Python 3.12 or newer.

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the package
pip install -e .

# Set required environment variables
export SPECTRA_BASE_URL=https://my.secure.software/acme-corp
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
.venv/bin/pytest                  # run tests (94 tests, <1s)
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
Dockerfile           # multi-stage Chainguard build
```

## License

MIT — see [LICENSE](LICENSE).
