import base64
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control.release import (Hold, FILES, candidate, digest, policy, publisher_preflight,
                             sign, verify)
from control.publisher import CloudinaryClient, publish_reviewed


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
    manifest = {"id": "ep063", "video_sha256": digest(video), "beats": [
        {"text": "The ship was lost.", "asset": {"source": "designed card", "license": None}}]}
    documents = {
        "topic.json": {"started_at": "2026-10-04T05:00:00+00:00"},
        "script.json": {"beats": [{"text": "The ship was lost.", "claims": [1]}]},
        "research.json": {"viable": True,
                          "sources": [{"label": "S1", "url": "https://museum.example/story"},
                                      {"label": "S2", "url": "https://archive.example/story"}],
                          "claims": [{"sources": ["S1", "S2"], "evidence": [
                              {"source": "S1", "quote": "The ship was lost during the long voyage."},
                              {"source": "S2", "quote": "The ship was lost while crossing the ocean."}]}]},
        "visuals.json": {"used": []},
        "review.json": {"rounds": [{"media_sha256": digest(video), "passed": True,
                                      "check": {"warnings": [], "speech_differences": [], "speech_error": None},
                                      "scores": {name: 4 for name in
                                                 ("hook", "clarity", "payoff", "visuals", "loop")},
                                      "frames": [], "speech": []}]},
        "work/manifest.json": manifest,
    }
    for name, data in documents.items():
        (episode / name).write_text(json.dumps(data), encoding="utf-8")
    (episode / "short.yaml").write_text(
        "id: ep063\ntitle: The final voyage\ndescription: A documented loss.\n"
        "sources:\n  - https://museum.example/story\n"
        "hashtags:\n  - '#history'\nbeats:\n  - text: The ship was lost.\n",
        encoding="utf-8")
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


@pytest.mark.parametrize("mutation", ["missing_second_quote", "unlicensed_visual", "dirty_review"])
def test_independent_structural_source_rights_and_media_gates_hold(fixture, mutation):
    source, _, episode, video, config = fixture
    if mutation == "missing_second_quote":
        path = episode / "research.json"
        data = json.loads(path.read_text())
        data["claims"][0]["evidence"] = data["claims"][0]["evidence"][:1]
    elif mutation == "unlicensed_visual":
        path = episode / "work" / "manifest.json"
        data = json.loads(path.read_text())
        data["beats"][0]["asset"] = {"source": "Wikimedia Commons", "license": "unknown",
                                        "url": "https://commons.example/image", "credit": "Archive"}
    else:
        path = episode / "review.json"
        data = json.loads(path.read_text())
        data["rounds"][0]["check"]["warnings"] = ["audio clipping"]
    path.write_text(json.dumps(data))
    if mutation == "unlicensed_visual":
        held_path = episode / "hold.json"
        held = json.loads(held_path.read_text())
        held["manifest_sha256"] = digest(path.read_bytes())
        held_path.write_text(json.dumps(held))
    git(source, "add", ".")
    git(source, "commit", "-m", "invalid independent evidence")
    commit = git(source, "rev-parse", "HEAD")
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
              "reviewer_key_sha256": digest(public_raw), "buffer_organization_id": "history-org",
              "youtube_channel_id": "history-yt",
              "publisher_media_url_prefix": "https://res.cloudinary.com/independent-history/video/upload/"}
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


class FakeBuffer:
    def __init__(self, media_url):
        self.media_url = media_url
        self.created = []
        self.by_channel = {"history-yt": []}
        self.by_id = {}
        self.services = ("youtube",)

    def organization(self, wanted):
        assert wanted == "history-org"

    def channels(self, organization):
        assert organization == "history-org"
        return [{"id": channel, "service": service, "isDisconnected": False,
                 "isLocked": False, "isQueuePaused": False}
                for channel, service in zip(self.by_channel, self.services)]

    def posts(self, organization, channel):
        assert organization == "history-org"
        return list(self.by_channel[channel])

    def post(self, post_id):
        return self.by_id[post_id]

    def create(self, payload):
        self.created.append(payload)
        post_id = f"buffer-{len(self.created)}"
        summary = {"id": post_id, "status": "scheduled", "dueAt": payload["dueAt"],
                   "text": payload["text"]}
        self.by_channel[payload["channelId"]].append(summary)
        self.by_id[post_id] = {**summary, "assets": [{"source": self.media_url}]}
        return summary


