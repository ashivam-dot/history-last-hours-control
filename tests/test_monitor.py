"""Post-due delivery checks against exact local receipts and simulated public readback."""

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from control import monitor
from control.publisher import PUBLISHER_PUBLIC_ID_PREFIX, copy_and_metadata
from control.release import Hold, digest, policy


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class FakeBuffer:
    def __init__(self, config: dict, post: dict):
        self.config = config
        self.detail = post
        self.listed = [{"id": post["id"], "status": "sent", "text": post["text"]}]
        self.calls = []

    def organization(self, organization: str) -> None:
        self.calls.append("organization")
        assert organization == self.config["buffer_organization_id"]

    def posts(self, organization: str, channel: str) -> list[dict]:
        self.calls.append("posts")
        assert organization == self.config["buffer_organization_id"]
        assert channel == self.config["youtube_channel_id"]
        return self.listed

    def sent_post(self, post_id: str) -> dict:
        self.calls.append("sent_post")
        assert post_id == self.detail["id"]
        return self.detail


class FakeState:
    def __init__(self, receipt: dict):
        self.item = receipt
        self.alerts = []
        self.resolved = []

    def receipts(self) -> list[dict]:
        return [self.item]

    def put(self, receipt: dict) -> None:
        self.item = receipt

    def alert_delivery(self, episode: str, reason: str) -> None:
        self.alerts.append((episode, reason))

    def resolve_delivery(self, episode: str) -> None:
        self.resolved.append(episode)


@pytest.fixture
def delivery(tmp_path):
    config = policy(Path(__file__).resolve().parents[1] / "policy.json")
    source = tmp_path / "producer"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    git(source, "remote", "add", "origin", config["source_remote"])
    episode = source / "content/episodes/ep063"
    (episode / "work").mkdir(parents=True)
    (episode / "short.yaml").write_text(
        "id: ep063\ntitle: The documented voyage\n"
        "description: One final voyage.\n"
        "sources:\n  - https://museum.example/voyage\n"
        "hashtags:\n  - '#history'\n",
        encoding="utf-8",
    )
    (episode / "work/manifest.json").write_text(
        json.dumps({"id": "ep063", "beats": [
            {"text": "The ship was lost.", "asset": {"source": "designed card"}}]}),
        encoding="utf-8",
    )
    git(source, "add", ".")
    git(source, "commit", "-m", "exact draft")
    commit = git(source, "rev-parse", "HEAD")
    video = b"the exact approved video bytes"
    media_hash = digest(video)
    copy_text, metadata = copy_and_metadata(source, commit, "ep063")
    due = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}ep063-{media_hash}"
    media_url = config["publisher_media_url_prefix"] + public_id + ".mp4"
    post_id = "bufferpost12345678"
    video_id = "AbCdEfGhI12"
    receipt = {
        "version": 1, "episode": "ep063", "source_commit": commit,
        "draft_sha256": "d" * 64, "media_sha256": media_hash,
        "phase": "scheduled", "due_at_utc": due.isoformat(),
        "publisher_receipt": {
            "episode": "ep063", "source_commit": commit,
            "media_sha256": media_hash,
            "control_media_public_id": public_id,
            "control_media_url": media_url,
            "youtube": {"id": post_id, "status": "scheduled", "due_at": due.isoformat()},
        },
    }
    post = {
        "id": post_id, "status": "sent", "dueAt": due.isoformat(),
        "sentAt": (due + timedelta(minutes=1)).isoformat(),
        "externalLink": f"https://www.youtube.com/watch?v={video_id}",
        "channelService": "youtube", "text": copy_text["youtube"],
        "error": None, "assets": [{"source": media_url}],
    }
    page = {
        "playability": "OK", "video_id": video_id,
        "channel_id": config["youtube_public_channel_id"],
        "external_channel_id": config["youtube_public_channel_id"],
        "title": metadata["youtube"]["title"],
        "description": copy_text["youtube"],
        "is_private": False, "is_unlisted": False,
    }
    return config, source, video, due, receipt, post, page


