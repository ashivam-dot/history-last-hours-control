"""Review exact producer Git blobs and hosted bytes without importing producer code."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

FILES = ("topic.json", "short.yaml", "script.json", "research.json", "visuals.json",
         "review.json", "work/manifest.json")
CHECKS = ("claim_sources", "visual_identity_rights", "full_video_audio")
CONTEXT = b"history-last-hours-independent-review-v1\0"
CONTEXT_V2 = b"history-last-hours-private-draft-review-v2\0"
EPISODE = re.compile(r"ep(\d{3,})\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
MAX_VIDEO_BYTES = 300 * 1024 * 1024
PASS_SCORES = ("hook", "clarity", "payoff", "visuals", "loop")
OPEN_LICENSES = {"CC0", "Public domain", "Pexels License", "Pixabay Content License"}
ORIGINAL_ASSETS = {"designed card", "AI generated"}


class Hold(RuntimeError):
    """An incomplete or changed candidate must stay held."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Hold(reason)


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_object(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Hold(f"{label} is invalid JSON") from exc
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise Hold(f"{label} is not a time") from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
            f"{label} must use UTC")
    return parsed


def source_time(value: object, label: str) -> datetime:
    """Interpret an aware producer timestamp on the control's UTC timeline."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise Hold(f"{label} is not a time") from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def policy(path: Path) -> dict:
    config = read_object(path.read_bytes(), "control policy")
    require(set(config) == {"version", "signing_enabled", "publishing_enabled", "intake_enabled", "source_remote",
                            "min_episode_id", "started_after_utc", "media_url_prefix",
                            "publisher_media_url_prefix",
                            "reviewer_key_sha256", "buffer_organization_id", "youtube_channel_id",
                            "qa_model", "qa_provider"},
            "control policy has missing or unexpected fields")
    require(type(config["version"]) is int and config["version"] == 1 and
            type(config["signing_enabled"]) is bool and
            type(config["publishing_enabled"]) is bool and
            type(config["intake_enabled"]) is bool, "control policy has invalid switches")
    floor = EPISODE.fullmatch(str(config["min_episode_id"]))
    require(bool(floor) and int(floor.group(1)) >= 63, "control policy permits legacy episodes")
    require(utc(config["started_after_utc"], "control cutoff") >= datetime(2026, 10, 4, 5, tzinfo=timezone.utc),
            "control policy predates rollout")
    require(config["source_remote"] == "https://github.com/ashivam-dot/creature-receipts.git",
            "control policy source repository differs")
    prefix = config["media_url_prefix"]
    require(isinstance(prefix, str) and prefix.startswith("https://res.cloudinary.com/") and
            prefix.endswith("/video/upload/"), "control policy media prefix is invalid")
    publisher_prefix = config["publisher_media_url_prefix"]
    require(publisher_prefix == "" or
            (isinstance(publisher_prefix, str) and
             publisher_prefix.startswith("https://res.cloudinary.com/") and
             publisher_prefix.endswith("/video/upload/") and publisher_prefix != prefix),
            "publisher media must use a different pinned Cloudinary account")
    require(config["reviewer_key_sha256"] == "" or bool(SHA.fullmatch(str(config["reviewer_key_sha256"]))),
            "control policy reviewer fingerprint is invalid")
    require(isinstance(config["youtube_channel_id"], str) and
            isinstance(config["buffer_organization_id"], str) and
            config["qa_provider"] in ("", "gemini", "openai") and
            isinstance(config["qa_model"], str) and
            (not config["qa_model"] or bool(re.fullmatch(r"[A-Za-z0-9_.-]+", config["qa_model"]))),
            "control policy Buffer destination or QA model is invalid")
    require(bool(config["qa_provider"]) == bool(config["qa_model"]),
            "control policy QA provider and model must be pinned together")
    return config


def _git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False,
                          env={key: value for key, value in os.environ.items()
                               if key != "HISTORY_CONTROL_POLICY_TOKEN"})
    if done.returncode:
        raise Hold("source Git checkout or committed candidate is unavailable")
    return done.stdout


def _blob(repo: Path, commit: str, path: str) -> bytes:
    return _git(repo, "show", f"{commit}:{path}")


def episode_root(commit: str, episode: str) -> str:
    require(bool(COMMIT.fullmatch(commit)) and bool(EPISODE.fullmatch(episode)),
            "candidate commit or episode ID is malformed")
    return f"content/episodes/{episode}/"


def _check_editorial_evidence(blobs: dict[str, bytes], episode: str, media_hash: str) -> None:
    """Independently enforce the producer certificate's structural source and rights gate."""
    try:
        spec = yaml.safe_load(blobs["short.yaml"])
    except yaml.YAMLError as exc:
        raise Hold("committed Short spec YAML is invalid") from exc
    research = read_object(blobs["research.json"], "committed research")
    script = read_object(blobs["script.json"], "committed script")
    manifest = read_object(blobs["work/manifest.json"], "committed manifest")
    review = read_object(blobs["review.json"], "committed producer review")
    require(isinstance(spec, dict) and spec.get("id") == episode,
            "committed spec identity differs")
    spec_beats = spec.get("beats")
    script_beats = script.get("beats")
    manifest_beats = manifest.get("beats")
    require(all(isinstance(beats, list) for beats in (spec_beats, script_beats, manifest_beats)) and
            len(spec_beats) > 0 and len(spec_beats) == len(script_beats) == len(manifest_beats),
            "spec, script, and render beats are incomplete")
    sources = research.get("sources")
    claims = research.get("claims")
    require(research.get("viable") is True and isinstance(sources, list) and
            isinstance(claims, list) and bool(sources) and bool(claims),
            "independent source evidence is incomplete")
    by_label: dict[str, str] = {}
    for source in sources:
        require(isinstance(source, dict) and isinstance(source.get("label"), str) and
                isinstance(source.get("url"), str), "research source is malformed")
        parsed = urlparse(source["url"])
        require(parsed.scheme == "https" and bool(parsed.hostname) and
                source["label"] not in by_label, "research source URL or label is invalid")
        by_label[source["label"]] = parsed.hostname
    cited: set[int] = set()
    for spec_beat, script_beat, render_beat in zip(spec_beats, script_beats, manifest_beats):
        require(all(isinstance(beat, dict) for beat in (spec_beat, script_beat, render_beat)) and
                isinstance(spec_beat.get("text"), str) and
                spec_beat["text"] == script_beat.get("text") == render_beat.get("text"),
                "narrated script differs from spec or render")
        numbers = script_beat.get("claims")
        require(isinstance(numbers, list) and bool(numbers) and
                all(type(number) is int and 1 <= number <= len(claims) for number in numbers),
                "narrated beat lacks valid claim citations")
        cited.update(numbers)
        asset = render_beat.get("asset")
        require(isinstance(asset, dict), "rendered visual asset is missing")
        source = asset.get("source")
        license_name = asset.get("license")
        if source in ORIGINAL_ASSETS:
            require(source != "AI generated" or spec.get("synthetic_media") is True,
                    "AI asset lacks synthetic-media disclosure")
        else:
            require(isinstance(license_name, str) and
                    (license_name in OPEN_LICENSES or
                     re.fullmatch(r"CC BY (?:2\.0|2\.5|3\.0|4\.0)(?: [a-z]{2})?", license_name, re.I)),
                    "rendered visual license is missing or ambiguous")
            require(isinstance(asset.get("url"), str) and
                    urlparse(asset["url"]).scheme == "https" and
                    isinstance(asset.get("credit"), str) and bool(asset["credit"].strip()),
                    "rendered visual lacks provenance or credit")
    for number in cited:
        claim = claims[number - 1]
        require(isinstance(claim, dict) and isinstance(claim.get("sources"), list) and
                isinstance(claim.get("evidence"), list), "cited claim is malformed")
        sites = set()
        for evidence in claim["evidence"]:
            if not isinstance(evidence, dict):
                continue
            label, quote = evidence.get("source"), evidence.get("quote")
            if (label in claim["sources"] and label in by_label and isinstance(quote, str) and
                    len(quote) >= 20 and len(quote.split()) >= 4):
                sites.add(by_label[label])
        require(len(sites) >= 2, "cited claim lacks quoted evidence from two source sites")
    rounds = review.get("rounds")
    require(isinstance(rounds, list) and bool(rounds), "producer final-media review is missing")
    kept = review.get("kept_round", len(rounds))
    require(type(kept) is int and 1 <= kept <= len(rounds), "producer kept review round is invalid")
    final = rounds[kept - 1]
    require(isinstance(final, dict) and isinstance(final.get("check"), dict) and
            isinstance(final.get("scores"), dict), "producer final-media review is malformed")
    checks = final["check"]
    scores = final["scores"]
    require(final.get("media_sha256") == media_hash and final.get("passed") is True and
            checks.get("warnings") == [] and checks.get("speech_differences") == [] and
            not checks.get("speech_error") and final.get("frames") == [] and
            final.get("speech") == [] and
            all(type(scores.get(name)) is int and scores[name] >= 4 for name in PASS_SCORES),
            "producer final-media review is not clean for exact video")


