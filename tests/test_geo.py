from hotel_scraper import geo

TRIESTE = (45.6495, 13.7768)


def test_haversine_known_distances():
    assert 110 < geo.haversine_km(*TRIESTE, 45.4408, 12.3155) < 125   # Venice
    assert 65 < geo.haversine_km(*TRIESTE, 46.0569, 14.5058) < 80     # Ljubljana


def test_bbox_contains_circle_edges():
    s, w, n, e = geo.bbox_for_radius(*TRIESTE, 700)
    assert s < 45.6495 - 6 and n > 45.6495 + 6
    assert w < 13.7768 - 9 and e > 13.7768 + 9


def test_tiles_cover_far_points_and_skip_corners():
    t = geo.tiles(*TRIESTE, 700, 2.0)
    s0, w0, n0, e0 = geo.bbox_for_radius(*TRIESTE, 700)

    def covered(lat, lon):
        return any(s <= lat <= n and w <= lon <= e for s, w, n, e in t)

    assert covered(41.9028, 12.4964)   # Rome
    assert covered(50.0755, 14.4378)   # Prague
    assert covered(42.6507, 18.0944)   # Dubrovnik
    assert not covered(s0 + 0.1, w0 + 0.1)  # bbox corner is outside the circle
