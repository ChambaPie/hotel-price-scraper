"""Which catalog properties get scanned.

watched = (active properties matching any auto_include rule) + include.txt - exclude.txt

CLI (used by the "Edit selection" workflow):
    python -m hotel_scraper.selection --include "osm:way/1, osm:node/2" --exclude "osm:way/3"
    python -m hotel_scraper.selection            # just re-resolve data/watchlist.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

from . import catalog as cat
from . import config

PID_RE = re.compile(r"^(osm:(node|way|relation)/\d+|ta:g\d+-d\d+)$")


def read_ids(path: Path | str) -> list[str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rule_matches(row: dict, rule: dict) -> bool:
    if rule.get("property_type") and row.get("kind") != rule["property_type"]:
        return False
    if rule.get("min_stars") is not None and (_num(row.get("stars")) or 0) < rule["min_stars"]:
        return False
    if rule.get("countries") and row.get("country") not in rule["countries"]:
        return False
    if rule.get("max_distance_km") is not None and (_num(row.get("distance_km")) or 1e9) > rule["max_distance_km"]:
        return False
    if rule.get("max_drive_hours") is not None:
        hours = _num(row.get("drive_hours"))  # unknown never matches
        if hours is None or hours > rule["max_drive_hours"]:
            return False
    if rule.get("min_review_score") is not None:
        score = _num(row.get("review_score"))  # TripAdvisor guest rating 0-5; unknown never matches
        if score is None or score < rule["min_review_score"]:
            return False
    if rule.get("min_reviews") is not None and (_num(row.get("review_count")) or 0) < rule["min_reviews"]:
        return False
    if rule.get("landscape"):  # any of the listed tags, e.g. [mountain, lake]
        if not set(rule["landscape"]) & set((row.get("landscape") or "").split(",")):
            return False
    return True


def rank_score(row: dict, prior_score: float, prior_reviews: float) -> float:
    """Guest rating shrunk toward a prior by review count (a 5.0 from 20 reviews ranks below a 4.8 from 2,000)."""
    r, v = _num(row.get("review_score")), _num(row.get("review_count")) or 0
    if r is None:
        return prior_score - 1
    return (v * r + prior_reviews * prior_score) / (v + prior_reviews)


def resolve(rows: list[dict], sel: dict, include: list[str], exclude: list[str], skip: set | None = None) -> list[dict]:
    """include.txt always; then each rule takes its best `max` matches (ranked by rank_score, then review count);
    then the overall cap `max_watched`, in rule order. A property counts under the first rule it matches.
    `skip`: ids that can't be priced right now; rules pass over them so the quota fills with the next best."""
    auto = sel.get("auto_include") or {}
    rules = auto.get("rules") or [] if auto.get("enabled", True) else []
    rk = auto.get("rank") or {}
    prior, weight = rk.get("prior_score", 4.3), rk.get("prior_reviews", 50)
    inc, exc = set(include), set(exclude)
    forced, by_rule = [], {i: [] for i in range(1, len(rules) + 1)}
    for r in rows:
        if r["pid"] in exc:
            continue
        if r["pid"] in inc:
            forced.append({**r, "watch_reason": "include"})
            continue
        if r.get("gone_since") or (skip and r["pid"] in skip):
            continue
        hit = next((i for i, rule in enumerate(rules, 1) if rule_matches(r, rule)), None)
        if hit:
            by_rule[hit].append({**r, "watch_reason": f"rule {hit}"})
    ruled = []
    for i, items in by_rule.items():
        items.sort(key=lambda r: (-rank_score(r, prior, weight), -(_num(r.get("review_count")) or 0), r["pid"]))
        quota = rules[i - 1].get("max")
        ruled.extend(items[:quota] if quota is not None else items)
    cap = auto.get("max_watched")
    return forced + (ruled[:cap] if cap is not None else ruled)


def _label(row: dict | None) -> str:
    if not row:
        return ""
    return f"  # {row.get('name', '')}, {row.get('city', '')}, {row.get('country', '')}"


def edit(include_path: Path, exclude_path: Path, add_include: list[str], add_exclude: list[str],
         known: dict[str, dict]) -> list[str]:
    """Append ids to one list and remove them from the other. Returns problems (unknown or malformed ids)."""
    problems = [p for p in add_include + add_exclude if not PID_RE.match(p)]
    problems += [p for p in add_include + add_exclude if PID_RE.match(p) and p not in known]
    if problems:
        return problems
    for target, other, ids in ((include_path, exclude_path, add_include), (exclude_path, include_path, add_exclude)):
        if not ids:
            continue
        drop = set(ids)
        if Path(other).exists():
            kept = [ln for ln in Path(other).read_text(encoding="utf-8").splitlines()
                    if ln.split("#", 1)[0].strip() not in drop]
            Path(other).write_text("\n".join(kept) + "\n", encoding="utf-8")
        have = set(read_ids(target))
        with open(target, "a", encoding="utf-8") as f:
            for pid in ids:
                if pid not in have:
                    f.write(f"{pid}{_label(known.get(pid))}\n")
                    have.add(pid)
    return []


def write_watchlist(rows: list[dict], path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = [k for k in (rows[0] if rows else {}) if k not in cat.META_FIELDS and k != "watch_reason"]
    cols = list(dict.fromkeys(cat.columns(rows, base or None) + (["watch_reason"] if rows else [])))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=cat.sort_key))


def unpriceable(cfg: dict) -> set:
    """Properties whose last price check failed (TripAdvisor has no partner prices for them), until they are due
    for a re-try after scan.retry_unpriceable_days."""
    from datetime import datetime, timedelta, timezone
    p, s = cfg["paths"], cfg.get("scan", {})
    if not p.get("heatmap") or not Path(p["heatmap"]).exists():
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=s.get("retry_unpriceable_days", 30))).isoformat(timespec="seconds")
    with open(p["heatmap"], encoding="utf-8", newline="") as f:
        return {r["pid"] for r in csv.DictReader(f) if r.get("status") == "error" and r.get("checked_at", "") >= cutoff}


def resolve_from_files(cfg: dict) -> list[dict]:
    p = cfg["paths"]
    sel = yaml.safe_load(Path(p["selection"]).read_text(encoding="utf-8")) or {}
    rows = cat.read(p["catalog"])
    watched = resolve(rows, sel, read_ids(p["include"]), read_ids(p["exclude"]), skip=unpriceable(cfg))
    write_watchlist(watched, p["watchlist"])
    return watched


def _split(s: str | None) -> list[str]:
    return [x for x in re.split(r"[\s,;]+", s or "") if x]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(config.DEFAULT_PATH))
    ap.add_argument("--include", default="", help="ids to always watch (comma or space separated)")
    ap.add_argument("--exclude", default="", help="ids to never watch")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    p = cfg["paths"]
    known = {r["pid"]: r for r in cat.read(p["catalog"])}
    problems = edit(Path(p["include"]), Path(p["exclude"]), _split(args.include), _split(args.exclude), known)
    if problems:
        print("Not in the catalog or malformed, nothing changed: " + ", ".join(problems), file=sys.stderr)
        return 2
    watched = resolve_from_files(cfg)
    print(f"Watching {len(watched)} of {len(known)} catalog properties.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
