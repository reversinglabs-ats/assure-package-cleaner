"""Configuration parsed from environment variables."""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Ceilings, not policy: both are orders of magnitude past any real configuration, and
# exist only to keep a typo'd value from reaching a C-level conversion that overflows.
_MAX_THRESHOLD_DAYS = 36_500  # 100 years
_MAX_INTERVAL_HOURS = 87_600  # 10 years
_MAX_REQUEST_DELAY = 3_600.0  # 1 hour between calls is already absurd

# Taken from logging itself rather than hand-listed. A hand-written allowlist shipped
# without WARN and FATAL — both accepted by basicConfig, so `-e LOG_LEVEL=WARN` was a
# working deployment that a narrower list turned into an exit-1 crashloop on upgrade.
# The point of validating here is to fail cleanly on a *typo*, not to be stricter than
# the thing it stands in for.
_VALID_LOG_LEVELS = tuple(sorted(logging.getLevelNamesMapping()))


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    base_url: str
    org: str
    api_token: str
    stale_threshold_days: int
    cleanup_interval_hours: int
    dry_run: bool
    request_delay_seconds: float
    log_level: str
    target_groups: frozenset[str]
    target_projects: frozenset[str]

    @classmethod
    def from_env(cls) -> Config:
        base_url_raw = os.environ.get("SPECTRA_ASSURE_BASE_URL", "").strip()
        if not base_url_raw:
            raise ConfigError("SPECTRA_ASSURE_BASE_URL is required")

        api_token = os.environ.get("SPECTRA_API_TOKEN", "").strip()
        if not api_token:
            raise ConfigError("SPECTRA_API_TOKEN is required")

        org_override_raw = os.environ.get("SPECTRA_ASSURE_ORG", "").strip()
        org_override = org_override_raw if org_override_raw else None

        base_url, org = _parse_base_url(base_url_raw, org_override=org_override)

        stale_threshold_days = _parse_int(
            "STALE_THRESHOLD_DAYS", 180, minimum=1, maximum=_MAX_THRESHOLD_DAYS
        )
        cleanup_interval_hours = _parse_int(
            "CLEANUP_INTERVAL_HOURS", 24, minimum=0, maximum=_MAX_INTERVAL_HOURS
        )
        request_delay_seconds = _parse_float(
            "REQUEST_DELAY_SECONDS", 0.5, minimum=0.0, maximum=_MAX_REQUEST_DELAY
        )
        # Validated here rather than left to basicConfig, which raises a bare ValueError
        # from __main__ *after* the config gate — a traceback instead of the clean
        # "Configuration error:" + exit 1 every other bad value gets, and a crashloop
        # under a Docker restart policy.
        # Set-but-empty means unset, as it does for the scope variables: `-e LOG_LEVEL=`
        # and a Compose `${LOG_LEVEL}` that interpolates to nothing must not stop the
        # container. Unlike the scope vars this needs no warning — the default is not a
        # widening of what gets deleted.
        log_level = os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO"
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL must be one of {', '.join(_VALID_LOG_LEVELS)}, got: {log_level!r}"
            )

        dry_run_raw = os.environ.get("DRY_RUN", "true").strip().lower()
        dry_run = dry_run_raw not in ("false", "0", "no")

        target_groups = _parse_csv_set("SPECTRA_ASSURE_GROUP")
        target_projects = _parse_csv_set("SPECTRA_ASSURE_PROJECT")

        return cls(
            base_url=base_url,
            org=org,
            api_token=api_token,
            stale_threshold_days=stale_threshold_days,
            cleanup_interval_hours=cleanup_interval_hours,
            dry_run=dry_run,
            request_delay_seconds=request_delay_seconds,
            log_level=log_level,
            target_groups=target_groups,
            target_projects=target_projects,
        )

    def log_settings(self) -> None:
        if len(self.api_token) > 8:
            masked_token = self.api_token[:4] + "****" + self.api_token[-4:]
        else:
            masked_token = "****"  # nosec B105 — this is a mask, not a password
        logger.info("Configuration:")
        logger.info("  Base URL:              %s", self.base_url)
        logger.info("  Organization:          %s", self.org)
        logger.info("  API Token:             %s", masked_token)
        logger.info("  Stale threshold:       %d days", self.stale_threshold_days)
        logger.info("  Cleanup interval:      %d hours", self.cleanup_interval_hours)
        logger.info("  Dry run:               %s", self.dry_run)
        logger.info("  Request delay:         %.1fs", self.request_delay_seconds)
        logger.info("  Log level:             %s", self.log_level)
        logger.info("  Group scope:           %s", _format_scope(self.target_groups))
        logger.info("  Project scope:         %s", _format_scope(self.target_projects))


