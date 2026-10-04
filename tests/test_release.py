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
from control.publisher import (CloudinaryClient, PUBLISHER_PUBLIC_ID_PREFIX,
                               publish_draft_reviewed, publish_reviewed)
from control.intake import committed_draft, intake_draft, validated_packet


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
              "publisher_media_url_prefix": "https://res.cloudinary.com/mw0oh0v8/video/upload/"}
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
        return self.url, f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{media_sha256}"


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
              "publisher_media_url_prefix": "https://res.cloudinary.com/mw0oh0v8/video/upload/"}
    approval = {"subject": subject, "decision": "approved", "checks": {name: True for name in
                ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    return source, commit, video, subject, config, review, base64.b64encode(public_raw).decode()


def test_control_publisher_schedules_exact_youtube_media_and_reconciles_without_duplicate(fixture, monkeypatch):
    source, commit, video, subject, config, review, public = approved_publisher(fixture)
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    monkeypatch.setattr("control.publisher.fetch_video", lambda *_: video)
    copy_url = config["publisher_media_url_prefix"] + PUBLISHER_PUBLIC_ID_PREFIX + "ep063-" + digest(video) + ".mp4"
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
                                          "changed_control_copy", "wrong_namespace",
                                          "missing_token", "missing_cloudinary"])
def test_control_publisher_holds_before_buffer_create(fixture, monkeypatch, failure):
    source, commit, video, subject, config, review, public = approved_publisher(fixture)
    monkeypatch.setattr("control.release.fetch_video", lambda *_: video)
    monkeypatch.setattr("control.publisher.fetch_video", lambda *_: video)
    copy_url = config["publisher_media_url_prefix"] + PUBLISHER_PUBLIC_ID_PREFIX + "ep063-" + digest(video) + ".mp4"
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
    elif failure == "wrong_namespace":
        media.url = config["publisher_media_url_prefix"] + "mool-katha/ep063-" + digest(video) + ".mp4"
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    with pytest.raises(Hold):
        publish_reviewed(source, commit, "ep063", review, public, config,
                         "" if failure == "missing_token" else "test-token",
                         "" if failure == "missing_cloudinary" else "test-cloudinary",
                         due, api, media)
    assert api.created == []


def test_control_cloudinary_upload_is_content_addressed_and_never_overwrites(monkeypatch):
    prefix = "https://res.cloudinary.com/mw0oh0v8/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@mw0oh0v8", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"video test bytes"
    video_hash = digest(video)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}ep063-{video_hash}"
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


def test_temporary_media_probe_deletes_only_its_own_test_asset(monkeypatch):
    prefix = "https://res.cloudinary.com/mw0oh0v8/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@mw0oh0v8", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"tiny test video"
    media_hash = digest(video)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}test/probe-{'a' * 32}-{media_hash}"
    calls = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def post(url, *, data, timeout, files=None):
        calls.append((url, data, files))
        if url.endswith("/video/upload"):
            return Response({"public_id": public_id, "resource_type": "video",
                             "bytes": len(video), "secure_url": prefix + public_id + ".mp4"})
        return Response({"result": "ok"})

    monkeypatch.setattr("control.publisher.requests.post", post)
    url, returned_id = client.upload_probe(video, public_id, media_hash)
    assert (url, returned_id) == (prefix + public_id + ".mp4", public_id)
    client.destroy_probe(public_id)
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == ["upload", "destroy"]
    assert calls[0][1]["overwrite"] == "false"
    assert calls[1][1]["invalidate"] == "true"
    assert calls[0][1]["public_id"] == calls[1][1]["public_id"] == public_id
    with pytest.raises(Hold, match="only an isolated test asset"):
        client.destroy_probe("mool-katha/ep003")
    assert len(calls) == 2


def test_control_cloudinary_private_draft_upload_and_authenticated_readback(monkeypatch):
    prefix = "https://res.cloudinary.com/mw0oh0v8/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@mw0oh0v8", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"private draft"
    media_hash = digest(video)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/ep063-{media_hash}"
    private_url = f"https://res.cloudinary.com/mw0oh0v8/video/authenticated/v123/{public_id}.mp4"
    calls = []

    class UploadResponse:
        status_code = 200

        def json(self):
            return {"public_id": public_id, "resource_type": "video", "type": "authenticated",
                    "bytes": len(video), "asset_id": "asset-12345678", "secure_url": private_url}

    class DownloadResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def iter_content(self, *_):
            yield video

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return UploadResponse() if url.endswith("/video/upload") else DownloadResponse()

    monkeypatch.setattr("control.publisher.requests.post", post)
    assert client.upload_private_draft(video, "ep063", media_hash) == (
        private_url, public_id, "asset-12345678")
    assert client.download_private_asset("asset-12345678") == video
    assert calls[0][1]["data"]["type"] == "authenticated"
    assert calls[0][1]["data"]["overwrite"] == "false"
    assert calls[1][0].endswith("/asset/download")
    assert calls[1][1]["data"]["asset_id"] == "asset-12345678"


