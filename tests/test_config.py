"""Tests for assure_package_cleaner.config."""

from __future__ import annotations

import io
import logging
from unittest.mock import patch

import pytest

from assure_package_cleaner.config import (
    _VALID_LOG_LEVELS,
    Config,
    ConfigError,
    _parse_base_url,
)

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

    @pytest.mark.parametrize("raw", ["100000000", "999999999999"])
    def test_absurdly_large_raises(self, raw):
        """Python ints are unbounded; timedelta(days=...) at the top of run_cycle is not.
        Accepted here, these reach OverflowError ("date value out of range", then "Python
        int too large to convert to C int") on every cycle — which in periodic mode is a
        crash/sleep/crash loop that deletes nothing forever while still exiting 0."""
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS=raw), clear=True):
            with pytest.raises(ConfigError, match="must be <="):
                Config.from_env()

    def test_the_ceiling_itself_is_accepted(self):
        with patch.dict("os.environ", _env(STALE_THRESHOLD_DAYS="36500"), clear=True):
            assert Config.from_env().stale_threshold_days == 36500

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

    def test_absurdly_large_raises(self):
        """Overflows Event.wait(seconds) in the periodic loop — and that call sits outside
        __main__'s try/except, so it takes the process down rather than being swallowed."""
        with patch.dict("os.environ", _env(CLEANUP_INTERVAL_HOURS="999999999999"), clear=True):
            with pytest.raises(ConfigError, match="must be <="):
                Config.from_env()

    def test_the_ceiling_itself_is_accepted(self):
        with patch.dict("os.environ", _env(CLEANUP_INTERVAL_HOURS="87600"), clear=True):
            assert Config.from_env().cleanup_interval_hours == 87600


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

    def test_absurdly_large_raises(self):
        """1e17 passes isfinite and the minimum, then OverflowErrors inside time.sleep on
        every API call — the crash/sleep/crash loop the _parse_int comment describes."""
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS="1e17"), clear=True):
            with pytest.raises(ConfigError, match="must be <="):
                Config.from_env()

    def test_the_ceiling_itself_is_accepted(self):
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS="3600"), clear=True):
            assert Config.from_env().request_delay_seconds == 3600.0

    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "infinity", "1e999"])
    def test_non_finite_raises(self, raw):
        """float() accepts all of these and the range check does not reject them —
        `nan < 0.0` is False. A nan delay then fails `if request_delay > 0` too, so the
        inter-request pacing silently disappears while the banner logs "Request delay:
        nans".

        The match is deliberately narrow. `isfinite` runs before the range check, so
        every value here — `-inf` included — must produce the *finite* error. Accepting
        "must be >= 0" as well would let the `-inf` case pass on the range check alone
        and stop discriminating against removal of the finite check.
        """
        with patch.dict("os.environ", _env(REQUEST_DELAY_SECONDS=raw), clear=True):
            with pytest.raises(ConfigError, match="must be a finite number"):
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


# ---------------------------------------------------------------------------
# Group/project scoping
# ---------------------------------------------------------------------------


