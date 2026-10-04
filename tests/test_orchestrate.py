"""Unattended orchestration invariants without live credentials or mutations."""

import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from control import orchestrate as orch
from control.release import Hold, policy


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def config():
    return policy(Path(__file__).resolve().parents[1] / "policy.json")


def test_discovery_pins_last_draft_change_not_unrelated_main_commit(tmp_path, config):
    repo = tmp_path / "producer"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "remote", "add", "origin", config["source_remote"])
    draft = repo / "content/episodes/ep063/draft.json"
    draft.parent.mkdir(parents=True)
    draft.write_text('{}')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "draft")
    exact = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("Unrelated edit\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "unrelated")
    assert exact != git(repo, "rev-parse", "HEAD")
    assert list(orch.draft_candidates(repo, config)) == [("ep063", exact)]
    (draft.parent / "publish.json").write_text('{}')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "legacy release")
    assert list(orch.draft_candidates(repo, config)) == [("ep063", exact)]
    with pytest.raises(Hold, match="release or hosted-media"):
        orch.producer_unreleased(repo, "HEAD", "ep063")
    git(repo, "remote", "set-url", "origin", "https://github.com/other/producer.git")
    with pytest.raises(Hold, match="origin"):
        list(orch.draft_candidates(repo, config))


class MemoryState:
    def __init__(self, receipts=()):
        self.items = {item["episode"]: item for item in receipts}
        self.alerts = []

    def ensure_branch(self):
        pass

    def receipts(self):
        return list(self.items.values())

    def receipt(self, episode):
        return self.items.get(episode)

    def put(self, receipt):
        self.items[receipt["episode"]] = receipt

    def alert(self, episode, stage, reason):
        self.alerts.append((episode, stage, reason))


def _receipt(phase="claimed", commit="a" * 40):
    return {"version": 1, "episode": "ep063", "source_commit": commit,
            "draft_sha256": "b" * 64, "media_sha256": "c" * 64,
            "phase": phase, "updated_at_utc": "2026-10-04T00:00:00+00:00"}


def _source():
    return {"draft_sha256": "b" * 64, "draft": {"media_sha256": "c" * 64}}


def test_changed_draft_holds_claim_and_opens_private_alert(monkeypatch, tmp_path, config):
    old = _receipt()
    state = MemoryState([old])
    monkeypatch.setattr(orch, "draft_candidates", lambda repo, config: iter([("ep063", "d" * 40)]))
    monkeypatch.setattr(orch, "exact_draft", lambda *args: _source())
    monkeypatch.setattr(orch, "producer_unreleased", lambda *args: None)
    with pytest.raises(Hold, match="draft changed"):
        orch.discover(SimpleNamespace(source=tmp_path, resume_episode=""), config, state)
    assert state.items["ep063"]["phase"] == "held"
    assert state.items["ep063"]["source_commit"] == old["source_commit"]
    assert state.alerts[0][:2] == ("ep063", "discovery")


def test_disabled_policy_claims_only_and_blocks_later_phases(monkeypatch, tmp_path, config):
    state = MemoryState()
    monkeypatch.setattr(orch, "draft_candidates", lambda repo, config: iter([("ep063", "a" * 40)]))
    monkeypatch.setattr(orch, "exact_draft", lambda *args: _source())
    monkeypatch.setattr(orch, "producer_unreleased", lambda *args: None)
    selected = orch.discover(SimpleNamespace(source=tmp_path, resume_episode=""), config, state)
    assert selected == {}
    assert state.receipt("ep063")["phase"] == "claimed"
    for action in orch.SWITCHES:
        assert not orch.enabled(config, action)


def test_ordered_idempotent_transitions_and_fixed_due_reservation():
    receipt = _receipt()
    with pytest.raises(Hold, match="out of order"):
        orch.advance(receipt, "signed")
    intake = orch.advance(receipt, "intake_verified")
    assert orch.advance(intake, "intake_verified") is intake
    qa = orch.advance(intake, "qa_approved")
    signed = orch.advance(qa, "signed")
    now = datetime(2026, 10, 4, 16, tzinfo=timezone.utc)
    occupied = [{"due_at_utc": datetime(2026, 10, 5, 17, tzinfo=timezone.utc).isoformat()}]
    due = orch.next_due(occupied, now)
    assert due == "2026-10-06T17:00:00+00:00"
    planned = {**orch.advance(signed, "release_planned"), "due_at_utc": due}
    assert orch.advance(planned, "release_planned") is planned
    assert datetime.fromisoformat(due) > now + timedelta(hours=2)


