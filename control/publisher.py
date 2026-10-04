"""Dormant control-owned Buffer publisher. No producer code or credentials are imported."""

from __future__ import annotations

import json
import hashlib
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

import requests
import yaml

from .release import (Hold, _blob, digest, episode_root, fetch_video, publisher_preflight,
                      read_object, require, utc, verify)

BUFFER_API = "https://api.buffer.com"
QUEUE_LIMIT = 10
MIN_LEAD = timedelta(minutes=30)
MAX_HORIZON = timedelta(days=30)
MAX_CLOUDINARY_UPLOAD = 95 * 1024 * 1024
PUBLISHER_PUBLIC_ID_PREFIX = "history-last-hours/"
PROBE_PUBLIC_ID = re.compile(r"history-last-hours/test/probe-[0-9a-f]{32}-[0-9a-f]{64}")

CHANNELS_QUERY = """query Channels($input: ChannelsInput!) {
  channels(input: $input) { id name service isDisconnected isLocked isQueuePaused }
}"""
POSTS_QUERY = """query Posts($input: PostsInput!, $after: String) {
  posts(input: $input, first: 50, after: $after) {
    edges { node { id status dueAt text } }
    pageInfo { hasNextPage endCursor }
  }
}"""
POST_QUERY = """query Post($id: PostId!) {
  post(input: {id: $id}) {
    id status dueAt text assets { ... on VideoAsset { source } }
  }
}"""
CREATE_MUTATION = """mutation Create($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}"""


