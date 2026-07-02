#!/usr/bin/env python3
"""Synchronize Vestigra public issues with private Blackbox engineering issues."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


PUBLIC_REPO_DEFAULT = "gaoxiaodiao/vestigra"
PRIVATE_REPO_DEFAULT = "gaoxiaodiao/blackbox"

STATUS_ACCEPTED = "status:accepted"
STATUS_IN_PROGRESS = "status:in-progress"
STATUS_FIXED_INTERNAL = "status:fixed-internal"
STATUS_RELEASED = "status:released"

STATUS_LABELS = OrderedDict(
    [
        (STATUS_ACCEPTED, ("2da44e", "Accepted for private implementation tracking.")),
        (STATUS_IN_PROGRESS, ("1d76db", "Private implementation work is in progress.")),
        (STATUS_FIXED_INTERNAL, ("8957e5", "Private fix is complete but the public issue remains open.")),
        (STATUS_RELEASED, ("0e8a16", "Fix or feature has shipped publicly.")),
    ]
)

PUBLIC_SYNC_LABELS = OrderedDict(
    [
        ("sync:blackbox", ("6f42c1", "This public issue is tracked in the private Blackbox repository.")),
    ]
)

PRIVATE_SYNC_LABELS = OrderedDict(
    [
        ("origin:vestigra", ("6f42c1", "Created from an accepted public Vestigra issue.")),
        (STATUS_ACCEPTED, STATUS_LABELS[STATUS_ACCEPTED]),
    ]
)

PUBLIC_MARKER_RE = re.compile(
    r"<!--\s*vestigra-public-issue:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\s*-->"
)
PUBLIC_FALLBACK_RE = re.compile(
    r"(?im)^Public issue:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\s*$"
)
COMMIT_REF_RE = re.compile(r"(?im)(?<![A-Za-z0-9_/.-])(implement|fix)\s+#([1-9][0-9]*)(?![0-9])")
PUBLIC_STATUS_COMMENT_MARKER = "<!-- vestigra-sync:public-status -->"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, payload: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class GitHub:
    def __init__(self, token: str) -> None:
        if not token:
            raise SystemExit(
                "GH_TOKEN or GITHUB_TOKEN is required. Configure VESTIGRA_ISSUE_SYNC_TOKEN in the repository secrets."
            )
        self.token = token

    def request(self, method: str, path: str, data: Any | None = None) -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        body = None if data is None else json.dumps(data).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "vestigra-issue-sync",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8", errors="replace")
            message = f"GitHub API {method} {url} failed with HTTP {error.code}"
            if payload:
                message = f"{message}: {payload}"
            raise GitHubError(error.code, message, payload) from error

    def paginate(self, path: str) -> list[Any]:
        separator = "&" if "?" in path else "?"
        page = 1
        items: list[Any] = []
        while True:
            data = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(data, list):
                raise RuntimeError(f"Expected paginated list from {path}")
            items.extend(data)
            if len(data) < 100:
                return items
            page += 1


@dataclass(frozen=True)
class IssueRef:
    repo: str
    number: int

    def marker(self) -> str:
        return f"<!-- vestigra-public-issue: {self.repo}#{self.number} -->"

    def display(self) -> str:
        return f"{self.repo}#{self.number}"


def repo_api(repo: str, suffix: str = "") -> str:
    owner, name = repo.split("/", 1)
    return f"/repos/{owner}/{name}{suffix}"


def label_api_name(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def issue_url(repo: str, issue_number: int) -> str:
    return f"https://github.com/{repo}/issues/{issue_number}"


def ensure_label(gh: GitHub, repo: str, name: str, color: str, description: str) -> None:
    payload = {"name": name, "color": color, "description": description}
    try:
        gh.request("POST", repo_api(repo, "/labels"), payload)
        return
    except GitHubError as error:
        if error.status != 422:
            raise

    update_payload = {"new_name": name, "color": color, "description": description}
    gh.request("PATCH", repo_api(repo, f"/labels/{label_api_name(name)}"), update_payload)


def ensure_labels(gh: GitHub, repo: str, labels: OrderedDict[str, tuple[str, str]]) -> None:
    for name, (color, description) in labels.items():
        ensure_label(gh, repo, name, color, description)


def add_issue_labels(gh: GitHub, repo: str, issue_number: int, labels: list[str]) -> None:
    if labels:
        gh.request("POST", repo_api(repo, f"/issues/{issue_number}/labels"), {"labels": labels})


def remove_issue_label(gh: GitHub, repo: str, issue_number: int, label: str) -> None:
    try:
        gh.request("DELETE", repo_api(repo, f"/issues/{issue_number}/labels/{label_api_name(label)}"))
    except GitHubError as error:
        if error.status != 404:
            raise


def set_public_status(gh: GitHub, issue_ref: IssueRef, status_label: str) -> None:
    if status_label not in STATUS_LABELS:
        raise ValueError(f"Unknown public status label: {status_label}")
    ensure_labels(gh, issue_ref.repo, STATUS_LABELS)
    ensure_labels(gh, issue_ref.repo, PUBLIC_SYNC_LABELS)
    for label in STATUS_LABELS:
        if label != status_label:
            remove_issue_label(gh, issue_ref.repo, issue_ref.number, label)
    add_issue_labels(gh, issue_ref.repo, issue_ref.number, [status_label, *PUBLIC_SYNC_LABELS.keys()])


def public_status_message(status_label: str) -> str:
    messages = {
        STATUS_ACCEPTED: (
            "Accepted for implementation. This public issue remains open while private engineering work proceeds."
        ),
        STATUS_IN_PROGRESS: "Implementation has started in the private engineering repository.",
        STATUS_FIXED_INTERNAL: (
            "A private engineering fix is complete. This issue stays open until the change is released or manually closed."
        ),
        STATUS_RELEASED: "The change has been released. This issue still requires a manual close decision.",
    }
    return messages[status_label]


def upsert_public_status_comment(gh: GitHub, issue_ref: IssueRef, status_label: str) -> None:
    body = f"{PUBLIC_STATUS_COMMENT_MARKER}\n{public_status_message(status_label)}"
    comments = gh.paginate(repo_api(issue_ref.repo, f"/issues/{issue_ref.number}/comments"))
    for comment in comments:
        if PUBLIC_STATUS_COMMENT_MARKER in (comment.get("body") or ""):
            if comment.get("body") != body:
                gh.request("PATCH", repo_api(issue_ref.repo, f"/issues/comments/{comment['id']}"), {"body": body})
            return
    gh.request("POST", repo_api(issue_ref.repo, f"/issues/{issue_ref.number}/comments"), {"body": body})


def extract_public_issue(body: str | None) -> IssueRef | None:
    content = body or ""
    match = PUBLIC_MARKER_RE.search(content) or PUBLIC_FALLBACK_RE.search(content)
    if not match:
        return None
    return IssueRef(repo=match.group(1), number=int(match.group(2)))


def private_issue_body(public_repo: str, public_issue: dict[str, Any]) -> str:
    public_number = int(public_issue["number"])
    public_ref = IssueRef(public_repo, public_number)
    public_body = public_issue.get("body") or ""
    if len(public_body) > 20000:
        public_body = f"{public_body[:20000]}\n\n[Original public body truncated by sync helper.]"
    author = (public_issue.get("user") or {}).get("login", "unknown")
    title = public_issue.get("title") or "(untitled)"
    url = public_issue.get("html_url") or issue_url(public_repo, public_number)
    return textwrap.dedent(
        f"""\
        {public_ref.marker()}

        # Public intake

        Public issue: {public_ref.display()}
        Public URL: {url}
        Public author: @{author}

        ## Original title

        {title}

        ## Original body

        {public_body}
        """
    )


def find_private_issue_for_public(gh: GitHub, private_repo: str, public_ref: IssueRef) -> dict[str, Any] | None:
    marker = public_ref.marker()
    issues = gh.paginate(repo_api(private_repo, "/issues?state=all"))
    for issue in issues:
        if "pull_request" in issue:
            continue
        if marker in (issue.get("body") or ""):
            return issue
    return None


def accept_public_issue(args: argparse.Namespace) -> int:
    gh = GitHub(token_from_env())
    public_issue = gh.request("GET", repo_api(args.public_repo, f"/issues/{args.issue_number}"))
    if "pull_request" in public_issue:
        print(f"{args.public_repo}#{args.issue_number} is a pull request; skipping.")
        return 0

    public_ref = IssueRef(args.public_repo, int(public_issue["number"]))
    ensure_labels(gh, args.private_repo, PRIVATE_SYNC_LABELS)
    private_issue = find_private_issue_for_public(gh, args.private_repo, public_ref)
    if private_issue is None:
        title = public_issue.get("title") or "(untitled)"
        private_issue = gh.request(
            "POST",
            repo_api(args.private_repo, "/issues"),
            {
                "title": f"[vestigra#{public_ref.number}] {title}",
                "body": private_issue_body(args.public_repo, public_issue),
                "labels": list(PRIVATE_SYNC_LABELS.keys()),
            },
        )
        print(f"Created private issue {args.private_repo}#{private_issue['number']} for {public_ref.display()}.")
    else:
        add_issue_labels(gh, args.private_repo, int(private_issue["number"]), list(PRIVATE_SYNC_LABELS.keys()))
        print(f"Reused private issue {args.private_repo}#{private_issue['number']} for {public_ref.display()}.")

    set_public_status(gh, public_ref, STATUS_ACCEPTED)
    upsert_public_status_comment(gh, public_ref, STATUS_ACCEPTED)
    return 0


def token_from_env() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def parse_commit_refs(message: str) -> list[tuple[int, str]]:
    refs: list[tuple[int, str]] = []
    for match in COMMIT_REF_RE.finditer(message or ""):
        refs.append((int(match.group(2)), match.group(1).lower()))
    return refs


def rank_status(status: str) -> int:
    return {STATUS_IN_PROGRESS: 1, STATUS_FIXED_INTERNAL: 2, STATUS_RELEASED: 3}.get(status, 0)


def record_status(statuses: dict[int, str], issue_number: int, status: str) -> None:
    current = statuses.get(issue_number)
    if current is None or rank_status(status) > rank_status(current):
        statuses[issue_number] = status


def collect_statuses_from_push(event: dict[str, Any], default_branch: str, ref_name: str) -> dict[int, str]:
    statuses: dict[int, str] = {}
    event_ref = event.get("ref") or ""
    on_default_branch = ref_name == default_branch or event_ref == f"refs/heads/{default_branch}"
    for commit in event.get("commits") or []:
        for issue_number, keyword in parse_commit_refs(commit.get("message") or ""):
            status = STATUS_FIXED_INTERNAL if on_default_branch and keyword == "fix" else STATUS_IN_PROGRESS
            record_status(statuses, issue_number, status)
    return statuses


def collect_statuses_from_pull_request(gh: GitHub, event: dict[str, Any], private_repo: str) -> dict[int, str]:
    statuses: dict[int, str] = {}
    pull_request = event.get("pull_request") or {}
    for text in [pull_request.get("title") or "", pull_request.get("body") or ""]:
        for issue_number, _keyword in parse_commit_refs(text):
            record_status(statuses, issue_number, STATUS_IN_PROGRESS)

    pr_number = pull_request.get("number")
    if pr_number:
        commits = gh.paginate(repo_api(private_repo, f"/pulls/{pr_number}/commits"))
        for commit in commits:
            message = ((commit.get("commit") or {}).get("message")) or ""
            for issue_number, _keyword in parse_commit_refs(message):
                record_status(statuses, issue_number, STATUS_IN_PROGRESS)
    return statuses


def collect_statuses_from_issue_event(event: dict[str, Any]) -> dict[int, str]:
    issue = event.get("issue") or {}
    if not issue or "pull_request" in issue:
        return {}
    action = event.get("action")
    if action == "closed":
        return {int(issue["number"]): STATUS_FIXED_INTERNAL}
    if action == "reopened":
        return {int(issue["number"]): STATUS_IN_PROGRESS}
    return {}


def collect_statuses_from_dispatch(event: dict[str, Any]) -> dict[int, str]:
    inputs = event.get("inputs") or {}
    issue_number = inputs.get("private_issue_number") or inputs.get("issue_number")
    status = inputs.get("status") or STATUS_IN_PROGRESS
    if not issue_number:
        return {}
    if status not in STATUS_LABELS:
        raise ValueError(f"Unsupported workflow_dispatch status: {status}")
    return {int(issue_number): status}


def collect_private_statuses(
    gh: GitHub,
    event: dict[str, Any],
    event_name: str,
    private_repo: str,
    default_branch: str,
    ref_name: str,
) -> dict[int, str]:
    if event_name == "push":
        return collect_statuses_from_push(event, default_branch, ref_name)
    if event_name == "pull_request":
        return collect_statuses_from_pull_request(gh, event, private_repo)
    if event_name == "issues":
        return collect_statuses_from_issue_event(event)
    if event_name == "workflow_dispatch":
        return collect_statuses_from_dispatch(event)
    return {}


def sync_private_status(args: argparse.Namespace) -> int:
    gh = GitHub(token_from_env())
    with open(args.event_path, "r", encoding="utf-8") as handle:
        event = json.load(handle)

    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME") or ""
    statuses = collect_private_statuses(
        gh=gh,
        event=event,
        event_name=event_name,
        private_repo=args.private_repo,
        default_branch=args.default_branch,
        ref_name=args.ref_name,
    )
    if not statuses:
        print(f"No implement #issue or fix #issue references found for event {event_name}; nothing to sync.")
        return 0

    for private_issue_number, status_label in sorted(statuses.items()):
        try:
            private_issue = gh.request("GET", repo_api(args.private_repo, f"/issues/{private_issue_number}"))
        except GitHubError as error:
            if error.status == 404:
                print(f"Private issue {args.private_repo}#{private_issue_number} does not exist; skipping.")
                continue
            raise
        public_ref = extract_public_issue(private_issue.get("body") or "")
        if public_ref is None:
            print(f"Private issue {args.private_repo}#{private_issue_number} has no public issue marker; skipping.")
            continue
        if args.public_repo and public_ref.repo != args.public_repo:
            print(
                f"Private issue {args.private_repo}#{private_issue_number} points to {public_ref.display()}, "
                f"not {args.public_repo}; skipping."
            )
            continue
        set_public_status(gh, public_ref, status_label)
        upsert_public_status_comment(gh, public_ref, status_label)
        print(f"Set {public_ref.display()} to {status_label} from {args.private_repo}#{private_issue_number}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    accept_parser = subparsers.add_parser("accept-public", help="Create or reuse a private issue for a public issue.")
    accept_parser.add_argument("--public-repo", default=PUBLIC_REPO_DEFAULT)
    accept_parser.add_argument("--private-repo", default=PRIVATE_REPO_DEFAULT)
    accept_parser.add_argument("--issue-number", type=int, required=True)
    accept_parser.set_defaults(func=accept_public_issue)

    sync_parser = subparsers.add_parser("sync-private", help="Update public issue status from private repo events.")
    sync_parser.add_argument("--public-repo", default=PUBLIC_REPO_DEFAULT)
    sync_parser.add_argument("--private-repo", default=PRIVATE_REPO_DEFAULT)
    sync_parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    sync_parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    sync_parser.add_argument("--default-branch", default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"))
    sync_parser.add_argument("--ref-name", default=os.environ.get("GITHUB_REF_NAME", ""))
    sync_parser.set_defaults(func=sync_private_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "event_path", None) == "":
        parser.error("--event-path is required for sync-private")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