def check(delivery, *, receipt=None, post=None, page=None, video=None, listed=None):
    config, source, expected_video, _, expected_receipt, expected_post, expected_page = delivery
    receipt = copy.deepcopy(expected_receipt if receipt is None else receipt)
    post = copy.deepcopy(expected_post if post is None else post)
    page = copy.deepcopy(expected_page if page is None else page)
    api = FakeBuffer(config, post)
    if listed is not None:
        api.listed = listed

    def public_reader(video_id):
        return page

    def media_reader(url, media_config):
        assert url == expected_post["assets"][0]["source"]
        assert media_config == {"media_url_prefix": config["publisher_media_url_prefix"]}
        return expected_video if video is None else video

    proof = monitor.verify_due(receipt, source, config, api, public_reader, media_reader)
    return proof, api


def test_exact_sent_post_and_public_video_produce_bound_proof(delivery):
    config, _, video, _, receipt, post, page = delivery
    proof, api = check(delivery)
    assert api.calls == ["organization", "posts", "sent_post"]
    assert proof["buffer_post_id"] == post["id"]
    assert proof["youtube_video_id"] == page["video_id"]
    assert proof["youtube_channel_id"] == config["youtube_public_channel_id"]
    assert proof["control_media_sha256"] == receipt["media_sha256"] == digest(video)
    assert proof["youtube_description_sha256"] == digest(post["text"].encode())


@pytest.mark.parametrize("change", [
    "buffer_missing", "not_sent", "wrong_due", "wrong_text", "wrong_media_url",
    "wrong_media_bytes", "wrong_youtube_host", "wrong_youtube_id", "wrong_channel",
    "unlisted", "wrong_title", "wrong_description", "wrong_source_commit",
])
def test_delivery_tampering_remains_unverified(delivery, change):
    _, _, video, _, receipt, post, page = delivery
    receipt, post, page = copy.deepcopy((receipt, post, page))
    listed = None
    if change == "buffer_missing":
        listed = []
    elif change == "not_sent":
        post["status"] = "scheduled"
    elif change == "wrong_due":
        post["dueAt"] = "2026-10-05T17:00:00+00:00"
    elif change == "wrong_text":
        post["text"] += " Altered."
    elif change == "wrong_media_url":
        post["assets"][0]["source"] += "?other=1"
    elif change == "wrong_media_bytes":
        video += b" altered"
    elif change == "wrong_youtube_host":
        post["externalLink"] = "https://evil.example/watch?v=AbCdEfGhI12"
    elif change == "wrong_youtube_id":
        post["externalLink"] = "https://www.youtube.com/watch?v=ZZZZZZZZZZZ"
    elif change == "wrong_channel":
        page["channel_id"] = "UCotherchannel"
    elif change == "unlisted":
        page["is_unlisted"] = True
    elif change == "wrong_title":
        page["title"] = "A different voyage"
    elif change == "wrong_description":
        page["description"] = "Changed description"
    elif change == "wrong_source_commit":
        receipt["source_commit"] = "a" * 40
    with pytest.raises(Hold):
        check(delivery, receipt=receipt, post=post, page=page, video=video, listed=listed)


def test_monitor_waits_for_due_grace_then_retries_failure(delivery):
    config, source, video, due, receipt, post, page = delivery
    state = FakeState(receipt)
    api = FakeBuffer(config, post)
    media_reader = lambda *_: video
    result = monitor.monitor_due(state, source, config, due + timedelta(hours=1, minutes=59),
                                 api, lambda _: page, media_reader)
    assert result == {"checked": 0, "verified": 0, "failed": 0}
    assert api.calls == [] and state.item["phase"] == "scheduled"

    bad_page = {**page, "is_unlisted": True}
    result = monitor.monitor_due(state, source, config, due + timedelta(hours=2),
                                 api, lambda _: bad_page, media_reader)
    assert result == {"checked": 1, "verified": 0, "failed": 1}
    assert state.item["phase"] == "scheduled"
    assert state.alerts[0][0] == "ep063"

    result = monitor.monitor_due(state, source, config, due + timedelta(days=1),
                                 api, lambda _: page, media_reader)
    assert result == {"checked": 1, "verified": 1, "failed": 0}
    assert state.item["phase"] == "published"
    assert state.item["post_due_verification"]["youtube_video_id"] == page["video_id"]
    assert state.resolved == ["ep063"]
    assert monitor.monitor_due(state, source, config, due + timedelta(days=2),
                               api, lambda _: page, media_reader)["checked"] == 0


