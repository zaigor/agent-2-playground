from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "pm-scanner/0.1 (read-only market scanner)"


class HttpError(RuntimeError):
    """Raised for transport failures and non-2xx responses."""


class HttpClient:
    """Small requests wrapper with retries, timeouts and JSON decoding.

    Honors HTTPS_PROXY / REQUESTS_CA_BUNDLE from the environment (requests does
    this by default), so it works behind the Claude Code egress proxy once the
    destination hosts are allow-listed.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 0.5,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:  # network / proxy failures
            raise HttpError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise HttpError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def post_json(self, url: str, body: Any) -> Any:
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HttpError(f"POST {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise HttpError(f"POST {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