def test_control_cloudinary_rejects_public_pre_qa_draft_url(monkeypatch):
    prefix = "https://res.cloudinary.com/mw0oh0v8/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@mw0oh0v8", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"private draft"
    media_hash = digest(video)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/ep063-{media_hash}"

    class Response:
        status_code = 200

        def json(self):
            return {"public_id": public_id, "resource_type": "video", "type": "upload",
                    "bytes": len(video), "asset_id": "asset-12345678",
                    "secure_url": prefix + public_id + ".mp4"}

    monkeypatch.setattr("control.publisher.requests.post", lambda *args, **kwargs: Response())
    with pytest.raises(Hold, match="identity, type, or size"):
        client.upload_private_draft(video, "ep063", media_hash)


def draft_fixture(fixture):
    source, _, episode, _, config = fixture
    config = {**config, "intake_enabled": True}
    video = b"\x00\x00\x00\x18ftypisom" + b"one exact private draft render"
    media_hash = digest(video)
    manifest_path = episode / "work" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["video_sha256"] = media_hash
    manifest_path.write_text(json.dumps(manifest))
    review_path = episode / "review.json"
    review = json.loads(review_path.read_text())
    review["rounds"][0]["media_sha256"] = media_hash
    review_path.write_text(json.dumps(review))
    (episode / "hold.json").unlink()
    draft = {"id": "ep063", "title": "The final voyage", "series": "History",
             "scores": {"hook": 4}, "anniversary": None, "media_sha256": media_hash,
             "spec_sha256": digest((episode / "short.yaml").read_bytes()),
             "manifest_sha256": digest(manifest_path.read_bytes()),
             "modal_volume": "creature-receipts-outbox",
             "modal_path": f"drafts/ep063-{media_hash}.mp4"}
    (episode / "draft.json").write_text(json.dumps(draft))
    git(source, "add", "-A")
    git(source, "commit", "-m", "private draft handoff")
    return source, git(source, "rev-parse", "HEAD"), episode, video, draft, config


def test_private_draft_intake_disabled_before_modal_or_media(fixture, tmp_path):
    source, commit, _, _, config = fixture
    with pytest.raises(Hold, match="intake is disabled"):
        intake_draft(source, commit, "ep063", config, "", tmp_path / "packet",
                     lambda _: pytest.fail("Modal read under disabled policy"))


class FakePrivateMedia:
    def __init__(self, url, video):
        self.url = url
        self.video = video
        self.uploads = []
        self.downloads = []

    def upload_private_draft(self, video, episode, media_sha256):
        assert digest(video) == media_sha256
        self.uploads.append((episode, media_sha256))
        return self.url, f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/{episode}-{media_sha256}", "asset-12345678"

    def download_private_asset(self, asset_id):
        self.downloads.append(asset_id)
        return self.video

    def upload_exact(self, video, episode, media_sha256):
        assert video == self.video and digest(video) == media_sha256
        self.uploads.append((episode, media_sha256))
        public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{media_sha256}"
        return self.public_url, public_id


def test_control_intake_binds_private_draft_source_and_exact_control_copy(fixture, tmp_path):
    source, commit, _, video, draft, config = draft_fixture(fixture)
    source_binding = committed_draft(source, commit, "ep063", config)
    assert source_binding["draft"] == draft
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    copy_url = private_prefix + "v123/" + PUBLISHER_PUBLIC_ID_PREFIX + "drafts/ep063-" + digest(video) + ".mp4"
    media = FakePrivateMedia(copy_url, video)
    output = tmp_path / "packet"
    packet = intake_draft(source, commit, "ep063", config, "test-cloudinary", output,
                          lambda path: video if path == draft["modal_path"] else pytest.fail("wrong Modal path"), media)
    subject = packet["subject"]
    assert subject["version"] == 2
    assert subject["source_commit"] == commit
    assert subject["draft_sha256"] == digest((output / "draft.json").read_bytes())
    assert subject["media_url"] == copy_url
    assert subject["media_delivery_type"] == "authenticated"
    assert subject["media_asset_id"] == "asset-12345678"
    assert (output / "ep063.mp4").read_bytes() == video
    assert (output / "evidence" / "work" / "manifest.json").is_file()
    hold = json.loads((output / "control_hold.json").read_text())
    assert hold["state"] == "held_for_independent_review"
    assert hold["media_sha256"] == digest(video)
    assert hold["source_commit"] == commit
    assert media.uploads == [("ep063", digest(video))]
    assert media.downloads == ["asset-12345678"]
    with pytest.raises(Hold, match="already exists"):
        intake_draft(source, commit, "ep063", config, "test-cloudinary", output,
                     lambda _: pytest.fail("duplicate Modal read"), media)


@pytest.mark.parametrize("failure", ["changed_private_video", "wrong_modal_path",
                                          "stale_manifest", "producer_hold"])
