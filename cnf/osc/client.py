"""cnf.osc.client — HTTP client for the CNF REST API, keyed off Keystone session."""
from __future__ import annotations

import httpx
from keystoneauth1.session import Session


class CnfClient:
    def __init__(self, endpoint: str, session: Session) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._ks_session = session

    def _get_token(self) -> str:
        return self._ks_session.get_token()

    def _headers(self) -> dict:
        return {
            "X-Auth-Token": self._get_token(),
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/v1{path}"

    def get(self, path: str, **kwargs) -> dict:
        with httpx.Client(timeout=30) as c:
            r = c.get(self._url(path), headers=self._headers(), **kwargs)
            r.raise_for_status()
            return r.json()

    def post(self, path: str, json: dict | None = None, **kwargs) -> dict:
        with httpx.Client(timeout=30) as c:
            r = c.post(self._url(path), headers=self._headers(), json=json, **kwargs)
            r.raise_for_status()
            return r.json()