class BufferClient:
    """A Buffer credential exists only in the separate publisher process."""

    def __init__(self, token: str):
        require(isinstance(token, str) and bool(token.strip()), "control publisher credential is missing")
        self._token = token

    def _call(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode()
        request = urllib.request.Request(BUFFER_API, data=body, method="POST",
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                require(response.status == 200, "Buffer returned a non-200 response")
                raw = response.read(2_000_001)
                require(len(raw) <= 2_000_000, "Buffer response is oversized")
                result = read_object(raw, "Buffer response")
        except (OSError, urllib.error.URLError) as exc:
            raise Hold("Buffer response is uncertain; inspect remote posts before retrying") from exc
        require(not result.get("errors") and isinstance(result.get("data"), dict),
                "Buffer returned a GraphQL error or missing data")
        return result["data"]

    def organization(self, wanted: str) -> None:
        data = self._call("query { account { organizations { id name } } }", {})
        organizations = (data.get("account") or {}).get("organizations")
        require(isinstance(organizations, list) and
                sum(item.get("id") == wanted for item in organizations if isinstance(item, dict)) == 1,
                "pinned Buffer organization is unavailable")

    def channels(self, organization: str) -> list[dict]:
        result = self._call(CHANNELS_QUERY, {"input": {"organizationId": organization}}).get("channels")
        require(isinstance(result, list), "Buffer channel list is unavailable")
        return result

    def posts(self, organization: str, channel: str) -> list[dict]:
        variables = {"input": {"organizationId": organization, "filter": {"channelIds": [channel]}},
                     "after": None}
        found: list[dict] = []
        cursors: set[str] = set()
        for _ in range(100):
            page = self._call(POSTS_QUERY, variables).get("posts")
            require(isinstance(page, dict) and isinstance(page.get("edges"), list) and
                    isinstance(page.get("pageInfo"), dict), "Buffer post page is incomplete")
            nodes = [edge.get("node") for edge in page["edges"] if isinstance(edge, dict)]
            require(len(nodes) == len(page["edges"]) and all(isinstance(node, dict) for node in nodes),
                    "Buffer post page has a malformed node")
            require(all(isinstance(node.get("id"), str) and node["id"] and
                        node.get("status") in ("scheduled", "sent", "error", "draft") and
                        isinstance(node.get("text"), str) for node in nodes),
                    "Buffer post page has an unknown or incomplete state")
            found.extend(nodes)
            if page["pageInfo"].get("hasNextPage") is False:
                ids = [item.get("id") for item in found]
                require(len(ids) == len(set(ids)) and all(isinstance(item, str) for item in ids),
                        "Buffer post history contains duplicate or missing IDs")
                return found
            cursor = page["pageInfo"].get("endCursor")
            require(isinstance(cursor, str) and cursor and cursor not in cursors,
                    "Buffer pagination cursor is invalid")
            cursors.add(cursor)
            variables["after"] = cursor
        raise Hold("Buffer post history exceeds bounded pagination")

    def post(self, post_id: str) -> dict:
        result = self._call(POST_QUERY, {"id": post_id}).get("post")
        require(isinstance(result, dict), "matching Buffer post cannot be inspected")
        return result

    def create(self, payload: dict) -> dict:
        result = self._call(CREATE_MUTATION, {"input": payload}).get("createPost")
        require(isinstance(result, dict) and result.get("__typename") == "PostActionSuccess" and
                isinstance(result.get("post"), dict),
                "Buffer create response is uncertain; inspect remote posts before retrying")
        return result["post"]


class CloudinaryClient:
    """Upload to the pinned cloud distinct from History's producer cloud."""

    def __init__(self, cloudinary_url: str, publisher_prefix: str):
        parsed = urlparse(cloudinary_url)
        cloud = parsed.hostname
        require(parsed.scheme == "cloudinary" and bool(cloud) and
                bool(parsed.username) and bool(parsed.password) and
                not parsed.path and not parsed.query and not parsed.fragment,
                "control Cloudinary credential is missing or invalid")
        require(publisher_prefix == f"https://res.cloudinary.com/{cloud}/video/upload/",
                "control Cloudinary account differs from pinned independent media account")
        self._cloud = cloud
        self._key = unquote(parsed.username)
        self._secret = unquote(parsed.password)
        self._prefix = publisher_prefix

    def _signed(self, params: dict) -> dict:
        payload = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        return {**params, "signature": hashlib.sha1((payload + self._secret).encode()).hexdigest(),
                "api_key": self._key}

    def _upload(self, video: bytes, public_id: str, media_sha256: str,
                *, allow_existing: bool) -> tuple[str, str]:
        require(len(video) >= 12 and video[4:8] == b"ftyp" and
                len(video) <= MAX_CLOUDINARY_UPLOAD and digest(video) == media_sha256,
                "control upload needs the exact bounded MP4")
        params = {"public_id": public_id, "overwrite": "false", "timestamp": int(time.time())}
        try:
            response = requests.post(
                f"https://api.cloudinary.com/v1_1/{self._cloud}/video/upload",
                data=self._signed(params),
                files={"file": (public_id.rsplit("/", 1)[-1] + ".mp4", video, "video/mp4")},
                timeout=(15, 600),
            )
        except requests.RequestException as exc:
            raise Hold("control media upload response is uncertain; inspect account before retrying") from exc
        if response.status_code in (200, 201):
            try:
                result = response.json()
            except ValueError as exc:
                raise Hold("control media upload response is invalid") from exc
            require(isinstance(result, dict) and result.get("public_id") == public_id and
                    result.get("resource_type") == "video" and result.get("bytes") == len(video) and
                    isinstance(result.get("secure_url"), str) and
                    result["secure_url"].startswith(self._prefix) and
                    (allow_existing or result.get("existing") is not True),
                    "control media upload identity or size differs")
        else:
            # Existing immutable public_id is safe only when the caller re-downloads
            # its deterministic URL and verifies every byte below.
            require(allow_existing and response.status_code == 409,
                    "control media upload failed; inspect account before retrying")
        return self._prefix + public_id + ".mp4", public_id

    def upload_exact(self, video: bytes, episode: str, media_sha256: str) -> tuple[str, str]:
        return self._upload(video, f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{media_sha256}",
                            media_sha256, allow_existing=True)

    def upload_private_draft(self, video: bytes, episode: str,
                             media_sha256: str) -> tuple[str, str, str]:
        """Keep a pre-QA draft authenticated even when its hash is public in Git."""
        require(len(video) >= 12 and video[4:8] == b"ftyp" and
                len(video) <= MAX_CLOUDINARY_UPLOAD and digest(video) == media_sha256,
                "private draft upload needs the exact bounded MP4")
        public_id = f"{PUBLISHER_PUBLIC_ID_PREFIX}drafts/{episode}-{media_sha256}"
        params = {"public_id": public_id, "overwrite": "false", "type": "authenticated",
                  "timestamp": int(time.time())}
        try:
            response = requests.post(
                f"https://api.cloudinary.com/v1_1/{self._cloud}/video/upload",
                data=self._signed(params),
                files={"file": (f"{episode}.mp4", video, "video/mp4")},
                timeout=(15, 600),
            )
        except requests.RequestException as exc:
            return self._reconcile_private_draft(video, public_id)
        if response.status_code not in (200, 201):
            return self._reconcile_private_draft(video, public_id)
        try:
            result = response.json()
        except ValueError as exc:
            raise Hold("private draft upload response is invalid") from exc
        self._require_private_identity(result, public_id, len(video))
        return result["secure_url"], public_id, result["asset_id"]

    def _require_private_identity(self, result: dict, public_id: str, size: int) -> None:
        private_prefix = f"https://res.cloudinary.com/{self._cloud}/video/authenticated/"
        require(isinstance(result, dict) and result.get("public_id") == public_id and
                result.get("resource_type") == "video" and result.get("type") == "authenticated" and
                result.get("bytes") == size and isinstance(result.get("asset_id"), str) and
                bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", result["asset_id"])) and
                isinstance(result.get("secure_url"), str) and
                bool(re.fullmatch(re.escape(private_prefix) + r"(?:v[0-9]+/)?" +
                                  re.escape(public_id) + r"\.mp4", result["secure_url"])),
                "private draft upload identity, type, or size differs")

    def _reconcile_private_draft(self, video: bytes, public_id: str) -> tuple[str, str, str]:
        """Recover only an exact authenticated asset after an ambiguous upload response."""
        endpoint = (f"https://api.cloudinary.com/v1_1/{self._cloud}/resources/video/authenticated/" +
                    quote(public_id, safe="/"))
        for attempt in range(6):
            try:
                response = requests.get(endpoint, auth=(self._key, self._secret), timeout=(15, 60))
            except requests.RequestException as exc:
                raise Hold("private draft upload is uncertain; control asset lookup failed") from exc
            if response.status_code == 404 and attempt < 5:
                time.sleep(5)
                continue
            require(response.status_code == 200,
                    "private draft upload is uncertain; exact authenticated asset is unavailable")
            try:
                result = response.json()
            except ValueError as exc:
                raise Hold("private draft asset lookup response is invalid") from exc
            self._require_private_identity(result, public_id, len(video))
            require(self.download_private_asset(result["asset_id"]) == video,
                    "existing authenticated draft differs from exact private render")
            return result["secure_url"], public_id, result["asset_id"]
        raise Hold("private draft asset lookup exhausted")

    def download_private_asset(self, asset_id: str) -> bytes:
        """Re-read authenticated media through Cloudinary's signed download API."""
        require(isinstance(asset_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", asset_id)),
                "private draft asset ID is malformed")
        params = {"asset_id": asset_id, "timestamp": int(time.time())}
        try:
            with requests.post(f"https://api.cloudinary.com/v1_1/{self._cloud}/asset/download",
                               data=self._signed(params), stream=True, timeout=(15, 120)) as response:
                require(response.status_code == 200, "private draft download is unavailable")
                chunks: list[bytes] = []
                count = 0
                for chunk in response.iter_content(1 << 20):
                    count += len(chunk)
                    require(count <= MAX_CLOUDINARY_UPLOAD,
                            "private draft download exceeds bounded size")
                    chunks.append(chunk)
                return b"".join(chunks)
        except requests.RequestException as exc:
            raise Hold("private draft download response is uncertain") from exc

    def upload_probe(self, video: bytes, public_id: str, media_sha256: str) -> tuple[str, str]:
        require(bool(PROBE_PUBLIC_ID.fullmatch(public_id)) and public_id.endswith(media_sha256),
                "probe public ID must stay in the isolated test namespace")
        return self._upload(video, public_id, media_sha256, allow_existing=False)

    def destroy_probe(self, public_id: str) -> None:
        require(bool(PROBE_PUBLIC_ID.fullmatch(public_id)),
                "only an isolated test asset may be deleted")
        params = {"public_id": public_id, "invalidate": "true", "timestamp": int(time.time())}
        try:
            response = requests.post(
                f"https://api.cloudinary.com/v1_1/{self._cloud}/video/destroy",
                data=self._signed(params), timeout=(15, 60),
            )
        except requests.RequestException as exc:
            raise Hold("test media deletion response is uncertain; inspect account") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise Hold("test media deletion response is invalid") from exc
        require(response.status_code == 200 and isinstance(result, dict) and result.get("result") == "ok",
                "test media was not confirmed deleted; inspect account")


def _spec_and_manifest(repo, commit: str, episode: str) -> tuple[dict, dict]:
    root = episode_root(commit, episode)
    try:
        spec = yaml.safe_load(_blob(repo, commit, root + "short.yaml"))
    except yaml.YAMLError as exc:
        raise Hold("committed spec YAML is invalid") from exc
    manifest = read_object(_blob(repo, commit, root + "work/manifest.json"), "committed manifest")
    require(isinstance(spec, dict) and spec.get("id") == manifest.get("id") == episode,
            "committed spec or manifest identity differs")
    return spec, manifest


def _clean(value: object, label: str, limit: int) -> str:
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit and
            "<" not in value and ">" not in value and "\x00" not in value,
            f"{label} is missing, unsafe, or too long")
    return value.strip()


def _source_urls(spec: dict) -> list[str]:
    values = spec.get("sources", [])
    require(isinstance(values, list), "source URLs are malformed")
    urls = []
    for value in values:
        url = _clean(value, "source URL", 1000)
        parsed = urlparse(url)
        require(parsed.scheme == "https" and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None,
                "source URL is not a direct HTTPS source")
        if url not in urls:
            urls.append(url)
    require(bool(urls), "source URLs are missing")
    return urls


def copy_and_metadata(repo, commit: str, episode: str) -> tuple[dict, dict]:
    """Build native platform text only from the signed commit's spec and manifest."""
    spec, manifest = _spec_and_manifest(repo, commit, episode)
    title = _clean(spec.get("title"), "YouTube title", 100)
    description = _clean(spec.get("description"), "YouTube description", 3000)
    sources = _source_urls(spec)
    tags = spec.get("hashtags", [])
    require(isinstance(tags, list) and all(isinstance(tag, str) and
            re.fullmatch(r"#[A-Za-z0-9_]+", tag) for tag in tags), "hashtags are malformed")
    beats = manifest.get("beats")
    require(isinstance(beats, list), "manifest beats are missing")
    credits = []
    for beat in beats:
        require(isinstance(beat, dict) and isinstance(beat.get("asset"), dict),
                "manifest asset is malformed")
        asset = beat["asset"]
        if asset.get("source") in ("designed card", "AI generated", "generated gradient", "local file"):
            continue
        credits.append("- " + ", ".join(_clean(asset.get(field), f"asset {field}", 1000)
                                       for field in ("credit", "license", "url")))
    youtube_blocks = [description, "Sources:\n" + "\n".join(f"- {url}" for url in sources)]
    if credits:
        youtube_blocks.append("Images:\n" + "\n".join(dict.fromkeys(credits)))
    disclosure = "Researched and written by History's Last Hours. Narrated with a synthetic voice."
    youtube_blocks.extend((disclosure, " ".join(tags[:3])))
    youtube_text = "\n\n".join(block for block in youtube_blocks if block)
    require(len(youtube_text.encode("utf-8")) <= 5000, "YouTube text exceeds 5000 bytes")
    category = spec.get("category_id", "27")
    require(isinstance(category, str) and bool(re.fullmatch(r"[0-9]{1,3}", category)),
            "YouTube category is invalid")
    synthetic = spec.get("synthetic_media", False)
    require(type(synthetic) is bool, "synthetic media flag is invalid")
    metadata = {
        "youtube": {"title": title, "categoryId": category, "privacy": "public",
                    "madeForKids": False, "notifySubscribers": True,
                    "isAiGenerated": synthetic, "embeddable": True},
    }
    return {"youtube": youtube_text}, metadata


def _channel(channels: list[dict], wanted: str, service: str) -> None:
    matches = [item for item in channels if isinstance(item, dict) and item.get("id") == wanted]
    require(len(matches) == 1 and matches[0].get("service") == service and
            all(matches[0].get(flag) is False for flag in
                ("isDisconnected", "isLocked", "isQueuePaused")),
            f"pinned {service} channel is missing, wrong, or unavailable")


def _matching(posts: list[dict], text: str, media_url: str, api: BufferClient) -> dict | None:
    matches = [post for post in posts if post.get("text") == text]
    require(len(matches) <= 1, "multiple matching Buffer posts require inspection")
    if not matches:
        return None
    summary = matches[0]
    require(summary.get("status") in ("scheduled", "sent"),
            "matching Buffer post is in an unsafe state")
    remote = api.post(summary["id"])
    assets = remote.get("assets")
    require(isinstance(assets, list) and all(isinstance(asset, dict) for asset in assets),
            "matching Buffer post has no inspectable assets")
    videos = [asset.get("source") for asset in assets]
    require(remote.get("id") == summary["id"] and remote.get("text") == text and
            remote.get("status") == summary["status"] and videos == [media_url],
            "matching Buffer post has unverified text, status, or media")
    return remote


def _create_or_reconcile(api: BufferClient, channel: str, text: str,
                         metadata: dict, media_url: str, when: datetime,
                         posts: list[dict]) -> dict:
    existing = _matching(posts, text, media_url, api)
    if existing:
        return {"id": existing["id"], "status": existing["status"],
                "due_at": existing.get("dueAt"), "reconciled": True}
    queued = [post for post in posts if post.get("status") not in ("sent", "error", "draft")]
    require(len(queued) < QUEUE_LIMIT, "Buffer channel queue is full")
    require(all(post.get("dueAt") != when.isoformat() for post in queued),
            "requested Buffer slot is already occupied")
    payload = {"channelId": channel, "text": text, "schedulingType": "automatic",
               "mode": "customScheduled", "dueAt": when.isoformat(),
               "assets": [{"video": {"url": media_url}}], "metadata": metadata}
    post = api.create(payload)
    require(isinstance(post.get("id"), str) and bool(post["id"]) and
            post.get("status") == "scheduled" and
            utc(post.get("dueAt"), "Buffer accepted due time") == when,
            "Buffer accepted post has uncertain identity, state, or due time")
    return {"id": post["id"], "status": post["status"],
            "due_at": post["dueAt"], "reconciled": False}


def publish_reviewed(repo, commit: str, episode: str, review: dict,
                     public_raw_b64: str, config: dict, buffer_token: str,
                     cloudinary_url: str, due_at_utc: str, api: BufferClient | None = None,
                     media_client: CloudinaryClient | None = None) -> dict:
    """One serialized exact-media release; disabled by tracked policy and absent credentials."""
    plan = publisher_preflight(repo, commit, episode, review, public_raw_b64, config)
    require(bool(buffer_token), "control publisher credential is missing")
    require(bool(cloudinary_url), "independent control media credential is missing")
    due = utc(due_at_utc, "release due time")
    now = datetime.now(timezone.utc)
    require(now + MIN_LEAD < due <= now + MAX_HORIZON,
            "release due time is outside the safe scheduling window")
    source_video = fetch_video(plan["subject"]["media_url"], config)
    require(digest(source_video) == plan["subject"]["media_sha256"],
            "producer hosted media changed before control copy")
    media_client = media_client or CloudinaryClient(cloudinary_url, config["publisher_media_url_prefix"])
    media_url, media_public_id = media_client.upload_exact(
        source_video, episode, plan["subject"]["media_sha256"])
    require(media_public_id == f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{plan['subject']['media_sha256']}" and
            media_url == f"{config['publisher_media_url_prefix']}{media_public_id}.mp4" and
            media_url != plan["subject"]["media_url"] and
            digest(fetch_video(media_url, {"media_url_prefix": config["publisher_media_url_prefix"]})) ==
            plan["subject"]["media_sha256"],
            "control-owned hosted media differs from signed video")
    return _schedule_exact(repo, commit, episode, plan["subject"], config, buffer_token,
                           due, media_url, media_public_id, api)


def publish_draft_reviewed(repo, commit: str, episode: str, packet_dir, review: dict,
                           public_raw_b64: str, config: dict, buffer_token: str,
                           cloudinary_url: str, due_at_utc: str,
                           api: BufferClient | None = None,
                           media_client: CloudinaryClient | None = None) -> dict:
    """Release an approved v2 packet from its exact authenticated control asset."""
    from .intake import validated_packet

    require(config["publishing_enabled"] is True, "control publisher is disabled")
    require(bool(config["buffer_organization_id"]) and bool(config["youtube_channel_id"]),
            "pinned Buffer destination is missing")
    require(bool(config["publisher_media_url_prefix"]) and
            config["publisher_media_url_prefix"] != config["media_url_prefix"],
            "independent publisher media account is missing")
    subject, packet_video = validated_packet(repo, commit, episode, config, packet_dir)
    verify(review, subject, public_raw_b64, config)
    require(review["version"] == 2, "private draft requires a version 2 approval")
    require(bool(buffer_token), "control publisher credential is missing")
    require(bool(cloudinary_url), "independent control media credential is missing")
    due = utc(due_at_utc, "release due time")
    now = datetime.now(timezone.utc)
    require(now + MIN_LEAD < due <= now + MAX_HORIZON,
            "release due time is outside the safe scheduling window")
    media_client = media_client or CloudinaryClient(cloudinary_url, config["publisher_media_url_prefix"])
    private_video = media_client.download_private_asset(subject["media_asset_id"])
    require(private_video == packet_video and digest(private_video) == subject["media_sha256"],
            "authenticated control asset differs from signed draft")
    media_url, media_public_id = media_client.upload_exact(
        private_video, episode, subject["media_sha256"])
    require(media_public_id == f"{PUBLISHER_PUBLIC_ID_PREFIX}{episode}-{subject['media_sha256']}" and
            media_url == f"{config['publisher_media_url_prefix']}{media_public_id}.mp4" and
            media_url != subject["media_url"] and
            digest(fetch_video(media_url, {"media_url_prefix": config["publisher_media_url_prefix"]})) ==
            subject["media_sha256"],
            "approved public copy differs from authenticated draft")
    return _schedule_exact(repo, commit, episode, subject, config, buffer_token,
                           due, media_url, media_public_id, api)


def _schedule_exact(repo, commit: str, episode: str, subject: dict, config: dict,
                    buffer_token: str, due: datetime, media_url: str,
                    media_public_id: str, api: BufferClient | None) -> dict:
    api = api or BufferClient(buffer_token)
    org = config["buffer_organization_id"]
    api.organization(org)
    channels = api.channels(org)
    _channel(channels, config["youtube_channel_id"], "youtube")
    copy, metadata = copy_and_metadata(repo, commit, episode)
    youtube_posts = api.posts(org, config["youtube_channel_id"])
    youtube_existing = _matching(youtube_posts, copy["youtube"], media_url, api)
    if youtube_existing:
        require(utc(youtube_existing.get("dueAt"), "existing YouTube due time") == due,
                "existing YouTube post has a different due time")
    if not youtube_existing:
        require(sum(post.get("status") not in ("sent", "error", "draft") for post in youtube_posts) < QUEUE_LIMIT,
                "YouTube Buffer queue is full")
    if not youtube_existing:
        require(digest(fetch_video(media_url, {"media_url_prefix": config["publisher_media_url_prefix"]})) ==
                subject["media_sha256"],
                "control-owned media changed before Buffer mutation")
    youtube = _create_or_reconcile(api, config["youtube_channel_id"],
                                   copy["youtube"], {"youtube": metadata["youtube"]},
                                   media_url, due, youtube_posts)
    return {"episode": episode, "media_sha256": subject["media_sha256"],
            "source_commit": commit, "control_media_url": media_url,
            "control_media_public_id": media_public_id, "youtube": youtube}
