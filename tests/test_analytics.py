"""Analytics stays bound to published proof and creates only dated private files."""

import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from control import analytics
from control.release import Hold


CHANNEL = "UC6e6OB3iw3yp8JnnBYxLItA"
COLLECTED = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)


def published_receipt(number: int, sent_at: datetime) -> dict:
    episode = f"ep{number:03d}"
    video_id = f"A{number:010d}"
    post_id = f"bufferpost{number:06d}"
    return {
        "version": 1, "episode": episode, "source_commit": "a" * 40,
        "draft_sha256": "c" * 64, "media_sha256": "b" * 64,
        "phase": "published",
        "publisher_receipt": {
            "episode": episode, "source_commit": "a" * 40,
            "media_sha256": "b" * 64,
            "youtube": {"id": post_id},
        },
        "post_due_verification": {
            "version": 1, "buffer_post_id": post_id, "buffer_status": "sent",
            "buffer_sent_at_utc": sent_at.isoformat(),
            "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "youtube_channel_id": CHANNEL,
            "youtube_title": "Exact verified video",
            "youtube_description_sha256": "d" * 64,
            "control_media_sha256": "b" * 64,
            "verified_at_utc": (sent_at + timedelta(hours=2)).isoformat(),
        },
    }


class FakeState:
    def __init__(self, receipts):
        self.items = receipts
        self.snapshots = {}

    def receipts(self):
        return self.items

    def create_snapshot(self, snapshot):
        key = (snapshot["episode"], snapshot["collected_at_utc"][:10])
        if key in self.snapshots:
            return False
        self.snapshots[key] = snapshot
        return True


def player(video_id="GeYxkROXScI", **renderer_changes):
    renderer = {"externalChannelId": CHANNEL, "isUnlisted": False,
                "viewCount": "8", "likeCount": "0"}
    renderer.update(renderer_changes)
    return {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": video_id, "channelId": CHANNEL,
                         "isPrivate": False},
        "microformat": {"playerMicroformatRenderer": renderer},
    }


def response_for(value, *, status_code=200):
    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __init__(self):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def iter_content(self, _):
            yield ("<script>var ytInitialPlayerResponse = " +
                   json.dumps(value) + ";</script>").encode()

    return Response()


def test_shorts_player_reads_decimal_counts_and_keeps_missing_explicit():
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response_for(player())

    counts = analytics.read_shorts_metrics("GeYxkROXScI", CHANNEL, get)
    assert counts == {"view_count": 8, "view_count_status": "present",
                      "like_count": 0, "like_count_status": "present"}
    assert calls[0][0] == "https://www.youtube.com/shorts/GeYxkROXScI"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True

    missing = player()
    del missing["microformat"]["playerMicroformatRenderer"]["viewCount"]
    missing["microformat"]["playerMicroformatRenderer"]["likeCount"] = "1,000"
    assert analytics.read_shorts_metrics(
        "GeYxkROXScI", CHANNEL, lambda *_args, **_kwargs: response_for(missing)) == {
            "view_count": None, "view_count_status": "missing",
            "like_count": None, "like_count_status": "malformed"}


@pytest.mark.parametrize("change", [
    "wrong_id", "wrong_channel", "wrong_external_channel", "private", "unlisted", "login_required",
])
def test_shorts_player_requires_exact_public_video_and_channel(change):
    value = player()
    if change == "wrong_id":
        value["videoDetails"]["videoId"] = "ZZZZZZZZZZZ"
    elif change == "wrong_channel":
        value["videoDetails"]["channelId"] = "UCotherchannel"
    elif change == "wrong_external_channel":
        value["microformat"]["playerMicroformatRenderer"]["externalChannelId"] = "UCotherchannel"
    elif change == "private":
        value["videoDetails"]["isPrivate"] = True
    elif change == "unlisted":
        value["microformat"]["playerMicroformatRenderer"]["isUnlisted"] = True
    else:
        value["playabilityStatus"]["status"] = "LOGIN_REQUIRED"
    with pytest.raises(Hold, match="unverified"):
        analytics.read_shorts_metrics(
            "GeYxkROXScI", CHANNEL, lambda *_args, **_kwargs: response_for(value))


def test_shorts_redirect_is_rejected():
    with pytest.raises(Hold, match="unavailable"):
        analytics.read_shorts_metrics(
            "GeYxkROXScI", CHANNEL,
            lambda *_args, **_kwargs: response_for(player(), status_code=302))


def test_shorts_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(analytics, "MAX_SHORTS_HTML", 8)
    with pytest.raises(Hold, match="oversized"):
        analytics.read_shorts_metrics(
            "GeYxkROXScI", CHANNEL,
            lambda *_args, **_kwargs: response_for(player()))


