from hotel_scraper import seller_merge


def test_each_reader_replaces_only_its_own_seller():
    base = [{"seller": "agoda", "pid": "a", "night": "1"}, {"seller": "official", "pid": "b", "night": "1"},
            {"seller": "trip_com", "pid": "c", "night": "1"}]
    parts = [[{"seller": "agoda", "pid": "a", "night": "2"}], [{"seller": "official", "pid": "b", "night": "3"}]]
    got = seller_merge.merge(base, parts, key=lambda r: (r["seller"], r["pid"], r["night"]))
    assert got == [{"seller": "agoda", "pid": "a", "night": "2"}, {"seller": "official", "pid": "b", "night": "3"},
                   {"seller": "trip_com", "pid": "c", "night": "1"}]          # a reader that didn't run keeps its rows
