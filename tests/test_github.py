from datetime import UTC, datetime

import httpx
import pytest

from gauntlet_harness.github import GitHubClient, RateLimited, parse_linked_issues, prs_from_git

from .fakerepo import ISSUE_10, make_pr_repo


def pr_json(number, merged_at, updated_at=None, body="", title=None):
    return {
        "number": number,
        "title": title or f"PR {number}",
        "body": body,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "merged_at": merged_at,
        "updated_at": updated_at or merged_at or "2026-01-01T00:00:00Z",
        "merge_commit_sha": f"{number:040x}",
        "base": {"sha": "b" * 40},
        "labels": [{"name": "bug"}],
    }


def client_for(handler):
    return GitHubClient(
        token="t0k", http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fixes #12", [12]),
        ("closes #3, resolves #4 and FIXED #3", [3, 4]),
        ("Resolves: #7", [7]),
        ("fixes o/r#9 and other/repo#10", [9]),
        ("relates to #5, see #6", []),
        ("prefix#3 hotfixes #8", []),
    ],
)
def test_linked_issues(text, expected):
    assert parse_linked_issues(text, "o/r") == expected


def test_pagination_filtering_and_issues():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.path, dict(req.url.params), req.headers.get("authorization")))
        if req.url.path == "/repos/o/r/pulls":
            page = int(req.url.params["page"])
            if page == 1:
                items = [
                    pr_json(5, "2026-03-05T00:00:00Z", body="Fixes #50"),
                    pr_json(4, None, updated_at="2026-03-04T00:00:00Z"),  # closed, not merged
                    pr_json(3, "2026-03-03T00:00:00Z", body="closes #51"),
                ]
                return httpx.Response(200, json=items, headers={"link": '<https://x?page=2>; rel="next"'})
            return httpx.Response(200, json=[pr_json(2, "2026-02-01T00:00:00Z")])
        if req.url.path == "/repos/o/r/issues/50":
            return httpx.Response(200, json={"title": "Crash on empty input", "body": "Steps..."})
        if req.url.path == "/repos/o/r/issues/51":  # it's actually a PR, so it's not an issue text
            return httpx.Response(200, json={"title": "x", "body": "y", "pull_request": {}})
        return httpx.Response(404, json={})

    client = client_for(handler)
    prs = client.list_merged_prs("o/r", limit=5)
    assert [p.number for p in prs] == [5, 3, 2]
    assert prs[0].issue_texts == {50: "Crash on empty input\n\nSteps..."}
    assert prs[1].linked_issues == [51] and prs[1].issue_texts == {}
    assert prs[0].merge_commit_sha == f"{5:040x}" and prs[0].labels == ["bug"]
    assert seen[0][2] == "Bearer t0k"
    assert seen[0][1]["state"] == "closed" and seen[0][1]["sort"] == "updated"

    # merged_after stops paging once updates are older than the cutoff
    seen.clear()
    prs = client.list_merged_prs("o/r", merged_after=datetime(2026, 3, 4, tzinfo=UTC), limit=10)
    assert [p.number for p in prs] == [5]
    assert [s[0] for s in seen].count("/repos/o/r/pulls") == 1


def test_limit_stops_early():
    def handler(req):
        return httpx.Response(200, json=[pr_json(n, "2026-03-01T00:00:00Z") for n in range(30, 0, -1)])

    client = client_for(handler)
    assert len(client.list_merged_prs("o/r", limit=2)) == 2
    assert client.requests == 1


def test_rate_limit():
    def handler(req):
        return httpx.Response(
            403,
            text="API rate limit exceeded",
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"},
        )

    with pytest.raises(RateLimited) as err:
        client_for(handler).list_merged_prs("o/r")
    assert err.value.reset_at == datetime.fromtimestamp(1893456000, tz=UTC)

    def handler429(req):
        return httpx.Response(429, text="slow down")

    with pytest.raises(RateLimited):
        client_for(handler429).list_merged_prs("o/r")


def test_missing_issue_is_skipped():
    def handler(req):
        if req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[pr_json(1, "2026-03-01T00:00:00Z", body="fixes #99")])
        return httpx.Response(404, json={"message": "Not Found"})

    prs = client_for(handler).list_merged_prs("o/r")
    assert prs[0].linked_issues == [99] and prs[0].issue_texts == {}


def test_token_only_from_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    got = []

    def handler(req):
        got.append(req.headers.get("authorization"))
        return httpx.Response(200, json=[])

    GitHubClient(
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
    ).list_merged_prs("o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "envtok")
    GitHubClient(
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x")
    ).list_merged_prs("o/r")
    assert got == [None, "Bearer envtok"]


def test_prs_from_git(tmp_path):
    repo, prs = make_pr_repo(tmp_path)
    assert [p.number for p in prs] == [3, 2, 1]
    one = prs[2]
    assert one.title == "Fix division and add mul"
    assert one.linked_issues == [10] and one.issue_texts == {10: ISSUE_10}
    assert one.source == "git" and one.base_sha and one.merge_commit_sha
    assert prs[0].title == "Describe calc in the README"

    # the cutoff drops older merges
    cutoff = datetime.fromisoformat(prs[1].merged_at)
    newer = prs_from_git(repo, "acme/calc", merged_after=cutoff)
    assert all(datetime.fromisoformat(p.merged_at) > cutoff for p in newer)


@pytest.mark.live
def test_live_github():
    client = GitHubClient(token=None)
    prs = client.list_merged_prs("pallets/itsdangerous", limit=3)
    assert prs and all(p.merged_at and p.merge_commit_sha for p in prs)
    assert client.requests <= 10
