"""One HTTP request builder for every network call (no Qt).

Proxies come from the usual *_proxy environment variables (urllib reads them).
API tokens (GITHUB_TOKEN / GITLAB_TOKEN) are added only for the API hosts and
as unredirected headers, so a redirect to a download mirror never sees them."""

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

USER_AGENT = "DRSTool (+https://github.com/arabcian/DRSTool)"
_TOKENS = {"api.github.com": ("GITHUB_TOKEN", "token "), "gitlab.com": ("GITLAB_TOKEN", "Bearer ")}


def make_request(url: str, accept: str = "*/*", method: str = "GET", ua: str = USER_AGENT,
                 headers: Optional[Dict[str, str]] = None) -> urllib.request.Request:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": ua, "Accept": accept, **(headers or {})})
    tok = _TOKENS.get(urllib.parse.urlsplit(url).hostname or "")
    if tok and os.environ.get(tok[0]):
        req.add_unredirected_header("Authorization", tok[1] + os.environ[tok[0]])
    return req


def open_url(url: str, timeout: float = 25, **kw):
    return urllib.request.urlopen(make_request(url, **kw), timeout=timeout)


def rate_limit_note(e: urllib.error.HTTPError) -> str:
    """Human message if `e` is an API rate-limit answer, else ''."""
    if e.code == 429 or (e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0"):
        reset = e.headers.get("X-RateLimit-Reset") or ""
        when = time.strftime("%H:%M", time.localtime(int(reset))) if reset.isdigit() else "later"
        return (f"API rate limit reached — retry after {when}, or export "
                "GITHUB_TOKEN / GITLAB_TOKEN before starting DRSTool")
    return ""
