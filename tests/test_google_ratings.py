"""Google rating, reviews, class and amenities from Google Hotels results (real page captured 2026-11-10)."""
import csv
from datetime import date
from pathlib import Path

from hotel_scraper import config
from hotel_scraper import google_ratings as gr

PAGE = (Path(__file__).parent / "fixtures" / "google_hotels_list_obertraun_2026-11.txt").read_text(encoding="utf-8")


def test_cards_from_real_results_page():
    cards = {c["name"]: c for c in gr.cards_from_text(PAGE)}
    assert len(cards) == 18
    assert cards["Haus Hepi"] == {"name": "Haus Hepi", "rating": 5.0, "reviews": 191, "stars": None,
                                  "amenities": ["Free breakfast", "Free Wi-Fi", "Free parking", "Smoke-free property"]}
    assert cards["Dormio Resort Obertraun"]["reviews"] == 2800                  # "(2.8k)"
    spa = cards["Spa Hotel Erzherzog Johann"]
    assert spa["stars"] == 4 and "Spa" in spa["amenities"] and spa["rating"] == 4.5
    assert cards["Haus Haider"]["amenities"] == [] and cards["Haus Haider"]["reviews"] == 7
    assert "Sign in" not in cards                                                # the guests box "2" isn't a rating


def test_counts():
    assert gr.parse_count("191") == 191 and gr.parse_count("2.8", "k") == 2800 and gr.parse_count("1,234") == 1234
    assert gr.parse_count("1.2", "M") == 1_200_000


def test_name_matching():
    cards = gr.cards_from_text(PAGE)
    card, sim = gr.best_card("Haus Hepi", cards, 0.72)
    assert card["name"] == "Haus Hepi" and sim == 1.0
    card, sim = gr.best_card("Hotel Erzherzog Johann", cards, 0.72)
    assert card["name"] == "Spa Hotel Erzherzog Johann"
    assert gr.best_card("Grand Hotel Duchi d'Aosta", cards, 0.72)[0] is None


def test_due_never_checked_first_then_oldest_misses_retried_sooner():
    w = [{"pid": p} for p in ("a", "b", "c", "d")]
    known = {"b": {"checked_at": "2026-10-01T00:00:00+00:00", "status": "ok"},
             "c": {"checked_at": "2026-11-20T00:00:00+00:00", "status": "ok"},
             "d": {"checked_at": "2026-11-10T00:00:00+00:00", "status": "not found"}}
    assert [x["pid"] for x in gr.due(w, known, date(2026, 11, 23), 30, 7)] == ["a", "b", "d"]


def test_run_fills_the_property_and_its_neighbours(tmp_path):
    cfg = config.load()
    cfg["paths"]["watchlist"] = str(tmp_path / "w.csv")
    cfg["paths"]["google_ratings"] = str(tmp_path / "g.csv")
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        wr = csv.DictWriter(f, ["pid", "name", "city", "gone_since"])
        wr.writeheader()
        wr.writerows([{"pid": "ta:1", "name": "Haus Hepi", "city": "Obertraun", "gone_since": ""},
                      {"pid": "ta:2", "name": "Dormio Resort Obertraun", "city": "Obertraun", "gone_since": ""},
                      {"pid": "ta:3", "name": "Nowhere Inn", "city": "Elsewhere", "gone_since": ""}])
    queries = []

    def fake(q):
        queries.append(q)
        return PAGE if "Obertraun" in q else "Sign in\n2\nNo results\n"
    stats = gr.run(cfg, 5, fake, today=date(2026, 11, 23), log=lambda m: None, sleep=lambda s: None)
    assert queries == ["Haus Hepi Obertraun", "Nowhere Inn Elsewhere"]          # Dormio came with Haus Hepi's list
    rows = {r["pid"]: r for r in csv.DictReader(open(cfg["paths"]["google_ratings"]))}
    assert rows["ta:1"]["google_rating"] == "5.0" and rows["ta:1"]["google_reviews"] == "191"
    assert rows["ta:2"]["google_reviews"] == "2800" and "Hot tub" in rows["ta:2"]["amenities"].split("|")
    assert rows["ta:3"]["status"] == "not found"
    assert stats["also_filled"] == 1 and stats["coverage"] == "2/3"
