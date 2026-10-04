"""Read one committed producer draft and make a control-owned review packet."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .publisher import CloudinaryClient, MAX_CLOUDINARY_UPLOAD, PUBLISHER_PUBLIC_ID_PREFIX
from .release import (COMMIT, EPISODE, FILES, SHA, Hold, _blob, _check_editorial_evidence,
                      _git, digest, episode_root, read_object, require, utc)

OUTBOX_VOLUME = "creature-receipts-outbox"
MAX_RETRY = 6


def committed_draft(repo: Path, commit: str, episode: str, config: dict) -> dict:
    """Validate Git evidence and the private outbox locator before reading Modal."""
    require(bool(COMMIT.fullmatch(commit)), "draft source commit is malformed")
    match = EPISODE.fullmatch(episode)
    floor = EPISODE.fullmatch(config["min_episode_id"])
    require(bool(match) and int(match.group(1)) >= int(floor.group(1)),
            "draft predates control episode floor")
    origin = _git(repo, "remote", "get-url", "origin").decode("utf-8").strip()
    require(origin == config["source_remote"], "draft source checkout remote differs")
    require(_git(repo, "rev-parse", "HEAD").decode("ascii").strip() == commit,
            "draft source checkout HEAD differs")
    root = episode_root(commit, episode)
    forbidden = ("hold.json", "publish.json", "release_certificate.json", "independent_review.json")
    existing = _git(repo, "ls-tree", "-r", "--name-only", commit, "--",
                    *(root + name for name in forbidden)).decode("utf-8").strip()
    require(not existing, "draft already has a producer release or hosted-media record")
    blobs = {name: _blob(repo, commit, root + name) for name in FILES}
    draft_blob = _blob(repo, commit, root + "draft.json")
    draft = read_object(draft_blob, "committed draft")
    required = {"id", "title", "series", "scores", "anniversary", "media_sha256",
                "spec_sha256", "manifest_sha256", "modal_volume", "modal_path"}
    require(set(draft) == required and draft.get("id") == episode and
            isinstance(draft.get("title"), str) and bool(draft["title"].strip()) and
            isinstance(draft.get("series"), str) and isinstance(draft.get("scores"), dict) and
            (draft.get("anniversary") is None or isinstance(draft["anniversary"], str)),
            "committed draft schema or identity differs")
    media_hash = draft["media_sha256"]
    require(isinstance(media_hash, str) and bool(SHA.fullmatch(media_hash)) and
            draft["modal_volume"] == OUTBOX_VOLUME and
            draft["modal_path"] == f"drafts/{episode}-{media_hash}.mp4",
            "committed draft media locator is malformed")
    files = {name: digest(body) for name, body in blobs.items()}
    manifest = read_object(blobs["work/manifest.json"], "committed manifest")
    topic = read_object(blobs["topic.json"], "committed topic")
    require(manifest.get("id") == episode and manifest.get("video_sha256") == media_hash and
            draft["spec_sha256"] == files["short.yaml"] and
            draft["manifest_sha256"] == files["work/manifest.json"],
            "draft, manifest, spec, or video hash differs")
    require(utc(topic.get("started_at"), "draft topic start") >=
            utc(config["started_after_utc"], "control cutoff"),
            "draft predates control cutoff")
    _check_editorial_evidence(blobs, episode, media_hash)
    return {"draft": draft, "draft_blob": draft_blob, "draft_sha256": digest(draft_blob),
            "blobs": blobs, "files": files}


def read_private_video(path: str) -> bytes:
    """Read only the exact draft path from the History Modal workspace volume."""
    require(isinstance(path, str) and bool(re.fullmatch(r"drafts/ep[0-9]{3,}-[0-9a-f]{64}\.mp4", path)),
            "private draft path is outside the pinned outbox namespace")
    require(bool(os.environ.get("MODAL_TOKEN_ID")) and bool(os.environ.get("MODAL_TOKEN_SECRET")),
            "control intake Modal credential is missing")
    try:
        import modal

        volume = modal.Volume.from_name(OUTBOX_VOLUME, create_if_missing=False)
        chunks: list[bytes] = []
        count = 0
        for chunk in volume.read_file(path):
            require(isinstance(chunk, bytes) and bool(chunk), "private draft has an invalid chunk")
            count += len(chunk)
            require(count <= MAX_CLOUDINARY_UPLOAD, "private draft exceeds bounded upload size")
            chunks.append(chunk)
        return b"".join(chunks)
    except Hold:
        raise
    except Exception as exc:
        raise Hold("private draft could not be read from the pinned Modal outbox") from exc


def _delivered_exact(media_client: CloudinaryClient, asset_id: str, video: bytes) -> None:
    for attempt in range(MAX_RETRY):
        try:
            delivered = media_client.download_private_asset(asset_id)
        except Hold:
            if attempt == MAX_RETRY - 1:
                raise
            time.sleep(5)
            continue
        require(delivered == video, "control-hosted draft differs from private reviewed render")
        return


def intake_draft(repo: Path, commit: str, episode: str, config: dict,
                 cloudinary_url: str, output: Path,
                 video_reader: Callable[[str], bytes] = read_private_video,
                 media_client: CloudinaryClient | None = None) -> dict:
    """Create a write-once review packet after independent source and media checks."""
    require(config["intake_enabled"] is True, "private draft intake is disabled")
    require(not output.exists(), "review packet path already exists")
    source = committed_draft(repo, commit, episode, config)
    draft = source["draft"]
    video = video_reader(draft["modal_path"])
    media_hash = draft["media_sha256"]
    require(12 <= len(video) <= MAX_CLOUDINARY_UPLOAD and video[4:8] == b"ftyp" and
            digest(video) == media_hash,
            "private draft MP4 differs from committed draft and manifest")
    require(bool(cloudinary_url), "control intake Cloudinary credential is missing")
    prefix = config["publisher_media_url_prefix"]
    require(prefix != config["media_url_prefix"], "control cloud must differ from producer cloud")
    media_client = media_client or CloudinaryClient(cloudinary_url, prefix)
    media_url, public_id, asset_id = media_client.upload_private_draft(video, episode, media_hash)
    private_prefix = prefix.replace("/video/upload/", "/video/authenticated/")
    require(public_id == f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/{episode}-{media_hash}" and
            isinstance(media_url, str) and
            bool(re.fullmatch(re.escape(private_prefix) + r"(?:v[0-9]+/)?" +
                              re.escape(public_id) + r"\.mp4", media_url)) and
            isinstance(asset_id, str) and bool(asset_id),
            "control-hosted private draft identity differs")
    _delivered_exact(media_client, asset_id, video)
    subject = {"version": 2, "id": episode, "source_commit": commit,
               "draft_sha256": source["draft_sha256"], "media_sha256": media_hash,
               "media_url": media_url, "media_public_id": public_id,
               "media_asset_id": asset_id, "media_delivery_type": "authenticated",
               "files": source["files"]}
    hold = {"version": 1, "state": "held_for_independent_review", "id": episode,
            "source_commit": commit, "draft_sha256": source["draft_sha256"],
            "media_sha256": media_hash, "media_url": media_url,
            "media_public_id": public_id, "spec_sha256": source["files"]["short.yaml"],
            "manifest_sha256": source["files"]["work/manifest.json"],
            "media_asset_id": asset_id, "media_delivery_type": "authenticated",
            "modal_volume": OUTBOX_VOLUME, "modal_path": draft["modal_path"],
            "intake_at_utc": datetime.now(timezone.utc).isoformat()}
    packet = {"version": 1, "subject": subject, "control_hold": hold,
              "source_files": source["files"], "video_filename": f"{episode}.mp4"}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    entries = {f"{episode}.mp4": video, "subject.json": json.dumps(subject, indent=2).encode() + b"\n",
               "control_hold.json": json.dumps(hold, indent=2).encode() + b"\n",
               "packet.json": json.dumps(packet, indent=2).encode() + b"\n",
               "draft.json": source["draft_blob"]}
    for name, body in source["blobs"].items():
        entries[f"evidence/{name}"] = body
    for name, body in entries.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(body)
    return packet


def validated_packet(repo: Path, commit: str, episode: str, config: dict,
                     packet_dir: Path) -> tuple[dict, bytes]:
    """Rebuild a v2 subject from exact Git blobs and a bounded private packet."""
    source = committed_draft(repo, commit, episode, config)
    names = {f"{episode}.mp4", "subject.json", "control_hold.json", "packet.json",
             "draft.json", *(f"evidence/{name}" for name in FILES)}
    require(packet_dir.is_dir() and not packet_dir.is_symlink(), "private review packet is missing")
    paths = list(packet_dir.rglob("*"))
    actual = {str(path.relative_to(packet_dir)) for path in paths if path.is_file()}
    folders = {str(path.relative_to(packet_dir)) for path in paths if path.is_dir()}
    require(actual == names and folders == {"evidence", "evidence/work"} and
            all(not path.is_symlink() for path in paths),
            "private review packet files differ")
    video_path = packet_dir / f"{episode}.mp4"
    require(12 <= video_path.stat().st_size <= MAX_CLOUDINARY_UPLOAD,
            "private review video exceeds bounded size")
    video = video_path.read_bytes()
    media_hash = source["draft"]["media_sha256"]
    require(video[4:8] == b"ftyp" and digest(video) == media_hash,
            "private review video differs from committed draft")
    require((packet_dir / "draft.json").read_bytes() == source["draft_blob"] and
            all((packet_dir / "evidence" / name).read_bytes() == body
                for name, body in source["blobs"].items()),
            "private review evidence differs from committed source")
    hold = read_object((packet_dir / "control_hold.json").read_bytes(), "control hold")
    require(set(hold) == {"version", "state", "id", "source_commit", "draft_sha256",
                          "media_sha256", "media_url", "media_public_id", "spec_sha256",
                          "manifest_sha256", "media_asset_id", "media_delivery_type",
                          "modal_volume", "modal_path", "intake_at_utc"} and
            type(hold["version"]) is int and hold["version"] == 1 and
            hold["state"] == "held_for_independent_review" and hold["id"] == episode and
            hold["source_commit"] == commit and hold["draft_sha256"] == source["draft_sha256"] and
            hold["media_sha256"] == media_hash and
            hold["spec_sha256"] == source["files"]["short.yaml"] and
            hold["manifest_sha256"] == source["files"]["work/manifest.json"] and
            hold["modal_volume"] == OUTBOX_VOLUME and
            hold["modal_path"] == source["draft"]["modal_path"] and
            hold["media_delivery_type"] == "authenticated",
            "control hold does not bind the exact private draft")
    utc(hold["intake_at_utc"], "control intake time")
    public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/{episode}-{media_hash}"
    private_prefix = config["publisher_media_url_prefix"].replace(
        "/video/upload/", "/video/authenticated/")
    require(bool(private_prefix.startswith("https://res.cloudinary.com/")) and
            hold["media_public_id"] == public_id and
            isinstance(hold["media_asset_id"], str) and
            bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", hold["media_asset_id"])) and
            isinstance(hold["media_url"], str) and
            bool(re.fullmatch(re.escape(private_prefix) + r"(?:v[0-9]+/)?" +
                              re.escape(public_id) + r"\.mp4", hold["media_url"])),
            "control hold has a public or unpinned draft URL")
    subject = {"version": 2, "id": episode, "source_commit": commit,
               "draft_sha256": source["draft_sha256"], "media_sha256": media_hash,
               "media_url": hold["media_url"], "media_public_id": public_id,
               "media_asset_id": hold["media_asset_id"],
               "media_delivery_type": "authenticated", "files": source["files"]}
    require(read_object((packet_dir / "subject.json").read_bytes(), "private subject") == subject,
            "private subject differs from source and control hold")
    packet = read_object((packet_dir / "packet.json").read_bytes(), "private packet index")
    require(packet == {"version": 1, "subject": subject, "control_hold": hold,
                       "source_files": source["files"], "video_filename": f"{episode}.mp4"},
            "private packet index differs from exact subject")
    return subject, video