def candidate(repo: Path, commit: str, episode: str, video: bytes, config: dict) -> dict:
    """Construct the exact subject expected by the dormant producer verifier."""
    require(bool(COMMIT.fullmatch(commit)), "source commit must be an immutable 40-character SHA")
    match = EPISODE.fullmatch(episode)
    floor = EPISODE.fullmatch(config["min_episode_id"])
    require(bool(match) and int(match.group(1)) >= int(floor.group(1)),
            "episode predates control floor")
    origin = _git(repo, "remote", "get-url", "origin").decode("utf-8").strip()
    require(origin == config["source_remote"], "source checkout remote differs")
    require(_git(repo, "rev-parse", "HEAD").decode("ascii").strip() == commit,
            "source checkout HEAD differs from candidate commit")
    root = episode_root(commit, episode)
    blobs = {name: _blob(repo, commit, root + name) for name in FILES}
    held = read_object(_blob(repo, commit, root + "hold.json"), "committed hold")
    manifest = read_object(blobs["work/manifest.json"], "committed manifest")
    topic = read_object(blobs["topic.json"], "committed topic")
    require(source_time(topic.get("started_at"), "topic start") >=
            utc(config["started_after_utc"], "control cutoff"),
            "candidate predates control cutoff")
    require(held.get("id") == manifest.get("id") == episode and held.get("superseded") is not True,
            "candidate identity or hosted state differs")
    media_hash = digest(video)
    require(bool(video) and len(video) <= MAX_VIDEO_BYTES and bool(SHA.fullmatch(media_hash)),
            "reviewed video is empty or oversized")
    require(manifest.get("video_sha256") == held.get("media_sha256") == media_hash,
            "reviewed video, manifest and hosted hold differ")
    files = {name: digest(data) for name, data in blobs.items()}
    require(held.get("spec_sha256") == files["short.yaml"] and
            held.get("manifest_sha256") == files["work/manifest.json"],
            "hosted hold has stale spec or manifest binding")
    _check_editorial_evidence(blobs, episode, media_hash)
    media_url = held.get("media_url")
    require(isinstance(media_url, str) and media_url.startswith(config["media_url_prefix"]) and
            not urlparse(media_url).username and not urlparse(media_url).password and
            urlparse(media_url).query == "" and urlparse(media_url).fragment == "",
            "hosted video URL differs from pinned Cloudinary account")
    require(isinstance(held.get("media_public_id"), str) and bool(held["media_public_id"]),
            "hosted public ID is missing")
    return {"id": episode, "media_sha256": media_hash, "media_url": media_url,
            "media_public_id": held["media_public_id"], "files": files}


