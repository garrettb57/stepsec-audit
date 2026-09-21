"""Thin GitHub REST + GraphQL client with rate-limit and retry handling.

Every response's X-RateLimit-* headers are tracked per resource bucket
(core, search, code_search, graphql) so we sleep *before* tripping a limit
instead of burning a request to find out.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Iterable, Sequence

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, user_agent: str = "stepsec-audit-crawler"):
        if not token:
            raise GitHubError("GITHUB_TOKEN is empty")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": user_agent,
            }
        )
        self.requests_made = 0
        self.requests_by_bucket: dict[str, int] = {}
        # bucket -> (remaining, reset_epoch)
        self._buckets: dict[str, tuple[int, int]] = {}
        # bucket -> limit, as reported by X-RateLimit-Limit
        self.limits: dict[str, int] = {}

    # ------------------------------------------------------------------ utils
    def _track(self, resp: requests.Response, default_bucket: str = "core") -> None:
        bucket = resp.headers.get("X-RateLimit-Resource") or default_bucket
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        limit = resp.headers.get("X-RateLimit-Limit")
        if remaining is not None and reset:
            self._buckets[bucket] = (int(remaining), int(reset))
        if limit and limit.isdigit():
            self.limits[bucket] = int(limit)
        self.requests_by_bucket[bucket] = self.requests_by_bucket.get(bucket, 0) + 1

    def _wait_if_exhausted(self, bucket: str) -> None:
        rem, reset = self._buckets.get(bucket, (1, 0))
        if rem <= 0:
            wait = max(1, reset - int(time.time()) + 2)
            log.info("bucket %s exhausted; sleeping %ss", bucket, wait)
            time.sleep(min(wait, 3600))

    @staticmethod
    def _retry_wait(resp: requests.Response, default: int = 60) -> int:
        ra = resp.headers.get("Retry-After")
        if ra and ra.isdigit():
            return min(int(ra) + 1, 3600)
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return min(max(1, int(reset) - int(time.time()) + 2), 3600)
        return default

    def bucket_status(self) -> dict[str, tuple[int, int]]:
        return dict(self._buckets)

    def check_budget(self, min_core: int = 5000) -> dict:
        """Fail fast on a token with the 1,000/hour GITHUB_TOKEN budget.
        GET /rate_limit does not count against any limit."""
        data = self.rest_json("/rate_limit")
        res = data.get("resources") or {}
        core = (res.get("core") or {}).get("limit", 0)
        gql = (res.get("graphql") or {}).get("limit", 0)
        for name, r in res.items():
            if isinstance(r, dict) and "limit" in r:
                self.limits[name] = r["limit"]
        if core < min_core:
            raise GitHubError(
                f"token core rate limit is {core}/hour (GraphQL {gql}); the crawler needs a personal access token "
                f"with {min_core}/hour. Add repo secret CRAWLER_TOKEN (fine-grained PAT, public repositories, read-only)."
            )
        log.info("rate limits: core %d/h, graphql %d/h, search %s/min, code_search %s/min", core, gql,
                 (res.get("search") or {}).get("limit"), (res.get("code_search") or {}).get("limit"))
        return {k: v.get("limit") for k, v in res.items() if isinstance(v, dict)}

    # ------------------------------------------------------------------- REST
    def rest(
        self,
        path: str,
        params: dict | None = None,
        method: str = "GET",
        json: Any = None,
        headers: dict | None = None,
        bucket: str = "core",
        retries: int = 8,
    ) -> requests.Response:
        url = path if path.startswith("http") else API + path
        backoff = 5
        for attempt in range(retries):
            self._wait_if_exhausted(bucket)
            try:
                resp = self.s.request(
                    method, url, params=params, json=json, headers=headers, timeout=90
                )
            except requests.RequestException as e:
                log.warning("network error %s (attempt %d): %s", url, attempt, e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            self.requests_made += 1
            self._track(resp, bucket)

            if resp.status_code in (403, 429):
                body = resp.text.lower()
                if (
                    resp.headers.get("X-RateLimit-Remaining") == "0"
                    or "rate limit" in body
                    or "abuse" in body
                    or resp.status_code == 429
                ):
                    wait = self._retry_wait(resp)
                    log.warning("rate limited on %s; sleeping %ss", path, wait)
                    time.sleep(wait)
                    continue
                raise GitHubError(f"{resp.status_code} {path}: {resp.text[:300]}")
            if resp.status_code >= 500 or resp.status_code == 408:
                log.warning("server %s on %s; retry in %ss", resp.status_code, path, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if resp.status_code >= 400:
                raise GitHubError(f"{resp.status_code} {path}: {resp.text[:300]}")
            return resp
        raise GitHubError(f"giving up on {path} after {retries} attempts")

    def rest_json(self, path: str, **kw) -> Any:
        return self.rest(path, **kw).json()

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 100) -> Iterable[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while page <= max_pages:
            params["page"] = page
            data = self.rest_json(path, params=params)
            if not data:
                return
            yield from data
            if len(data) < params["per_page"]:
                return
            page += 1

    # ----------------------------------------------------------------- search
    def search(self, kind: str, q: str, page: int = 1, per_page: int = 100, **extra) -> dict:
        """kind: 'code' | 'issues' | 'repositories' | 'commits'."""
        bucket = "code_search" if kind == "code" else "search"
        params = {"q": q, "page": page, "per_page": per_page, **extra}
        for attempt in range(4):
            resp = self.rest(f"/search/{kind}", params=params, bucket=bucket)
            try:
                data = resp.json()
            except ValueError as e:
                log.warning("search: undecodable response (attempt %d): %s", attempt, str(e)[:80])
                if attempt == 3:
                    raise GitHubError(f"search {kind}: undecodable response for {q!r} page {page}")
                time.sleep(5)
                continue
            if data.get("incomplete_results") and attempt < 3:
                log.info("incomplete_results for %r page %d; retrying", q, page)
                time.sleep(3)
                continue
            return data
        return data

    # ---------------------------------------------------------------- GraphQL
    def graphql(self, query: str, variables: dict | None = None, retries: int = 6) -> dict:
        backoff = 5
        for attempt in range(retries):
            self._wait_if_exhausted("graphql")
            try:
                resp = self.s.post(
                    API + "/graphql",
                    json={"query": query, "variables": variables or {}},
                    timeout=180,
                )
            except requests.RequestException as e:
                log.warning("graphql network error (attempt %d): %s", attempt, e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            self.requests_made += 1
            self._track(resp, "graphql")
            if resp.status_code in (403, 429):
                wait = self._retry_wait(resp)
                log.warning("graphql rate limited; sleeping %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                # 502 usually means the query was too heavy; caller shrinks batch.
                raise GitHubError(f"graphql {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise GitHubError(f"graphql {resp.status_code}: {resp.text[:400]}")
            try:
                data = resp.json()
            except ValueError as e:
                # truncated or non-JSON body; transient on GitHub's side. Retry, then let
                # run_batched shrink the batch by raising GitHubError.
                log.warning("graphql: undecodable response (%d bytes, attempt %d): %s", len(resp.content), attempt, str(e)[:80])
                if attempt >= 2:
                    raise GitHubError(f"graphql: undecodable response after {attempt + 1} attempts ({len(resp.content)} bytes)")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            errors = data.get("errors") or []
            if errors and not data.get("data"):
                types = {e.get("type") for e in errors}
                if "RATE_LIMITED" in types:
                    wait = self._retry_wait(resp, default=120)
                    log.warning("graphql RATE_LIMITED; sleeping %ss", wait)
                    time.sleep(wait)
                    continue
                raise GitHubError(f"graphql errors: {errors[:3]}")
            rl = (data.get("data") or {}).get("rateLimit")
            if rl and rl.get("remaining", 1) < 50:
                log.info("graphql points low (%s); pausing 60s", rl.get("remaining"))
                time.sleep(60)
            return data
        raise GitHubError("graphql: giving up")


def batched(seq: Sequence, n: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def run_batched(
    gh: GitHub,
    items: Sequence,
    build_query: Callable[[Sequence], str],
    batch_size: int,
    min_batch: int = 1,
) -> Iterable[tuple[Sequence, dict]]:
    """Run build_query(batch) over items; halve the batch on server errors.

    Yields (batch, response_json) so the caller can map aliases back.
    """
    i = 0
    size = batch_size
    while i < len(items):
        batch = items[i : i + size]
        try:
            data = gh.graphql(build_query(batch))
        except GitHubError as e:
            if size > min_batch:
                size = max(min_batch, size // 2)
                log.warning("graphql batch failed (%s); shrinking to %d", str(e)[:120], size)
                continue
            log.error("graphql batch of %d failed permanently: %s", len(batch), str(e)[:200])
            yield batch, {"data": {}, "errors": [{"message": str(e)}]}
            i += len(batch)
            continue
        yield batch, data
        i += len(batch)
        if size < batch_size:
            size = min(batch_size, size * 2)


def gql_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pages_for(total: int, per_page: int = 100, cap: int = 1000) -> int:
    return math.ceil(min(total, cap) / per_page)
