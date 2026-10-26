import json
import sys
from datetime import datetime, timedelta, timezone

from control import atlas as control

STUB_PUBLISH = """
def posts(since=None):
    return []
def delete_post(post_id):
    raise AssertionError("nothing is on Buffer")
"""

STUB_ATLAS = """
def title_problems(ep, ds):
    return []
def schedule(*a, **k):
    raise AssertionError("direct Shorts never go to Buffer")
"""


def _producer(tmp_path, n):
    src = tmp_path / "producer" / "pipeline" / "src" / "ytc"
    (src / "atlas").mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "publish.py").write_text(STUB_PUBLISH)
    (src / "atlas" / "__init__.py").write_text("")
    (src / "atlas" / "publish.py").write_text(STUB_ATLAS)
    (src / "atlas" / "receipt.py").write_text(
        'FORMAT = "cinema-v2"\ndef digest(p):\n    return "qa"\n'
        'def verify(folder, sha):\n    return {"version": 1, "checked_at": "t", "claims_sha256": "c", '
        '"quality_module_sha256": "q"}\n')
    (src / "atlas" / "data.py").write_text("class Dataset:\n    @staticmethod\n    def load(p):\n        return Dataset()\n")
    (src / "atlas" / "episode.py").write_text(
        "class AtlasEpisode:\n    @staticmethod\n    def load(p):\n        e = AtlasEpisode()\n"
        "        e.id = p.parent.name\n        e.title = 'Is YOUR Country Here?'\n        return e\n")
    for i in range(n):
        folder = tmp_path / "producer" / "content" / "atlas" / f"atlas{200 + i}"
        folder.mkdir(parents=True)
        (folder / "qa.json").write_text("{}")
        (folder / "atlas.yaml").write_text("id: x\n")
        (folder / "ready.json").write_text(json.dumps({
            "id": folder.name, "modal_volume": control.VOLUME, "modal_path": "p", "media_sha256": "m",
            "qa_sha256": "qa", "format": "cinema-v2", "look": "versus" if i % 2 else "map_reveal"}))
    return tmp_path / "producer"


def _setup(tmp_path, monkeypatch, n, state, direct=True):
    here = tmp_path / "atlas"
    here.mkdir()
    for name in ("STATE", "HEALTH", "RETRIES", "HOLD", "WITHDRAWN", "DIRECT"):
        monkeypatch.setattr(control, name, here / getattr(control, name).name)
    if direct:
        control.DIRECT.write_text("{}")
    control.STATE.write_text(json.dumps(state))
    monkeypatch.setattr(control, "_video", lambda path, sha: (_ for _ in ()).throw(AssertionError("no download")))
    monkeypatch.setattr(control, "_title_held", lambda ep, ds, check=None: [])
    calls = []

    def upload(folder, ready, taken):
        assert "atlas.yaml" in [p.name for p in folder.iterdir()]
        when = max(taken + [datetime.now(timezone.utc)]) + timedelta(hours=5)
        calls.append(folder.name)
        return {"id": folder.name, "title": "t", "route": "youtube", "youtube_video_id": "v" + folder.name,
                "due_at": when.isoformat(), "status": "scheduled", "uploaded_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(control, "_upload", upload)
    producer = _producer(tmp_path, n)
    for mod in [m for m in sys.modules if m == "ytc" or m.startswith("ytc.")]:
        monkeypatch.delitem(sys.modules, mod)
    return producer, calls


def test_direct_uploads_fill_a_week_ahead_within_the_daily_quota(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    producer, calls = _setup(tmp_path, monkeypatch, 8, {})

    assert control.main([str(producer)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["route"] == "youtube" and len(report["scheduled"]) == control.DAILY_UPLOADS == len(calls)
    state = json.loads(control.STATE.read_text())
    assert all(r["route"] == "youtube" and r["look"] for r in state.values())
    dues = sorted(datetime.fromisoformat(r["due_at"]) for r in state.values())
    assert dues[0] > now and len(set(dues)) == len(dues)


def test_a_week_already_waiting_uploads_nothing_and_a_live_upload_is_confirmed(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=3)).isoformat()
    state = {f"atlas{100 + i}": {"route": "youtube", "title": "t", "youtube_url": "u", "uploaded_at": old,
                                 "due_at": (now + timedelta(hours=8 * (i + 1))).isoformat()}
             for i in range(control.AHEAD_DIRECT)}
    state["atlas099"] = {"route": "youtube", "title": "t", "youtube_url": "u", "uploaded_at": old,
                         "due_at": (now - timedelta(hours=1)).isoformat()}
    producer, calls = _setup(tmp_path, monkeypatch, 3, state)
    monkeypatch.setattr(control, "confirm_delivery", lambda record: {"confirmed": True})

    assert control.main([str(producer)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["scheduled"] == [] == calls and report["future_posts"] == control.AHEAD_DIRECT
    after = json.loads(control.STATE.read_text())
    assert after["atlas099"]["delivery"]["confirmed"] and after["atlas099"]["status"] == "sent"


def test_a_hold_unschedules_direct_uploads(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    state = {"atlas150": {"route": "youtube", "title": "t", "youtube_video_id": "vf",
                          "due_at": (now + timedelta(hours=5)).isoformat()},
             "atlas149": {"route": "youtube", "title": "t", "youtube_video_id": "vp", "youtube_url": "u",
                          "due_at": (now - timedelta(hours=5)).isoformat(), "delivery": {"confirmed": True}}}
    producer, calls = _setup(tmp_path, monkeypatch, 2, state)
    control.HOLD.write_text(json.dumps({"reason": "review"}))
    asked = []
    monkeypatch.setattr(control, "_withdraw", lambda ids: asked.extend(ids) or ids)

    assert control.main([str(producer)]) == 0

    assert asked == ["vf"] and calls == []
    assert list(json.loads(control.STATE.read_text())) == ["atlas149"]
    assert json.loads(control.WITHDRAWN.read_text())["atlas150"]["status"] == "withdrawn"


def test_without_the_flag_shorts_stay_on_buffer(tmp_path, monkeypatch, capsys):
    producer, calls = _setup(tmp_path, monkeypatch, 2, {}, direct=False)
    control.main([str(producer)])
    report = json.loads(capsys.readouterr().out)
    assert report["route"] == "buffer" and calls == [] and len(report["failed"]) == control.AHEAD
