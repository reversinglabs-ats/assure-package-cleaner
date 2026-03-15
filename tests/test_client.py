"""Tests for assure_package_cleaner.client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from assure_package_cleaner.client import APIError, SpectraClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(**kwargs: object) -> SpectraClient:
    defaults: dict[str, object] = {
        "base_url": "https://my.secure.software/api/public/v1",
        "org": "acme",
        "api_token": "test-token",
        "request_delay": 0,  # no delay in tests
    }
    defaults.update(kwargs)
    return SpectraClient(**defaults)  # type: ignore[arg-type]


def _ok_response(json_data: dict | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = ""
    return resp


def _error_response(status_code: int = 500, body: str = "Internal Server Error") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.json.side_effect = ValueError("not json")
    return resp


# ---------------------------------------------------------------------------
# list_groups
# ---------------------------------------------------------------------------


class TestListGroups:
    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_groups(self, mock_get: MagicMock):
        groups = [{"name": "group-a"}, {"name": "group-b"}]
        mock_get.return_value = _ok_response({"groups": groups})
        client = _make_client()

        result = client.list_groups()

        assert result == groups
        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        assert url == "https://my.secure.software/api/public/v1/list/acme"

    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_empty_list_when_key_missing(self, mock_get: MagicMock):
        mock_get.return_value = _ok_response({})
        client = _make_client()
        assert client.list_groups() == []

    @patch("assure_package_cleaner.client.requests.get")
    def test_raises_api_error_on_non_200(self, mock_get: MagicMock):
        mock_get.return_value = _error_response(403, "Forbidden")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.list_groups()
        assert exc_info.value.status_code == 403
        assert "403" in str(exc_info.value)


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_projects(self, mock_get: MagicMock):
        projects = [{"name": "project-1"}]
        mock_get.return_value = _ok_response({"projects": projects})
        client = _make_client()

        result = client.list_projects("group-a")

        assert result == projects
        url = mock_get.call_args[0][0]
        assert "/list/acme/group-a" in url

    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_empty_list_when_key_missing(self, mock_get: MagicMock):
        mock_get.return_value = _ok_response({})
        client = _make_client()
        assert client.list_projects("group-a") == []


# ---------------------------------------------------------------------------
# list_packages
# ---------------------------------------------------------------------------


class TestListPackages:
    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_packages(self, mock_get: MagicMock):
        packages = [{"name": "my-pkg"}]
        mock_get.return_value = _ok_response({"packages": packages})
        client = _make_client()

        result = client.list_packages("group-a", "project-1")

        assert result == packages
        url = mock_get.call_args[0][0]
        assert "/list/acme/group-a/pkg:rl/project-1" in url

    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_empty_list_when_key_missing(self, mock_get: MagicMock):
        mock_get.return_value = _ok_response({})
        client = _make_client()
        assert client.list_packages("group-a", "project-1") == []


# ---------------------------------------------------------------------------
# list_versions
# ---------------------------------------------------------------------------


class TestListVersions:
    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_versions(self, mock_get: MagicMock):
        versions = [{"version": "1.0.0"}, {"version": "2.0.0"}]
        mock_get.return_value = _ok_response({"versions": versions})
        client = _make_client()

        result = client.list_versions("g", "p", "pkg")

        assert result == versions
        url = mock_get.call_args[0][0]
        assert "/list/acme/g/pkg:rl/p/pkg" in url

    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_empty_list_when_key_missing(self, mock_get: MagicMock):
        mock_get.return_value = _ok_response({})
        client = _make_client()
        assert client.list_versions("g", "p", "pkg") == []


# ---------------------------------------------------------------------------
# get_version_status
# ---------------------------------------------------------------------------


class TestGetVersionStatus:
    @patch("assure_package_cleaner.client.requests.get")
    def test_returns_status_dict(self, mock_get: MagicMock):
        status = {"analysis": {"timestamp": "2025-01-01T00:00:00Z"}}
        mock_get.return_value = _ok_response(status)
        client = _make_client()

        result = client.get_version_status("g", "p", "pkg", "1.0.0")

        assert result == status
        url = mock_get.call_args[0][0]
        assert "/status/acme/g/pkg:rl/p/pkg@1.0.0" in url

    @patch("assure_package_cleaner.client.requests.get")
    def test_raises_on_non_200(self, mock_get: MagicMock):
        mock_get.return_value = _error_response(404, "Not Found")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.get_version_status("g", "p", "pkg", "1.0.0")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# delete_package
# ---------------------------------------------------------------------------


class TestDeletePackage:
    @patch("assure_package_cleaner.client.requests.delete")
    def test_succeeds_on_204(self, mock_delete: MagicMock):
        mock_delete.return_value = _ok_response(status_code=204)
        client = _make_client()

        # Should not raise
        client.delete_package("g", "p", "pkg")

        mock_delete.assert_called_once()
        url = mock_delete.call_args[0][0]
        assert "/delete/acme/g/pkg:rl/p/pkg" in url

    @patch("assure_package_cleaner.client.requests.delete")
    def test_succeeds_on_200(self, mock_delete: MagicMock):
        mock_delete.return_value = _ok_response(status_code=200)
        client = _make_client()

        # Should not raise
        client.delete_package("g", "p", "pkg")

    @patch("assure_package_cleaner.client.requests.delete")
    def test_raises_on_non_2xx(self, mock_delete: MagicMock):
        mock_delete.return_value = _error_response(403, "Forbidden")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.delete_package("g", "p", "pkg")
        assert exc_info.value.status_code == 403

    @patch("assure_package_cleaner.client.requests.delete")
    def test_raises_on_500(self, mock_delete: MagicMock):
        mock_delete.return_value = _error_response(500, "Internal Server Error")
        client = _make_client()

        with pytest.raises(APIError):
            client.delete_package("g", "p", "pkg")


# ---------------------------------------------------------------------------
# Network exceptions
# ---------------------------------------------------------------------------


class TestNetworkExceptions:
    @patch("assure_package_cleaner.client.requests.get")
    def test_get_timeout_raises_api_error(self, mock_get: MagicMock):
        mock_get.side_effect = requests.Timeout("Connection timed out")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.list_groups()
        assert exc_info.value.status_code == 0
        assert "Connection timed out" in str(exc_info.value)

    @patch("assure_package_cleaner.client.requests.get")
    def test_get_connection_error_raises_api_error(self, mock_get: MagicMock):
        mock_get.side_effect = requests.ConnectionError("Failed to connect")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.list_groups()
        assert exc_info.value.status_code == 0
        assert "Failed to connect" in str(exc_info.value)

    @patch("assure_package_cleaner.client.requests.delete")
    def test_delete_timeout_raises_api_error(self, mock_delete: MagicMock):
        mock_delete.side_effect = requests.Timeout("Connection timed out")
        client = _make_client()

        with pytest.raises(APIError) as exc_info:
            client.delete_package("g", "p", "pkg")
        assert exc_info.value.status_code == 0
        assert "Connection timed out" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Authorization header
# ---------------------------------------------------------------------------


class TestAuthHeader:
    @patch("assure_package_cleaner.client.requests.get")
    def test_bearer_token_sent(self, mock_get: MagicMock):
        mock_get.return_value = _ok_response({"groups": []})
        client = _make_client(api_token="my-secret-token")

        client.list_groups()

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer my-secret-token"


# ---------------------------------------------------------------------------
# _delay
# ---------------------------------------------------------------------------


class TestDelay:
    @patch("assure_package_cleaner.client.time.sleep")
    @patch("assure_package_cleaner.client.requests.get")
    def test_delay_called_when_positive(self, mock_get: MagicMock, mock_sleep: MagicMock):
        mock_get.return_value = _ok_response({"groups": []})
        client = _make_client(request_delay=1.0)

        client.list_groups()

        mock_sleep.assert_called_once_with(1.0)

    @patch("assure_package_cleaner.client.time.sleep")
    @patch("assure_package_cleaner.client.requests.get")
    def test_delay_not_called_when_zero(self, mock_get: MagicMock, mock_sleep: MagicMock):
        mock_get.return_value = _ok_response({"groups": []})
        client = _make_client(request_delay=0)

        client.list_groups()

        mock_sleep.assert_not_called()

    @patch("assure_package_cleaner.client.time.sleep")
    @patch("assure_package_cleaner.client.requests.delete")
    def test_delay_called_on_delete(self, mock_delete: MagicMock, mock_sleep: MagicMock):
        mock_delete.return_value = _ok_response(status_code=204)
        client = _make_client(request_delay=0.25)

        client.delete_package("g", "p", "pkg")

        mock_sleep.assert_called_once_with(0.25)


# ---------------------------------------------------------------------------
# APIError attributes
# ---------------------------------------------------------------------------


class TestAPIError:
    def test_error_message_format(self):
        err = APIError("GET", "https://example.com/api", 404, "Not Found")
        assert err.status_code == 404
        assert "GET" in str(err)
        assert "404" in str(err)
        assert "Not Found" in str(err)
        assert "https://example.com/api" in str(err)