class TestScopingConfig:
    def test_defaults_are_empty(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset()
        assert cfg.target_projects == frozenset()

    def test_group_single_value(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP="grp"), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset({"grp"})

    def test_group_comma_list(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP="a,b,c"), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset({"a", "b", "c"})

    def test_group_whitespace_stripped(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP="a, b , c"), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset({"a", "b", "c"})

    def test_group_empty_elements_dropped(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP="a,,b,"), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset({"a", "b"})

    def test_group_only_commas_is_empty(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP=" , , "), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset()

    def test_set_but_empty_warns(self, capsys):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP=" , , "), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset()
        assert "SPECTRA_ASSURE_GROUP is set but contains no usable names" in capsys.readouterr().err

    def test_group_set_to_empty_string_warns(self, capsys):
        """`-e SPECTRA_ASSURE_GROUP=` must not silently widen the walk to the whole org."""
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP=""), clear=True):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset()
        assert "SPECTRA_ASSURE_GROUP is set but contains no usable names" in capsys.readouterr().err

    def test_project_set_to_empty_string_warns(self, capsys):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_PROJECT=""), clear=True):
            cfg = Config.from_env()
        assert cfg.target_projects == frozenset()
        assert (
            "SPECTRA_ASSURE_PROJECT is set but contains no usable names" in capsys.readouterr().err
        )

    def test_group_and_project_are_not_transposed(self):
        with patch.dict(
            "os.environ",
            _env(SPECTRA_ASSURE_GROUP="grp-a", SPECTRA_ASSURE_PROJECT="proj-x"),
            clear=True,
        ):
            cfg = Config.from_env()
        assert cfg.target_groups == frozenset({"grp-a"})
        assert cfg.target_projects == frozenset({"proj-x"})

    def test_unset_does_not_warn(self, capsys):
        with patch.dict("os.environ", _env(), clear=True):
            Config.from_env()
        assert "no usable names" not in capsys.readouterr().err

    def test_warning_survives_logging_being_silenced(self, capsys):
        """The warning must not be suppressible by logging config — it is the only
        signal that a malformed scope var widened the walk to the whole org.

        Setting LOG_LEVEL would prove nothing here: from_env() only parses it into a
        string, and basicConfig runs later in main(). Silence logging for real instead.
        """
        root = logging.getLogger()
        handler = logging.StreamHandler(io.StringIO())
        root.addHandler(handler)
        original_level = root.level
        root.setLevel(logging.ERROR)
        logging.disable(logging.CRITICAL)
        try:
            with patch.dict("os.environ", _env(SPECTRA_ASSURE_GROUP=""), clear=True):
                Config.from_env()
        finally:
            logging.disable(logging.NOTSET)
            root.setLevel(original_level)
            root.removeHandler(handler)

        captured = capsys.readouterr()
        assert "no usable names" in captured.err
        assert captured.out == ""
        # Nothing reached the logging machinery, so nothing could have filtered it.
        assert handler.stream.getvalue() == ""

    def test_project_comma_list(self):
        with patch.dict("os.environ", _env(SPECTRA_ASSURE_PROJECT="p1,p2"), clear=True):
            cfg = Config.from_env()
        assert cfg.target_projects == frozenset({"p1", "p2"})

    def test_project_unset_is_empty(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()
        assert cfg.target_projects == frozenset()

    def test_log_settings_shows_scope(self):
        with patch.dict(
            "os.environ",
            _env(SPECTRA_ASSURE_GROUP="grp-a", SPECTRA_ASSURE_PROJECT="proj-x"),
            clear=True,
        ):
            cfg = Config.from_env()

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("assure_package_cleaner.config")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            cfg.log_settings()
            output = handler.stream.getvalue()
            assert "grp-a" in output
            assert "proj-x" in output
        finally:
            logger.removeHandler(handler)

    def test_log_settings_shows_all_when_unscoped(self):
        with patch.dict("os.environ", _env(), clear=True):
            cfg = Config.from_env()

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("assure_package_cleaner.config")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            cfg.log_settings()
            output = handler.stream.getvalue()
            assert "Group scope:           (all)" in output
            assert "Project scope:         (all)" in output
        finally:
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# LOG_LEVEL validation
# ---------------------------------------------------------------------------


class TestLogLevel:
    @pytest.mark.parametrize(
        "raw",
        ["debug", "INFO", "Warning", "error", "critical", "notset", "WARN", "warn", "FATAL"],
    )
    def test_valid_levels_any_case(self, raw):
        """WARN and FATAL are the ones that matter here. A hand-written allowlist omitted
        both — logging accepts them, so `-e LOG_LEVEL=WARN` was a working deployment that
        validation turned into an exit-1 crashloop on every start."""
        with patch.dict("os.environ", _env(LOG_LEVEL=raw), clear=True):
            assert Config.from_env().log_level == raw.upper()

    def test_the_allowlist_is_exactly_what_logging_accepts(self):
        """Derived, not hand-listed: anything basicConfig would take must pass the gate,
        or validation is stricter than the thing it stands in for."""
        assert set(_VALID_LOG_LEVELS) == set(logging.getLevelNamesMapping())

    @pytest.mark.parametrize("raw", ["verbose", "trace", "10", ""])
    def test_invalid_level_is_a_clean_config_error(self, raw):
        """LOG_LEVEL was the one variable that escaped the config gate: basicConfig raised
        a bare ValueError from __main__ *after* validation, so an operator got a traceback
        instead of "Configuration error:" + exit 1 — and a crashloop under a restart policy.
        """
        env = _env()
        env["LOG_LEVEL"] = raw
        with patch.dict("os.environ", env, clear=True):
            if raw == "":
                # Empty means "unset" for this variable, and keeps the INFO default.
                assert Config.from_env().log_level == "INFO"
            else:
                with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
                    Config.from_env()


# ---------------------------------------------------------------------------
# Base URL edge cases
# ---------------------------------------------------------------------------


class TestBaseUrlEdgeCases:
    @pytest.mark.parametrize("raw", ["HTTPS://my.secure.software/org", "HtTp://localhost:8080/org"])
    def test_scheme_check_is_case_insensitive(self, raw):
        """A capitalised scheme used to fail the startswith check, get prefixed again, and
        leave urlparse reading the scheme as the host: base_url https://HTTPS:/... with
        the real hostname as the org."""
        base_url, org = _parse_base_url(raw)
        assert org == "org"
        assert "HTTPS" not in base_url and "HtTp" not in base_url

    def test_credentials_are_masked_in_the_log(self, caplog):
        """The API token is masked in log_settings; a password embedded in the base URL
        was not, and survives into base_url verbatim."""
        env = _env(SPECTRA_ASSURE_BASE_URL="https://user:s3cr3t@my.secure.software/acme")
        with patch.dict("os.environ", env, clear=True):
            cfg = Config.from_env()

        handler = logging.StreamHandler(io.StringIO())
        log = logging.getLogger("assure_package_cleaner.config")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            cfg.log_settings()
            output = handler.stream.getvalue()
        finally:
            log.removeHandler(handler)

        assert "s3cr3t" not in output
        assert "user:****@my.secure.software" in output
