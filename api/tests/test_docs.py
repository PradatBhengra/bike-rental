import pathlib
import re

import pytest

from app import db
from app.docs import chunking, ingest, search
from tests.conftest import auth, rows

CORPUS = pathlib.Path(__file__).parents[2] / "corpus"


def test_split_sections_by_numbered_headings():
    md = """# Terms

Intro line.

## 4 Cancellations

### 4.2 Refunds

Money back.

```
## 9 Not a heading inside a fence
```

## 5 Fees
Pay up.
"""
    sections = chunking.split_sections(md, "Terms")
    assert [(s["section_no"], s["anchor"]) for s in sections] == [("0", "s0"), ("4.2", "s4-2"), ("5", "s5")]
    assert sections[1]["section_path"] == "Terms > 4 Cancellations > 4.2 Refunds"
    assert "## 9 Not a heading" in sections[1]["body"]  # the fence kept it as text


def test_long_section_splits_with_overlap():
    paragraphs = [" ".join(f"w{p}x{i}" for i in range(120)) for p in range(4)]
    parts = chunking.pieces("\n\n".join(paragraphs))
    assert len(parts) == 3
    assert parts[1].startswith(paragraphs[1]) and parts[2].startswith(paragraphs[2])  # each starts with the last paragraph before


def test_rrf_rewards_agreement():
    assert search.rrf([[1, 2, 3], [3, 1, 4]]) == [1, 3, 2, 4]


@pytest.fixture
def corpus():
    with db.tx() as conn:
        for path in sorted(CORPUS.glob("*.md")):
            ingest.load_file(conn, path)


def test_whole_corpus_indexes(corpus):
    assert rows("SELECT count(*) AS n FROM document WHERE active_version_id IS NOT NULL")[0]["n"] == len(list(CORPUS.glob("*.md")))
    assert rows("SELECT version FROM corpus_state")[0]["version"] == len(list(CORPUS.glob("*.md")))
    with db.tx() as conn:  # loading again changes nothing
        assert not any(ingest.load_file(conn, p) for p in CORPUS.glob("*.md"))


def test_hybrid_search_finds_the_refund_rule(corpus):
    with db.tx() as conn:
        found = search.search(conn, "What refund do I get if I cancel 5 hours before the start?")
    top = found["results"][0]
    assert (top["slug"], top["anchor"]) == ("cancellations-refunds", "s2-1")
    assert len(found["results"]) == 5 and found["keyword_ids"] and found["vector_ids"]


def test_doc_type_filter(corpus):
    with db.tx() as conn:
        found = search.search(conn, "what does E02 mean", doc_type="error_codes")
    assert {r["slug"] for r in found["results"]} == {"ebike-error-codes"}
    assert found["results"][0]["anchor"] == "s1-2"


def test_stopword_only_query(corpus):
    with db.tx() as conn:
        found = search.search(conn, "what is it")
        weird = search.search(conn, "can't won't ' | & ! :*")
    assert found["keyword_ids"] == [] and len(found["results"]) == 5
    assert len(weird["results"]) == 5


def test_new_version_replaces_the_old_one(corpus):
    with db.tx() as conn:
        version_id, created = ingest.add_version(conn, "station-guide", "Station guide", "station", "## 1 Morabadi\n\nNow open 24 hours.")
        assert created
        ingest.index_version(conn, version_id)
        found = search.search(conn, "Morabadi opening hours", doc_type="station")
    assert [r["body"] for r in found["results"]] == ["Now open 24 hours."]


def test_help_pages(client, corpus, make_user):
    listed = client.get("/api/v1/docs").json()
    assert {"slug": "rental-terms", "title": "Rental terms", "doc_type": "policy"} in listed
    page = client.get("/api/v1/docs/cancellations-refunds").json()
    assert [s["anchor"] for s in page["sections"]][:3] == ["s1-1", "s1-2", "s2-1"]
    assert client.get("/api/v1/docs/nope").status_code == 404
    route = client.get("/api/v1/docs/route-kanke-dam").json()
    assert route["route_geojson"]["type"] == "LineString"

    admin = make_user("admin")
    files = {"file": ("x.md", b"---\nslug: faq\ntitle: FAQ\ntype: policy\n---\n## 1 Hello\n\nWorld.", "text/markdown")}
    r = client.post("/api/v1/admin/documents", files=files, headers=auth(admin))
    assert r.status_code == 202 and r.json()["created"] is True
    assert client.post("/api/v1/admin/documents", files=files, headers=auth(admin)).json()["created"] is False
    assert client.post("/api/v1/admin/documents", files=files, headers=auth(make_user())).status_code == 403


def test_policy_numbers_match_settings():
    """The documents explain the rules; the code reads the numbers from `setting`. They must agree."""
    s = {r["key"]: r["value"] for r in rows("SELECT key, value FROM setting")}
    text = {p.stem: p.read_text(encoding="utf-8") for p in CORPUS.glob("*.md")}
    terms, refunds, late = text["rental-terms"], text["cancellations-refunds"], text["late-returns-fees"]
    assert f"holds the bike for {s['hold_minutes']} minutes" in terms
    assert f"hold ends after {s['hold_minutes']} minutes" in refunds
    assert f"shortest booking is {s['min_booking_minutes']} minutes and the longest is {s['max_booking_hours']} hours" in terms
    assert f"up to {s['max_advance_days']} days ahead" in terms
    assert f"at most {s['max_unpaid_holds']} unpaid bookings" in terms
    assert f"up to {s['early_pickup_minutes']} minutes before" in terms
    assert f"grace period of {s['late_grace_minutes']} minutes" in late
    assert f"{s['no_show_after_minutes']} minutes after the start" in refunds
    assert ("gets no refund" in refunds) == (s["no_show_refund_pct"] == 0)
    tiers = sorted(s["refund_tiers"], key=lambda t: -t["min_hours_before"])
    stated = re.findall(r"^- Cancel (.+?) before the start: (\d+)% refund\.$|^- Cancel (.+?) before the start: no refund\.$", refunds, re.M)
    assert [int(pct or 0) for _, pct, _ in stated] == [t["pct"] for t in tiers]
    assert stated[0][0] == f"{tiers[0]['min_hours_before']} hours or more"
    assert stated[1][0] == f"between {tiers[1]['min_hours_before']} and {tiers[0]['min_hours_before']} hours"


def test_late_rates_in_documents_match_pricing():
    """The late fee list in the document must match the seed's pricing plans."""
    from app.seed import BIKE_TYPES

    late = (CORPUS / "late-returns-fees.md").read_text(encoding="utf-8")
    for bike in BIKE_TYPES:
        assert f"- {bike['name']}: ₹{bike['late_fee_per_hour_paise'] // 100} an hour." in late
