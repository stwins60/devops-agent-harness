"""Minimal JSON HTTP client on top of urllib (no third-party dependency).

Used by the Jira, GitHub, GitLab, Prometheus and generic REST adapters.
Errors are converted to ToolError with a classified ``kind`` so the recovery
layer can decide between retry, alternative API or human escalation.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from tools.base import ToolError


class HttpClient:
    def __init__(self, base_url: str, *, token: Optional[str] = None, basic_user: Optional[str] = None,
                 basic_password: Optional[str] = None, headers: Optional[dict[str, str]] = None, timeout: int = 30,
                 max_retries: int = 2, token_header: str = "Authorization", token_prefix: str = "Bearer") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {"Accept": "application/json", "User-Agent": "devops-agent-harness/0.1"}
        if headers:
            self.headers.update(headers)
        if token:
            self.headers[token_header] = f"{token_prefix} {token}".strip() if token_prefix else token
        elif basic_user and basic_password:
            raw = f"{basic_user}:{basic_password}".encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

    def request(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None, body: Any = None,
                raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read().decode("utf-8", errors="replace")
                    if raw or not payload:
                        return payload
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        return payload
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
                if exc.code in (429, 502, 503, 504) and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 8) * 0.25)
                    continue
                raise ToolError(f"HTTP {exc.code} {method} {url}: {text[:500]}", kind=_kind_for_status(exc.code),
                                advice=_advice_for_status(exc.code)) from exc
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 8) * 0.25)
                    continue
                raise ToolError(f"network error {method} {url}: {exc.reason}", kind="network",
                                advice="verify the endpoint is reachable (DNS, VPN, firewall) before retrying") from exc
            except TimeoutError as exc:
                raise ToolError(f"timeout {method} {url}", kind="timeout") from exc

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, body=body, **kw)

    def patch(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", path, body=body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)


def _kind_for_status(code: int) -> str:
    if code in (401,):
        return "auth"
    if code in (403,):
        return "permission"
    if code == 404:
        return "not_found"
    if code == 429:
        return "rate_limit"
    if code in (400, 422):
        return "invalid"
    if code >= 500:
        return "network"
    return "unknown"


def _advice_for_status(code: int) -> str:
    return {
        401: "credentials rejected: check the token/env var; do not retry blindly",
        403: "permission denied: the token lacks the required scope; do not retry",
        404: "resource not found: verify identifiers (issue key, repo, project)",
        429: "rate limited: back off and reduce request volume",
        422: "request rejected as invalid: review payload (e.g. branch already has a PR, transition not allowed)",
    }.get(code, "server error: retry later or escalate")
