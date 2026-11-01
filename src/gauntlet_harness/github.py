"""Find recently merged pull requests, through the public REST API or plain git history.

Read-only. The API path takes an optional token from the GITHUB_TOKEN environment variable
(unauthenticated calls get 60 requests an hour). The git path needs no API at all: it reads
merge commits and squash-merge subjects from a clone.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .schema import PRInfo

API = "https://api.github.com"

# GitHub's closing keywords: close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved
CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\b",
    re.IGNORECASE,
)
MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) from \S+")
SQUASH_SUBJECT_RE = re.compile(r"^(?P<title>.*\S)\s+\(#(?P<num>\d+)\)$")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    def __init__(self, reset_at: datetime | None, message: str = "") -> None:
        self.reset_at = reset_at
        when = reset_at.isoformat() if reset_at else "unknown"
        super().__init__(
            message or f"GitHub rate limit hit, resets at {when}. Set GITHUB_TOKEN for more.", status=429
        )


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_linked_issues(text: str, repo: str) -> list[int]:
    """Issue numbers a PR closes, by GitHub's keywords. Cross-repo references are ignored."""
    found: list[int] = []
    for other, num in CLOSING_RE.findall(text or ""):
        if other and other.lower() != repo.lower():
            continue
        n = int(num)
        if n not in found:
            found.append(n)
    return found


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = API,
        http: httpx.Client | None = None,
    ) -> None:
        token = token or os.environ.get("GITHUB_TOKEN") or None
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gauntlet-harness",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._http = http or httpx.Client(base_url=base_url, timeout=30, follow_redirects=True)
        self.requests = 0  # how many calls we've made, handy against the unauthenticated limit

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = self._http.get(path, params=params, headers=self._headers)
        self.requests += 1
        limited = resp.status_code == 429 or (
            resp.status_code == 403
            and (resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower())
        )
        if limited:
            reset = resp.headers.get("x-ratelimit-reset")
            raise RateLimited(datetime.fromtimestamp(int(reset), tz=UTC) if reset else None)
        if resp.status_code >= 400:
            raise GitHubError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp

    def get_issue(self, repo: str, number: int) -> dict[str, Any] | None:
        try:
            return self._get(f"/repos/{repo}/issues/{number}").json()
        except GitHubError as e:
            if e.status in (404, 410):  # deleted or transferred issues
                return None
            raise

    def list_merged_prs(
        self,
        repo: str,
        *,
        merged_after: datetime | None = None,
        limit: int = 30,
        max_pages: int = 10,
    ) -> list[PRInfo]:
        """Newest merged PRs first. Linked issues are resolved to their text."""
        after = _aware(merged_after)
        per_page = min(100, max(30, limit * 3))
        prs: list[PRInfo] = []
        for page in range(1, max_pages + 1):
            resp = self._get(
                f"/repos/{repo}/pulls",
                {
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            items = resp.json()
            if not items:
                break
            for item in items:
                merged = parse_time(item.get("merged_at"))
                if merged is None or (after and merged <= after):
                    continue
                prs.append(self._to_pr(repo, item))
                if len(prs) >= limit:
                    break
            if len(prs) >= limit:
                break
            # sorted by last update, and a PR can't be merged after its last update, so once
            # updates are older than the cutoff nothing further down can qualify
            last_update = parse_time(items[-1].get("updated_at"))
            if after and last_update and last_update < after:
                break
            if 'rel="next"' not in resp.headers.get("link", ""):
                break

        for pr in prs:
            for n in pr.linked_issues:
                issue = self.get_issue(repo, n)
                if issue and "pull_request" not in issue:  # the issues API also serves PRs
                    pr.issue_texts[n] = f"{issue.get('title', '')}\n\n{issue.get('body') or ''}".strip()
        return prs

    @staticmethod
    def _to_pr(repo: str, item: dict[str, Any]) -> PRInfo:
        title, body = item.get("title") or "", item.get("body") or ""
        return PRInfo(
            repo=repo,
            number=item["number"],
            title=title,
            body=body,
            url=item.get("html_url") or f"https://github.com/{repo}/pull/{item['number']}",
            merged_at=item.get("merged_at"),
            base_sha=(item.get("base") or {}).get("sha"),
            merge_commit_sha=item.get("merge_commit_sha"),
            linked_issues=parse_linked_issues(f"{title}\n{body}", repo),
            labels=[label["name"] for label in item.get("labels") or [] if "name" in label],
            source="api",
        )


def prs_from_git(
    clone_path: str | Path,
    repo: str,
    *,
    limit: int = 30,
    merged_after: datetime | None = None,
    ref: str = "HEAD",
) -> list[PRInfo]:
    """Merged PRs read straight from first-parent history, no API calls.

    Understands GitHub merge commits ("Merge pull request #N from ...") and squash merges
    ("Title (#N)"). No issue texts, since those only live in the API.
    """
    after = _aware(merged_after)
    out = subprocess.run(
        ["git", "-C", str(clone_path), "log", "--first-parent", "--format=%H%x00%P%x00%cI%x00%B%x1e", ref],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")

    prs: list[PRInfo] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, message = record.split("\x00", 3)
        merged = parse_time(date)
        if after and merged and merged <= after:
            break  # first-parent history is chronological, so everything after is older
        lines = message.strip().splitlines()
        subject = lines[0] if lines else ""
        rest = "\n".join(lines[1:]).strip()
        if m := MERGE_SUBJECT_RE.match(subject):
            number = int(m.group(1))
            title = rest.splitlines()[0] if rest else subject
            body = "\n".join(rest.splitlines()[1:]).strip()
        elif m := SQUASH_SUBJECT_RE.match(subject):
            number, title, body = int(m.group("num")), m.group("title"), rest
        else:
            continue
        first_parent = parents.split()[0] if parents.strip() else None
        prs.append(
            PRInfo(
                repo=repo,
                number=number,
                title=title,
                body=body,
                url=f"https://github.com/{repo}/pull/{number}",
                merged_at=merged.isoformat() if merged else None,
                base_sha=first_parent,
                merge_commit_sha=sha,
                linked_issues=parse_linked_issues(f"{title}\n{body}", repo),
                source="git",
            )
        )
        if len(prs) >= limit:
            break
    return prs