def test_only_newest_bound_published_receipts_are_inspected(monkeypatch):
    monkeypatch.setattr(analytics, "MAX_VIDEOS_PER_RUN", 2)
    sent = datetime(2026, 10, 4, 17, tzinfo=timezone.utc)
    oldest = published_receipt(63, sent)
    middle = published_receipt(64, sent + timedelta(minutes=1))
    newest = published_receipt(65, sent + timedelta(minutes=2))
    unbound = published_receipt(66, sent + timedelta(minutes=3))
    unbound["post_due_verification"]["youtube_channel_id"] = "UCotherchannel"
    scheduled = published_receipt(67, sent + timedelta(minutes=4))
    scheduled["phase"] = "scheduled"
    state = FakeState([oldest, scheduled, newest, unbound, middle])
    seen = []

    def read(video_id, channel_id):
        seen.append((video_id, channel_id))
        return {"view_count": 8, "view_count_status": "present",
                "like_count": None, "like_count_status": "missing"}

    result = analytics.collect(state, {"youtube_public_channel_id": CHANNEL}, read,
                               lambda: COLLECTED)
    assert result == {"published_receipts": 4, "eligible": 3, "selected": 2,
                      "older_skipped": 1, "invalid_published": 1,
                      "created": 2, "existing": 0, "failed": 0,
                      "errors": [{"episode": "ep066", "reason":
                                  "published receipt delivery proof is not bound to its exact public video"}]}
    assert seen == [(newest["post_due_verification"]["youtube_video_id"], CHANNEL),
                    (middle["post_due_verification"]["youtube_video_id"], CHANNEL)]
    snapshot = state.snapshots[("ep065", "2026-10-05")]
    assert snapshot["buffer_sent_at_utc"] == newest["post_due_verification"]["buffer_sent_at_utc"]
    assert snapshot["collected_at_utc"] == COLLECTED.isoformat()
    assert snapshot["video_age_seconds"] == int((COLLECTED - sent - timedelta(minutes=2)).total_seconds())
    assert snapshot["like_count"] is None and snapshot["like_count_status"] == "missing"
    again = analytics.collect(state, {"youtube_public_channel_id": CHANNEL}, read,
                              lambda: COLLECTED)
    assert again["created"] == 0 and again["existing"] == 2


def test_no_published_control_receipt_makes_no_public_or_private_calls():
    held = published_receipt(64, datetime(2026, 10, 4, 17, tzinfo=timezone.utc))
    held["phase"] = "held"
    held.pop("post_due_verification")
    state = FakeState([held])

    def unexpected_reader(*_):
        raise AssertionError("no public read should occur")

    result = analytics.collect(state, {"youtube_public_channel_id": CHANNEL},
                               unexpected_reader, lambda: COLLECTED)
    assert result == {"published_receipts": 0, "eligible": 0, "selected": 0,
                      "older_skipped": 0, "invalid_published": 0,
                      "created": 0, "existing": 0, "failed": 0, "errors": []}
    assert state.snapshots == {}


def test_private_contents_write_has_create_only_semantics():
    state = object.__new__(analytics.AnalyticsState)
    files = {}
    requests = []

    def file(path):
        return files.get(path)

    def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        assert kwargs["allowed"] == (201,)
        body = kwargs["json"]
        assert "sha" not in body and body["branch"] == "unattended-state"
        files[path.removeprefix("/contents/")] = {"sha": "a" * 40}
        return {"content": {"sha": "a" * 40}}

    state.file = file
    state.request = request
    snapshot = {"episode": "ep063", "collected_at_utc": COLLECTED.isoformat(),
                "view_count": 8}
    assert state.create_snapshot(copy.deepcopy(snapshot)) is True
    assert state.create_snapshot(copy.deepcopy(snapshot)) is False
    assert len(requests) == 1
    assert requests[0][1] == "/contents/analytics/ep063/2026-10-05.json"
    decoded = json.loads(base64.b64decode(requests[0][2]["json"]["content"]))
    assert decoded == snapshot


def test_concurrent_create_never_rewrites_an_existing_snapshot():
    state = object.__new__(analytics.AnalyticsState)
    files = {}
    state.file = lambda path: files.get(path)

    def raced_request(method, path, **kwargs):
        assert method == "PUT" and "sha" not in kwargs["json"]
        files[path.removeprefix("/contents/")] = {"sha": "a" * 40}
        raise Hold("private GitHub state API returned 422")

    state.request = raced_request
    assert state.create_snapshot({"episode": "ep063",
                                  "collected_at_utc": COLLECTED.isoformat()}) is False