class FakeMedia:
    def __init__(self, url):
        self.url = url
        self.uploads = []

    def upload_exact(self, video, episode, media_sha256):
        assert digest(video) == media_sha256
        self.uploads.append((episode, media_sha256))
        return self.url, f"history-control/{episode}-{media_sha256}"


def approved_publisher(fixture):
    source, commit, _, video, config = fixture
    subject = candidate(source, commit, "ep063", video, config)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "publishing_enabled": True,
              "reviewer_key_sha256": digest(public_raw), "buffer_organization_id": "history-org",
              "youtube_channel_id": "history-yt",
              "publisher_media_url_prefix": "https://res.cloudinary.com/independent-history/video/upload/"}
    approval = {"subject": subject, "decision": "approved", "checks": {name: True for name in
                ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    return source, commit, video, subject, config, review, base64.b64encode(public_raw).decode()


def test_control_publisher_schedules_exact_youtube_media_and_reconciles_without_duplicate(fixture, monkeypatch):
    source, commit, video, subject, config, review, public = approved_publisher(fixture)
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    monkeypatch.setattr("control.publisher.fetch_video", lambda *_: video)
    copy_url = config["publisher_media_url_prefix"] + "history-control/ep063-" + digest(video) + ".mp4"
    api = FakeBuffer(copy_url)
    media = FakeMedia(copy_url)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    first = publish_reviewed(source, commit, "ep063", review, public, config,
                             "test-token", "test-cloudinary", due, api, media)
    assert [item["channelId"] for item in api.created] == ["history-yt"]
    assert all(item["assets"] == [{"video": {"url": copy_url}}] for item in api.created)
    assert first["control_media_url"] == copy_url
    assert copy_url != subject["media_url"]
    assert first["youtube"]["reconciled"] is False
    second = publish_reviewed(source, commit, "ep063", review, public, config,
                              "test-token", "test-cloudinary", due, api, media)
    assert len(api.created) == 1
    assert second["youtube"]["reconciled"] is True
    assert "instagram" not in second


@pytest.mark.parametrize("failure", ["wrong_channel", "duplicate", "changed_media",
                                          "changed_control_copy", "missing_token", "missing_cloudinary"])
def test_control_publisher_holds_before_buffer_create(fixture, monkeypatch, failure):
    source, commit, video, subject, config, review, public = approved_publisher(fixture)
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    monkeypatch.setattr("control.publisher.fetch_video", lambda *_: video)
    copy_url = config["publisher_media_url_prefix"] + "history-control/ep063-" + digest(video) + ".mp4"
    api = FakeBuffer(copy_url)
    media = FakeMedia(copy_url)
    if failure == "wrong_channel":
        api.services = ("instagram",)
    elif failure == "duplicate":
        spec_text = __import__("control.publisher", fromlist=["copy_and_metadata"]).copy_and_metadata(
            source, commit, "ep063")[0]["youtube"]
        api.by_channel["history-yt"] = [
            {"id": "one", "status": "scheduled", "text": spec_text},
            {"id": "two", "status": "scheduled", "text": spec_text},
        ]
    elif failure == "changed_media":
        monkeypatch.setattr("control.publisher.fetch_video", lambda *_: video + b"changed")
    elif failure == "changed_control_copy":
        monkeypatch.setattr("control.publisher.fetch_video", lambda url, *_:
                            video + b"changed" if url.startswith(config["publisher_media_url_prefix"]) else video)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    with pytest.raises(Hold):
        publish_reviewed(source, commit, "ep063", review, public, config,
                         "" if failure == "missing_token" else "test-token",
                         "" if failure == "missing_cloudinary" else "test-cloudinary",
                         due, api, media)
    assert api.created == []


def test_control_cloudinary_upload_is_content_addressed_and_never_overwrites(monkeypatch):
    prefix = "https://res.cloudinary.com/independent-history/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@independent-history", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"video test bytes"
    video_hash = digest(video)
    public_id = f"history-control/ep063-{video_hash}"
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"public_id": public_id, "resource_type": "video", "bytes": len(video),
                    "secure_url": prefix + "v123/" + public_id + ".mp4"}

    def post(url, *, data, files, timeout):
        calls.append((url, data, files, timeout))
        return Response()

    monkeypatch.setattr("control.publisher.requests.post", post)
    url, returned_id = client.upload_exact(video, "ep063", video_hash)
    assert returned_id == public_id
    assert url == prefix + public_id + ".mp4"
    assert calls[0][1]["overwrite"] == "false"
    assert calls[0][2]["file"][1] == video
    with pytest.raises(Hold, match="account differs"):
        CloudinaryClient("cloudinary://key:secret@producer-account", prefix)
