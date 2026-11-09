import csv
import json
from datetime import date
from pathlib import Path

import pytest

from hotel_scraper import booking_browser as bb
from hotel_scraper import config, last_minute as lmm

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 9)


def test_real_venice_cards():
    # captured on GitHub Actions, 2026-11-09: two "Late Escape Deal" cards and a "Limited-time Deal"
    cards = bb.cards_from_html((FIX / "booking_search_cards_venice.html").read_text())
    got = [(c["name"], bb.parse_card(c)["badge"], bb.parse_card(c)["price"], bb.parse_card(c)["original_price"]) for c in cards]
    assert got == [("Palazzo Veneziano - Venice Collection", "Late Escape Deal", 135, 192),
                   ("Casanova Hideaway", "Limited-time Deal", 97, 161),
                   ("Corte Barozzi Venice Suites", "Late Escape Deal", 87, 132)]
    p = bb.parse_card(cards[1])
    assert p["booking_discount"] == 0.398 and p["score"] == 9.1 and p["currency"] == "EUR" and not p["members_only"]


def test_is_offer():
    assert lmm.is_offer({"badge": "Late Escape Deal"}) and lmm.is_offer({"booking_discount": 0.3})
    assert not lmm.is_offer({"badge": "", "booking_discount": None})


def test_towns_rotate_busiest_first():
    watched = [{"city": "Venice"}] * 3 + [{"city": "Bled"}] * 2 + [{"city": "Grado"}]
    assert lmm.towns_to_search(watched, {}, 2) == ["Venice", "Bled"]
    assert lmm.towns_to_search(watched, {"Venice": "2026-11-09T11:00", "Bled": "2026-11-09T11:00"}, 2) == ["Grado", "Venice"]


class FakeSession:
    def __init__(self, cards):
        self.cards = cards

    def search(self, q, night, scrolls=0, raw=False):
        return self.cards if q == "Venice" else []


def test_collect_keeps_offer_cards(tmp_path):
    cfg = config.load()
    for k in ("watchlist", "booking_ids", "last_minute"):
        cfg["paths"][k] = str(tmp_path / f"{k}.csv")
    cfg["paths"]["last_minute_state"] = str(tmp_path / "state.json")
    cfg["last_minute"].update(nights_ahead=[0, 1], towns_per_run=1)
    with open(cfg["paths"]["watchlist"], "w", newline="") as f:
        w = csv.DictWriter(f, ["pid", "city"]); w.writeheader(); w.writerow({"pid": "ta:g1-d1", "city": "Venice"})
    cards = [dict(c, cc="it", slug=f"x{i}") for i, c in enumerate(bb.cards_from_html((FIX / "booking_search_cards_venice.html").read_text()))]
    assert lmm.collect(cfg, FakeSession(cards), TODAY, log=lambda m: None) == 6
    rows = list(csv.DictReader(open(cfg["paths"]["last_minute"])))
    assert len(rows) == 6 and {r["badge"] for r in rows} == {"Late Escape Deal", "Limited-time Deal"}
