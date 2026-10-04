import base64
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control.release import (Hold, FILES, candidate, digest, policy, publisher_preflight,
                             sign, verify)
from control.publisher import (CloudinaryClient, PUBLISHER_PUBLIC_ID_PREFIX, _create_or_reconcile,
                               publish_draft_reviewed, publish_reviewed)
from control.intake import committed_draft, intake_draft, read_private_video, validated_packet
from control import orchestrate as orch
from control.qa import (MAX_PAGE_BYTES, GeminiQA, GeminiTransientHold, OpenAIQA,
                        fetch_public_document, inspect_media,
                        qa_draft, verified_qa_report,
                        verify_sources, verify_visual_rights)


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


def test_hosted_candidate_accepts_producer_ist_start(fixture):
    source, _, episode, video, config = fixture
    topic_path = episode / "topic.json"
    topic_path.write_text(json.dumps({"started_at": "2026-10-04T13:37:50+05:30"}))
    git(source, "add", "-A")
    git(source, "commit", "-m", "IST source time")
    commit = git(source, "rev-parse", "HEAD")
    assert candidate(source, commit, "ep063", video, config)["id"] == "ep063"


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
    private_url = f"https://res.cloudinary.com/mw0oh0v8/video/authenticated/{public_id}.mp4"
    signed_url = (f"https://res.cloudinary.com/mw0oh0v8/video/authenticated/"
                  f"s--signed--/v123/{public_id}.mp4")
    calls = []

    class UploadResponse:
        status_code = 200

        def json(self):
            return {"public_id": public_id, "resource_type": "video", "type": "authenticated",
                    "format": "mp4", "bytes": len(video), "asset_id": "asset-12345678",
                    "secure_url": signed_url}

    class AnonymousResponse:
        status_code = 401

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

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
    anonymous_calls = []

    def deny_unsigned(url, **kwargs):
        anonymous_calls.append((url, kwargs))
        return AnonymousResponse()

    monkeypatch.setattr("control.publisher.requests.get", deny_unsigned)
    assert client.upload_private_draft(video, "ep063", media_hash) == (
        private_url, public_id, "asset-12345678")
    assert anonymous_calls[0][0] == private_url
    assert anonymous_calls[0][1]["stream"] is True
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
                    "format": "mp4", "bytes": len(video), "asset_id": "asset-12345678",
                    "secure_url": prefix + public_id + ".mp4"}

    monkeypatch.setattr("control.publisher.requests.post", lambda *args, **kwargs: Response())
    with pytest.raises(Hold, match="identity, type, or size"):
        client.upload_private_draft(video, "ep063", media_hash)


@pytest.mark.parametrize("response_mode", ["conflict", "uncertain"])
def test_private_draft_upload_reconciles_only_identical_authenticated_asset(monkeypatch, response_mode):
    prefix = "https://res.cloudinary.com/mw0oh0v8/video/upload/"
    client = CloudinaryClient("cloudinary://only-key:only-secret@mw0oh0v8", prefix)
    video = b"\x00\x00\x00\x18ftypisom" + b"private draft"
    media_hash = digest(video)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/ep063-{media_hash}"
    private_url = prefix.replace("/video/upload/", "/video/authenticated/") + public_id + ".mp4"

    class Response:
        status_code = 409

    class Lookup:
        status_code = 200

        def json(self):
            return {"public_id": public_id, "resource_type": "video", "type": "authenticated",
                    "format": "mp4", "bytes": len(video), "asset_id": "asset-12345678",
                    "secure_url": private_url}

    class AnonymousResponse:
        status_code = 401

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    lookups = []

    def get(url, **kwargs):
        lookups.append((url, kwargs))
        return Lookup() if url.startswith("https://api.cloudinary.com/") else AnonymousResponse()

    def post(*args, **kwargs):
        if response_mode == "uncertain":
            from requests import ConnectionError
            raise ConnectionError("upload response lost")
        return Response()

    monkeypatch.setattr("control.publisher.requests.post", post)
    monkeypatch.setattr("control.publisher.requests.get", get)
    monkeypatch.setattr(client, "download_private_asset", lambda asset_id: video)
    assert client.upload_private_draft(video, "ep063", media_hash) == (
        private_url, public_id, "asset-12345678")
    assert lookups[0][0].endswith("/resources/video/authenticated/" + public_id)
    assert lookups[0][1]["auth"] == ("only-key", "only-secret")
    monkeypatch.setattr(client, "download_private_asset", lambda asset_id: video + b"changed")
    with pytest.raises(Hold, match="existing authenticated draft differs"):
        client.upload_private_draft(video, "ep063", media_hash)


