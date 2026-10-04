"""Dormant control-owned Buffer publisher. No producer code or credentials are imported."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import yaml

from .release import Hold, _blob, digest, episode_root, fetch_video, publisher_preflight, read_object, require, utc

BUFFER_API = "https://api.buffer.com"
QUEUE_LIMIT = 10
MIN_LEAD = timedelta(minutes=30)
MAX_HORIZON = timedelta(days=30)

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
                     due_at_utc: str, api: BufferClient | None = None) -> dict:
    """One serialized exact-media release; disabled by tracked policy and absent credentials."""
    plan = publisher_preflight(repo, commit, episode, review, public_raw_b64, config)
    require(bool(buffer_token), "control publisher credential is missing")
    due = utc(due_at_utc, "release due time")
    now = datetime.now(timezone.utc)
    require(now + MIN_LEAD < due <= now + MAX_HORIZON,
            "release due time is outside the safe scheduling window")
    api = api or BufferClient(buffer_token)
    org = config["buffer_organization_id"]
    api.organization(org)
    channels = api.channels(org)
    _channel(channels, config["youtube_channel_id"], "youtube")
    copy, metadata = copy_and_metadata(repo, commit, episode)
    youtube_posts = api.posts(org, config["youtube_channel_id"])
    youtube_existing = _matching(youtube_posts, copy["youtube"], plan["subject"]["media_url"], api)
    if youtube_existing:
        require(utc(youtube_existing.get("dueAt"), "existing YouTube due time") == due,
                "existing YouTube post has a different due time")
    if not youtube_existing:
        require(sum(post.get("status") not in ("sent", "error", "draft") for post in youtube_posts) < QUEUE_LIMIT,
                "YouTube Buffer queue is full")
    if not youtube_existing:
        require(digest(fetch_video(plan["subject"]["media_url"], config)) ==
                plan["subject"]["media_sha256"],
                "hosted media changed before Buffer mutation")
    youtube = _create_or_reconcile(api, config["youtube_channel_id"],
                                   copy["youtube"], {"youtube": metadata["youtube"]},
                                   plan["subject"]["media_url"], due, youtube_posts)
    return {"episode": episode, "media_sha256": plan["subject"]["media_sha256"],
            "source_commit": commit, "youtube": youtube}