def fetch_video(url: str, config: dict) -> bytes:
    require(isinstance(url, str) and url.startswith(config["media_url_prefix"]) and urlparse(url).query == "" and
            urlparse(url).fragment == "", "hosted video URL differs from pinned account")
    request = urllib.request.Request(url, headers={"Accept": "video/*"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            require(response.status == 200 and response.headers.get("Content-Type", "").lower().startswith("video/"),
                    "hosted video is not an HTTP 200 video")
            body = response.read(MAX_VIDEO_BYTES + 1)
            length = response.headers.get("Content-Length")
            require(length is None or int(length) == len(body), "hosted video is truncated")
    except (OSError, ValueError) as exc:
        raise Hold("hosted video could not be read completely") from exc
    require(0 < len(body) <= MAX_VIDEO_BYTES, "hosted video is empty or oversized")
    return body


def approval_subject(approval: dict, subject: dict) -> None:
    require(set(approval) == {"subject", "decision", "checks", "reviewed_at_utc"},
            "approval has missing or unexpected fields")
    require(approval["subject"] == subject and approval["decision"] == "approved" and
            isinstance(approval["checks"], dict) and set(approval["checks"]) == set(CHECKS) and
            all(approval["checks"][name] is True for name in CHECKS),
            "independent approval does not cover the exact candidate and all checks")
    reviewed = utc(approval["reviewed_at_utc"], "independent review time")
    require(reviewed <= datetime.now(timezone.utc) + timedelta(minutes=5),
            "independent review time is in the future")


def sign(approval: dict, subject: dict, private_raw_b64: str, config: dict) -> dict:
    require(config["signing_enabled"] is True, "independent signing is disabled")
    approval_subject(approval, subject)
    version = 2 if subject.get("version") == 2 else 1
    require(version == 1 or (subject.get("source_commit") and
                            subject.get("media_delivery_type") == "authenticated"),
            "private review subject is incomplete")
    try:
        key_raw = base64.b64decode(private_raw_b64, validate=True)
        require(len(key_raw) == 32, "Ed25519 signing key is invalid")
        key = Ed25519PrivateKey.from_private_bytes(key_raw)
    except (ValueError, TypeError) as exc:
        raise Hold("Ed25519 signing key is invalid") from exc
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = digest(public)
    require(config["reviewer_key_sha256"] == fingerprint,
            "signing key differs from pinned reviewer")
    review = {"version": version, "subject": subject, "reviewer_key_sha256": fingerprint,
              "reviewed_at_utc": approval["reviewed_at_utc"], "decision": "approved",
              "checks": approval["checks"]}
    context = CONTEXT_V2 if version == 2 else CONTEXT
    if os.environ.get("HISTORY_UNATTENDED_LIVE_POLICY") == "1":
        from .orchestrate import require_mutation_policy
        require_mutation_policy(config, "sign")
    review["signature"] = base64.b64encode(key.sign(context + canonical(review))).decode("ascii")
    return review


def verify(review: dict, subject: dict, public_raw_b64: str, config: dict) -> None:
    require(set(review) == {"version", "subject", "reviewer_key_sha256", "reviewed_at_utc",
                            "decision", "checks", "signature"} and type(review.get("version")) is int and
            review["version"] in (1, 2) and
            review["version"] == (2 if subject.get("version") == 2 else 1),
            "signed review schema is invalid")
    approval_subject({name: review[name] for name in ("subject", "decision", "checks", "reviewed_at_utc")}, subject)
    try:
        public = base64.b64decode(public_raw_b64, validate=True)
        require(len(public) == 32, "Ed25519 public key is invalid")
        require(digest(public) == review["reviewer_key_sha256"] == config["reviewer_key_sha256"],
                "trusted reviewer fingerprint differs")
        signature = base64.b64decode(review["signature"], validate=True)
        unsigned = {name: value for name, value in review.items() if name != "signature"}
        context = CONTEXT_V2 if review["version"] == 2 else CONTEXT
        Ed25519PublicKey.from_public_bytes(public).verify(signature, context + canonical(unsigned))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise Hold("independent review signature is invalid") from exc


def publisher_preflight(repo: Path, commit: str, episode: str, review: dict,
                        public_raw_b64: str, config: dict) -> dict:
    """Credential-free gate for a future control-owned publisher job; never mutates Buffer."""
    require(config["publishing_enabled"] is True, "control publisher is disabled")
    require(bool(config["buffer_organization_id"]), "pinned Buffer organization is missing")
    require(bool(config["youtube_channel_id"]), "pinned YouTube destination is missing")
    require(bool(config["publisher_media_url_prefix"]) and
            config["publisher_media_url_prefix"] != config["media_url_prefix"],
            "independent publisher media account is missing")
    root = episode_root(commit, episode)
    held = read_object(_blob(repo, commit, root + "hold.json"), "committed hold")
    video = fetch_video(held.get("media_url", ""), config)
    subject = candidate(repo, commit, episode, video, config)
    verify(review, subject, public_raw_b64, config)
    return {"source_commit": commit, "subject": subject,
            "youtube_channel_id": config["youtube_channel_id"]}