def test_held_release_cannot_be_automatically_resumed(monkeypatch, tmp_path, config):
    state = MemoryState([{**_receipt("held"), "resume_phase": "release_planned"}])
    monkeypatch.setattr(orch, "draft_candidates", lambda repo, config: iter([("ep063", "a" * 40)]))
    monkeypatch.setattr(orch, "exact_draft", lambda *args: _source())
    monkeypatch.setattr(orch, "producer_unreleased", lambda *args: None)
    with pytest.raises(Hold, match="manual Buffer inspection"):
        orch.discover(SimpleNamespace(source=tmp_path, resume_episode="ep063"), config, state)
    assert state.receipt("ep063")["phase"] == "held"


def test_failure_holds_receipt_and_alerts_even_when_policy_is_invalid(monkeypatch):
    state = MemoryState([_receipt("qa_approved")])

    def bad_policy(path):
        raise Hold("bad policy")

    monkeypatch.setattr(orch, "policy", bad_policy)
    monkeypatch.setattr(orch, "GitHubState", lambda *args: state)
    orch.run(SimpleNamespace(command="fail", episode="ep063", stage="sign", policy=Path("unused")))
    held = state.receipt("ep063")
    assert held["phase"] == "held" and held["resume_phase"] == "qa_approved"
    assert state.alerts == [("ep063", "sign", "Actions job failed; inspect the linked run and private artifacts")]


def test_release_reservation_is_durable_and_blocks_automatic_retry(monkeypatch, tmp_path, config):
    state = MemoryState([_receipt("signed")])
    live = {**config, "intake_enabled": True, "signing_enabled": True, "publishing_enabled": True}
    monkeypatch.setattr(orch, "policy", lambda path: live)
    monkeypatch.setattr(orch, "GitHubState", lambda *args: state)
    output = tmp_path / "reservation.json"
    args = SimpleNamespace(command="reserve", episode="ep063", commit="a" * 40,
                           output=output, policy=Path("unused"))
    orch.run(args)
    first = json.loads(output.read_text())["due_at_utc"]
    assert state.receipt("ep063")["phase"] == "release_planned"
    with pytest.raises(Hold, match="inspect Buffer"):
        orch.run(args)
    assert state.receipt("ep063")["due_at_utc"] == first


def test_existing_phases_wait_for_their_next_policy_switch(monkeypatch, tmp_path, config):
    state = MemoryState([_receipt("qa_approved")])
    monkeypatch.setattr(orch, "draft_candidates", lambda repo, config: iter([("ep063", "a" * 40)]))
    monkeypatch.setattr(orch, "producer_unreleased", lambda *args: None)
    monkeypatch.setattr(orch, "exact_draft", lambda *args: _source())
    args = SimpleNamespace(source=tmp_path, resume_episode="")
    intake_only = {**config, "intake_enabled": True}
    assert orch.discover(args, intake_only, state) == {}
    signing = {**intake_only, "signing_enabled": True}
    assert orch.discover(args, signing, state)["episode"] == "ep063"
    state.put(_receipt("signed"))
    assert orch.discover(args, signing, state) == {}
    publishing = {**signing, "publishing_enabled": True}
    assert orch.discover(args, publishing, state)["publishing_enabled"] is True


def test_release_planned_reentry_holds_and_alerts_before_second_attempt(monkeypatch, tmp_path, config):
    state = MemoryState([{**_receipt("release_planned"), "due_at_utc": "2026-10-06T17:00:00+00:00"}])
    monkeypatch.setattr(orch, "draft_candidates", lambda repo, config: iter([("ep063", "a" * 40)]))
    with pytest.raises(Hold, match="inspect the external Buffer post"):
        orch.discover(SimpleNamespace(source=tmp_path, resume_episode=""), config, state)
    assert state.receipt("ep063")["phase"] == "held"
    assert state.receipt("ep063")["resume_phase"] == "release_planned"
    assert state.alerts[0][:2] == ("ep063", "discovery")