def _parse_base_url(raw: str, *, org_override: str | None = None) -> tuple[str, str]:
    """Extract the API base URL and org from a portal URL.

    Org resolution priority:
        1. org_override (from SPECTRA_ASSURE_ORG env var)
        2. First path segment from URL (e.g. /acme-corp)
        3. Subdomain extraction: first label of hostname, capitalized
    """
    # Case-insensitive: "HTTPS://host/org" would otherwise be prefixed again, and
    # urlparse then reads "HTTPS" as the host and the real host as the org.
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)

    # Checked before anything else is derived from the URL. netloc carries any
    # user:password@ through verbatim, and base_url is logged by log_settings,
    # interpolated into every client DEBUG line, and embedded in every APIError message —
    # which surfaces at the default INFO level via logger.exception. Masking at each of
    # those sites is whack-a-mole; keeping credentials out of base_url is not.
    #
    # Rejected rather than stripped, because such a URL cannot be a working deployment:
    # requests builds an Authorization: Basic header from the userinfo and overwrites the
    # Bearer token this API actually needs, so the deployment is already 401-ing. Failing
    # at startup with the reason beats a 401 storm whose diagnosis leaks the password.
    if "@" in parsed.netloc:
        raise ConfigError(
            "SPECTRA_ASSURE_BASE_URL must not contain credentials. Remove the "
            "'user:password@' portion and authenticate with SPECTRA_API_TOKEN — "
            "requests would otherwise replace the Bearer token with Basic auth."
        )

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    if org_override:
        org = org_override
    elif path_parts:
        org = path_parts[0]
    else:
        # Subdomain fallback: first label of hostname, capitalized
        hostname = parsed.hostname or ""
        parts = hostname.split(".")
        if len(parts) >= 2 and parts[0]:
            org = parts[0].capitalize()
        else:
            raise ConfigError(
                "Cannot determine organization. Either include the org in the URL path "
                "(e.g. https://my.secure.software/acme-corp), or set the SPECTRA_ASSURE_ORG "
                "environment variable."
            )

    # netloc, not a rebuild from parsed.hostname — that would strip the brackets an IPv6
    # literal needs and produce an unparseable https://2001:db8::1:8443/… .
    base_url = f"{parsed.scheme}://{parsed.netloc}/api/public/v1"

    return base_url, org


def _parse_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got: {value}")
    # Python ints are unbounded; the C-level conversions downstream are not. Without this
    # the value is accepted here and blows up later — `timedelta(days=...)` at the top of
    # run_cycle, or `Event.wait(seconds)` in the periodic loop — where in periodic mode it
    # becomes a crash/sleep/crash loop that deletes nothing forever while exiting 0.
    if value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got: {value}")
    return value


def _parse_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got: {raw!r}") from exc
    # Checked before the range test, which both nan and inf slip past: `nan < minimum` is
    # False. A nan delay then also fails `if self.request_delay > 0`, silently removing
    # the inter-request pacing while the startup banner reports "Request delay: nans".
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number, got: {raw!r}")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got: {value}")
    # Same reason as _parse_int's ceiling: 1e17 passes isfinite and the minimum, then
    # OverflowErrors inside time.sleep on every single API call.
    if value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got: {value}")
    return value


def _parse_csv_set(name: str) -> frozenset[str]:
    """Parse a comma-separated env var into a set of stripped, non-empty values."""
    raw = os.environ.get(name)
    result = frozenset(item.strip() for item in (raw or "").split(",") if item.strip())
    if raw is not None and not result:
        # from_env() runs before logging is configured, so a logger call here would go out
        # through logging.lastResort: unformatted, and invisible to a structured-log
        # pipeline. Print to stderr like the ConfigError handler in __main__ instead —
        # that also keeps the warning unconditional, where routing it through the root
        # logger would let LOG_LEVEL=ERROR silence the one signal that scope widened.
        print(
            f"WARNING: {name} is set but contains no usable names — "
            "treating as no scope (all). Check for stray whitespace or commas.",
            file=sys.stderr,
        )
    return result


def _format_scope(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "(all)"
