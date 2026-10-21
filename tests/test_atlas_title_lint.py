"""The publisher's own title check (control/atlas.py title_problems): the plainest of the producer's rules, run on the
real titles that went out on Atlas Shorts before 2026-10-22 (revenue-push gap audit #9)."""

import pytest

from control import atlas as control

HELD = [
    ("atlas030", "places Where One City Holds 92% of City Dwellers", "", "", "lowercase"),
    ("atlas028", "61 places Still Have Most People Outside Cities", "", "", "lowercase"),
    ("atlas019", "The Emptiest and Most Crowded places", "", "", "lowercase"),
    ("atlas017", "The Youngest and Oldest places on Earth", "", "", "lowercase"),
    ("atlas002", "45 places with Falling Populations", "", "", "lowercase"),
    ("atlas004", "47 places Where Life Expectancy Reaches 80", "", "", "lowercase"),
    ("atlas021", "Cuba has 9.5 doctors while Niger has 0.0", "Physicians (per 1,000 people)", "SH.MED.PHYS.ZS",
     "reads as nothing"),
    ("atlas053", "Why Greece Has 165.1% College Enrollment", "School enrollment, tertiary (% gross)", "SE.TER.ENRR",
     "over 100%"),
]


@pytest.mark.parametrize("episode_id,title,label,indicator,expected", HELD, ids=[h[0] for h in HELD])
def test_the_published_bad_titles_are_held(episode_id, title, label, indicator, expected):
    assert any(expected in p for p in control.title_problems(title, label, indicator))


@pytest.mark.parametrize("title", [
    "Places Where One City Holds 92% of City Dwellers",
    "47 Places Where Life Expectancy Reaches 80",
    "Cuba Has 9.5 Doctors per 1,000. Does YOUR Country?",
    "Where More Boys Are Born",
    "Is YOUR Country One of the Most Crowded?",
    "Places With 0 Volcanoes",
])
def test_ordinary_titles_pass(title):
    assert control.title_problems(title, "Physicians (per 1,000 people)", "SH.MED.PHYS.ZS") == []


def test_never_stricter_than_the_producer():
    # Two decimals and a table-row ending are the producer's calls (writer.title_lint), not this copy's.
    assert control.title_problems("Where More Boys Are Born: 1.01 to 1.11 Worldwide") == []
    assert control.title_problems("Bulgaria Has 93.8 Bank Branches to DR Congo 0.6") == []
    # The producer's title_case keeps unit symbols after a number, words with a capital inside and small words
    # lowercase, so writer.title_lint passes these (review 2026-10-22); a Short held here forever would be stuck.
    assert control.title_problems("3,000 mm of Rain a Year in Colombia") == []
    assert control.title_problems("eSwatini Has the Most HIV") == []
    assert control.title_problems("Iceland Uses 16,248 kg of Energy Per Person") == []
    assert control.title_problems("100% of Adults in Iceland Have a Bank Account") == []
    # Over 100% is fine signed, for a growth rate, a gross ratio said as gross, or data with falls below -100.
    assert control.title_problems("Where Money Fell -512%") == []
    assert control.title_problems("Where Prices Rose 512%", indicator="FP.CPI.TOTL.ZG") == []
    assert control.title_problems("Greece's Gross Enrollment Is 165%", "School enrollment, tertiary (% gross)") == []
    assert control.title_problems("Where It Moved 512%", values=[-512.0, 3.0]) == []
    assert control.title_problems("Where Prices Rose 512%")


def test_decimals_codes_and_brackets():
    assert any("2 decimals" in p for p in control.title_problems("Places With 0.123 Doctors"))
    assert control.title_problems("Where SH.MED.PHYS.ZS Is Highest")
    assert control.title_problems("Doctors (per 1,000 People) Around the World")


def test_a_broken_check_never_stops_a_publish():
    class Ep:
        id, title = "atlas900", "Fine Title"
    class Ds:
        label, indicator, values = "x", "y", {"A": "not a number"}
    assert control._title_held(Ep(), Ds()) == []
    assert control._title_held(Ep(), Ds(), lambda ep, ds: 1 / 0) == []


def test_a_title_held_only_by_the_producer_is_held_not_failed():
    class Ep:
        id, title = "atlas075", "Bulgaria Has 93.8 Bank Branches to DR Congo 0.6"
    class Ds:
        label, indicator, values = "x", "y", {}
    # Two decimals and a table-row ending are the producer's rules; schedule would raise on them (an error).
    assert control.title_problems(Ep.title) == []
    assert control._title_held(Ep(), Ds(), lambda ep, ds: ["ends on a bare value"]) == ["ends on a bare value"]


def test_a_held_title_is_skipped_not_failed_and_the_next_short_still_posts(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from tests.test_atlas_hold import _producer
    here = tmp_path / "atlas"
    here.mkdir()
    for name in ("STATE", "HEALTH", "RETRIES", "HOLD", "WITHDRAWN"):
        monkeypatch.setattr(control, name, here / getattr(control, name).name)
    monkeypatch.setenv("ATLAS_TEST_POSTS", "[]")
    monkeypatch.setenv("ATLAS_TEST_LOG", str(tmp_path / "deleted.txt"))
    producer = _producer(tmp_path)
    atlas = producer / "pipeline" / "src" / "ytc" / "atlas"
    (atlas / "receipt.py").write_text('FORMAT = "v3"\ndef digest(p): return "qa"\ndef verify(f, s): return {}\n')
    (atlas / "episode.py").write_text(
        "import json\nclass AtlasEpisode:\n    @classmethod\n    def load(cls, p):\n"
        "        e = cls(); e.id, e.title = p.parent.name, json.loads(p.read_text())['title']; return e\n")
    (atlas / "data.json").write_text("")
    (atlas / "data.py").write_text(
        "class Dataset:\n    label, indicator, values = 'Physicians (per 1,000 people)', 'SH.MED.PHYS.ZS', {'NER': 0.04}\n"
        "    @classmethod\n    def load(cls, p): return cls()\n")
    (atlas / "publish.py").write_text(
        "def schedule(ep, folder, ds, video):\n"
        "    return {'id': ep.id, 'title': ep.title, 'due_at': '2026-10-23T12:00:00-04:00', 'buffer_post_id': 'p'}\n")
    content = producer / "content" / "atlas"
    for name, title in (("atlas009", "Cuba has 9.5 doctors while Niger has 0.0"),
                        ("atlas010", "Cuba Has 9.5 Doctors per 1,000. Does YOUR Country?")):
        (content / name).mkdir(exist_ok=True)
        (content / name / "ready.json").write_text(json.dumps({
            "id": name, "format": "v3", "modal_volume": control.VOLUME, "qa_sha256": "qa", "media_sha256": "m",
            "modal_path": "x"}))
        (content / name / "atlas.yaml").write_text(json.dumps({"title": title}))
    monkeypatch.setattr(control, "_video", lambda path, sha: b"mp4")
    monkeypatch.setattr(control, "quality_record", lambda ready, accepted: {})
    for mod in [m for m in sys.modules if m == "ytc" or m.startswith("ytc.")]:
        monkeypatch.delitem(sys.modules, mod)

    control.main([str(producer)])

    report = json.loads(capsys.readouterr().out)
    assert [h["id"] for h in report["title_held"]] == ["atlas009"] and report["failed"] == []
    assert [s["id"] for s in report["scheduled"]] == ["atlas010"]
    health = json.loads(control.HEALTH.read_text())
    assert health["title_held"] == ["atlas009"] and health["errors"] == []
