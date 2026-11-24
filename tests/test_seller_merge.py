from hotel_scraper import seller_merge


def test_each_reader_replaces_only_its_own_seller():
    base = [{"seller": "agoda", "pid": "a", "night": "1"}, {"seller": "official", "pid": "b", "night": "1"},
            {"seller": "trip_com", "pid": "c", "night": "1"}]
    parts = [[{"seller": "agoda", "pid": "a", "night": "2"}], [{"seller": "official", "pid": "b", "night": "3"}]]
    got = seller_merge.merge(base, parts, key=lambda r: (r["seller"], r["pid"], r["night"]))
    assert got == [{"seller": "agoda", "pid": "a", "night": "2"}, {"seller": "official", "pid": "b", "night": "3"},
                   {"seller": "trip_com", "pid": "c", "night": "1"}]          # a reader that didn't run keeps its rows


def test_rows_repeated_by_an_unfiltered_part_are_kept_once():
    """Regression (run 45374215059): cancelled reader jobs uploaded the whole ids file unfiltered, four times; the merge
    kept every copy (12,624 rows for 1,500 hotels)."""
    base = [{"seller": "agoda", "pid": "a", "status": "ok", "checked_at": "2026-11-24T09:00:00"}]
    whole = [{"seller": "agoda", "pid": "a", "status": "ok", "checked_at": "2026-11-24T09:00:00"},
             {"seller": "hrs", "pid": "a", "status": "ok", "checked_at": "2026-11-24T09:00:00"}]
    newer = [{"seller": "hrs", "pid": "a", "status": "not found", "checked_at": "2026-11-24T10:00:00"}]
    rows = seller_merge.merge(base + base + base, [whole, whole, whole, newer], key=lambda r: (r["seller"], r["pid"]))
    assert [(r["seller"], r["status"]) for r in rows] == [("agoda", "ok"), ("hrs", "not found")]
