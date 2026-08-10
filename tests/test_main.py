"""Tests for assure_package_cleaner.__main__.

Only the config-to-cleaner wiring is covered. It is the one path where a bug
(e.g. passing target_projects as target_groups) widens the walk instead of
narrowing it, so a scoped run would delete across the whole org.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from assure_package_cleaner.__main__ import main

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

    def test_single_run_mode_runs_one_cycle(self):
        cleaner_cls = _run_main(_ENV)

        cleaner_cls.return_value.run_cycle.assert_called_once_with()
