import base64
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control.release import (Hold, FILES, candidate, digest, policy, publisher_preflight,
                             sign, verify)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "remote", "add", "origin", "https://github.com/ashivam-dot/creature-receipts.git")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    episode = source / "content" / "episodes" / "ep063"
    (episode / "work").mkdir(parents=True)
    video = b"a complete test video byte sequence"
    manifest = {"id": "ep063", "video_sha256": digest(video), "beats": []}
    documents = {
        "topic.json": {"started_at": "2026-10-04T05:00:00+00:00"},
        "script.json": {"beats": []}, "research.json": {"viable": True},
        "visuals.json": {"used": []},
        "review.json": {"rounds": [{"media_sha256": digest(video), "passed": True}]},
        "work/manifest.json": manifest,
    }
    for name, data in documents.items():
        (episode / name).write_text(json.dumps(data), encoding="utf-8")
    (episode / "short.yaml").write_text("id: ep063\n", encoding="utf-8")
    held = {"id": "ep063", "media_sha256": digest(video),
            "media_url": "https://res.cloudinary.com/uj4a07e7/video/upload/v1/history/ep063.mp4",
            "media_public_id": "history/ep063",
            "spec_sha256": digest((episode / "short.yaml").read_bytes()),
            "manifest_sha256": digest((episode / "work" / "manifest.json").read_bytes())}
    (episode / "hold.json").write_text(json.dumps(held), encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "test candidate")
    commit = git(source, "rev-parse", "HEAD")
    config = policy(Path(__file__).resolve().parents[1] / "policy.json")
    return source, commit, episode, video, config


def test_exact_committed_candidate_matches_producer_subject(fixture):
    source, commit, _, video, config = fixture
    subject = candidate(source, commit, "ep063", video, config)
    assert subject["media_sha256"] == hashlib.sha256(video).hexdigest()
    assert set(subject["files"]) == set(FILES)
    assert subject["id"] == "ep063"


@pytest.mark.parametrize("mutation", ["video", "hold", "review", "commit", "origin"])
def test_changed_or_untrusted_candidate_holds(fixture, mutation):
    source, commit, episode, video, config = fixture
    if mutation == "video":
        video += b"changed"
    elif mutation == "hold":
        held = json.loads((episode / "hold.json").read_text())
        held["manifest_sha256"] = "0" * 64
        (episode / "hold.json").write_text(json.dumps(held))
        git(source, "add", ".")
        git(source, "commit", "-m", "stale binding")
        commit = git(source, "rev-parse", "HEAD")
    elif mutation == "review":
        review = json.loads((episode / "review.json").read_text())
        review["rounds"][0]["passed"] = False
        (episode / "review.json").write_text(json.dumps(review))
        git(source, "add", ".")
        git(source, "commit", "-m", "failed review")
        commit = git(source, "rev-parse", "HEAD")
    elif mutation == "commit":
        commit = "0" * 40
    else:
        git(source, "remote", "set-url", "origin", "https://github.com/attacker/other.git")
    with pytest.raises(Hold):
        candidate(source, commit, "ep063", video, config)


def test_signing_is_disabled_by_tracked_policy(fixture):
    source, commit, _, video, config = fixture
    subject = candidate(source, commit, "ep063", video, config)
    approval = {"subject": subject, "decision": "approved", "checks": {name: True for name in
                ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    with pytest.raises(Hold, match="disabled"):
        sign(approval, subject, "", config)


def test_signed_review_matches_producer_contract_and_tampering_holds(fixture):
    source, commit, _, video, config = fixture
    subject = candidate(source, commit, "ep063", video, config)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "reviewer_key_sha256": digest(public_raw)}
    approval = {"subject": subject, "decision": "approved", "checks": {name: True for name in
                ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    verify(review, subject, base64.b64encode(public_raw).decode(), config)
    altered = {**review, "reviewed_at_utc": "2026-10-04T05:00:00+00:00"}
    with pytest.raises(Hold, match="signature"):
        verify(altered, subject, base64.b64encode(public_raw).decode(), config)
    denied = {**approval, "checks": {**approval["checks"], "full_video_audio": False}}
    with pytest.raises(Hold, match="all checks"):
        sign(denied, subject, base64.b64encode(private_raw).decode(), config)
    changed = {**subject, "media_sha256": "0" * 64}
    with pytest.raises(Hold, match="exact candidate"):
        verify(review, changed, base64.b64encode(public_raw).decode(), config)


def test_publisher_preflight_fails_before_network_when_disabled(fixture, monkeypatch):
    source, commit, _, _, config = fixture
    monkeypatch.setattr("control.release.fetch_video", lambda *_: pytest.fail("network reached"))
    with pytest.raises(Hold, match="publisher is disabled"):
        publisher_preflight(source, commit, "ep063", {}, "", config)


def test_future_publisher_gate_rechecks_video_signature_and_destination(fixture, monkeypatch):
    source, commit, _, video, config = fixture
    subject = candidate(source, commit, "ep063", video, config)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "publishing_enabled": True,
              "reviewer_key_sha256": digest(public_raw), "youtube_channel_id": "history-yt"}
    approval = {"subject": subject, "decision": "approved", "checks": {name: True for name in
                ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    public_b64 = base64.b64encode(public_raw).decode()
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    plan = publisher_preflight(source, commit, "ep063", review, public_b64, config)
    assert plan["youtube_channel_id"] == "history-yt"
    assert plan["subject"] == subject
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video + b"tampered")
    with pytest.raises(Hold, match="reviewed video, manifest"):
        publisher_preflight(source, commit, "ep063", review, public_b64, config)
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    with pytest.raises(Hold, match="destination"):
        publisher_preflight(source, commit, "ep063", review, public_b64,
                            {**config, "youtube_channel_id": ""})
