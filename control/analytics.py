"""Append-only private analytics for videos already verified after delivery.

This command only reads the anonymous public Shorts player. It never changes a
release receipt or calls Buffer, and its metrics are not used by release gates.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .orchestrate import GitHubState, STATE_BRANCH
from .release import COMMIT, EPISODE, SHA, Hold, policy, require, utc

MAX_VIDEOS_PER_RUN = 20
MAX_SHORTS_HTML = 5 * 1024 * 1024
MAX_SHORTS_SECONDS = 45
MAX_REPORTED_ERRORS = 20
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
POST_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
DECIMAL_COUNT = re.compile(r"[0-9]{1,20}\Z")
PLAYER_MARKER = re.compile(r"\bytInitialPlayerResponse\s*=\s*")


@dataclass(frozen=True)
class VerifiedVideo:
    episode: str
    source_commit: str
    media_sha256: str
    buffer_post_id: str
    buffer_sent_at: datetime
    post_due_verified_at: datetime
    youtube_video_id: str
    youtube_channel_id: str


def verified_video(receipt: dict, pinned_channel_id: str) -> VerifiedVideo:
    """Select only an exact video bound by the post-due delivery proof."""
    episode = receipt.get("episode")
    commit = receipt.get("source_commit")
    media_hash = receipt.get("media_sha256")
    require(type(receipt.get("version")) is int and receipt["version"] == 1 and
            receipt.get("phase") == "published" and
            isinstance(episode, str) and bool(EPISODE.fullmatch(episode)) and
            isinstance(commit, str) and bool(COMMIT.fullmatch(commit)) and
            isinstance(media_hash, str) and bool(SHA.fullmatch(media_hash)),
            "published receipt identity is invalid")
    publisher = receipt.get("publisher_receipt")
    proof = receipt.get("post_due_verification")
    require(isinstance(publisher, dict) and isinstance(proof, dict) and
            (publisher.get("episode"), publisher.get("source_commit"),
             publisher.get("media_sha256")) == (episode, commit, media_hash),
            "published receipt has no bound publisher and delivery proof")
    publisher_youtube = publisher.get("youtube")
    post_id = proof.get("buffer_post_id")
    video_id = proof.get("youtube_video_id")
    require(isinstance(publisher_youtube, dict) and
            isinstance(post_id, str) and bool(POST_ID.fullmatch(post_id)) and
            publisher_youtube.get("id") == post_id and
            isinstance(video_id, str) and bool(VIDEO_ID.fullmatch(video_id)) and
            type(proof.get("version")) is int and proof["version"] == 1 and
            proof.get("buffer_status") == "sent" and
            proof.get("control_media_sha256") == media_hash and
            proof.get("youtube_channel_id") == pinned_channel_id and
            proof.get("youtube_url") == f"https://www.youtube.com/watch?v={video_id}" and
            isinstance(proof.get("youtube_title"), str) and
            isinstance(proof.get("youtube_description_sha256"), str) and
            bool(SHA.fullmatch(proof["youtube_description_sha256"])),
            "published receipt delivery proof is not bound to its exact public video")
    sent_at = utc(proof.get("buffer_sent_at_utc"), "verified Buffer sent time")
    verified_at = utc(proof.get("verified_at_utc"), "post-due verification time")
    require(verified_at >= sent_at, "post-due verification predates Buffer delivery")
    return VerifiedVideo(episode, commit, media_hash, post_id, sent_at, verified_at,
                         video_id, pinned_channel_id)


def decimal_metric(renderer: dict, field: str) -> tuple[int | None, str]:
    """Keep missing and malformed public counts visibly distinct from zero."""
    if field not in renderer:
        return None, "missing"
    value = renderer[field]
    if not isinstance(value, str) or DECIMAL_COUNT.fullmatch(value) is None:
        return None, "malformed"
    return int(value), "present"


def read_shorts_metrics(video_id: str, pinned_channel_id: str,
                        getter=requests.get) -> dict:
    """Read one bounded anonymous Shorts player on a fixed YouTube route."""
    require(isinstance(video_id, str) and bool(VIDEO_ID.fullmatch(video_id)),
            "public YouTube video ID is malformed")
    url = f"https://www.youtube.com/shorts/{video_id}"
    started = time.monotonic()
    try:
        with getter(url, stream=True, allow_redirects=False, timeout=(10, 30),
                    headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}) as response:
            require(response.status_code == 200 and
                    response.headers.get("Content-Type", "").lower().startswith("text/html"),
                    "public Shorts player page is unavailable")
            chunks = []
            size = 0
            for chunk in response.iter_content(1 << 20):
                size += len(chunk)
                require(size <= MAX_SHORTS_HTML, "public Shorts player page is oversized")
                require(time.monotonic() - started <= MAX_SHORTS_SECONDS,
                        "public Shorts player page exceeded its read deadline")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise Hold("public Shorts player page could not be read") from exc
    html = b"".join(chunks).decode("utf-8", errors="replace")
    marker = PLAYER_MARKER.search(html)
    require(marker is not None, "public Shorts player could not be found")
    try:
        player, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    except ValueError as exc:
        raise Hold("public Shorts player response is malformed") from exc
    require(isinstance(player, dict), "public Shorts player response is incomplete")
    details = player.get("videoDetails")
    microformat = player.get("microformat")
    playability = player.get("playabilityStatus")
    renderer = (microformat.get("playerMicroformatRenderer")
                if isinstance(microformat, dict) else None)
    status = playability.get("status") if isinstance(playability, dict) else None
    require(status == "OK" and isinstance(details, dict) and isinstance(renderer, dict) and
            details.get("videoId") == video_id and
            details.get("channelId") == pinned_channel_id and
            renderer.get("externalChannelId") == pinned_channel_id and
            details.get("isPrivate") is False and renderer.get("isUnlisted") is False,
            f"public Shorts video, channel, or visibility is unverified (player status: {status or 'missing'})")
    views, views_status = decimal_metric(renderer, "viewCount")
    likes, likes_status = decimal_metric(renderer, "likeCount")
    return {"view_count": views, "view_count_status": views_status,
            "like_count": likes, "like_count_status": likes_status}


class AnalyticsState(GitHubState):
    """Create one UTC-dated snapshot per episode without updating old files."""

    def create_snapshot(self, snapshot: dict) -> bool:
        episode = snapshot.get("episode")
        require(isinstance(episode, str) and bool(EPISODE.fullmatch(episode)),
                "analytics snapshot episode is invalid")
        collected = utc(snapshot.get("collected_at_utc"), "analytics collection time")
        path = f"analytics/{episode}/{collected.date().isoformat()}.json"
        if self.file(path) is not None:
            return False
        body = {"message": f"Record History analytics for {episode} on {collected.date().isoformat()}",
                "content": base64.b64encode((json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode()).decode(),
                "branch": STATE_BRANCH}
        try:
            # No sha is supplied: the Contents API can create this path but cannot update it.
            self.request("PUT", f"/contents/{path}", allowed=(201,), json=body)
        except Hold:
            # A concurrent run may have created the same dated file first.
            if self.file(path) is not None:
                return False
            raise
        return True


def collect(state: AnalyticsState, config: dict, public_reader=read_shorts_metrics,
            clock=lambda: datetime.now(timezone.utc)) -> dict:
    """Inspect at most the newest 20 verified videos and report older skips."""
    published = [receipt for receipt in state.receipts() if receipt.get("phase") == "published"]
    videos = []
    errors = []
    invalid = 0
    for receipt in published:
        try:
            videos.append(verified_video(receipt, config["youtube_public_channel_id"]))
        except Hold as exc:
            invalid += 1
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.append({"episode": receipt.get("episode"), "reason": str(exc)})
    videos.sort(key=lambda video: (video.buffer_sent_at, video.episode), reverse=True)
    selected = videos[:MAX_VIDEOS_PER_RUN]
    result = {"published_receipts": len(published), "eligible": len(videos),
              "selected": len(selected), "older_skipped": max(0, len(videos) - len(selected)),
              "invalid_published": invalid, "created": 0, "existing": 0,
              "failed": 0, "errors": errors}
    for video in selected:
        try:
            metrics = public_reader(video.youtube_video_id, video.youtube_channel_id)
            require(isinstance(metrics, dict) and
                    metrics.get("view_count_status") in ("present", "missing", "malformed") and
                    metrics.get("like_count_status") in ("present", "missing", "malformed") and
                    (type(metrics.get("view_count")) is int if
                     metrics["view_count_status"] == "present" else
                     metrics.get("view_count") is None) and
                    (type(metrics.get("like_count")) is int if
                     metrics["like_count_status"] == "present" else
                     metrics.get("like_count") is None),
                    "public Shorts metric result is invalid")
            collected = clock()
            require(isinstance(collected, datetime) and collected.tzinfo is not None and
                    collected.utcoffset() == timedelta(0),
                    "analytics collection clock must use UTC")
            collected = collected.astimezone(timezone.utc)
            age_seconds = int((collected - video.buffer_sent_at).total_seconds())
            require(age_seconds >= 0, "verified video has not reached its Buffer sent time")
            snapshot = {
                "version": 1, "episode": video.episode,
                "source_commit": video.source_commit, "media_sha256": video.media_sha256,
                "buffer_post_id": video.buffer_post_id,
                "buffer_sent_at_utc": video.buffer_sent_at.isoformat(),
                "post_due_verified_at_utc": video.post_due_verified_at.isoformat(),
                "youtube_video_id": video.youtube_video_id,
                "youtube_channel_id": video.youtube_channel_id,
                "youtube_shorts_url": f"https://www.youtube.com/shorts/{video.youtube_video_id}",
                "metric_source": "playerMicroformatRenderer",
                "collected_at_utc": collected.isoformat(),
                "video_age_seconds": age_seconds,
                **metrics,
            }
            if state.create_snapshot(snapshot):
                result["created"] += 1
            else:
                result["existing"] += 1
        except Hold as exc:
            result["failed"] += 1
            if len(result["errors"]) < MAX_REPORTED_ERRORS:
                result["errors"].append({"episode": video.episode, "reason": str(exc)})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read public History Shorts into private dated analytics")
    parser.add_argument("--policy", type=Path, default=Path(__file__).resolve().parents[1] / "policy.json")
    args = parser.parse_args(argv)
    try:
        config = policy(args.policy)
        state = AnalyticsState(os.environ.get("GITHUB_TOKEN", ""),
                               os.environ.get("GITHUB_REPOSITORY", ""),
                               os.environ.get("GITHUB_SHA", ""))
        result = collect(state, config)
        print(json.dumps(result, sort_keys=True))
        return 2 if result["invalid_published"] or result["failed"] else 0
    except (Hold, OSError) as exc:
        print(f"Analytics hold: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
