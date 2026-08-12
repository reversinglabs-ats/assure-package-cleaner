"""Tests for assure_package_cleaner.__main__.

Only the config-to-cleaner wiring is covered. It is the one path where a bug
(e.g. passing target_projects as target_groups) widens the walk instead of
narrowing it, so a scoped run would delete across the whole org.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from assure_package_cleaner.__main__ import _shutdown, main

_ENV = {
    "SPECTRA_ASSURE_BASE_URL": "https://my.secure.software/acme-corp",
    "SPECTRA_API_TOKEN": "tok_1234567890abcdef",
    "CLEANUP_INTERVAL_HOURS": "0",
    "SPECTRA_ASSURE_GROUP": "grp-a,grp-b",
    "SPECTRA_ASSURE_PROJECT": "proj-x",
}


def _run_main(env: dict[str, str]) -> MagicMock:
    """Run main() in single-run mode with the client/cleaner stubbed out.

    Returns the patched Cleaner class so callers can inspect its call kwargs.
    """
    with patch.dict("os.environ", env, clear=True):
        with (
            patch("assure_package_cleaner.__main__.SpectraClient"),
            patch("assure_package_cleaner.__main__.Cleaner") as cleaner_cls,
            patch("assure_package_cleaner.__main__.signal.signal"),
            # main() calls basicConfig, which installs a root handler and sets the root
            # level for the rest of the session. Keep that out of the other tests.
            patch("assure_package_cleaner.__main__.logging.basicConfig"),
        ):
            main()
    return cleaner_cls


class TestMainWiring:
    def test_scope_is_wired_to_the_cleaner(self):
        cleaner_cls = _run_main(_ENV)

        kwargs = cleaner_cls.call_args.kwargs
        assert kwargs["target_groups"] == frozenset({"grp-a", "grp-b"})
        assert kwargs["target_projects"] == frozenset({"proj-x"})

    def test_group_scope_survives_an_unset_project_scope(self):
        """Transposing the two fields would leave target_groups empty here — i.e. org-wide."""
        env = {k: v for k, v in _ENV.items() if k != "SPECTRA_ASSURE_PROJECT"}
        cleaner_cls = _run_main(env)

        kwargs = cleaner_cls.call_args.kwargs
        assert kwargs["target_groups"] == frozenset({"grp-a", "grp-b"})
        assert kwargs["target_projects"] == frozenset()

    def test_dry_run_default_reaches_the_cleaner(self):
        """DRY_RUN's default must survive the wiring — hardcoding dry_run=False here
        would turn every run into a live deletion run."""
        env = {k: v for k, v in _ENV.items() if k != "DRY_RUN"}
        cleaner_cls = _run_main(env)

        assert cleaner_cls.call_args.kwargs["dry_run"] is True

    def test_dry_run_false_reaches_the_cleaner(self):
        cleaner_cls = _run_main({**_ENV, "DRY_RUN": "false"})

        assert cleaner_cls.call_args.kwargs["dry_run"] is False

    def test_threshold_and_shutdown_are_wired(self):
        cleaner_cls = _run_main({**_ENV, "STALE_THRESHOLD_DAYS": "42"})

        kwargs = cleaner_cls.call_args.kwargs
        assert kwargs["stale_threshold_days"] == 42
        # The signal handlers set this exact event; a fresh one would ignore SIGTERM.
        assert kwargs["shutdown"] is _shutdown

    def test_client_gets_the_parsed_url_org_and_delay(self):
        with patch.dict("os.environ", {**_ENV, "REQUEST_DELAY_SECONDS": "2.5"}, clear=True):
            with (
                patch("assure_package_cleaner.__main__.SpectraClient") as client_cls,
                patch("assure_package_cleaner.__main__.Cleaner"),
                patch("assure_package_cleaner.__main__.signal.signal"),
                patch("assure_package_cleaner.__main__.logging.basicConfig"),
            ):
                main()

        kwargs = client_cls.call_args.kwargs
        assert kwargs["base_url"] == "https://my.secure.software/api/public/v1"
        assert kwargs["org"] == "acme-corp"
        assert kwargs["api_token"] == "tok_1234567890abcdef"
        assert kwargs["request_delay"] == 2.5

    def test_single_run_mode_runs_one_cycle(self):
        cleaner_cls = _run_main(_ENV)

        cleaner_cls.return_value.run_cycle.assert_called_once_with()