def test_publisher_receipt_must_match_reserved_identity_and_time(monkeypatch, tmp_path, config):
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=17, minute=0,
                                                                    second=0, microsecond=0).isoformat()
    state = MemoryState([{**_receipt("release_planned"), "due_at_utc": due}])
    live = {**config, "intake_enabled": True, "signing_enabled": True, "publishing_enabled": True}
    monkeypatch.setattr(orch, "policy", lambda path: live)
    monkeypatch.setattr(orch, "GitHubState", lambda *args: state)
    publisher = tmp_path / "publisher.json"
    receipt = {"episode": "ep063", "source_commit": "a" * 40, "media_sha256": "c" * 64,
               "youtube": {"status": "scheduled", "due_at": due}}
    args = SimpleNamespace(command="complete", episode="ep063", commit="a" * 40,
                           publisher_receipt=publisher, policy=Path("unused"))
    publisher.write_text(json.dumps({**receipt, "media_sha256": "0" * 64}))
    with pytest.raises(Hold, match="differs"):
        orch.run(args)
    publisher.write_text(json.dumps(receipt))
    orch.run(args)
    assert state.receipt("ep063")["phase"] == "published"


def test_contents_write_uses_prior_sha_for_compare_and_swap(monkeypatch):
    state = orch.GitHubState("test-token", "owner/control", "a" * 40)
    calls = []
    monkeypatch.setattr(state, "file", lambda path: {"sha": "old-blob"})
    monkeypatch.setattr(state, "request", lambda method, path, **kwargs: calls.append((method, path, kwargs)))
    state.put(_receipt())
    method, path, kwargs = calls[0]
    assert (method, path) == ("PUT", "/contents/receipts/ep063.json")
    assert kwargs["json"]["branch"] == orch.STATE_BRANCH
    assert kwargs["json"]["sha"] == "old-blob"


def test_failure_alert_opens_then_updates_one_private_issue(monkeypatch):
    state = orch.GitHubState("test-token", "owner/control", "a" * 40)
    calls = []
    existing = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return existing
        return {}

    monkeypatch.setattr(state, "request", request)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/control")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    state.alert("ep063", "qa", "independent check held")
    assert calls[-1][0:2] == ("POST", "/issues")
    assert calls[-1][2]["json"]["title"] == "[unattended] ep063 held"
    assert "/actions/runs/123" in calls[-1][2]["json"]["body"]
    existing.append({"number": 7, "title": "[unattended] ep063 held"})
    state.alert("ep063", "publish", "publisher held")
    assert calls[-1][0:2] == ("PATCH", "/issues/7")


@pytest.mark.parametrize("operation", ["sign", "publish"])
def test_inflight_run_holds_when_main_policy_switch_is_turned_off(monkeypatch, config, operation):
    state = orch.GitHubState("test-token", "owner/control", "a" * 40)
    local = {**config, "intake_enabled": True, "signing_enabled": True,
             "publishing_enabled": True}
    live = {**local, **({"signing_enabled": False} if operation == "sign" else
                       {"publishing_enabled": False})}
    entry = {"type": "file", "content": base64.b64encode(json.dumps(live).encode()).decode()}
    monkeypatch.setattr(state, "request", lambda *args, **kwargs: entry)
    with pytest.raises(Hold, match="main policy changed"):
        state.require_live_policy(local, operation)
    entry["content"] = base64.b64encode(json.dumps(local).encode()).decode()
    state.require_live_policy(local, operation)


def test_workflow_separates_credentials_and_gates_release():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/unattended-control.yml"
    workflow = yaml.safe_load(path.read_text())
    jobs = workflow["jobs"]
    assert jobs["intake"]["environment"] == jobs["publish"]["environment"] == "history-publisher"
    assert jobs["qa"]["environment"] == "history-independent-qa"
    assert jobs["sign"]["environment"] == "history-review-signing"
    assert "signing_enabled == 'true'" in jobs["sign"]["if"]
    assert "publishing_enabled == 'true'" in jobs["publish"]["if"]
    assert jobs["publish"]["concurrency"]["group"] == "history-control-publisher"
    for stage in ("intake", "qa", "sign", "publish"):
        assert "control.orchestrate current" in json.dumps(jobs[stage])
    for job in jobs.values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
    qa_text = json.dumps(jobs["qa"])
    sign_text = json.dumps(jobs["sign"])
    assert "HISTORY_PUBLISHER_BUFFER_API_KEY" not in qa_text + sign_text
    assert "HISTORY_REVIEW_SIGNING_KEY" not in qa_text
    assert "HISTORY_QA_GEMINI_API_KEY" not in sign_text
    for stage, command in (("sign", "sign-qa-draft"), ("publish", "publish-draft")):
        steps = jobs[stage]["steps"]
        mutation = next(index for index, step in enumerate(steps) if command in step.get("run", ""))
        script = steps[mutation]["run"]
        assert script.index(f"live-policy --operation {stage}") < script.index(f"control {command}")
        assert "env -u GITHUB_TOKEN uv run" in script
