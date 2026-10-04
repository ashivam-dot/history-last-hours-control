"""Independent, issue-backed alerts for failed or missing History control runs.

This job has no publisher credentials and never reads producer code or artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPOSITORY = "ashivam-dot/history-last-hours-control"
UTC = timezone.utc
GRACE = timedelta(minutes=90)
RUN_URL = re.compile(r"https://github\.com/ashivam-dot/history-last-hours-control/actions/runs/[0-9]+\Z")


@dataclass(frozen=True)
class Watch:
    name: str
    workflow_file: str
    slug: str
    hours: tuple[int, ...]
    minute: int


WATCHES = (
    Watch("Unattended private History control (policy gated)",
          "unattended-control.yml", "control", (4, 10, 16, 22), 30),
    Watch("Verify History YouTube delivery after the due time",
          "post-due-monitor.yml", "delivery", (19,), 0),
)


class AlertError(RuntimeError):
    """The alert could not be safely checked or recorded."""


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AlertError("run or activation time is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AlertError("run or activation time is malformed") from None
    if parsed.utcoffset() is None:
        raise AlertError("run or activation time has no zone")
    return parsed.astimezone(UTC)


def last_due(now: datetime, activated: datetime, watch: Watch) -> datetime | None:
    """Most recent elapsed slot, allowing a bounded GitHub schedule delay."""
    now, activated = now.astimezone(UTC), activated.astimezone(UTC)
    slots = (
        datetime.combine((now - timedelta(days=days)).date(),
                         datetime.min.time(), UTC).replace(hour=hour, minute=watch.minute)
        for days in range(3) for hour in watch.hours
    )
    eligible = [slot for slot in slots if activated < slot and slot + GRACE <= now]
    return max(eligible, default=None)


def scheduled_run_present(runs: list[dict], slot: datetime) -> bool:
    """A manual run never hides a missed automatic slot."""
    for run in runs:
        if (run.get("event") == "schedule" and run.get("head_branch") == "main"
                and isinstance(run.get("created_at"), str)
                and parse_time(run["created_at"]) >= slot - timedelta(minutes=5)):
            return True
    return False


def api(method: str, path: str, token: str, payload: dict | None = None) -> object:
    if not token:
        raise AlertError("GitHub issue token is unavailable")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise AlertError("GitHub alert response is oversized")
        return json.loads(body) if body else {}
    except AlertError:
        raise
    except Exception:
        raise AlertError("GitHub alert state is unavailable") from None


def runs_for(watch: Watch, token: str) -> list[dict]:
    result = api("GET", f"/actions/workflows/{watch.workflow_file}/runs?per_page=100", token)
    if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
        raise AlertError("workflow run list is malformed")
    return [row for row in result["workflow_runs"] if isinstance(row, dict)]


def open_issues(token: str) -> list[dict]:
    found = []
    for page in range(1, 6):
        result = api("GET", f"/issues?state=open&per_page=100&page={page}", token)
        if not isinstance(result, list):
            raise AlertError("private issue list is malformed")
        found.extend(item for item in result if isinstance(item, dict) and "pull_request" not in item)
        if len(result) < 100:
            return found
    raise AlertError("private issue list exceeds page limit")


def sync_issue(token: str, watch: Watch, kind: str, incident: str | None,
               detail: str = "", run_url: str = "") -> str:
    if kind not in ("failed", "missed"):
        raise AlertError("unknown alert kind")
    title = f"History control: {watch.slug} {kind}"
    matching = [item for item in open_issues(token) if item.get("title") == title]
    if len(matching) > 1:
        raise AlertError("duplicate private alert issues require review")
    current = matching[0] if matching else None
    if incident is None:
        if current:
            api("PATCH", f"/issues/{current['number']}", token, {"state": "closed"})
            return "resolved"
        return "clear"
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", incident):
        raise AlertError("alert incident identifier is malformed")
    fingerprint = hashlib.sha256(incident.encode()).hexdigest()[:16]
    marker = f"<!-- history-control-alert:{watch.slug}:{kind}:{fingerprint} -->"
    if current and marker in str(current.get("body") or ""):
        return "already_open"
    safe_url = (run_url if isinstance(run_url, str) and RUN_URL.fullmatch(run_url)
                else f"https://github.com/{REPOSITORY}/actions")
    body = (f"A cloud automation check needs attention.\n\n"
            f"Workflow: `{watch.workflow_file}`\n\n"
            f"Incident: `{incident}`\n\n"
            f"{detail[:500]}\n\n"
            f"Run: {safe_url}\n\n{marker}\n")
    if current:
        api("PATCH", f"/issues/{current['number']}", token, {"body": body})
        return "updated"
    created = api("POST", "/issues", token, {"title": title, "body": body})
    if not isinstance(created, dict) or not isinstance(created.get("number"), int):
        raise AlertError("private alert issue creation was not confirmed")
    return "opened"


def from_workflow_run(event: dict, token: str) -> str:
    run = event.get("workflow_run")
    if (not isinstance(run, dict) or (event.get("repository") or {}).get("full_name") != REPOSITORY
            or run.get("head_branch") != "main" or run.get("status") != "completed"):
        return "ignored"
    watch = next((item for item in WATCHES if item.name == run.get("name")), None)
    if watch is None:
        return "ignored"
    completed = [item for item in runs_for(watch, token)
                 if item.get("head_branch") == "main" and item.get("status") == "completed"]
    if not completed:
        raise AlertError("watched completed run is unavailable")
    latest = max(completed, key=lambda item: (item.get("created_at") or "", item.get("id") or 0))
    conclusion = latest.get("conclusion")
    if conclusion in ("success", "skipped"):
        return sync_issue(token, watch, "failed", None)
    run_id = latest.get("id")
    if (not isinstance(run_id, int) or not isinstance(conclusion, str)
            or not re.fullmatch(r"[a-z_]{1,40}", conclusion)):
        raise AlertError("watched run conclusion is malformed")
    return sync_issue(token, watch, "failed", str(run_id),
                      f"Latest completed run concluded `{conclusion}`.", latest.get("html_url", ""))


def check_missed(now: datetime, token: str, enabled: bool, activated_at: str) -> dict[str, str]:
    if enabled and not activated_at:
        raise AlertError("automation activation time is not configured")
    activated = parse_time(activated_at) if enabled else now
    results = {}
    for watch in WATCHES:
        if not enabled:
            results[watch.slug] = sync_issue(token, watch, "missed", None)
            continue
        workflow = api("GET", f"/actions/workflows/{watch.workflow_file}", token)
        if not isinstance(workflow, dict) or workflow.get("path") != f".github/workflows/{watch.workflow_file}":
            raise AlertError("watched workflow identity is unavailable")
        slot = last_due(now, activated, watch)
        if workflow.get("state") != "active":
            results[watch.slug] = sync_issue(token, watch, "missed", "inactive",
                                             "The scheduled workflow is inactive.")
        elif slot is None or scheduled_run_present(runs_for(watch, token), slot):
            results[watch.slug] = sync_issue(token, watch, "missed", None)
        else:
            results[watch.slug] = sync_issue(token, watch, "missed", slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                             f"No automatic run appeared for the {slot.isoformat()} UTC slot.")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("workflow-run", "missed"))
    parser.add_argument("--event", type=Path)
    args = parser.parse_args()
    try:
        token = os.environ.get("GH_TOKEN", "")
        if args.mode == "workflow-run":
            if not args.event or not args.event.is_file() or args.event.stat().st_size > 2_000_000:
                raise AlertError("workflow event is unavailable")
            event = json.loads(args.event.read_text(encoding="utf-8"))
            if not isinstance(event, dict):
                raise AlertError("workflow event is malformed")
            result = from_workflow_run(event, token)
        else:
            result = check_missed(datetime.now(UTC), token,
                                  os.environ.get("HISTORY_CONTROL_AUTOMATION") == "1",
                                  os.environ.get("HISTORY_CONTROL_ALERTS_STARTED_AT", ""))
        print(json.dumps({"alert_result": result}, sort_keys=True))
        return 0
    except (AlertError, ValueError):
        print("History owner alert needs retry; check this workflow run", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
