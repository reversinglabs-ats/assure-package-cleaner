"""Tests for assure_package_cleaner.config."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from assure_package_cleaner.config import Config, ConfigError, _parse_base_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_ENV = {
    "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/acme-corp",
    "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
}


def _env(**overrides: str) -> dict[str, str]:
    """Return a minimal valid env dict, with optional overrides/additions."""
    merged = {**_REQUIRED_ENV, **overrides}
    return merged


# ---------------------------------------------------------------------------
# Required env vars
# ---------------------------------------------------------------------------


class TestRequiredEnvVars:
    def test_missing_base_url_raises(self):
        with patch.dict("os.environ", {"SPECTRA_API_TOKEN": "tok"}, clear=True):
            with pytest.raises(ConfigError, match="SPECTRA_ASSURE_BASE_URL is required"):
                Config.from_env()

    def test_empty_base_url_raises(self):
        with patch.dict(
            "os.environ",
            {"SPECTRA_ASSURE_BASE_URL": "", "SPECTRA_API_TOKEN": "tok"},
            clear=True,
        ):
            with pytest.raises(ConfigError, match="SPECTRA_ASSURE_BASE_URL is required"):
                Config.from_env()

    def test_missing_api_token_raises(self):
        with patch.dict(
            "os.environ",
            {"SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/org"},
            clear=True,
        ):
            with pytest.raises(ConfigError, match="SPECTRA_API_TOKEN is required"):
                Config.from_env()

    def test_empty_api_token_raises(self):
        with patch.dict(
            "os.environ",
            {"SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/org", "SPECTRA_API_TOKEN": ""},
            clear=True,
        ):
            with pytest.raises(ConfigError, match="SPECTRA_API_TOKEN is required"):
                Config.from_env()


# ---------------------------------------------------------------------------
# Valid env vars produce correct Config
# ---------------------------------------------------------------------------


class TestValidConfig:
    def test_minimal_required_env(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()

        assert cfg.base_url == "https://my.secure.software/api/public/v1"
        assert cfg.org == "acme-corp"
        assert cfg.api_token == "tok_1234567890abcdef"

    def test_all_custom_values(self):
        env = _env(
            STALE_THRESHOLD_DAYS="30",
            CLEANUP_INTERVAL_HOURS="12",
            DRY_RUN="false",
            REQUEST_DELAY_SECONDS="1.5",
            LOG_LEVEL="debug",
        )
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()

        assert cfg.stale_threshold_days == 30
        assert cfg.cleanup_interval_hours == 12
        assert cfg.dry_run is False
        assert cfg.request_delay_seconds == 1.5
        assert cfg.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_stale_threshold_days_default(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.stale_threshold_days == 180

    def test_cleanup_interval_hours_default(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.cleanup_interval_hours == 24

    def test_dry_run_default_is_true(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is True

    def test_request_delay_default(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.request_delay_seconds == 0.5

    def test_log_level_default(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.log_level == "INFO"


# ---------------------------------------------------------------------------
# DRY_RUN parsing
# ---------------------------------------------------------------------------


class TestDryRunParsing:
    @pytest.mark.parametrize("value", ["false", "False", "FALSE"])
    def test_false_values(self, value: str):
        with patch.dict("os.environ", _env(DRY_RUN=value), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is False

    @pytest.mark.parametrize("value", ["0"])
    def test_zero_is_false(self, value: str):
        with patch.dict("os.environ", _env(DRY_RUN=value), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is False

    @pytest.mark.parametrize("value", ["no", "No", "NO"])
    def test_no_is_false(self, value: str):
        with patch.dict("os.environ", _env(DRY_RUN=value), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_true_values(self, value: str):
        with patch.dict("os.environ", _env(DRY_RUN=value), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is True

    @pytest.mark.parametrize("value", ["1", "yes", "Yes", "YES"])
    def test_yes_and_one_are_true(self, value: str):
        with patch.dict("os.environ", _env(DRY_RUN=value), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is True

    def test_arbitrary_string_is_true(self):
        """Any value not in ('false', '0', 'no') is treated as truthy."""
        with patch.dict("os.environ", _env(DRY_RUN="anything"), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is True


# ---------------------------------------------------------------------------
# _parse_base_url
# ---------------------------------------------------------------------------


class TestParseBaseUrl:
    def test_full_https_url(self):
        base_url, org = _parse_base_url("https://my.secure.software/acme-corp")
        assert base_url == "https://my.secure.software/api/public/v1"
        assert org == "acme-corp"

    def test_http_url_preserved(self):
        base_url, org = _parse_base_url("http://localhost:8080/test-org")
        assert base_url == "http://localhost:8080/api/public/v1"
        assert org == "test-org"

    def test_url_without_scheme_gets_https(self):
        base_url, org = _parse_base_url("my.secure.software/acme-corp")
        assert base_url == "https://my.secure.software/api/public/v1"
        assert org == "acme-corp"

    def test_url_with_trailing_slash(self):
        base_url, org = _parse_base_url("https://my.secure.software/acme-corp/")
        assert base_url == "https://my.secure.software/api/public/v1"
        assert org == "acme-corp"

    def test_url_with_extra_path_segments(self):
        """Only the first path segment is taken as the org."""
        base_url, org = _parse_base_url("https://my.secure.software/acme-corp/extra/path")
        assert org == "acme-corp"
        assert base_url == "https://my.secure.software/api/public/v1"

    def test_subdomain_fallback_example(self):
        base_url, org = _parse_base_url("https://example.secure.software")
        assert base_url == "https://example.secure.software/api/public/v1"
        assert org == "Example"

    def test_subdomain_fallback_trial(self):
        base_url, org = _parse_base_url("https://trial.secure.software")
        assert base_url == "https://trial.secure.software/api/public/v1"
        assert org == "Trial"

    def test_subdomain_fallback_with_trailing_slash(self):
        base_url, org = _parse_base_url("https://example.secure.software/")
        assert base_url == "https://example.secure.software/api/public/v1"
        assert org == "Example"

    def test_org_override_wins_over_path(self):
        base_url, org = _parse_base_url(
            "https://my.secure.software/acme-corp", org_override="custom-org"
        )
        assert org == "custom-org"
        assert base_url == "https://my.secure.software/api/public/v1"

    def test_org_override_wins_over_subdomain(self):
        base_url, org = _parse_base_url("https://example.secure.software", org_override="my-org")
        assert org == "my-org"
        assert base_url == "https://example.secure.software/api/public/v1"

    def test_localhost_no_subdomain_no_path_raises(self):
        with pytest.raises(ConfigError, match="Cannot determine organization"):
            _parse_base_url("http://localhost:8080")


# ---------------------------------------------------------------------------
# STALE_THRESHOLD_DAYS validation
# ---------------------------------------------------------------------------


class TestStaleThresholdDays:
    def test_non_integer_raises(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="abc"), clear=True):
            with pytest.raises(ConfigError, match="must be an integer"):
                Config.from_env()

    def test_float_string_raises(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="3.5"), clear=True):
            with pytest.raises(ConfigError, match="must be an integer"):
                Config.from_env()

    def test_zero_raises(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="0"), clear=True):
            with pytest.raises(ConfigError, match="must be >= 1"):
                Config.from_env()

    def test_negative_raises(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="-5"), clear=True):
            with pytest.raises(ConfigError, match="must be >= 1"):
                Config.from_env()

    def test_minimum_valid_value(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="1"), clear=True):
            cfg = Config.from_env()
        assert cfg.stale_threshold_days == 1


# ---------------------------------------------------------------------------
# CLEANUP_INTERVAL_HOURS validation
# ---------------------------------------------------------------------------


class TestCleanupIntervalHours:
    def test_zero_is_valid(self):
        """Zero means single-run mode."""
        with patch.dict("os.environ", _env(CLEANUP_INTERVAL_HOURS="0"), clear=True):
            cfg = Config.from_env()
        assert cfg.cleanup_interval_hours == 0

    def test_negative_raises(self):
        with patch.dict("os.environ", _env(CLEANUP_INTERVAL_HOURS="-1"), clear=True):
            with pytest.raises(ConfigError, match="must be >= 0"):
                Config.from_env()

    def test_non_integer_raises(self):
        with patch.dict("os.environ", _env(CLEANUP_INTERVAL_HOURS="abc"), clear=True):
            with pytest.raises(ConfigError, match="must be an integer"):
                Config.from_env()


# ---------------------------------------------------------------------------
# REQUEST_DELAY_SECONDS validation
# ---------------------------------------------------------------------------


class TestRequestDelay:
    def test_zero_is_valid(self):
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS="0"), clear=True):
            cfg = Config.from_env()
        assert cfg.request_delay_seconds == 0.0

    def test_negative_raises(self):
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS="-0.1"), clear=True):
            with pytest.raises(ConfigError, match="must be >= 0"):
                Config.from_env()

    def test_non_numeric_raises(self):
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS="fast"), clear=True):
            with pytest.raises(ConfigError, match="must be a number"):
                Config.from_env()


# ---------------------------------------------------------------------------
# Config is frozen (immutable)
# ---------------------------------------------------------------------------


class TestConfigFrozen:
    def test_cannot_mutate_config(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        with pytest.raises(AttributeError):
            cfg.dry_run = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Whitespace-only env vars rejected
# ---------------------------------------------------------------------------


class TestWhitespaceEnvVars:
    def test_whitespace_only_base_url_raises(self):
        with patch.dict(
            "os.environ",
            {"SPECTRA_ASSURE_BASE_URL": "   ", "SPECTRA_API_TOKEN": "tok_1234567890abcdef"},
            clear=True,
        ):
            with pytest.raises(ConfigError, match="SPECTRA_ASSURE_BASE_URL is required"):
                Config.from_env()

    def test_whitespace_only_api_token_raises(self):
        with patch.dict(
            "os.environ",
            {
                "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/org",
                "SPECTRA_API_TOKEN": "   ",
            },
            clear=True,
        ):
            with pytest.raises(ConfigError, match="SPECTRA_API_TOKEN is required"):
                Config.from_env()

    def test_whitespace_padded_dry_run_false(self):
        """DRY_RUN=' false ' should be treated as false after stripping."""
        with patch.dict("os.environ", _env(DRY_RUN=" false "), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is False

    def test_whitespace_padded_dry_run_no(self):
        with patch.dict("os.environ", _env(DRY_RUN=" no "), clear=True):
            cfg = Config.from_env()
        assert cfg.dry_run is False


# ---------------------------------------------------------------------------
# Token masking safety
# ---------------------------------------------------------------------------


class TestTokenMasking:
    def test_long_token_is_partially_masked(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        # Token is "tok_1234567890abcdef" (20 chars) -> should show first 4 + **** + last 4
        import io
        import logging

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("assure_package_cleaner.config")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            cfg.log_settings()
            output = handler.stream.getvalue()
            assert "tok_" in output
            assert "cdef" in output
            assert "tok_1234567890abcdef" not in output
        finally:
            logger.removeHandler(handler)

    def test_short_token_fully_masked(self):
        with patch.dict(
            "os.environ",
            {
                "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/org",
                "SPECTRA_API_TOKEN": "abcd",
            },
            clear=True,
        ):
            cfg = Config.from_env()

        import io
        import logging

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("assure_package_cleaner.config")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            cfg.log_settings()
            output = handler.stream.getvalue()
            assert "abcd" not in output
            assert "****" in output
        finally:
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# SPECTRA_ASSURE_ORG override
# ---------------------------------------------------------------------------


class TestSpectraAssureOrg:
    def test_org_env_var_overrides_url_path(self):
        env = {
            "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/acme-corp",
            "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
            "SPECTRA_ASSURE_ORG": "custom-org",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()
        assert cfg.org == "custom-org"
        assert cfg.base_url == "https://my.secure.software/api/public/v1"

    def test_org_env_var_with_pathless_url(self):
        env = {
            "SPECTRA_ASSURE_BASE_URL": "https://example.secure.software",
            "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
            "SPECTRA_ASSURE_ORG": "custom-org",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()
        assert cfg.org == "custom-org"

    def test_org_env_var_whitespace_stripped(self):
        env = {
            "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/acme-corp",
            "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
            "SPECTRA_ASSURE_ORG": "  my-org  ",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()
        assert cfg.org == "my-org"

    def test_org_env_var_empty_string_ignored(self):
        """Empty SPECTRA_ASSURE_ORG should fall through to URL path parsing."""
        env = {
            "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/acme-corp",
            "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
            "SPECTRA_ASSURE_ORG": "",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()
        assert cfg.org == "acme-corp"
