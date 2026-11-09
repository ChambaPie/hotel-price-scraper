from datetime import date

from hotel_scraper import dates

TODAY = date(2026, 11, 7)   # a Saturday
BASE = {"horizon_days": 365, "min_lead_days": 0, "date_windows": [], "exclude_windows": [],
        "checkin_weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "stay_nights": [1]}


def test_full_horizon():
    d = dates.checkin_dates(BASE, TODAY)
    assert len(d) == 365 and d[0] == TODAY and d[-1] == date(2027, 11, 6)


def test_windows_excludes_weekdays():
    cfg = {**BASE, "date_windows": [{"from": "2027-01-25", "to": "2027-02-07"}],
           "exclude_windows": [{"from": date(2027, 2, 1), "to": date(2027, 2, 3)}], "checkin_weekdays": ["fri", "sat"]}
    assert dates.checkin_dates(cfg, TODAY) == [date(2027, 1, 29), date(2027, 1, 30), date(2027, 2, 5), date(2027, 2, 6)]


def test_multi_night_stays_skip_excluded_nights():
    cfg = {**BASE, "horizon_days": 3, "stay_nights": [1, 2], "exclude_windows": [{"from": "2026-11-09", "to": "2026-11-09"}]}
    # check-in 9th is excluded; the 2-night stay from the 8th would sleep on the 9th, so it goes too
    assert dates.stays(cfg, TODAY) == [(date(2026, 11, 7), date(2026, 11, 8)), (date(2026, 11, 7), date(2026, 11, 9)),
                                       (date(2026, 11, 8), date(2026, 11, 9))]
