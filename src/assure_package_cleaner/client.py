"""HTTP client for the Spectra Assure Portal API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_DEFAULT_RETRY_AFTER = 60  # seconds; aligns with burst-limit window


def _parse_retry_after(resp: requests.Response) -> int:
    """Extract Retry-After seconds from response, with fallback."""
    raw = resp.headers.get("Retry-After")
    if raw is not None:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_RETRY_AFTER


class APIError(Exception):
    """Raised when the Spectra Assure API returns an error."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"{method} {url} returned {status_code}: {body}")


@dataclass
class SpectraClient:
    base_url: str
    org: str
    api_token: str
    request_delay: float = 0.5

    def list_groups(self) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self._q(self.org)}")
        return data.get("groups", [])

    def list_projects(self, group: str) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self._q(self.org)}/{self._q(group)}")
        return data.get("projects", [])

    def list_packages(self, group: str, project: str) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self._q(self.org)}/{self._q(group)}/pkg:rl/{self._q(project)}")
        return data.get("packages", [])

    def list_versions(self, group: str, project: str, package: str) -> list[dict[str, Any]]:
        data = self._get(
            f"/list/{self._q(self.org)}/{self._q(group)}"
            f"/pkg:rl/{self._q(project)}/{self._q(package)}"
        )
        return data.get("versions", [])

    def get_version_status(
        self, group: str, project: str, package: str, version: str
    ) -> dict[str, Any]:
        return self._get(
            f"/status/{self._q(self.org)}/{self._q(group)}"
            f"/pkg:rl/{self._q(project)}/{self._q(package)}@{self._q(version)}"
        )

    def delete_package(self, group: str, project: str, package: str) -> None:
        url = (
            f"{self.base_url}/delete/{self._q(self.org)}/{self._q(group)}"
            f"/pkg:rl/{self._q(project)}/{self._q(package)}"
        )
        resp = self._with_retry(
            "DELETE", url, lambda: requests.delete(url, headers=self._headers(), timeout=30)
        )
        if resp.status_code not in (200, 204):
            raise APIError("DELETE", url, resp.status_code, resp.text[:200])

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = self._with_retry(
            "GET", url, lambda: requests.get(url, headers=self._headers(), timeout=30)
        )
        if resp.status_code != 200:
            raise APIError("GET", url, resp.status_code, resp.text[:200])
        try:
            return resp.json()
        except ValueError as exc:
            raise APIError("GET", url, resp.status_code, "Response is not valid JSON") from exc

    def _with_retry(
        self,
        method: str,
        url: str,
        call: Callable[[], requests.Response],
    ) -> requests.Response:
        """Execute an HTTP request with rate-limit (429) retry handling."""
        for attempt in range(_MAX_RETRIES + 1):
            logger.debug("%s %s", method, url)
            self._delay()
            try:
                resp = call()
            except requests.RequestException as exc:
                raise APIError(method, url, 0, str(exc)) from exc
            if resp.status_code != 429 or attempt == _MAX_RETRIES:
                return resp
            wait = _parse_retry_after(resp)
            logger.warning(
                "Rate limited (429) on %s %s — retry %d/%d after %ds",
                method,
                url,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait)
        return resp  # unreachable, but satisfies type checker

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    @staticmethod
    def _q(value: str) -> str:
        return quote(value, safe="")

    def _delay(self) -> None:
        if self.request_delay > 0:
            time.sleep(self.request_delay)
