"""Remix turbo-stream decoding on CHECK24's real search-box answer (2026-11-23)."""
from pathlib import Path

from hotel_scraper import turbo_stream

FX = Path(__file__).parent / "fixtures"


def test_check24_autocomplete_decodes_to_suggestions():
    doc = turbo_stream.decode((FX / "check24_autocomplete_artemide_2026-11.data").read_text())
    s = doc["data"]["data"]["suggestions"]
    assert doc["data"]["ok"] is True and doc["data"]["data"]["term"] == "Hotel Artemide Rom"
    assert s[0]["id"] == "019ab52aed947586a93ef85135f9a408" and s[0]["name"] == "Hotel Artemide"
    assert s[0]["parentLocationName"] == "Rom" and s[0]["type"] == 2
    assert [x["parentLocationName"] for x in s[1:3]] == ["San Vito lo Capo", "Aversa"]   # shared values resolved


def test_constants_and_shared_values():
    assert turbo_stream.decode('[{"_1":2,"_3":-7},"a",[4,4],"b",5]') == {"a": [5, 5], "b": None}