@pytest.mark.parametrize("status", [200, 500])
def test_private_draft_unsigned_url_must_be_inaccessible(monkeypatch, status):
    class Response:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("control.publisher.requests.get", lambda *args, **kwargs: Response())
    with pytest.raises(Hold, match="publicly readable or unavailable"):
        CloudinaryClient._require_private_not_public(
            "https://res.cloudinary.com/mw0oh0v8/video/authenticated/draft.mp4")


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
        intake_draft(source, commit, "ep063", {**config, "intake_enabled": False},
                     "", tmp_path / "packet",
                     lambda _: pytest.fail("Modal read under disabled policy"))


def test_private_draft_reader_checks_modal_workspace_before_volume(monkeypatch):
    import modal

    monkeypatch.setenv("MODAL_TOKEN_ID", "test-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-secret")

    class Workspace:
        name = "wrong-workspace"

        def hydrate(self):
            return self

    monkeypatch.setattr(modal.Workspace, "from_context", lambda: Workspace())
    monkeypatch.setattr(modal.Volume, "from_name", lambda *args, **kwargs:
                        pytest.fail("outbox read before workspace check"))
    with pytest.raises(Hold, match="different workspace"):
        read_private_video("drafts/ep063-" + "a" * 64 + ".mp4")


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


@pytest.mark.parametrize("started_at,accepted", [
    ("2026-10-04T13:37:50+05:30", True),
    ("2026-10-04T05:00:00+00:00", True),
    ("2026-10-04T10:29:59+05:30", False),
    ("2026-10-04T13:37:50", False),
])
def test_draft_source_start_accepts_aware_ist_and_enforces_utc_cutoff(fixture, started_at, accepted):
    source, _, episode, _, _, config = draft_fixture(fixture)
    topic_path = episode / "topic.json"
    topic = json.loads(topic_path.read_text())
    topic["started_at"] = started_at
    topic_path.write_text(json.dumps(topic))
    git(source, "add", "-A")
    git(source, "commit", "--allow-empty", "-m", "source time")
    commit = git(source, "rev-parse", "HEAD")
    if accepted:
        assert committed_draft(source, commit, "ep063", config)["draft"]["id"] == "ep063"
    else:
        with pytest.raises(Hold, match="draft topic start|predates control cutoff"):
            committed_draft(source, commit, "ep063", config)


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
    for failure in ("wrong_org", "wrong_channel", "full_queue", "occupied_slot",
                    "existing_wrong_media"):
        before_copy = FakePrivateMedia(private_url, video)
        before_copy.public_url = public_url
        bad_api = FakeBuffer(public_url)
        if failure == "wrong_org":
            def missing_org(_):
                raise Hold("pinned organization unavailable")

            bad_api.organization = missing_org
        elif failure == "wrong_channel":
            bad_api.services = ("instagram",)
        elif failure == "full_queue":
            bad_api.by_channel["history-yt"] = [
                {"id": str(index), "status": "scheduled", "text": f"unrelated-{index}",
                 "dueAt": "2026-10-07T00:00:00+00:00"} for index in range(10)]
        elif failure == "occupied_slot":
            bad_api.by_channel["history-yt"] = [
                {"id": "occupied", "status": "scheduled", "text": "unrelated", "dueAt": due}]
        else:
            copy_text = __import__("control.publisher", fromlist=["copy_and_metadata"]).copy_and_metadata(
                source, commit, "ep063")[0]["youtube"]
            bad_api.by_channel["history-yt"] = [
                {"id": "old", "status": "scheduled", "text": copy_text, "dueAt": due}]
            bad_api.by_id["old"] = {**bad_api.by_channel["history-yt"][0],
                                    "assets": [{"source": "https://wrong.example/video.mp4"}]}
        with pytest.raises(Hold):
            publish_draft_reviewed(source, commit, "ep063", packet_dir, review, public_b64,
                                   config, "test-token", "test-cloudinary", due, bad_api, before_copy)
        assert before_copy.uploads == [] and bad_api.created == []
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


