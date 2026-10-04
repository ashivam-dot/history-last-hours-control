"""Scheduled control of exact producer drafts, with private durable receipts.

This module never reads producer Python or handles media. The existing control
commands do that work in separate, environment-scoped Actions jobs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .intake import committed_draft
from .release import COMMIT, EPISODE, SHA, Hold, policy, read_object, require, utc

STATE_BRANCH = "unattended-state"
RECEIPT_DIR = "receipts"
PHASES = ("claimed", "intake_verified", "qa_approved", "signed", "release_planned", "published")
NEXT = dict(zip(PHASES, PHASES[1:]))
SWITCHES = {
    "intake": ("intake_enabled",),
    "qa": ("intake_enabled",),
    "sign": ("intake_enabled", "signing_enabled"),
    "publish": ("intake_enabled", "signing_enabled", "publishing_enabled"),
}
SOURCE_PATH = re.compile(r"content/episodes/(ep[0-9]{3,})/draft\.json\Z")
FORBIDDEN_PRODUCER_RECORDS = ("hold.json", "publish.json", "release_certificate.json",
                              "independent_review.json")


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError as exc:
        raise Hold("producer Git discovery failed") from exc


def draft_candidates(repo: Path, config: dict):
    """Yield the commit that last changed each draft, in episode order."""
    require(_git(repo, "remote", "get-url", "origin") == config["source_remote"],
            "producer origin differs from pinned policy")
    require(_git(repo, "symbolic-ref", "--short", "HEAD") == "main",
            "producer discovery must inspect main")
    floor = int(EPISODE.fullmatch(config["min_episode_id"]).group(1))
    paths = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "content/episodes").splitlines()
    found = []
    for path in paths:
        match = SOURCE_PATH.fullmatch(path)
        if match and int(EPISODE.fullmatch(match.group(1)).group(1)) >= floor:
            found.append((int(EPISODE.fullmatch(match.group(1)).group(1)), match.group(1), path))
    for _, episode, path in sorted(found):
        commit = _git(repo, "log", "-1", "--format=%H", "HEAD", "--", path)
        require(bool(COMMIT.fullmatch(commit)) and
                _git(repo, "merge-base", "--is-ancestor", commit, "HEAD") == "",
                "draft commit is not on producer main")
        yield episode, commit


def exact_draft(repo: Path, commit: str, episode: str, config: dict) -> dict:
    """Validate an immutable detached checkout without running producer code."""
    require(bool(COMMIT.fullmatch(commit)) and bool(EPISODE.fullmatch(episode)),
            "candidate identity is malformed")
    with tempfile.TemporaryDirectory(prefix="history-draft-git-") as temp:
        checkout = Path(temp) / "source"
        _git(repo, "worktree", "add", "--detach", str(checkout), commit)
        try:
            return committed_draft(checkout, commit, episode, config)
        finally:
            _git(repo, "worktree", "remove", "--force", str(checkout))


def producer_unreleased(repo: Path, ref: str, episode: str) -> None:
    root = f"content/episodes/{episode}/"
    existing = _git(repo, "ls-tree", "-r", "--name-only", ref, "--",
                    *(root + name for name in FORBIDDEN_PRODUCER_RECORDS))
    require(not existing, "producer main already has a release or hosted-media record")


def current_draft(repo: Path, episode: str, commit: str, config: dict) -> None:
    """Recheck producer main immediately before each cloud phase."""
    require(bool(EPISODE.fullmatch(episode)) and bool(COMMIT.fullmatch(commit)),
            "candidate identity is malformed")
    require(_git(repo, "remote", "get-url", "origin") == config["source_remote"],
            "producer origin differs from pinned policy")
    _git(repo, "fetch", "--no-tags", "origin", "main")
    path = f"content/episodes/{episode}/draft.json"
    require(_git(repo, "log", "-1", "--format=%H", "FETCH_HEAD", "--", path) == commit,
            "producer main draft changed after control claim")
    producer_unreleased(repo, "FETCH_HEAD", episode)


def bound(receipt: dict, episode: str, commit: str, draft_hash: str, media_hash: str) -> bool:
    return (receipt.get("episode"), receipt.get("source_commit"),
            receipt.get("draft_sha256"), receipt.get("media_sha256")) == (
                episode, commit, draft_hash, media_hash)


def enabled(config: dict, operation: str) -> bool:
    return all(config[name] is True for name in SWITCHES[operation])


def require_mutation_policy(config: dict, operation: str) -> None:
    """Re-read private control main at a trusted unattended mutation boundary."""
    require(operation in ("sign", "publish"), "live policy operation is invalid")
    require(enabled(config, operation), f"{operation} policy is disabled in this run")
    state = GitHubState(os.environ.get("HISTORY_CONTROL_POLICY_TOKEN", ""),
                        os.environ.get("GITHUB_REPOSITORY", ""),
                        os.environ.get("GITHUB_SHA", ""))
    state.require_live_policy(config, operation)


def advance(receipt: dict, phase: str) -> dict:
    require(phase in PHASES[1:], "receipt phase is invalid")
    current = receipt.get("phase")
    require(current in PHASES, "held or invalid receipt cannot advance")
    if PHASES.index(current) >= PHASES.index(phase):
        return receipt
    require(NEXT[current] == phase, "receipt phase transition is out of order")
    return {**receipt, "phase": phase, "updated_at_utc": datetime.now(timezone.utc).isoformat()}


def next_due(receipts: list[dict], now: datetime) -> str:
    """Reserve one 17:00 UTC daily slot, at least two hours ahead."""
    occupied = {item.get("due_at_utc") for item in receipts if item.get("due_at_utc")}
    for day in range(30):
        date = (now + timedelta(days=day)).date()
        candidate = datetime(date.year, date.month, date.day, 17, tzinfo=timezone.utc)
        if candidate > now + timedelta(hours=2) and candidate <= now + timedelta(days=29):
            due = candidate.isoformat()
            if due not in occupied:
                return due
    raise Hold("no unreserved release slot is available")


class GitHubState:
    """GitHub Contents API state on a private branch, with per-file CAS writes."""

    def __init__(self, token: str, repository: str, run_sha: str):
        require(bool(token) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)),
                "GitHub state credentials or repository are missing")
        require(bool(COMMIT.fullmatch(run_sha)), "control run commit is malformed")
        self.base = f"https://api.github.com/repos/{repository}"
        self.run_sha = run_sha
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})

    def request(self, method: str, path: str, *, allowed=(200,), **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=(10, 30), **kwargs)
        except requests.RequestException as exc:
            raise Hold("private GitHub state API is unavailable") from exc
        require(response.status_code in allowed,
                f"private GitHub state API returned {response.status_code} for {method} {path.split('?')[0]}")
        if response.status_code == 404:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise Hold("private GitHub state API returned invalid JSON") from exc

    def ensure_branch(self) -> None:
        path = f"/git/ref/heads/{STATE_BRANCH}"
        if self.request("GET", path, allowed=(200, 404)) is not None:
            return
        self.request("POST", "/git/refs", allowed=(201, 422),
                     json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": self.run_sha})
        require(self.request("GET", path, allowed=(200, 404)) is not None,
                "private receipt branch could not be created")

    def file(self, path: str):
        return self.request("GET", f"/contents/{path}?ref={STATE_BRANCH}", allowed=(200, 404))

    def receipt(self, episode: str) -> dict | None:
        require(bool(EPISODE.fullmatch(episode)), "receipt episode is malformed")
        entry = self.file(f"{RECEIPT_DIR}/{episode}.json")
        if entry is None:
            return None
        require(entry.get("type") == "file" and isinstance(entry.get("content"), str),
                "private receipt content is invalid")
        try:
            value = json.loads(base64.b64decode(entry["content"]).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise Hold("private receipt JSON is invalid") from exc
        require(isinstance(value, dict) and type(value.get("version")) is int and
                value["version"] == 1 and
                value.get("episode") == episode and
                isinstance(value.get("source_commit"), str) and
                bool(COMMIT.fullmatch(value["source_commit"])) and
                isinstance(value.get("draft_sha256"), str) and
                bool(SHA.fullmatch(value["draft_sha256"])) and
                isinstance(value.get("media_sha256"), str) and
                bool(SHA.fullmatch(value["media_sha256"])) and
                value.get("phase") in (*PHASES, "held"),
                "private receipt identity or phase is invalid")
        return value

    def require_live_policy(self, config: dict, operation: str) -> None:
        """A running job cannot use a policy superseded on control main."""
        entry = self.request("GET", "/contents/policy.json?ref=main", allowed=(200, 404))
        require(isinstance(entry, dict) and entry.get("type") == "file" and
                isinstance(entry.get("content"), str), "current control policy is unavailable")
        try:
            live = read_object(base64.b64decode(entry["content"], validate=True),
                               "current control policy")
        except (ValueError, UnicodeError) as exc:
            raise Hold("current control policy is invalid") from exc
        require(live == config, "control main policy changed after this run started")
        require(enabled(live, operation), f"current {operation} policy is disabled")

    def receipts(self) -> list[dict]:
        listing = self.file(RECEIPT_DIR)
        if listing is None:
            return []
        require(isinstance(listing, list) and len(listing) < 1000,
                "private receipt directory exceeds bounded listing")
        return [self.receipt(item["name"][:-5]) for item in listing
                if item.get("type") == "file" and re.fullmatch(r"ep[0-9]{3,}\.json", item.get("name", ""))]

    def put(self, receipt: dict) -> None:
        episode = receipt["episode"]
        require(bool(EPISODE.fullmatch(episode)), "receipt episode is malformed")
        path = f"{RECEIPT_DIR}/{episode}.json"
        old = self.file(path)
        body = {"message": f"Record unattended control state for {episode}",
                "content": base64.b64encode((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()).decode(),
                "branch": STATE_BRANCH}
        if old is not None:
            body["sha"] = old["sha"]
        self.request("PUT", f"/contents/{path}", allowed=(200, 201), json=body)

    def alert(self, episode: str | None, stage: str, reason: str) -> None:
        title = f"[unattended] {episode or 'discovery'} held"
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                   f"{os.environ.get('GITHUB_RUN_ID', '')}")
        body = f"Stage: {stage}\nReason: {reason[:500]}\nRun: {run_url}\n"
        issue = None
        for page in range(1, 11):
            entries = self.request("GET", f"/issues?state=open&per_page=100&page={page}")
            require(isinstance(entries, list), "private issue listing is invalid")
            issue = next((entry for entry in entries if entry.get("title") == title and
                          "pull_request" not in entry), None)
            if issue or len(entries) < 100:
                break
        if issue:
            self.request("PATCH", f"/issues/{issue['number']}", json={"body": body})
        else:
            self.request("POST", "/issues", allowed=(201,), json={"title": title, "body": body})


def _identity(args, state: GitHubState) -> dict:
    require(bool(EPISODE.fullmatch(args.episode)) and bool(COMMIT.fullmatch(args.commit)),
            "candidate identity is malformed")
    receipt = state.receipt(args.episode)
    require(receipt is not None and receipt["source_commit"] == args.commit,
            "candidate does not match private receipt")
    return receipt


def discover(args, config: dict, state: GitHubState) -> dict:
    state.ensure_branch()
    receipts = {item["episode"]: item for item in state.receipts()}
    candidates = list(draft_candidates(args.source, config))
    visible = {episode for episode, _ in candidates}
    for episode, old in receipts.items():
        if episode not in visible and old["phase"] not in ("held", "published"):
            held = {**old, "phase": "held", "resume_phase": old["phase"],
                    "held_reason": "claimed draft disappeared from producer main",
                    "updated_at_utc": datetime.now(timezone.utc).isoformat()}
            state.put(held)
            state.alert(episode, "discovery", held["held_reason"])
            raise Hold(held["held_reason"])
    resume = args.resume_episode
    if resume:
        require(bool(EPISODE.fullmatch(resume)), "resume episode is malformed")
        require(resume in receipts and receipts[resume]["phase"] == "held",
                "only a held receipt can be resumed")
        require(receipts[resume].get("resume_phase") in PHASES[:4],
                "release attempts require manual Buffer inspection; automatic resume is disabled")
    for episode, commit in candidates:
        if resume and episode != resume:
            continue
        old = receipts.get(episode)
        if old and old["phase"] == "held" and not resume:
            continue
        if old and old["phase"] == "release_planned":
            held = {**old, "phase": "held", "resume_phase": "release_planned",
                    "held_reason": "release outcome is uncertain; inspect the external Buffer post before resume",
                    "updated_at_utc": datetime.now(timezone.utc).isoformat()}
            state.put(held)
            state.alert(episode, "discovery", held["held_reason"])
            raise Hold(held["held_reason"])
        if old and old["phase"] == "published" and old["source_commit"] == commit:
            # The exact Git commit is immutable. A changed draft has a new commit.
            continue
        try:
            producer_unreleased(args.source, "HEAD", episode)
            source = exact_draft(args.source, commit, episode, config)
        except Hold:
            if old:
                held = {**old, "phase": "held", "resume_phase": old["phase"],
                        "held_reason": "producer draft failed immutable validation",
                        "updated_at_utc": datetime.now(timezone.utc).isoformat()}
                state.put(held)
            state.alert(episode, "discovery", "producer draft failed immutable validation")
            raise Hold("producer draft failed immutable validation")
        draft_hash = source["draft_sha256"]
        media_hash = source["draft"]["media_sha256"]
        if old and not bound(old, episode, commit, draft_hash, media_hash):
            held = {**old, "phase": "held", "held_reason": "producer draft changed after control claim",
                    "resume_phase": old["phase"], "updated_at_utc": datetime.now(timezone.utc).isoformat()}
            state.put(held)
            state.alert(episode, "discovery", held["held_reason"])
            raise Hold(held["held_reason"])
        if old and old["phase"] == "published":
            continue
        if resume:
            old = {k: v for k, v in old.items() if k not in ("held_reason", "resume_phase")}
            old["phase"] = receipts[episode]["resume_phase"]
            old["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            state.put(old)
        if not old:
            old = {"version": 1, "episode": episode, "source_commit": commit,
                   "draft_sha256": draft_hash, "media_sha256": media_hash,
                   "phase": "claimed", "updated_at_utc": datetime.now(timezone.utc).isoformat()}
            state.put(old)
        phase = old["phase"]
        if (phase in ("claimed", "intake_verified") and not enabled(config, "intake") or
                phase == "qa_approved" and not enabled(config, "sign") or
                phase == "signed" and not enabled(config, "publish")):
            continue
        return {"episode": episode, "source_commit": commit,
                "intake_enabled": enabled(config, "intake"),
                "signing_enabled": enabled(config, "sign"),
                "publishing_enabled": enabled(config, "publish")}
    require(not resume, "held episode is no longer a producer-main draft")
    return {}


def run(args) -> dict | None:
    if args.command == "current":
        current_draft(args.source, args.episode, args.commit, policy(args.policy))
        return None
    state = GitHubState(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", ""),
                        os.environ.get("GITHUB_SHA", ""))
    if args.command == "fail":
        reason = "Actions job failed; inspect the linked run and private artifacts"
        if args.episode:
            receipt = state.receipt(args.episode)
            if receipt and receipt["phase"] != "published":
                if receipt["phase"] != "held":
                    receipt = {**receipt, "resume_phase": receipt["phase"], "phase": "held",
                               "held_reason": reason,
                               "updated_at_utc": datetime.now(timezone.utc).isoformat()}
                    state.put(receipt)
        state.alert(args.episode or None, args.stage, reason)
        return None
    config = policy(args.policy)
    if args.command == "discover":
        result = discover(args, config, state)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
        return result
    if args.command == "live-policy":
        require(enabled(config, args.operation), f"{args.operation} policy is disabled in this run")
        state.require_live_policy(config, args.operation)
        return None
    receipt = _identity(args, state)
    if args.command == "gate":
        require(enabled(config, args.operation), f"{args.operation} policy is disabled")
        require(receipt["phase"] in PHASES and receipt["phase"] != "published",
                "candidate is held or already published")
        return None
    if args.command == "advance":
        operation = {"intake_verified": "intake", "qa_approved": "qa", "signed": "sign"}[args.phase]
        require(enabled(config, operation), f"{operation} policy is disabled")
        updated = advance(receipt, args.phase)
        if updated != receipt:
            state.put(updated)
        return updated
    if args.command == "reserve":
        require(enabled(config, "publish"), "publishing policy is disabled")
        require(receipt["phase"] == "signed",
                "release is already planned or held; inspect Buffer before another attempt")
        now = datetime.now(timezone.utc)
        due = next_due(state.receipts(), now)
        receipt = {**advance(receipt, "release_planned"), "due_at_utc": due}
        state.put(receipt)
        args.output.write_text(json.dumps({"due_at_utc": receipt["due_at_utc"]}) + "\n")
        return receipt
    if args.command == "complete":
        require(enabled(config, "publish"), "publishing policy is disabled")
        require(receipt["phase"] == "release_planned", "release was not reserved")
        try:
            published = json.loads(args.publisher_receipt.read_text())
        except (OSError, ValueError) as exc:
            raise Hold("publisher receipt is missing or invalid") from exc
        require(isinstance(published, dict) and
                (published.get("episode"), published.get("source_commit"),
                 published.get("media_sha256")) ==
                (receipt["episode"], receipt["source_commit"], receipt["media_sha256"]) and
                isinstance(published.get("youtube"), dict) and
                published["youtube"].get("status") in ("scheduled", "sent") and
                utc(published["youtube"].get("due_at"), "publisher due time") ==
                utc(receipt["due_at_utc"], "reserved due time"),
                "publisher receipt differs from reserved release")
        state.put({**advance(receipt, "published"), "publisher_receipt": published})
        return None
    raise Hold("unknown orchestration command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unattended private History control")
    parser.add_argument("--policy", type=Path, default=Path(__file__).resolve().parents[1] / "policy.json")
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover")
    discovery.add_argument("--source", type=Path, required=True)
    discovery.add_argument("--output", type=Path, required=True)
    discovery.add_argument("--resume-episode", default="")
    current = commands.add_parser("current")
    current.add_argument("--source", type=Path, required=True)
    current.add_argument("--episode", required=True)
    current.add_argument("--commit", required=True)
    live_policy = commands.add_parser("live-policy")
    live_policy.add_argument("--operation", choices=("sign", "publish"), required=True)
    for name in ("gate", "advance", "reserve", "complete"):
        command = commands.add_parser(name)
        command.add_argument("--episode", required=True)
        command.add_argument("--commit", required=True)
        if name == "gate":
            command.add_argument("--operation", choices=tuple(SWITCHES), required=True)
        if name == "advance":
            command.add_argument("--phase", choices=PHASES[1:4], required=True)
        if name == "reserve":
            command.add_argument("--output", type=Path, required=True)
        if name == "complete":
            command.add_argument("--publisher-receipt", type=Path, required=True)
    failure = commands.add_parser("fail")
    failure.add_argument("--episode", default="")
    failure.add_argument("--stage", required=True)
    args = parser.parse_args(argv)
    try:
        run(args)
        return 0
    except (Hold, FileNotFoundError) as exc:
        print(f"Unattended control hold: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