def test_issue_resolution_failure_keeps_scheduled_receipt_retryable(delivery):
    config, source, video, due, receipt, post, page = delivery

    class UnavailableIssues(FakeState):
        def resolve_delivery(self, episode):
            raise Hold("private issue API is unavailable")

    state = UnavailableIssues(receipt)
    result = monitor.monitor_due(state, source, config, due + timedelta(hours=2),
                                 FakeBuffer(config, post), lambda _: page, lambda *_: video)
    assert result == {"checked": 1, "verified": 0, "failed": 1}
    assert state.item["phase"] == "scheduled"
    assert state.alerts == [("ep063", "private issue API is unavailable")]


@pytest.mark.parametrize("link", [
    "http://www.youtube.com/watch?v=AbCdEfGhI12",
    "https://youtube.com.evil.example/watch?v=AbCdEfGhI12",
    "https://www.youtube.com/watch?v=AbCdEfGhI12&feature=share",
    "https://www.youtube.com/watch?v=AbCdEfGhI12#fragment",
    "https://www.youtube.com/redirect?q=https://evil.example",
    "https://www.youtube.com/watch?v=AbCdEfGhI12&v=ZZZZZZZZZZZ",
])
def test_youtube_link_must_identify_one_video_on_pinned_host(link):
    with pytest.raises(Hold):
        monitor.video_id_from_buffer(link)


def test_public_watch_parser_reads_bounded_anonymous_player(delivery):
    config, _, _, _, _, _, page = delivery
    player = {"playabilityStatus": {"status": "OK"},
              "videoDetails": {"videoId": page["video_id"],
                               "channelId": config["youtube_public_channel_id"],
                               "title": page["title"],
                               "shortDescription": page["description"],
                               "isPrivate": False},
              "microformat": {"playerMicroformatRenderer": {
                  "externalChannelId": config["youtube_public_channel_id"],
                  "isUnlisted": False}}}

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def iter_content(self, _):
            yield ("<html><script>var ytInitialPlayerResponse = " +
                   json.dumps(player) + ";</script></html>").encode()

    def get(url, **kwargs):
        assert url == f"https://www.youtube.com/watch?v={page['video_id']}"
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        return Response()

    assert monitor.read_public_video(page["video_id"], get) == page


@pytest.mark.parametrize("shorts_status,should_pass", [("OK", True), ("LOGIN_REQUIRED", False)])
def test_public_readback_uses_shorts_when_watch_requires_login(delivery, shorts_status, should_pass):
    config, _, _, _, _, _, page = delivery
    calls = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, player):
            self.player = player

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def iter_content(self, _):
            yield ("<script>var ytInitialPlayerResponse = " +
                   json.dumps(self.player) + ";</script>").encode()

    public_player = {
        "playabilityStatus": {"status": shorts_status},
        "videoDetails": {"videoId": page["video_id"],
                         "channelId": config["youtube_public_channel_id"],
                         "title": page["title"],
                         "shortDescription": page["description"],
                         "isPrivate": False},
        "microformat": {"playerMicroformatRenderer": {
            "externalChannelId": config["youtube_public_channel_id"],
            "isUnlisted": False}},
    }

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return Response({"playabilityStatus": {"status": "LOGIN_REQUIRED"}} if
                        "/watch?" in url else public_player)

    if should_pass:
        assert monitor.read_public_video(page["video_id"], get) == page
    else:
        with pytest.raises(Hold, match="both fixed routes"):
            monitor.read_public_video(page["video_id"], get)
    assert calls == [f"https://www.youtube.com/watch?v={page['video_id']}",
                     f"https://www.youtube.com/shorts/{page['video_id']}"]
