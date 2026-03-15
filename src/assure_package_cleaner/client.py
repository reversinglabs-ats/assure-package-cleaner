"""HTTP client for the Spectra Assure Portal API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


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
        data = self._get(f"/list/{self.org}")
        return data.get("groups", [])

    def list_projects(self, group: str) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self.org}/{group}")
        return data.get("projects", [])

    def list_packages(self, group: str, project: str) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self.org}/{group}/pkg:rl/{project}")
        return data.get("packages", [])

    def list_versions(self, group: str, project: str, package: str) -> list[dict[str, Any]]:
        data = self._get(f"/list/{self.org}/{group}/pkg:rl/{project}/{package}")
        return data.get("versions", [])

    def get_version_status(
        self, group: str, project: str, package: str, version: str
    ) -> dict[str, Any]:
        return self._get(f"/status/{self.org}/{group}/pkg:rl/{project}/{package}@{version}")

    def delete_package(self, group: str, project: str, package: str) -> None:
        url = f"{self.base_url}/delete/{self.org}/{group}/pkg:rl/{project}/{package}"
        logger.debug("DELETE %s", url)
        self._delay()
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            raise APIError("DELETE", url, 0, str(exc)) from exc
        if resp.status_code not in (200, 204):
            raise APIError("DELETE", url, resp.status_code, resp.text[:200])

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        logger.debug("GET %s", url)
        self._delay()
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
        except requests.RequestException as exc:
            raise APIError("GET", url, 0, str(exc)) from exc
        if resp.status_code != 200:
            raise APIError("GET", url, resp.status_code, resp.text[:200])
        return resp.json()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _delay(self) -> None:
        if self.request_delay > 0:
            time.sleep(self.request_delay)
