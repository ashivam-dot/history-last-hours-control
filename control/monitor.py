"""Verify scheduled Buffer posts reached the pinned public YouTube channel."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .orchestrate import GitHubState, _git, advance
from .publisher import BufferClient, PUBLISHER_PUBLIC_ID_PREFIX, copy_and_metadata
from .release import COMMIT, EPISODE, SHA, Hold, digest, fetch_video, policy, require, utc

DELIVERY_GRACE = timedelta(hours=2)
MAX_WATCH_HTML = 5 * 1024 * 1024
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
POST_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
PLAYER_MARKER = re.compile(r"\bytInitialPlayerResponse\s*=\s*")


def video_id_from_buffer(link: object) -> str:
    """Use an external link only to select an ID on a fixed YouTube host."""
    require(isinstance(link, str), "Buffer sent post has no YouTube link")
    parsed = urlparse(link)
    require(parsed.scheme == "https" and parsed.netloc in ("www.youtube.com", "youtube.com") and
            not parsed.fragment and not parsed.username and not parsed.password,
            "Buffer sent post has an unpinned YouTube link")
    if parsed.path == "/watch":
        values = parse_qs(parsed.query, keep_blank_values=True)
        require(set(values) == {"v"} and len(values["v"]) == 1,
                "Buffer YouTube watch link has unexpected parameters")
        video_id = values["v"][0]
    else:
        require(not parsed.query and bool(re.fullmatch(r"/shorts/[A-Za-z0-9_-]{11}", parsed.path)),
                "Buffer YouTube link is not a watch or Shorts page")
        video_id = parsed.path.rsplit("/", 1)[-1]
    require(bool(VIDEO_ID.fullmatch(video_id)), "Buffer YouTube video ID is malformed")
    return video_id


def read_public_video(video_id: str, getter=requests.get) -> dict:
    """Read bounded anonymous YouTube player HTML from two fixed routes."""
    require(bool(VIDEO_ID.fullmatch(video_id)), "public YouTube video ID is malformed")
    failures = []
    for url in (f"https://www.youtube.com/watch?v={video_id}",
                f"https://www.youtube.com/shorts/{video_id}"):
        try:
            try:
                with getter(url, stream=True, allow_redirects=False, timeout=(15, 60),
                            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}) as response:
                    require(response.status_code == 200 and
                            response.headers.get("Content-Type", "").lower().startswith("text/html"),
                            "public YouTube player page is unavailable")
                    chunks = []
                    count = 0
                    for chunk in response.iter_content(1 << 20):
                        count += len(chunk)
                        require(count <= MAX_WATCH_HTML, "public YouTube player page is oversized")
                        chunks.append(chunk)
            except requests.RequestException as exc:
                raise Hold("public YouTube player page could not be read") from exc
            html = b"".join(chunks).decode("utf-8", errors="replace")
            marker = PLAYER_MARKER.search(html)
            require(marker is not None, "public YouTube player could not be verified")
            try:
                player, _ = json.JSONDecoder().raw_decode(html[marker.end():])
            except ValueError as exc:
                raise Hold("public YouTube player response is malformed") from exc
            require(isinstance(player, dict), "public YouTube player response is incomplete")
            details = player.get("videoDetails")
            microformat = player.get("microformat")
            playability = player.get("playabilityStatus")
            details_meta = (microformat.get("playerMicroformatRenderer")
                            if isinstance(microformat, dict) else None)
            status = playability.get("status") if isinstance(playability, dict) else None
            require(status == "OK" and isinstance(details, dict) and
                    isinstance(details_meta, dict) and details.get("videoId") == video_id,
                    f"public YouTube video details are unavailable (player status: {status or 'missing'})")
            result = {"playability": status,
                      "video_id": details.get("videoId"), "channel_id": details.get("channelId"),
                      "title": details.get("title"), "description": details.get("shortDescription"),
                      "is_private": details.get("isPrivate"),
                      "is_unlisted": details_meta.get("isUnlisted"),
                      "external_channel_id": details_meta.get("externalChannelId")}
            require(isinstance(result["channel_id"], str) and
                    isinstance(result["external_channel_id"], str) and
                    isinstance(result["title"], str) and
                    isinstance(result["description"], str) and
                    type(result["is_private"]) is bool and type(result["is_unlisted"]) is bool,
                    "public YouTube video metadata is incomplete")
            return result
        except Hold as exc:
            failures.append(str(exc))
    raise Hold("public YouTube readback failed on both fixed routes: " + "; ".join(failures))


def verify_due(receipt: dict, source: Path, config: dict, api: BufferClient,
               public_reader=read_public_video, media_reader=fetch_video) -> dict:
    """Bind one sent Buffer ID, exact hosted asset, and public video to its scheduled receipt."""
    episode = receipt.get("episode")
    commit = receipt.get("source_commit")
    media_hash = receipt.get("media_sha256")
    require(receipt.get("phase") == "scheduled" and bool(EPISODE.fullmatch(str(episode))) and
            bool(COMMIT.fullmatch(str(commit))) and bool(SHA.fullmatch(str(media_hash))),
            "scheduled receipt identity is malformed")
    due = utc(receipt.get("due_at_utc"), "reserved due time")
    publisher = receipt.get("publisher_receipt")
    require(isinstance(publisher, dict) and
            (publisher.get("episode"), publisher.get("source_commit"),
             publisher.get("media_sha256")) == (episode, commit, media_hash),
            "scheduled publisher receipt differs from exact draft")
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{media_hash}"
    media_url = config["publisher_media_url_prefix"] + public_id + ".mp4"
    require(publisher.get("control_media_public_id") == public_id and
            publisher.get("control_media_url") == media_url,
            "scheduled publisher media is not the pinned exact copy")
    youtube = publisher.get("youtube")
    require(isinstance(youtube, dict) and bool(POST_ID.fullmatch(str(youtube.get("id")))) and
            youtube.get("status") in ("scheduled", "sent") and
            utc(youtube.get("due_at"), "publisher accepted due time") == due,
            "scheduled Buffer post identity or due time differs")
    require(_git(source, "remote", "get-url", "origin") == config["source_remote"] and
            _git(source, "symbolic-ref", "--short", "HEAD") == "main" and
            _git(source, "merge-base", "--is-ancestor", commit, "HEAD") == "",
            "exact producer source commit is no longer on main")
    copy, metadata = copy_and_metadata(source, commit, episode)
    organization = config["buffer_organization_id"]
    channel = config["youtube_channel_id"]
    api.organization(organization)
    listed = api.posts(organization, channel)
    require(any(item.get("id") == youtube["id"] for item in listed),
            "exact Buffer post is missing from pinned YouTube channel")
    post = api.sent_post(youtube["id"])
    require(post.get("id") == youtube["id"] and post.get("status") == "sent" and
            post.get("channelService") == "youtube" and
            utc(post.get("dueAt"), "sent Buffer due time") == due and
            post.get("text") == copy["youtube"] and
            post.get("error") in (None, {}),
            "exact Buffer post was not sent with approved text and due time")
    sent_at = utc(post.get("sentAt"), "Buffer sent time")
    assets = post.get("assets")
    require(isinstance(assets, list) and len(assets) == 1 and
            isinstance(assets[0], dict) and assets[0].get("source") == media_url,
            "sent Buffer post does not use exact control media")
    require(digest(media_reader(media_url, {"media_url_prefix": config["publisher_media_url_prefix"]})) ==
            media_hash, "sent Buffer media bytes differ from reviewed draft")
    video_id = video_id_from_buffer(post.get("externalLink"))
    page = public_reader(video_id)
    require(isinstance(page, dict) and page.get("playability") == "OK" and
            page.get("video_id") == video_id and
            page.get("channel_id") == config["youtube_public_channel_id"] and
            page.get("external_channel_id") == config["youtube_public_channel_id"] and
            page.get("title") == metadata["youtube"]["title"] and
            page.get("description") == copy["youtube"] and
            page.get("is_private") is False and page.get("is_unlisted") is False,
            "exact public YouTube video or channel could not be verified")
    return {"version": 1, "buffer_post_id": youtube["id"], "buffer_status": "sent",
            "buffer_sent_at_utc": sent_at.isoformat(), "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "youtube_channel_id": config["youtube_public_channel_id"],
            "youtube_title": metadata["youtube"]["title"],
            "youtube_description_sha256": digest(copy["youtube"].encode()),
            "control_media_sha256": media_hash,
            "verified_at_utc": datetime.now(timezone.utc).isoformat()}


def monitor_due(state: GitHubState, source: Path, config: dict, now: datetime,
                api: BufferClient, public_reader=read_public_video,
                media_reader=fetch_video) -> dict:
    """Read external systems only; record proof or privately alert after the grace period."""
    checked = verified = failed = 0
    for receipt in state.receipts():
        if receipt.get("phase") != "scheduled":
            continue
        episode = receipt["episode"]
        counted = False
        try:
            due = utc(receipt.get("due_at_utc"), "reserved due time")
            if due > now - DELIVERY_GRACE:
                continue
            checked += 1
            counted = True
            proof = verify_due(receipt, source, config, api, public_reader, media_reader)
            state.resolve_delivery(episode)
            state.put({**advance(receipt, "published"), "post_due_verification": proof})
            verified += 1
        except Hold as exc:
            if not counted:
                checked += 1
            state.alert_delivery(episode, str(exc))
            failed += 1
    return {"checked": checked, "verified": verified, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only post-due Buffer and YouTube verification")
    parser.add_argument("--policy", type=Path, default=Path(__file__).resolve().parents[1] / "policy.json")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    state = GitHubState(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", ""),
                        os.environ.get("GITHUB_SHA", ""))
    try:
        config = policy(args.policy)
        api = BufferClient(os.environ.get("HISTORY_PUBLISHER_BUFFER_API_KEY", ""))
        result = monitor_due(state, args.source, config, datetime.now(timezone.utc), api)
        print(json.dumps(result, sort_keys=True))
        return 2 if result["failed"] else 0
    except Hold as exc:
        print(f"Control hold: {exc}", file=sys.stderr)
        state.alert_delivery(None, str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
