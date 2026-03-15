"""Configuration parsed from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


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

        stale_threshold_days = _parse_int("STALE_THRESHOLD_DAYS", 180, minimum=1)
        cleanup_interval_hours = _parse_int("CLEANUP_INTERVAL_HOURS", 24, minimum=0)
        request_delay_seconds = _parse_float("REQUEST_DELAY_SECONDS", 0.5, minimum=0.0)
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        dry_run_raw = os.environ.get("DRY_RUN", "true").strip().lower()
        dry_run = dry_run_raw not in ("false", "0", "no")

        return cls(
            base_url=base_url,
            org=org,
            api_token=api_token,
            stale_threshold_days=stale_threshold_days,
            cleanup_interval_hours=cleanup_interval_hours,
            dry_run=dry_run,
            request_delay_seconds=request_delay_seconds,
            log_level=log_level,
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


def _parse_base_url(raw: str, *, org_override: str | None = None) -> tuple[str, str]:
    """Extract the API base URL and org from a portal URL.

    Org resolution priority:
        1. org_override (from SPECTRA_ASSURE_ORG env var)
        2. First path segment from URL (e.g. /acme-corp)
        3. Subdomain extraction: first label of hostname, capitalized
    """
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
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

    base_url = f"{parsed.scheme}://{parsed.netloc}/api/public/v1"

    return base_url, org


def _parse_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got: {value}")
    return value


def _parse_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got: {value}")
    return value
