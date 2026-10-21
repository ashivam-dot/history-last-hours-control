"""Atlas in Numbers publisher: schedule the producer's parked Shorts on Buffer with keys only this job holds.

The producer (ashivam-dot/creature-receipts, `ytc.atlas.pipeline`) renders each Short on Modal, parks the MP4 in the
creature-receipts-outbox volume and commits content/atlas/<id>/ready.json with its hash. This job reads those
records from a checkout of the producer, fetches the exact bytes, and calls the producer's own `ytc.atlas.publish`
to host and schedule them. Each post is recorded in atlas/published.json, which the producer copies back, and its
status is refreshed from Buffer on every run. A post Buffer failed to send is deleted and scheduled again, at most
MAX_RETRIES times. atlas/health.json tells the producer's watchdog how the last run went.

The run fails (and GitHub emails the owner) when nothing is queued and nothing is waiting: the producer has stopped.

Usage: python -m control.atlas PRODUCER_CHECKOUT [--list]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "atlas"
STATE = HERE / "published.json"
HEALTH = HERE / "health.json"
RETRIES = HERE / "retries.json"
# While hold.json ({"reason": ...}) exists, queued Atlas posts are taken off Buffer into withdrawn.json and nothing new
# is scheduled. A withdrawn Short is never scheduled again by this job.
HOLD = HERE / "hold.json"
WITHDRAWN = HERE / "withdrawn.json"
WORKSPACE = "aksha-shivam18"
VOLUME = "creature-receipts-outbox"
# Future Atlas posts kept in Buffer: a day of the producer's SLOTS (12:00 and 19:00 ET from 2026-10-22), so a
# missed run costs no post.
AHEAD = 2
MAX_RETRIES = 2
MAX_BYTES = 200 * 1024 * 1024


# Titles a viewer reads as an error, which went out on Atlas Shorts before 2026-10-22 (revenue-push gap audit #9):
# "places Where One City Holds 92% of City Dwellers", "Cuba has 9.5 doctors while Niger has 0.0", "Why Greece Has
# 165.1% College Enrollment". The producer's writer.title_lint and publish.schedule hold these and more; this copy of
# the plainest rules runs here first, so a producer regression can't ship one. It is never stricter than the producer:
# a percentage over 100 passes when signed ("-512%"), for a growth rate (.ZG), for a gross ratio the title calls
# gross, or when the data has values below -100 (said as a fall).
_ZERO = re.compile(r"(?<![\w.])0\.0+(?![\d.]*\d)")
_DECIMALS = re.compile(r"(?<![\d.])\d[\d,]*\.\d{3,}(?!\d)")
_PERCENT = re.compile(r"((?<![\w.,])-|\u2212|\bminus\s+)?(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_CODE = re.compile(r"\b[A-Z][A-Z0-9]+(?:\.[A-Z0-9]{2,}){2,}\b")
_BRACKETS = re.compile(r"[(\[][^)\]]*[)\]]")
# The producer's writer._SMALL and writer._TITLE_UNITS: words its title_case leaves lowercase.
_SMALL = frozenset("a an and as at but by for in nor of on or per the to vs via".split())
_UNITS = frozenset("m km cm mm kg g t ha l kwh mwh gwh twh mbps kbps gbps mb gb".split())
_FIRST_WORD = re.compile(r"[^A-Za-z]*([A-Za-z][A-Za-z'\u2019]*)")


def title_problems(title: str, label: str = "", indicator: str = "", values=()) -> list[str]:
    out = []
    first = _FIRST_WORD.match(title)
    word = first.group(1) if first else ""
    # Only a first word the producer's title_case would capitalise: not a small word ("100% of Adults"), a unit
    # symbol ("3,000 mm of Rain") or a word with a capital inside it ("eSwatini", "kWh").
    lower_first = (word[:1].islower() and word not in _SMALL and word not in _UNITS
                   and not any(c.isupper() for c in word[1:]))
    if lower_first or re.search(r"\b(places|countries)\b", title):
        out.append("starts with a lowercase word or has a lowercase 'places'")
    if zero := _ZERO.search(title):
        out.append(f"'{zero.group(0)}' reads as nothing")
    if decimals := _DECIMALS.search(title):
        out.append(f"'{decimals.group(0)}' has more than 2 decimals")
    gross = re.search(r"\bgross\b", label or "", re.I) and re.search(r"\bgross\b", title, re.I)
    exempt = gross or (indicator or "").upper().endswith(".ZG") or any(v < -100 for v in values)
    for m in _PERCENT.finditer(title):
        if not m.group(1) and not exempt and float(m.group(2).replace(",", "")) > 100:
            out.append(f"'{m.group(0)}' is over 100%")
    if _CODE.search(title) or _BRACKETS.search(title):
        out.append("quotes an indicator code or a unit in brackets")
    return out


def _title_held(ep, ds, producer_check=None) -> list[str]:
    """title_problems of a parked Short, then the producer's own check (ytc.atlas.publish.title_problems, which
    publish.schedule raises on), so a title held only by a producer rule is held here too rather than failed. A check
    that itself breaks never stops a publish (publish.schedule still runs the producer's full lint)."""
    try:
        if held := title_problems(ep.title, getattr(ds, "label", "") or "", getattr(ds, "indicator", "") or "",
                                  [float(v) for v in (getattr(ds, "values", None) or {}).values()]):
            return held
        return list(producer_check(ep, ds)) if callable(producer_check) else []
    except Exception as err:
        print(f"atlas title lint skipped for {getattr(ep, 'id', '?')}: {type(err).__name__}", file=sys.stderr)
        return []


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _video(path: str, sha: str) -> bytes:
    import modal

    if modal.Workspace.from_context().hydrate().name != WORKSPACE:
        raise RuntimeError("the Modal token belongs to a different workspace")
    volume = modal.Volume.from_name(VOLUME, create_if_missing=False)
    data = bytearray()
    for chunk in volume.read_file(path):
        data += chunk
        if len(data) > MAX_BYTES:
            raise RuntimeError(f"{path} is larger than {MAX_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != sha or data[4:8] != b"ftyp":
        raise RuntimeError(f"{path} differs from the producer's record")
    return bytes(data)


def quality_record(ready: dict, accepted: dict) -> dict:
    return {"qa_sha256": ready["qa_sha256"], "version": accepted["version"],
            "checked_at": accepted["checked_at"], "claims_sha256": accepted["claims_sha256"],
            "quality_module_sha256": accepted["quality_module_sha256"]}


def confirm_delivery(record: dict, getter=None) -> dict:
    """Confirm the reported video through YouTube's official oEmbed API, without website scraping.

    oEmbed does not establish public vs unlisted visibility or processing status; record that limit explicitly.
    """
    import re
    import requests
    getter = getter or requests.get
    url = record.get("youtube_url") or ""
    match = re.fullmatch(r"https://(?:www\.)?youtube\.com/(?:shorts/([\w-]{11})|watch\?v=([\w-]{11}))", url)
    if not match:
        return {"confirmed": False, "reason": "Buffer has no valid exact YouTube video URL"}
    video_id = match.group(1) or match.group(2)
    try:
        response = getter("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}, timeout=20)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {"confirmed": False, "video_id": video_id, "reason": "YouTube oEmbed not yet available"}
    expected_author = "https://www.youtube.com/@atlasinnumbers"
    if data.get("title") != record.get("title") or data.get("author_url", "").rstrip("/").lower() != expected_author:
        return {"confirmed": False, "video_id": video_id, "reason": "YouTube title/channel identity mismatch"}
    return {"confirmed": True, "video_id": video_id, "checked_at": datetime.now(timezone.utc).isoformat(),
            "method": "official-oembed", "visibility": "public-or-unlisted; authenticated API needed to distinguish"}


def ordered(waiting: list[Path], atlas, state: dict, producer: Path) -> list[Path]:
    """Waiting Shorts in the producer's order (`ytc.atlas.publish.order`: best score first, no family twice in a
    row, none left stale), after the post scheduled last; folder-name order with a producer that has no order."""
    order = getattr(atlas, "order", None)
    if order is None:
        return waiting
    def due(key: str) -> datetime:
        # Compared as instants, not strings: "-04:00" and "-05:00" (or "Z") stamps sort wrong as text.
        try:
            when = datetime.fromisoformat(state[key]["due_at"])
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    last = max(state, key=due, default=None)
    try:
        return order(waiting, producer / "content" / "atlas" / last if last else None)
    except Exception as err:  # ordering is a preference; never let it stop a post
        print(f"atlas order failed, using name order: {type(err).__name__}: {err}", file=sys.stderr)
        return waiting


def main(argv: list[str]) -> int:
    producer = Path(argv[0]).resolve()
    sys.path.insert(0, str(producer / "pipeline" / "src"))
    from ytc import publish as pub
    from ytc.atlas import publish as atlas
    from ytc.atlas import receipt as quality
    from ytc.atlas.data import Dataset
    from ytc.atlas.episode import AtlasEpisode

    now = datetime.now(timezone.utc)
    posts = {p["id"]: p for p in pub.posts(since=now - timedelta(days=30))}
    queue = [p for p in posts.values() if p["status"] not in ("sent", "error", "draft")]
    future = [p for p in queue if p.get("dueAt") and datetime.fromisoformat(p["dueAt"]) > now]
    report: dict = {"queue": [{"id": p["id"], "status": p["status"], "due": p.get("dueAt"),
                               "text": (p.get("text") or "")[:70]} for p in queue]}
    if "--list" in argv:
        print(json.dumps(report, indent=2))
        return 0

    state, retries = _load(STATE), _load(RETRIES)
    errors = []
    for episode_id, record in list(state.items()):
        post = posts.get(record.get("buffer_post_id"))
        if post is None:
            continue
        record.update({"status": post["status"], "youtube_url": post.get("externalLink") or record.get("youtube_url")})
        if post.get("sentAt"):
            record["sent_at"] = post["sentAt"]
        if post["status"] == "sent" and not record.get("delivery", {}).get("confirmed"):
            record["delivery"] = confirm_delivery(record)
            sent_at = record.get("sent_at")
            if sent_at and not record["delivery"]["confirmed"] and now - datetime.fromisoformat(sent_at.replace("Z", "+00:00")) > timedelta(hours=4):
                errors.append({"id": episode_id, "error": "Sent post remains unconfirmed on YouTube: " + record["delivery"]["reason"]})
        if post["status"] == "error":
            message = (post.get("error") or {}).get("message") or "unknown"
            if retries.get(episode_id, 0) < MAX_RETRIES:
                pub.delete_post(post["id"])
                retries[episode_id] = retries.get(episode_id, 0) + 1
                del state[episode_id]
            else:
                errors.append({"id": episode_id, "error": message})
    _save(STATE, state)
    _save(RETRIES, retries)

    withdrawn = _load(WITHDRAWN)
    if HOLD.exists():
        reason = _load(HOLD).get("reason") or "held"
        report["withdrawn"] = []
        for episode_id, record in list(state.items()):
            post = posts.get(record.get("buffer_post_id"))
            if not post or post["status"] not in ("scheduled", "pending") or not post.get("dueAt"):
                continue
            if datetime.fromisoformat(post["dueAt"]) <= now:
                continue
            pub.delete_post(post["id"])
            withdrawn[episode_id] = record | {"status": "withdrawn", "withdrawn_at": now.isoformat(), "reason": reason}
            del state[episode_id]
            _save(STATE, state)
            _save(WITHDRAWN, withdrawn)
            report["withdrawn"].append({"id": episode_id, "title": record.get("title"), "due_at": record.get("due_at")})
        _save(HEALTH, {"checked_at": now.isoformat(timespec="seconds"), "future_posts": 0, "waiting": 0,
                       "errors": errors, "problem": "", "held": reason})
        print(json.dumps(report | {"held": reason, "errors": errors}, indent=2))
        return 1 if errors else 0

    report["replaced"] = []
    for episode_id, record in list(state.items()):
        folder = producer / "content" / "atlas" / episode_id
        post = posts.get(record.get("buffer_post_id"))
        if not post or post["status"] not in ("scheduled", "pending") or not (folder / "qa.json").exists():
            continue
        try:
            ready = _load(folder / "ready.json")
            if record.get("quality", {}).get("qa_sha256") == ready.get("qa_sha256"):
                continue
            if ready.get("id") != episode_id or ready.get("modal_volume") != VOLUME:
                raise RuntimeError("replacement ready.json identity mismatch")
            if quality.digest(folder / "qa.json") != ready.get("qa_sha256"):
                raise RuntimeError("replacement lacks a bound QA receipt")
            accepted = quality.verify(folder, ready["media_sha256"])
            ep, ds = AtlasEpisode.load(folder / "atlas.yaml"), Dataset.load(folder / "data.json")
            with tempfile.TemporaryDirectory() as tmp:
                video = Path(tmp) / "short.mp4"
                video.write_bytes(_video(ready["modal_path"], ready["media_sha256"]))
                updated = atlas.replace_queued(ep, Path(tmp), ds, video, record)
            state[episode_id] = updated | {"media_sha256": ready["media_sha256"],
                "quality": quality_record(ready, accepted), "replaced_from": {
                    "media_sha256": record.get("media_sha256"), "title": record["title"],
                    "replaced_at": now.isoformat()}}
            _save(STATE, state)
            report["replaced"].append({"id": episode_id, "title": updated["title"], "due_at": updated["due_at"]})
        except Exception as err:
            errors.append({"id": episode_id, "error": "checked queued replacement: " + str(err)[:300]})

    waiting = [p.parent for p in sorted((producer / "content" / "atlas").glob("atlas*/ready.json"))
               if p.parent.name not in state and p.parent.name not in withdrawn]
    waiting = ordered(waiting, atlas, state, producer)
    report |= {"future_posts": len(future), "waiting": [w.name for w in waiting], "scheduled": [], "failed": [],
               "old_format": [], "title_held": []}
    for folder in waiting:
        if len(report["scheduled"]) >= max(AHEAD - len(future), 0):
            break
        try:
            ready = json.loads((folder / "ready.json").read_text(encoding="utf-8"))
            # The producer re-renders a Short in an older visual format before it may post; skip it until then.
            if ready.get("format") != getattr(quality, "FORMAT", None):
                report["old_format"].append(folder.name)
                continue
            if ready["id"] != folder.name or ready["modal_volume"] != VOLUME:
                raise RuntimeError("ready.json doesn't match its folder")
            if quality.digest(folder / "qa.json") != ready.get("qa_sha256"):
                raise RuntimeError("ready record does not bind the quality receipt")
            accepted = quality.verify(folder, ready["media_sha256"])
            ep = AtlasEpisode.load(folder / "atlas.yaml")
            ds = Dataset.load(folder / "data.json")
            # Held, not failed: the producer doesn't count a held title as reserve and re-makes the Short.
            if held := _title_held(ep, ds, getattr(atlas, "title_problems", None)):
                report["title_held"].append({"id": folder.name, "title": ep.title, "problems": held})
                continue
            with tempfile.TemporaryDirectory() as tmp:
                video = Path(tmp) / "short.mp4"
                video.write_bytes(_video(ready["modal_path"], ready["media_sha256"]))
                record = atlas.schedule(ep, Path(tmp), ds, video)
        except Exception as err:  # one bad Short mustn't stop the rest
            report["failed"].append({"id": folder.name, "error": f"{type(err).__name__}: {str(err)[:300]}"})
            continue
        state[folder.name] = record | {"media_sha256": ready["media_sha256"], "quality": quality_record(ready, accepted)}
        _save(STATE, state)
        report["scheduled"].append({"id": folder.name, "title": record["title"], "due_at": record["due_at"]})

    starving = not future and not report["scheduled"]
    problem = ("Nothing is queued in Buffer and the producer has no Short waiting: check Modal atlas_run."
               if starving else "")
    if starving and report["title_held"]:
        problem = ("Nothing is queued in Buffer; the waiting Shorts' titles are held ("
                   + ", ".join(h["id"] for h in report["title_held"]) + "): the producer re-makes them.")
    errors += report["failed"]
    _save(HEALTH, {"checked_at": now.isoformat(timespec="seconds"), "future_posts": len(future) + len(report["scheduled"]),
                   "waiting": len(waiting) - len(report["scheduled"]), "errors": errors, "problem": problem,
                   "title_held": [h["id"] for h in report["title_held"]]})
    if report["title_held"]:
        print("atlas title lint: held " + ", ".join(f"{h['id']} ({'; '.join(h['problems'])})"
                                                   for h in report["title_held"]), file=sys.stderr)
    print(json.dumps(report | {"problem": problem, "errors": errors}, indent=2))
    return 1 if starving or errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