@pytest.mark.parametrize("flip_at", ["before_public_copy", "before_buffer_create"])
def test_live_policy_flip_blocks_draft_mutation_after_preflight(fixture, tmp_path, monkeypatch, flip_at):
    source, commit, _, video, _, config = draft_fixture(fixture)
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    private_url = private_prefix + PUBLISHER_PUBLIC_ID_PREFIX + "drafts/ep063-" + digest(video) + ".mp4"
    media = FakePrivateMedia(private_url, video)
    packet_dir = tmp_path / "packet"
    intake_draft(source, commit, "ep063", config, "test-cloudinary", packet_dir,
                 lambda _: video, media)
    subject, _ = validated_packet(source, commit, "ep063", config, packet_dir)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "publishing_enabled": True,
              "reviewer_key_sha256": digest(public_raw), "buffer_organization_id": "history-org",
              "youtube_channel_id": "history-yt"}
    approval = {"subject": subject, "decision": "approved",
                "checks": {name: True for name in ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    public_url = config["publisher_media_url_prefix"] + PUBLISHER_PUBLIC_ID_PREFIX + "ep063-" + digest(video) + ".mp4"
    media.public_url = public_url
    api = FakeBuffer(public_url)
    live = {"publishing_enabled": True}
    post_reads = []
    original_posts = api.posts

    def posts(organization, channel):
        post_reads.append(channel)
        if len(post_reads) == (1 if flip_at == "before_public_copy" else 2):
            live["publishing_enabled"] = False
        return original_posts(organization, channel)

    def check_live_policy(self, local, operation):
        assert operation == "publish" and local is config
        if not live["publishing_enabled"]:
            raise Hold("main publishing switch was turned off")

    api.posts = posts
    monkeypatch.setattr(orch.GitHubState, "require_live_policy", check_live_policy)
    monkeypatch.setattr(orch, "current_draft", lambda *_args: None)
    monkeypatch.setattr("control.publisher.fetch_video", lambda url, _: video)
    monkeypatch.setenv("HISTORY_UNATTENDED_LIVE_POLICY", "1")
    monkeypatch.setenv("HISTORY_CONTROL_POLICY_TOKEN", "test-control-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/control")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    with pytest.raises(Hold, match="main publishing switch was turned off"):
        publish_draft_reviewed(source, commit, "ep063", packet_dir, review,
                               base64.b64encode(public_raw).decode(), config,
                               "test-token", "test-cloudinary", due, api, media)
    assert not api.created
    assert len(media.uploads) == (1 if flip_at == "before_public_copy" else 2)


@pytest.mark.parametrize("fail_on", [1, 2, 3])
def test_unattended_publish_rechecks_producer_main_at_each_mutation_boundary(
        fixture, tmp_path, monkeypatch, fail_on):
    source, commit, _, video, _, config = draft_fixture(fixture)
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    private_url = private_prefix + PUBLISHER_PUBLIC_ID_PREFIX + "drafts/ep063-" + digest(video) + ".mp4"
    media = FakePrivateMedia(private_url, video)
    packet_dir = tmp_path / "packet"
    intake_draft(source, commit, "ep063", config, "test-cloudinary", packet_dir,
                 lambda _: video, media)
    subject, _ = validated_packet(source, commit, "ep063", config, packet_dir)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "signing_enabled": True, "publishing_enabled": True,
              "reviewer_key_sha256": digest(public_raw), "buffer_organization_id": "history-org",
              "youtube_channel_id": "history-yt"}
    approval = {"subject": subject, "decision": "approved",
                "checks": {name: True for name in ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    review = sign(approval, subject, base64.b64encode(private_raw).decode(), config)
    public_url = config["publisher_media_url_prefix"] + PUBLISHER_PUBLIC_ID_PREFIX + "ep063-" + digest(video) + ".mp4"
    media.public_url = public_url
    api = FakeBuffer(public_url)
    checks = []

    def current(repo, episode, source_commit, policy):
        assert (repo, episode, source_commit, policy) == (source, "ep063", commit, config)
        checks.append(fail_on)
        if len(checks) == fail_on:
            raise Hold("producer main withdrew the exact draft")

    monkeypatch.setattr(orch, "current_draft", current)
    monkeypatch.setattr(orch.GitHubState, "require_live_policy", lambda *_args: None)
    monkeypatch.setattr("control.publisher.fetch_video", lambda url, _: video)
    monkeypatch.setenv("HISTORY_UNATTENDED_LIVE_POLICY", "1")
    monkeypatch.setenv("HISTORY_CONTROL_POLICY_TOKEN", "test-control-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/control")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    uploads_before_release = len(media.uploads)
    with pytest.raises(Hold, match="producer main withdrew"):
        publish_draft_reviewed(source, commit, "ep063", packet_dir, review,
                               base64.b64encode(public_raw).decode(), config,
                               "test-token", "test-cloudinary", due, api, media)
    assert len(checks) == fail_on
    assert len(media.uploads) - uploads_before_release == (0 if fail_on == 1 else 1)
    assert api.created == []


def test_live_policy_is_checked_after_reconcile_and_before_buffer_create():
    api = FakeBuffer("https://example.test/exact.mp4")
    due = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)

    def switched_off():
        raise Hold("main publishing switch was turned off")

    with pytest.raises(Hold, match="switch was turned off"):
        _create_or_reconcile(api, "history-yt", "exact text", {}, api.media_url, due, [],
                             before_create=switched_off)
    assert api.created == []


def test_live_policy_is_checked_before_ed25519_signature(fixture, monkeypatch):
    _, _, _, subject, config, _, _ = approved_publisher(fixture)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    config = {**config, "intake_enabled": True, "reviewer_key_sha256": digest(public_raw)}
    approval = {"subject": subject, "decision": "approved",
                "checks": {name: True for name in ("claim_sources", "visual_identity_rights", "full_video_audio")},
                "reviewed_at_utc": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setenv("HISTORY_UNATTENDED_LIVE_POLICY", "1")
    monkeypatch.setenv("HISTORY_CONTROL_POLICY_TOKEN", "test-control-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/control")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(orch.GitHubState, "require_live_policy",
                        lambda self, config, operation: (_ for _ in ()).throw(Hold("main signing switch off")))
    with pytest.raises(Hold, match="main signing switch off"):
        sign(approval, subject, base64.b64encode(private_raw).decode(), config)


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


def test_automated_qa_approves_only_exact_live_sources_and_complete_media(fixture, tmp_path,
                                                                            monkeypatch):
    source, _, episode, video, _, config = draft_fixture(fixture)
    research_path = episode / "research.json"
    research = json.loads(research_path.read_text())
    research["claims"][0]["claim"] = "The ship was lost during its voyage."
    research_path.write_text(json.dumps(research))
    git(source, "add", "-A")
    git(source, "commit", "-m", "reviewable claim text")
    commit = git(source, "rev-parse", "HEAD")
    private_prefix = config["publisher_media_url_prefix"].replace("/video/upload/", "/video/authenticated/")
    private_url = (private_prefix + PUBLISHER_PUBLIC_ID_PREFIX +
                   "drafts/ep063-" + digest(video) + ".mp4")
    packet_dir = tmp_path / "packet"
    intake_draft(source, commit, "ep063", config, "test-cloudinary", packet_dir,
                 lambda _: video, FakePrivateMedia(private_url, video))
    documents = {
        "https://museum.example/story":
            "Museum account. The ship was lost during the long voyage. " + "Context " * 30,
        "https://archive.example/story":
            "Archive account. The ship was lost while crossing the ocean. " + "Context " * 30,
    }

    def fetcher(url):
        return documents[url]

    class FakeQA:
        provider = "gemini"
        model = "test-vision"
        transcript = "The ship was lost."
        claim_supported = True

        def transcribe(self, audio):
            return self.transcript

        def assess(self, claims, visuals, script, transcript, media):
            assert len(claims) == len(visuals) == 1
            assert len(claims[0]["sources"]) == 2
            assert media["duration_seconds"] == 20
            return {"claims": [{"index": 1, "supported": self.claim_supported, "reason": "two sources agree"}],
                    "visuals": [{"beat": 1, "identity_ok": True, "rights_ok": True,
                                 "quality_ok": True, "reason": "original card matches"}],
                    "audio_matches_script": True, "audio_reason": "narration matches",
                    "full_video_quality_ok": True, "video_reason": "all frames pass"}

    def inspector(video_bytes, folder):
        assert video_bytes == video
        return {"duration_seconds": 20, "width": 1080, "height": 1920,
                "integrated_lufs": -14, "true_peak_dbfs": -2,
                "audio_path": folder / "review.mp3", "frames": []}

    api = FakeQA()
    config = {**config, "qa_provider": api.provider, "qa_model": api.model}
    report_path, approval_path = tmp_path / "report.json", tmp_path / "approval.json"
    report = qa_draft(source, commit, "ep063", packet_dir, config, report_path, approval_path,
                      api, fetcher, inspector)
    assert report["status"] == "approved"
    approval = json.loads(approval_path.read_text())
    assert approval["subject"]["source_commit"] == commit
    assert set(approval["checks"].values()) == {True}
    verified_qa_report(report, approval, approval["subject"], config, packet_dir)
    with pytest.raises(Hold, match="does not bind"):
        verified_qa_report({**report, "qa_model": "other-model"}, approval,
                           approval["subject"], config, packet_dir)

    class TransientQA(FakeQA):
        def assess(self, claims, visuals, script, transcript, media):
            raise GeminiTransientHold(
                "independent Gemini QA quota or capacity exhausted after bounded retries")

    monkeypatch.setenv("GITHUB_RUN_ID", "300")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    with pytest.raises(GeminiTransientHold):
        qa_draft(source, commit, "ep063", packet_dir, config,
                 tmp_path / "transient-report.json", tmp_path / "transient-approval.json",
                 TransientQA(), fetcher, inspector)
    transient = json.loads((tmp_path / "transient-report.json").read_text())
    assert transient["failure_code"] == "gemini_transient"
    assert transient["actions_run_key"] == "300/1"
    assert transient["draft_sha256"] == approval["subject"]["draft_sha256"]
    assert transient["media_sha256"] == approval["subject"]["media_sha256"]
    assert not (tmp_path / "transient-approval.json").exists()

    documents["https://archive.example/story"] = "Changed archive page. " + "Context " * 30
    with pytest.raises(Hold, match="two independent live sites"):
        qa_draft(source, commit, "ep063", packet_dir, config,
                 tmp_path / "held-report.json", tmp_path / "held-approval.json",
                 api, fetcher, inspector)
    held = json.loads((tmp_path / "held-report.json").read_text())
    assert held["status"] == "held" and held["stage"] == "independent_sources"
    assert not (tmp_path / "held-approval.json").exists()
    documents["https://archive.example/story"] = (
        "Archive account. The ship was lost while crossing the ocean. " + "Context " * 30)
    api.claim_supported = False
    with pytest.raises(Hold, match="unsupported claim"):
        qa_draft(source, commit, "ep063", packet_dir, config,
                 tmp_path / "model-held-report.json", tmp_path / "model-held-approval.json",
                 api, fetcher, inspector)
    assert not (tmp_path / "model-held-approval.json").exists()
    assert json.loads((tmp_path / "model-held-report.json").read_text())["verdict"]["claims"][0]["supported"] is False


def test_independent_rights_page_must_show_pinned_license():
    manifest = {"beats": [{"text": "The ship was lost.", "asset": {
        "source": "Wikimedia Commons", "license": "CC BY 4.0", "credit": "Archivist",
        "url": "https://commons.wikimedia.org/wiki/File:Ship.jpg", "title": "Ship"}}]}
    with pytest.raises(Hold, match="license is not visible"):
        verify_visual_rights(manifest, lambda _: "A ship image and author description")
    checked = verify_visual_rights(manifest, lambda _: "A ship image. License CC BY 4.0. Archivist.")
    assert checked[0]["rights_page_excerpt"].startswith("A ship image")


def test_independent_claim_quotes_require_distinct_live_sites():
    research = {"sources": [{"label": "A", "url": "https://one.example.org/story"},
                            {"label": "B", "url": "https://two.example.org/story"}],
                "claims": [{"claim": "A vessel sank.", "evidence": [
                    {"source": "A", "quote": "The vessel sank at sea during the voyage."},
                    {"source": "B", "quote": "The vessel sank at sea on the voyage."}]}]}
    pages = {"https://one.example.org/story": "The vessel sank at sea during the voyage.",
             "https://two.example.org/story": "The vessel sank at sea on the voyage."}
    with pytest.raises(Hold, match="two independent live sites"):
        verify_sources(research, lambda url: pages[url])
    research["sources"][1]["url"] = "https://archive.example.net/story"
    pages["https://archive.example.net/story"] = pages["https://two.example.org/story"]
    assert len(verify_sources(research, lambda url: pages[url])[0]["sources"]) == 2


def test_independent_evidence_fetch_rejects_private_addresses_before_request(monkeypatch):
    monkeypatch.setattr("control.qa.urllib3.HTTPSConnectionPool", lambda *args, **kwargs:
                        pytest.fail("private network request attempted"))
    with pytest.raises(Hold, match="IP literal"):
        fetch_public_document("https://127.0.0.1/private")
    monkeypatch.setattr("control.qa.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("10.0.0.1", 443))])
    with pytest.raises(Hold, match="publicly routable"):
        fetch_public_document("https://archive.example.org/page")


def test_independent_evidence_https_socket_is_pinned_to_vetted_ip(monkeypatch):
    calls = []
    document = ("<html><body>Verified public history archive account with a long independent "
                "description of the voyage and a dated primary record. " * 4 + "</body></html>").encode()

    class Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def stream(self, size, decode_content):
            assert size == 1 << 16 and decode_content is True
            yield document

        def release_conn(self):
            calls.append("released")

    class Pool:
        def __init__(self, ip, **kwargs):
            calls.append((ip, kwargs))

        def urlopen(self, method, target, **kwargs):
            calls.append((method, target, kwargs))
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr("control.qa.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr("control.qa.urllib3.HTTPSConnectionPool", Pool)
    monkeypatch.setattr("control.qa.requests.get", lambda *args, **kwargs:
                        pytest.fail("unvetted DNS re-resolution attempted"))
    text = fetch_public_document("https://archive.example.org/history/voyage?q=ship")
    assert "Verified public history archive" in text
    assert calls[0][0] == "93.184.215.14"
    assert calls[0][1]["server_hostname"] == calls[0][1]["assert_hostname"] == "archive.example.org"
    assert calls[0][1]["cert_reqs"] == "CERT_REQUIRED"
    assert calls[1][0:2] == ("GET", "/history/voyage?q=ship")
    assert calls[1][2]["headers"]["Host"] == "archive.example.org"
    assert calls[1][2]["redirect"] is False
    assert calls[-2:] == ["released", "closed"]


def test_independent_evidence_does_not_follow_redirect_from_vetted_ip(monkeypatch):
    class Response:
        status = 302
        headers = {"Location": "https://127.0.0.1/private"}

        def release_conn(self):
            pass

    class Pool:
        def __init__(self, *args, **kwargs):
            pass

        def urlopen(self, method, target, **kwargs):
            assert kwargs["redirect"] is False
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("control.qa.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr("control.qa.urllib3.HTTPSConnectionPool", Pool)
    with pytest.raises(Hold, match="unavailable or redirected"):
        fetch_public_document("https://archive.example.org/page")


def test_independent_evidence_revalidates_one_archive_cdn_redirect(monkeypatch):
    hosts = []
    document = ("A verified original inquiry describes the bridge and its construction in "
                "detail for the independent record. " * 5).encode()

    class Response:
        def __init__(self, status, headers):
            self.status, self.headers = status, headers

        def stream(self, size, decode_content):
            yield document

        def release_conn(self):
            pass

    class Pool:
        def __init__(self, ip, **kwargs):
            hosts.append((ip, kwargs["server_hostname"]))

        def urlopen(self, method, target, **kwargs):
            assert kwargs["redirect"] is False
            if hosts[-1][1] == "archive.org":
                return Response(302, {"Location": "https://dn1.ca.archive.org/0/items/report/report_djvu.txt"})
            return Response(200, {"Content-Type": "text/plain"})

        def close(self):
            pass

    def addresses(host, port, **kwargs):
        return [(None, None, None, None, (("93.184.215.14" if host == "archive.org"
                                         else "93.184.215.15"), 443))]

    monkeypatch.setattr("control.qa.socket.getaddrinfo", addresses)
    monkeypatch.setattr("control.qa.urllib3.HTTPSConnectionPool", Pool)
    text = fetch_public_document("https://archive.org/download/report/report_djvu.txt")
    assert "verified original inquiry" in text
    assert hosts == [("93.184.215.14", "archive.org"),
                     ("93.184.215.15", "dn1.ca.archive.org")]


def test_independent_evidence_caps_decoded_response_from_pinned_socket(monkeypatch):
    closed = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def stream(self, size, decode_content):
            yield b"x" * (MAX_PAGE_BYTES + 1)

        def release_conn(self):
            closed.append("released")

    class Pool:
        def __init__(self, *args, **kwargs):
            pass

        def urlopen(self, *args, **kwargs):
            return Response()

        def close(self):
            closed.append("closed")

    monkeypatch.setattr("control.qa.socket.getaddrinfo",
                        lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr("control.qa.urllib3.HTTPSConnectionPool", Pool)
    with pytest.raises(Hold, match="oversized"):
        fetch_public_document("https://archive.example.org/page")
    assert closed == ["released", "closed"]


def test_multimodal_review_sends_full_frame_set_with_strict_json(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"sample frame")
    sent = []
    verdict = {"claims": [], "visuals": [], "audio_matches_script": True,
               "audio_reason": "clear", "full_video_quality_ok": True, "video_reason": "clear"}

    class Response:
        status_code = 200

        def json(self):
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(verdict)}]}]}

    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()

    monkeypatch.setattr("control.qa.requests.post", post)
    api = OpenAIQA("test-key", "test-vision-model")
    result = api.assess([], [], "The ship was lost", "The ship was lost",
                        {"duration_seconds": 20, "audio_path": tmp_path / "audio.mp3", "frames": [frame]})
    assert result == verdict
    assert sent[0][0] == "https://api.openai.com/v1/responses"
    body = sent[0][1]["json"]
    assert body["store"] is False and body["text"]["format"]["strict"] is True
    assert sum(item["type"] == "input_image" for item in body["input"][1]["content"]) == 1


def test_gemini_qa_retries_quota_then_returns_schema_verdict_without_key_in_url(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"sample frame")
    calls = []
    delays = []
    verdict = {"claims": [], "visuals": [], "audio_matches_script": True,
               "audio_reason": "clear", "full_video_quality_ok": True, "video_reason": "clear"}

    class Response:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": json.dumps(verdict)}]}}]}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response((429, 503, 200)[len(calls) - 1])

    monkeypatch.setattr("control.qa.requests.post", post)
    api = GeminiQA("test-key", "test-vision-model", delays.append)
    result = api.assess([], [], "The ship was lost", "The ship was lost",
                        {"duration_seconds": 20, "audio_path": tmp_path / "audio.mp3", "frames": [frame]})
    assert result == verdict and delays == [5, 15]
    assert all("test-key" not in url for url, _ in calls)
    assert all(kwargs["headers"]["x-goog-api-key"] == "test-key" for _, kwargs in calls)
    body = calls[-1][1]["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"]["type"] == "object"
    assert sum("inlineData" in part for part in body["contents"][0]["parts"]) == 1


def test_gemini_quota_exhaustion_holds_after_bounded_retries(monkeypatch):
    calls = []
    delays = []

    class Response:
        status_code = 429

    def post(url, **kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr("control.qa.requests.post", post)
    api = GeminiQA("test-key", "test-vision-model", delays.append)
    with pytest.raises(GeminiTransientHold, match="quota or capacity exhausted"):
        api._call([{"text": "review"}])
    assert len(calls) == 4 and delays == [5, 15, 30]


def test_gemini_transcribes_full_mixed_audio_inline(monkeypatch, tmp_path):
    audio = tmp_path / "review.mp3"
    audio.write_bytes(b"bounded complete audio bytes")
    sent = []

    class Response:
        status_code = 200

        def json(self):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "The entire voyage ended when the ship was lost."}]}}]}

    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()

    monkeypatch.setattr("control.qa.requests.post", post)
    transcript = GeminiQA("test-key", "test-vision-model").transcribe(audio)
    assert "ship was lost" in transcript
    parts = sent[0][1]["json"]["contents"][0]["parts"]
    assert parts[1]["inlineData"]["mimeType"] == "audio/mpeg"
    assert base64.b64decode(parts[1]["inlineData"]["data"]) == audio.read_bytes()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg is unavailable")
def test_independent_media_probe_decodes_complete_synthetic_video_and_holds_bad_audio(tmp_path):
    video = tmp_path / "synthetic.mp4"
    made = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=360x640:r=12:d=18",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=18",
        "-c:v", "mpeg4", "-q:v", "8", "-c:a", "aac", "-shortest", str(video),
    ], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    (tmp_path / "inspect").mkdir()
    with pytest.raises(Hold, match="loudness or peak"):
        inspect_media(video.read_bytes(), tmp_path / "inspect")