def test_control_intake_rejects_unbound_or_hosted_draft_before_upload(fixture, tmp_path, failure):
    source, commit, episode, video, draft, config = draft_fixture(fixture)
    if failure == "wrong_modal_path":
        draft["modal_path"] = "../mool-katha/ep003.mp4"
        (episode / "draft.json").write_text(json.dumps(draft))
    elif failure == "stale_manifest":
        draft["manifest_sha256"] = "0" * 64
        (episode / "draft.json").write_text(json.dumps(draft))
    elif failure == "producer_hold":
        (episode / "hold.json").write_text("{}")
    if failure != "changed_private_video":
        git(source, "add", "-A")
        git(source, "commit", "-m", "invalid draft handoff")
        commit = git(source, "rev-parse", "HEAD")
    media = FakePrivateMedia("https://res.cloudinary.com/mw0oh0v8/video/authenticated/never-uploaded.mp4", video)
    with pytest.raises(Hold):
        intake_draft(source, commit, "ep063", config, "test-cloudinary", tmp_path / "packet",
                     lambda _: video + b"changed" if failure == "changed_private_video" else
                     pytest.fail("Modal read before source validation"), media)
    assert media.uploads == []


def test_v2_packet_signing_and_publisher_handoff(fixture, tmp_path, monkeypatch):
    source, commit, _, video, draft, config = draft_fixture(fixture)
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    private_url = (private_prefix + "v123/" + PUBLISHER_PUBLIC_ID_PREFIX +
                   "drafts/ep063-" + digest(video) + ".mp4")
    packet_dir = tmp_path / "packet"
    intake_draft(source, commit, "ep063", config, "test-cloudinary", packet_dir,
                 lambda path: video, FakePrivateMedia(private_url, video))
    subject, packet_video = validated_packet(source, commit, "ep063", config, packet_dir)
    assert packet_video == video and subject["version"] == 2
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "publishing_enabled": True,
              "reviewer_key_sha256": digest(public_raw), "buffer_organization_id": "history-org",
              "youtube_channel_id": "history-yt"}
    approval = {"subject": subject, "decision": "approved",
                "checks": {name: True for name in
                           ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    public_b64 = base64.b64encode(public_raw).decode()
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    assert review["version"] == 2
    verify(review, subject, public_b64, config)
    with pytest.raises(Hold, match="schema"):
        verify({**review, "version": 1}, subject, public_b64, config)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}ep063-{digest(video)}"
    public_url = config["publisher_media_url_prefix"] + public_id + ".mp4"
    media = FakePrivateMedia(private_url, video)
    media.public_url = public_url
    api = FakeBuffer(public_url)
    monkeypatch.setattr("control.publisher.fetch_video", lambda url, _: video)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    receipt = publish_draft_reviewed(source, commit, "ep063", packet_dir, review, public_b64,
                                     config, "test-token", "test-cloudinary", due, api, media)
    assert receipt["control_media_url"] == public_url
    assert media.downloads == [subject["media_asset_id"]]
    assert media.uploads == [("ep063", digest(video))]
    assert len(api.created) == 1
    assert api.created[0]["assets"] == [{"video": {"url": public_url}}]
    media.video = video + b"changed"
    with pytest.raises(Hold, match="authenticated control asset"):
        publish_draft_reviewed(source, commit, "ep063", packet_dir, review, public_b64,
                               config, "test-token", "test-cloudinary", due, api, media)
    assert len(media.uploads) == 1 and len(api.created) == 1
    (packet_dir / "ep063.mp4").write_bytes(video + b"changed")
    with pytest.raises(Hold, match="private review video"):
        publish_draft_reviewed(source, commit, "ep063", packet_dir, review, public_b64,
                               config, "test-token", "test-cloudinary", due, api, media)
    assert len(api.created) == 1


def test_v2_packet_rejects_public_media_before_signing(fixture, tmp_path):
    source, commit, _, video, _, config = draft_fixture(fixture)
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    private_url = (private_prefix + "v123/" + PUBLISHER_PUBLIC_ID_PREFIX +
                   "drafts/ep063-" + digest(video) + ".mp4")
    packet_dir = tmp_path / "packet"
    intake_draft(source, commit, "ep063", config, "test-cloudinary", packet_dir,
                 lambda _: video, FakePrivateMedia(private_url, video))
    subject_path = packet_dir / "subject.json"
    hold_path = packet_dir / "control_hold.json"
    index_path = packet_dir / "packet.json"
    subject = json.loads(subject_path.read_text())
    hold = json.loads(hold_path.read_text())
    index = json.loads(index_path.read_text())
    public_url = config["publisher_media_url_prefix"] + subject["media_public_id"] + ".mp4"
    subject["media_url"] = hold["media_url"] = public_url
    index["subject"] = subject
    index["control_hold"] = hold
    subject_path.write_text(json.dumps(subject))
    hold_path.write_text(json.dumps(hold))
    index_path.write_text(json.dumps(index))
    with pytest.raises(Hold, match="public or unpinned"):
        validated_packet(source, commit, "ep063", config, packet_dir)
