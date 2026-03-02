# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import time
import html as _html
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

import mlb_engine as eng  # your engine module

# ----------------------------
# HTML escaping helpers
# ----------------------------
def h(s: str) -> str:
    """Escape a string for HTML. Use ONLY when you're sure it's a string."""
    return _html.escape(s, quote=True)

def hs(x: Any) -> str:
    """Escape anything (None/int/float/etc) safely for HTML."""
    return _html.escape("" if x is None else str(x), quote=True)

def lower_attr(x: Any) -> str:
    """Safe lowercased HTML-escaped attribute value."""
    return hs(x).lower()

# ----------------------------
# Config + constants
# ----------------------------
LA_TZ = ZoneInfo("America/Los_Angeles")

# Render note: /tmp is writable but not persistent across deploys/restarts.
WATCHLIST_PATH = Path("/tmp/watchlist.json")
ODDS_PATH = Path("/tmp/odds.json")

TEAM_CACHE_DIR = Path("/tmp/team_cache")
GAME_CACHE_DIR = Path("/tmp/game_cache")
PARK_CACHE_DIR = Path("/tmp/park_cache")

for d in (TEAM_CACHE_DIR, GAME_CACHE_DIR, PARK_CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

MLB_BASE = "https://statsapi.mlb.com"

# Some MLB venues don't include coordinates in StatsAPI.
# Fallback lat/lon map for Open-Meteo.
VENUE_COORD_FALLBACK: dict[int, tuple[float, float]] = {
    4309: (26.5219, -81.8711),  # JetBlue Park at Fenway South (Fort Myers, FL)
}

# ----------------------------
# In-memory TTL cache
# ----------------------------
_MEM: Dict[str, Tuple[float, Any]] = {}  # key -> (expires_ts, data)

def mem_get(key: str) -> Any:
    hit = _MEM.get(key)
    if not hit:
        return None
    exp, data = hit
    if time.time() > exp:
        _MEM.pop(key, None)
        return None
    return data

def mem_set(key: str, data: Any, ttl: int) -> None:
    _MEM[key] = (time.time() + int(ttl), data)

def mem_bust(prefix: str) -> None:
    for k in list(_MEM.keys()):
        if k.startswith(prefix):
            _MEM.pop(k, None)

# ----------------------------
# Requests session (retries/backoff)
# ----------------------------
def build_http_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "MLB-Analytics-FastAPI/1.0"})
    return s

HTTP = build_http_session()

def mlb_get(path: str, params: Optional[dict] = None) -> dict:
    r = HTTP.get(f"{MLB_BASE}{path}", params=params or {}, timeout=12)
    r.raise_for_status()
    return r.json()

# ----------------------------
# Disk cache helpers
# ----------------------------
def cache_read(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None

def cache_write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

# ----------------------------
# Watchlist (TTL-cached reads)
# ----------------------------
def load_watchlist() -> Dict[str, List[Dict[str, Any]]]:
    k = "watchlist:load"
    cached = mem_get(k)
    if cached is not None:
        return cached

    try:
        if WATCHLIST_PATH.exists():
            data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("players", []), list):
                mem_set(k, data, ttl=3)
                return data
    except Exception:
        pass

    data = {"players": []}
    mem_set(k, data, ttl=3)
    return data

def save_watchlist(wl: Dict[str, Any]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(wl, indent=2), encoding="utf-8")
    mem_bust("watchlist:")

def add_watch(pid: int, name: str, season: int, group: str = "hitting") -> None:
    wl = load_watchlist()
    players = wl.get("players", [])
    for p in players:
        if int(p.get("id", -1)) == int(pid) and int(p.get("season", -1)) == int(season) and p.get("group") == group:
            return
    players.append({"id": int(pid), "name": str(name), "season": int(season), "group": str(group)})
    wl["players"] = players
    save_watchlist(wl)

def remove_watch(index: int) -> None:
    wl = load_watchlist()
    players = wl.get("players", [])
    if 0 <= index < len(players):
        players.pop(index)
        wl["players"] = players
        save_watchlist(wl)

def is_in_watchlist(pid: int, season: int, group: str = "hitting") -> bool:
    wl = load_watchlist()
    for p in wl.get("players", []):
        if int(p.get("id", -1)) == int(pid) and int(p.get("season", -1)) == int(season) and p.get("group") == group:
            return True
    return False

# ----------------------------
# Odds (TTL-cached reads)
# ----------------------------
def load_odds() -> dict:
    k = "odds:load"
    cached = mem_get(k)
    if cached is not None:
        return cached

    try:
        if ODDS_PATH.exists():
            data = json.loads(ODDS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("odds", {})
                mem_set(k, data, ttl=3)
                return data
    except Exception:
        pass

    data = {"odds": {}}
    mem_set(k, data, ttl=3)
    return data

def save_odds(obj: dict) -> None:
    ODDS_PATH.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    mem_bust("odds:")

def odds_key(pid: int, date: str) -> str:
    return f"{int(pid)}|{date}"

def set_odds(pid: int, date: str, american: int) -> None:
    obj = load_odds()
    obj.setdefault("odds", {})
    obj["odds"][odds_key(pid, date)] = {
        "odds": int(american),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    save_odds(obj)

def clear_odds(pid: int, date: str) -> None:
    obj = load_odds()
    k = odds_key(pid, date)
    if k in obj.get("odds", {}):
        del obj["odds"][k]
        save_odds(obj)

def get_odds(pid: int, date: str, odds_obj: Optional[dict] = None) -> Optional[int]:
    obj = odds_obj if isinstance(odds_obj, dict) else load_odds()
    rec = obj.get("odds", {}).get(odds_key(pid, date))
    if not rec:
        return None
    try:
        return int(rec.get("odds"))
    except Exception:
        return None

# ----------------------------
# Math helpers
# ----------------------------
def model_hit_game_prob(p_hit_per_ab: float, ab_proj: float = 3.8) -> float:
    p = max(0.0001, min(0.60, float(p_hit_per_ab)))
    ab = max(1.0, float(ab_proj))
    return 1.0 - (1.0 - p) ** ab

def fmt_pct2(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    return f"{p*100:.0f}%"

def american_to_implied_prob(odds: Optional[int]) -> Optional[float]:
    if odds is None:
        return None
    try:
        o = int(odds)
    except Exception:
        return None
    if o == 0:
        return None
    if o > 0:
        return 100.0 / (o + 100.0)
    return (-o) / ((-o) + 100.0)

def fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    return f"{p*100:.1f}%"

def model_hr_game_prob(p_hr_per_pa: float, pa_proj: float = 4.2) -> float:
    p = max(0.0000001, min(0.25, float(p_hr_per_pa)))
    pa = max(1.0, float(pa_proj))
    return 1.0 - (1.0 - p) ** pa

def safe_int(x: Any) -> int:
    try:
        return int(float(x))
    except Exception:
        return 0

def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _to_int(x: Any) -> Optional[int]:
    try:
        return int(float(x))
    except Exception:
        return None

def mean_std(values: List[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return (None, None)
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    return (mu, var ** 0.5)

def z_score(val: Optional[float], mu: Optional[float], sd: Optional[float]) -> Optional[float]:
    if val is None or mu is None or sd is None or sd == 0:
        return None
    return (val - mu) / sd

def fmt_z(z: Optional[float]) -> str:
    if z is None:
        return "n/a"
    return f"{z:+.2f}"

def badge_for_z(z: Optional[float]) -> str:
    if z is None:
        return '<span class="badge text-bg-secondary fs-6">n/a</span>'
    if z >= 1.5:
        cls = "text-bg-success"
    elif z <= -1.5:
        cls = "text-bg-danger"
    else:
        cls = "text-bg-primary"
    return f'<span class="badge {cls} fs-6">{z:+.2f}</span>'
def _sort_games_most_recent_first(games: list[dict]) -> list[dict]:
    # Try to sort by gameDate if present; otherwise keep original order
    def key(g: dict):
        s = g.get("gameDate") or g.get("date") or g.get("officialDate") or ""
        try:
            # handle "2026-03-01" or ISO timestamps
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.min
    # sort descending (most recent first)
    return sorted(games, key=key, reverse=True)

def _rolling_metrics_for_window(games: list[dict], window: int) -> dict:
    g = games[:window]
    if not g:
        return {}

    ab = sum(safe_int(x.get("atBats")) for x in g)
    h_ = sum(safe_int(x.get("hits")) for x in g)
    hr = sum(safe_int(x.get("homeRuns")) for x in g)
    pa = sum(safe_int(x.get("plateAppearances")) for x in g)
    so = sum(safe_int(x.get("strikeOuts")) for x in g)

    # OPS: average over games where present (simple avg; fine for display)
    ops_vals = []
    for x in g:
        v = _to_float(x.get("ops"))
        if v is not None:
            ops_vals.append(v)
    ops = (sum(ops_vals) / len(ops_vals)) if ops_vals else None

    avg = (h_ / ab) if ab > 0 else None
    k_pct = (so / pa) if pa > 0 else None
    hr_g = (hr / window) if window > 0 else None

    return {
        "games": window,
        "AVG": avg,
        "OPS": ops,
        "HR": hr,
        "HR/G": hr_g,
        "PA": pa,
        "K%": k_pct,
    }
    
def _window_series(games: list[dict], window: int, metric: str) -> list[Optional[float]]:
    """
    Build a series of metric values for each rolling window slice inside `games`.
    games must be most-recent-first.
    """
    vals: list[Optional[float]] = []
    if len(games) < window:
        return vals

    for i in range(0, len(games) - window + 1):
        chunk = games[i:i + window]
        m = _rolling_metrics_for_window(chunk, window)
        vals.append(_to_float(m.get(metric)))
    return vals
    
# ----------------------------
# Cached MLB API helpers
# ----------------------------
def today_yyyy_mm_dd() -> str:
    return datetime.now(LA_TZ).strftime("%Y-%m-%d")

def _safe_date_yyyy_mm_dd(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return today_yyyy_mm_dd()
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return today_yyyy_mm_dd()

def fmt_time_pt(iso_utc: str) -> str:
    if not iso_utc:
        return "tbd"
    try:
        dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        dt_pt = dt_utc.astimezone(LA_TZ)
        t = dt_pt.strftime("%I:%M %p PT")
        return t.lstrip("0")
    except Exception:
        return "tbd"

def get_today_games(day: str) -> List[dict]:
    k = f"sched:today:{day}"
    cached = mem_get(k)
    if cached is not None:
        return cached
    sched = mlb_get("/api/v1/schedule", params={"sportId": 1, "date": day, "hydrate": "venue,probablePitcher"})
    dates = sched.get("dates") or []
    games = (dates[0].get("games") if dates else []) or []
    mem_set(k, games, ttl=60)
    return games

def schedule_range(start: str, end: str, hydrate: str = "") -> dict:
    k = f"sched:range:{start}:{end}:{hydrate}"
    cached = mem_get(k)
    if cached is not None:
        return cached
    params = {"sportId": 1, "startDate": start, "endDate": end}
    if hydrate:
        params["hydrate"] = hydrate
    data = mlb_get("/api/v1/schedule", params=params)
    mem_set(k, data, ttl=60 * 60 * 6)
    return data

def get_feed_live_cached(game_pk: int) -> Optional[dict]:
    k = f"feed:{int(game_pk)}"
    cached = mem_get(k)
    if cached is not None:
        return cached
    try:
        feed = mlb_get(f"/api/v1.1/game/{int(game_pk)}/feed/live")
        mem_set(k, feed, ttl=45)
        return feed
    except Exception:
        return None

def get_boxscore_cached(game_pk: int) -> Optional[dict]:
    p = GAME_CACHE_DIR / f"box_{int(game_pk)}.json"
    k = f"box:{int(game_pk)}"
    cached = mem_get(k)
    if cached is not None:
        return cached

    data = cache_read(p)
    if data:
        mem_set(k, data, ttl=120)
        return data

    try:
        data = mlb_get(f"/api/v1/game/{int(game_pk)}/boxscore")
        cache_write(p, data)
        mem_set(k, data, ttl=120)
        return data
    except Exception:
        return None

def get_active_roster(team_id: int) -> List[dict]:
    data = mlb_get(f"/api/v1/teams/{int(team_id)}/roster", params={"rosterType": "active"})
    return data.get("roster") or []

def get_today_team_ids(day: str) -> List[int]:
    games = get_today_games(day)
    out: List[int] = []
    for g in games:
        try:
            out.append(int(g["teams"]["home"]["team"]["id"]))
            out.append(int(g["teams"]["away"]["team"]["id"]))
        except Exception:
            pass
    return sorted(set(out))

# ----------------------------
# Name cache + lineup extraction
# ----------------------------
NAME_CACHE: Dict[int, str] = {}

def fetch_people_names(person_ids: List[int]) -> Dict[int, str]:
    ids = [i for i in sorted(set(person_ids)) if i not in NAME_CACHE]
    if not ids:
        return {}
    try:
        pdata = mlb_get("/api/v1/people", params={"personIds": ",".join(map(str, ids))})
        people = pdata.get("people") or []
        out: Dict[int, str] = {}
        for p in people:
            pid = p.get("id")
            nm = p.get("fullName")
            if pid and nm:
                out[int(pid)] = nm
        return out
    except Exception:
        return {}

def extract_lineup_hitters(feed: dict, side: str) -> List[dict]:
    """
    side: 'home' or 'away'
    Returns list of hitters with pid/name/battingOrder/pos.
    """
    out: List[dict] = []
    box = (feed.get("liveData") or {}).get("boxscore") or {}
    teams = box.get("teams") or {}
    t = teams.get(side) or {}
    batters = t.get("batters") or []
    players = box.get("players") or {}

    missing: List[int] = []
    for pid in batters:
        pid_int = int(pid)
        p = players.get(f"ID{pid_int}") or {}
        person = p.get("person") or {}
        name = person.get("fullName")
        if name:
            NAME_CACHE[pid_int] = name
        elif pid_int not in NAME_CACHE:
            missing.append(pid_int)

    fetched = fetch_people_names(missing)
    for k, v in fetched.items():
        NAME_CACHE[k] = v

    for pid in batters:
        pid_int = int(pid)
        p = players.get(f"ID{pid_int}") or {}
        name = NAME_CACHE.get(pid_int) or f"ID {pid_int}"
        bo = p.get("battingOrder") or ""
        pos = (p.get("position") or {}).get("abbreviation") or ""
        out.append({"pid": pid_int, "name": name, "battingOrder": bo, "pos": pos})
    return out

def batch_people_season_hitting_stats(person_ids: List[int], season: int) -> Dict[int, dict]:
    """
    Returns {pid: stat_dict} for season hitting stats in ONE request (chunked).
    """
    ids = [int(x) for x in person_ids if x]
    if not ids:
        return {}

    out: Dict[int, dict] = {}
    chunk_size = 75

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        pdata = mlb_get(
            "/api/v1/people",
            params={
                "personIds": ",".join(map(str, chunk)),
                "hydrate": f"stats(group=[hitting],type=[season],season={season})",
            },
        )
        for p in (pdata.get("people") or []):
            pid = p.get("id")
            stats = (p.get("stats") or [])
            stat = None
            if stats and (stats[0].get("splits") or []):
                stat = (stats[0]["splits"][0].get("stat") or {})
            if pid and isinstance(stat, dict):
                out[int(pid)] = stat
    return out

def park_leaderboard(window_days: int = 30) -> list[dict]:
    if window_days not in (7, 14, 30):
        window_days = 30

    # mem cache
    k = f"board:parks:{window_days}:{today_yyyy_mm_dd()}"
    cached = mem_get(k)
    if cached is not None:
        return cached

    today = datetime.now(LA_TZ).date()
    start = today - timedelta(days=window_days)
    end = today

    cache_path = PARK_CACHE_DIR / f"parks_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.json"
    disk = cache_read(cache_path)
    if disk and isinstance(disk.get("rows"), list):
        mem_set(k, disk["rows"], ttl=60 * 60)  # 1 hour
        return disk["rows"]

    sched = schedule_range(
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
        hydrate="venue",
    )

    venue_map: dict[str, dict] = {}

    for d in (sched.get("dates") or []):
        for g in (d.get("games") or []):
            status = ((g.get("status") or {}).get("detailedState") or "")
            if status != "Final":
                continue

            game_pk = g.get("gamePk")
            if not game_pk:
                continue

            venue = (g.get("venue") or {})
            venue_name = venue.get("name") or "Unknown Park"
            venue_id = venue.get("id") or ""

            box = get_boxscore_cached(int(game_pk))
            if not box:
                continue

            teams = (box.get("teams") or {})
            away_bat = (teams.get("away") or {}).get("teamStats", {}).get("batting", {}) or {}
            home_bat = (teams.get("home") or {}).get("teamStats", {}).get("batting", {}) or {}

            hr_away = away_bat.get("homeRuns")
            hr_home = home_bat.get("homeRuns")
            if hr_away is None or hr_home is None:
                continue

            total_hr = int(hr_away) + int(hr_home)

            kk = f"{venue_id}|{venue_name}"
            rec = venue_map.get(kk)
            if not rec:
                rec = {"venue": venue_name, "venue_id": venue_id, "games": 0, "hr_total": 0}
                venue_map[kk] = rec

            rec["games"] += 1
            rec["hr_total"] += total_hr

    rows = []
    for rec in venue_map.values():
        games = rec["games"]
        hr_total = rec["hr_total"]
        rows.append({
            "venue": rec["venue"],
            "games": games,
            "hr_total": hr_total,
            "hr_per_game": (hr_total / games) if games else 0.0,
        })

    rows.sort(key=lambda r: (-r["hr_per_game"], -r["games"], r["venue"]))

    cache_write(cache_path, {"rows": rows})
    mem_set(k, rows, ttl=60 * 60)
    return rows
    
def hot_teams(window_days: int = 14) -> list[dict]:
    if window_days not in (7, 14, 30):
        window_days = 14

    # mem cache
    k = f"board:hot_teams:{window_days}:{today_yyyy_mm_dd()}"
    cached = mem_get(k)
    if cached is not None:
        return cached

    today = datetime.now(LA_TZ).date()
    start = today - timedelta(days=window_days)
    end = today

    cache_path = TEAM_CACHE_DIR / f"hot_teams_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.json"
    disk = cache_read(cache_path)
    if disk and isinstance(disk.get("rows"), list):
        mem_set(k, disk["rows"], ttl=60 * 30)  # 30 min
        return disk["rows"]

    sched = schedule_range(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    agg: dict[int, dict] = {}

    for d in (sched.get("dates") or []):
        for g in (d.get("games") or []):
            status = ((g.get("status") or {}).get("detailedState") or "")
            if status != "Final":
                continue

            game_pk = g.get("gamePk")
            if not game_pk:
                continue

            box = get_boxscore_cached(int(game_pk))
            if not box:
                continue

            teams = (box.get("teams") or {})

            for side in ("away", "home"):
                t = teams.get(side) or {}
                team = (t.get("team") or {})
                team_id = team.get("id")
                team_name = team.get("name") or side.title()

                batting = (t.get("teamStats") or {}).get("batting") or {}
                hr = batting.get("homeRuns")
                r = batting.get("runs")
                ops = batting.get("ops")
                pa = batting.get("plateAppearances")

                if team_id is None or hr is None or r is None:
                    continue

                tid = int(team_id)

                rec = agg.get(tid)
                if not rec:
                    rec = {
                        "team": team_name,
                        "team_id": tid,
                        "games": 0,
                        "hr_total": 0,
                        "r_total": 0,
                        "pa_total": 0,
                        "ops_pa_sum": 0.0,
                        "ops_games_sum": 0.0,
                        "ops_games_n": 0,
                    }
                    agg[tid] = rec

                rec["games"] += 1
                rec["hr_total"] += int(hr)
                rec["r_total"] += int(r)

                # OPS: prefer PA-weighted average when possible
                try:
                    ops_f = float(ops) if ops is not None else None
                except Exception:
                    ops_f = None

                try:
                    pa_i = int(pa) if pa is not None else None
                except Exception:
                    pa_i = None

                if ops_f is not None and pa_i is not None and pa_i > 0:
                    rec["pa_total"] += pa_i
                    rec["ops_pa_sum"] += ops_f * pa_i
                elif ops_f is not None:
                    rec["ops_games_sum"] += ops_f
                    rec["ops_games_n"] += 1

    rows: list[dict] = []
    for rec in agg.values():
        g = rec["games"] or 0
        hr_g = (rec["hr_total"] / g) if g else 0.0
        r_g = (rec["r_total"] / g) if g else 0.0

        ops_val = None
        if rec["pa_total"] > 0:
            ops_val = rec["ops_pa_sum"] / rec["pa_total"]
        elif rec["ops_games_n"] > 0:
            ops_val = rec["ops_games_sum"] / rec["ops_games_n"]

        rows.append({
            "team": rec["team"],
            "games": g,
            "hr_total": rec["hr_total"],
            "r_total": rec["r_total"],
            "hr_g": hr_g,
            "r_g": r_g,
            "ops": ops_val,
        })

    rows.sort(key=lambda r: (-(r["hr_g"]), -(r["ops"] if r["ops"] is not None else -999), -(r["r_g"])))

    cache_write(cache_path, {"rows": rows})
    mem_set(k, rows, ttl=60 * 30)
    return rows
def get_venue_detail_cached(venue_id: int) -> Optional[dict]:
    if not venue_id:
        return None
    k = f"venue:{int(venue_id)}"
    cached = mem_get(k)
    if cached is not None:
        return cached
    try:
        data = mlb_get("/api/v1/venues", params={"venueIds": int(venue_id)})
        venues = data.get("venues") or []
        v = venues[0] if venues else None
        if v:
            mem_set(k, v, ttl=60 * 60 * 24)  # 24h
        return v
    except Exception:
        return None
        
def venue_lat_lon(venue_id: Optional[int], v: dict) -> tuple[Optional[float], Optional[float]]:
    # 1) hard fallback first
    if venue_id and int(venue_id) in VENUE_COORD_FALLBACK:
        lat, lon = VENUE_COORD_FALLBACK[int(venue_id)]
        return (float(lat), float(lon))

    if not v:
        return (None, None)

    loc = v.get("location") or {}
    coords = loc.get("defaultCoordinates") or {}

    # 2) try a few shapes
    lat = coords.get("latitude") or loc.get("latitude") or v.get("latitude")
    lon = coords.get("longitude") or loc.get("longitude") or v.get("longitude")

    try:
        if lat is None or lon is None:
            return (None, None)
        return (float(lat), float(lon))
    except Exception:
        return (None, None)

def open_meteo_hourly(lat: float, lon: float) -> Optional[dict]:
    k = f"wx:{lat:.3f},{lon:.3f}:{today_yyyy_mm_dd()}"
    cached = mem_get(k)
    if cached is not None:
        return cached
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_direction_10m",
            "timezone": "America/Los_Angeles",
            "forecast_days": 2,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        }
        r = HTTP.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        mem_set(k, data, ttl=60 * 15)  # 15 min
        return data
    except Exception:
        return None

def pick_hourly_weather(wx: dict, iso_utc: str) -> Optional[dict]:
    """
    Pick the closest hourly forecast to the game time (rounded to nearest hour).
    Assumes Open-Meteo timezone is America/Los_Angeles (as requested).
    """
    if not wx or not iso_utc:
        return None

    hourly = wx.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None

    # game time -> PT
    try:
        dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        dt_pt = dt_utc.astimezone(LA_TZ)
    except Exception:
        return None

    # Parse Open-Meteo times as *local PT naive*, because the API returns local timestamps
    # (since you request timezone=America/Los_Angeles)
    best_i = None
    best_diff = None

    for i, t in enumerate(times):
        try:
            dt_local = datetime.fromisoformat(t)  # naive local time
            # compare against PT time made naive
            diff = abs((dt_local - dt_pt.replace(tzinfo=None)).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_i = i
        except Exception:
            continue

    if best_i is None:
        return None

    def at(key: str):
        arr = hourly.get(key) or []
        return arr[best_i] if best_i < len(arr) else None

    return {
        "time_pt": times[best_i],
        "temp_f": at("temperature_2m"),
        "wind_mph": at("wind_speed_10m"),
        "wind_dir": at("wind_direction_10m"),
        "precip_pct": at("precipitation_probability"),
    }

    def get_arr(key):
        arr = hourly.get(key) or []
        return arr[idx] if idx < len(arr) else None

    return {
        "time_pt": times[idx],
        "temp_f": get_arr("temperature_2m"),  # this is Celsius unless you request units
        "wind_mph": get_arr("wind_speed_10m"),
        "wind_dir": get_arr("wind_direction_10m"),
        "precip_pct": get_arr("precipitation_probability"),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
    }
    
def hits_leaders(season: int = 2025, limit: int = 50) -> list[dict]:
    # Official MLB season leaderboard for HITS
    try:
        season = int(season)
    except Exception:
        season = datetime.now().year

    try:
        limit = int(limit)
    except Exception:
        limit = 50
    limit = max(1, min(200, limit))

    data = mlb_get(
        "/api/v1/stats/leaders",
        params={
            "leaderCategories": "hits",
            "season": season,
            "sportId": 1,            # MLB
            "limit": limit,
            "leaderGameTypes": "R",   # Regular season
        },
    )

    out: list[dict] = []
    ll = (data.get("leagueLeaders") or [])
    leaders = (ll[0].get("leaders") if ll else []) or []

    for r in leaders:
        person = r.get("person") or {}
        team = r.get("team") or {}
        out.append({
            "pid": person.get("id"),
            "name": person.get("fullName") or "Unknown",
            "team": team.get("name") or "",
            "hits": r.get("value"),  # already the hits number
        })

    return out 
        
# ----------------------------
# App + UI layout
# ----------------------------
app = FastAPI(title="MLB Analytics")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

def layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{hs(title)}</title>
  
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#0f1115">

  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
  <link rel="icon" type="image/png" sizes="512x512" href="/static/icons/icon-512.png">

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">

  <style>
    body {{
      font-family: 'Inter', sans-serif;
      background: #0f1115;
      color: #e6e6e6;
    }}
    .sidebar {{ background: #161a22; }}
    .sidebar a {{
      color: #aaa; text-decoration: none; display: block; padding: 10px 0;
    }}
    .sidebar a:hover {{ color: #fff; }}
    .card-dark {{
      background: #1b2029; border-radius: 16px; padding: 16px;
      border: 1px solid rgba(255,255,255,0.06);
    }}
    .soft-card {{
      background: #ffffff; color: #111;
      border-radius: 16px; border: 1px solid rgba(0,0,0,0.08);
    }}
    .muted {{ color: rgba(0,0,0,0.55); }}
    .dark-muted {{ color: rgba(255,255,255,0.65); }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }}
    .card-dark .table {{ color: #e6e6e6; }}
    .card-dark .table td {{ border-color: rgba(255,255,255,0.08); }}
  </style>
</head>

<body>

<nav class="navbar navbar-dark bg-dark d-lg-none">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold">MLB Analytics</span>
    <button class="navbar-toggler" type="button" data-bs-toggle="offcanvas" data-bs-target="#mobileSidebar">
      <span class="navbar-toggler-icon"></span>
    </button>
  </div>
</nav>

<div class="offcanvas offcanvas-start text-bg-dark d-lg-none sidebar" tabindex="-1" id="mobileSidebar">
  <div class="offcanvas-header">
    <h5 class="offcanvas-title fw-bold">MLB Analytics</h5>
    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="offcanvas"></button>
  </div>
  <div class="offcanvas-body">
    <a href="/">Dashboard</a>
    <a href="/leaderboard/hits?season=2025">Hit Leaders</a>
    <a href="/today-edge">Today Edge</a>
    <a href="/today">Today</a>
    <a href="/today-hitters">Today's Hitters</a>
    <a href="/today-hits">Today Hits</a>
    <a href="/today-ks">Today Ks</a>
    <a href="/leaderboard/parks">Parks</a>
    <a href="/leaderboard/teams-hot">Hot Teams</a>
    <a href="/leaderboard/hr-props">HR Board</a>
    <a href="/leaderboard/heat">Heat Board</a>
    <a href="/suggest/hitters">Auto-Suggest Hitters</a>
    <a href="/watchlist">Watchlist</a>
  </div>
</div>

<div class="container-fluid">
  <div class="row">
    <nav class="col-lg-2 d-none d-lg-block sidebar min-vh-100 p-4">
      <h4 class="fw-bold mb-4">MLB Analytics</h4>
      <a href="/">Dashboard</a>
      <a href="/leaderboard/hits?season=2025">Hit Leaders</a>
      <a href="/today-edge">Today Edge</a>
      <a href="/today">Today</a>
      <a href="/today-hitters">Today's Hitters</a>
      <a href="/today-hits">Today Hits</a>
      <a href="/today-ks">Today Ks</a>
      <a href="/leaderboard/parks">Parks</a>
      <a href="/leaderboard/teams-hot">Hot Teams</a>
      <a href="/leaderboard/hr-props">HR Board</a>
      <a href="/leaderboard/heat">Heat Board</a>
      <a href="/suggest/hitters">Auto-Suggest Hitters</a>
      <a href="/watchlist">Watchlist</a>
    </nav>

    <main class="col-12 col-lg-10 p-4">
      <h2 class="fw-bold mb-4">{hs(title)}</h2>
      {body}
    </main>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    # --- Pull small previews (keep everything safe) ---
    try:
        edge_preview = today_edge_board_data(limit=6)  # list of dicts (name, edge, model_p, implied, etc.)
    except Exception:
        edge_preview = []

    try:
        hot_teams_preview = hot_teams(window_days=7)[:6]
    except Exception:
        hot_teams_preview = []

    try:
        parks_preview = park_leaderboard(window_days=14)[:6]
    except Exception:
        parks_preview = []

    # optional: show today's games count
    day = today_yyyy_mm_dd()
    try:
        games = get_today_games(day)
        games_n = len(games)
    except Exception:
        games_n = 0

    # --- Render helpers ---
    def fmt_edge_cell(r: dict) -> str:
        name = r.get("name", "")
        pid = r.get("pid")
        model_p = r.get("model_p")
        implied = r.get("implied")
        edge = r.get("edge")

        edge_str = "n/a" if edge is None else f"{edge*100:+.1f}%"
        model_str = "n/a" if model_p is None else f"{model_p*100:.1f}%"
        imp_str = "n/a" if implied is None else f"{implied*100:.1f}%"

        # badge color by edge
        if edge is None:
            badge = '<span class="badge text-bg-secondary">n/a</span>'
        elif edge >= 0.03:
            badge = f'<span class="badge text-bg-success">{hs(edge_str)}</span>'
        elif edge <= -0.03:
            badge = f'<span class="badge text-bg-danger">{hs(edge_str)}</span>'
        else:
            badge = f'<span class="badge text-bg-primary">{hs(edge_str)}</span>'

        link = f"/player/{int(pid)}" if pid else "#"
        return f"""
<div class="d-flex justify-content-between align-items-center py-2 border-bottom border-light border-opacity-10">
  <div class="me-3">
    <a class="link-light fw-semibold text-decoration-none" href="{hs(link)}">{hs(name)}</a>
    <div class="dark-muted small">Model {hs(model_str)} · Implied {hs(imp_str)}</div>
  </div>
  <div class="text-end">{badge}</div>
</div>
"""

    def fmt_team_cell(r: dict) -> str:
        team = r.get("team", "")
        hr_g = r.get("hr_g", 0) or 0
        ops = r.get("ops")
        ops_str = "n/a"
        try:
            if ops is not None:
                ops_str = f"{float(ops):.3f}"
        except Exception:
            pass

        return f"""
<div class="d-flex justify-content-between align-items-center py-2 border-bottom border-light border-opacity-10">
  <div class="fw-semibold">{hs(team)}</div>
  <div class="dark-muted small">HR/G {float(hr_g):.2f} · OPS {hs(ops_str)}</div>
</div>
"""

    def fmt_park_cell(r: dict) -> str:
        venue = r.get("venue", "")
        hrpg = r.get("hr_per_game", 0) or 0
        games = r.get("games", 0) or 0
        return f"""
<div class="d-flex justify-content-between align-items-center py-2 border-bottom border-light border-opacity-10">
  <div class="fw-semibold">{hs(venue)}</div>
  <div class="dark-muted small">HR/G {float(hrpg):.2f} · Games {int(games)}</div>
</div>
"""

    # --- Build sections ---
    edge_html = (
        "".join(fmt_edge_cell(r) for r in edge_preview)
        if edge_preview
        else "<div class='dark-muted'>Add hitters to your Watchlist → then Today Edge populates here.</div>"
    )

    teams_html = (
        "".join(fmt_team_cell(r) for r in hot_teams_preview)
        if hot_teams_preview
        else "<div class='dark-muted'>No recent team data yet.</div>"
    )

    parks_html = (
        "".join(fmt_park_cell(r) for r in parks_preview)
        if parks_preview
        else "<div class='dark-muted'>No park data yet.</div>"
    )

    body = f"""
<!-- HERO -->
<div class="card-dark mb-4 p-4">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-3">
    <div>
      <div class="display-6 fw-bold">MLB Betting Analytics</div>
      <div class="dark-muted mt-2">
        HR edges, hot offenses, favorable parks, and daily prop tools.
      </div>

      <div class="d-flex gap-2 flex-wrap mt-3">
        <a class="btn btn-primary" href="/today-edge">Today Edge</a>
        <a class="btn btn-warning" href="/leaderboard/hr-props">HR Props Board</a>
        <a class="btn btn-outline-light" href="/watchlist">Watchlist</a>
        <a class="btn btn-outline-light" href="/suggest/hitters">Auto-Suggest Hitters</a>
      </div>
    </div>

    <!-- KPI chips -->
    <div class="d-flex gap-2 flex-wrap">
      <div class="px-3 py-2 rounded-3 border border-light border-opacity-10">
        <div class="dark-muted small">Today</div>
        <div class="fw-semibold">{hs(day)}</div>
      </div>
      <div class="px-3 py-2 rounded-3 border border-light border-opacity-10">
        <div class="dark-muted small">Games</div>
        <div class="fw-semibold">{int(games_n)}</div>
      </div>
      <div class="px-3 py-2 rounded-3 border border-light border-opacity-10">
        <div class="dark-muted small">Quick</div>
        <div class="fw-semibold">Search any player</div>
      </div>
    </div>
  </div>
</div>

<!-- SEARCH -->
<div class="card-dark mb-4 p-3">
  <form class="d-flex gap-2" action="/search" method="get">
    <input class="form-control form-control-lg" name="q"
           placeholder="Search a player (e.g., Aaron Judge)" autocomplete="off">
    <button class="btn btn-primary btn-lg" type="submit">Search</button>
  </form>
</div>

<!-- MAIN GRID -->
<div class="row g-3">

  <!-- Left: Today Edge preview -->
  <div class="col-12 col-lg-6">
    <div class="card-dark p-3 h-100">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="fw-semibold">Top HR Edges Today</div>
        <a class="btn btn-outline-light btn-sm" href="/today-edge">Open</a>
      </div>
      <div class="dark-muted small mb-2">Sorted by (Model − Implied). Green = positive edge.</div>
      {edge_html}
    </div>
  </div>

  <!-- Right: Hot teams -->
  <div class="col-12 col-lg-6">
    <div class="card-dark p-3 h-100">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="fw-semibold">Hottest Teams (7d)</div>
        <a class="btn btn-outline-light btn-sm" href="/leaderboard/teams-hot">Open</a>
      </div>
      <div class="dark-muted small mb-2">Ranked by HR/G then OPS then R/G.</div>
      {teams_html}
    </div>
  </div>

  <!-- Parks -->
  <div class="col-12 col-lg-6">
    <div class="card-dark p-3 h-100">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="fw-semibold">Hottest Parks (14d)</div>
        <a class="btn btn-outline-light btn-sm" href="/leaderboard/parks?window=14">Open</a>
      </div>
      <div class="dark-muted small mb-2">HR per game based on completed games.</div>
      {parks_html}
    </div>
  </div>

  <!-- Quick links -->
  <div class="col-12 col-lg-6">
    <div class="card-dark p-3 h-100">
      <div class="fw-semibold mb-2">Quick Tools</div>

      <div class="row g-2">
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/today">Today Games</a>
        </div>
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/today-hitters">Today&apos;s Hitters</a>
        </div>
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/today-hits">Today Hits</a>
        </div>
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/today-ks">Today Ks</a>
        </div>
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/leaderboard/heat">Heat Board</a>
        </div>
        <div class="col-6">
          <a class="btn btn-outline-light w-100" href="/leaderboard/hits?season=2025">Hit Leaders</a>
        </div>
      </div>

      <div class="dark-muted small mt-3">
        Tip: use <strong>Auto-Suggest Hitters</strong> → add to Watchlist → Edge/HR boards populate.
      </div>
    </div>
  </div>

</div>
"""
    return layout("MLB Analytics Dashboard", body)

@app.get("/search", response_class=HTMLResponse)
def search(q: str = ""):
    q = (q or "").strip()
    matches = eng.search_players(q) if q else []

    items = ""
    for m in (matches or [])[:25]:
        pid = m.get("id")
        full = m.get("fullName") or "Unknown"
        team = m.get("team") or "-"
        pos = m.get("primaryPosition") or m.get("pos") or "-"

        items += f"""
<a class="list-group-item list-group-item-action d-flex justify-content-between align-items-center"
   href="/player/{int(pid)}">
  <div>
    <div class="fw-semibold">{hs(full)}</div>
    <div class="small text-secondary">{hs(pos)} - {hs(team)}</div>
  </div>
  <span class="badge text-bg-secondary mono">ID {hs(pid)}</span>
</a>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <form class="d-flex gap-2" action="/search" method="get">
    <input class="form-control form-control-lg" name="q" value="{hs(q)}" placeholder="Aaron Judge">
    <button class="btn btn-primary btn-lg" type="submit">Search</button>
  </form>
</div>

<div class="card-dark">
  <div class="fw-semibold mb-2">Results</div>
  <div class="list-group">{items if items else '<div class="dark-muted">No results.</div>'}</div>
</div>
"""
    return layout("Search", body)

@app.get("/player/{pid}", response_class=HTMLResponse)
def player_dashboard(pid: int, season: int = datetime.now().year):
    name = f"Player {pid}"
    if hasattr(eng, "api_get"):
        try:
            pdata = eng.api_get(f"/people/{pid}")
            people = pdata.get("people") or []
            if people:
                name = people[0].get("fullName") or name
        except Exception:
            pass
    
    already = is_in_watchlist(pid, season, "hitting")
 
    add_btn = (
        '<button class="btn btn-success" type="button" disabled>Added</button>'
        if already
        else f"""
<form action="/watchlist/add" method="post">
  <input type="hidden" name="pid" value="{pid}">
  <input type="hidden" name="name" value="{hs(name)}">
  <input type="hidden" name="season" value="{season}">
  <button class="btn btn-primary" type="submit">+ Watchlist</button>
</form>
"""
    )

    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h4 mb-0 fw-semibold">{hs(name)}</div>
      <div class="muted">Player ID <span class="mono">{pid}</span></div>
    </div>

    <div class="d-flex gap-2 flex-wrap">
      <form class="d-flex gap-2" action="/player/{pid}" method="get">
        <input class="form-control" name="season" value="{season}" style="max-width:120px;">
        <button class="btn btn-outline-secondary" type="submit">Load season</button>
      </form>
      {add_btn}
    </div>
  </div>
</div>
    
<div class="row g-3">
  <div class="col-12 col-md-6">
    <div class="card-dark">
      <div class="fw-semibold mb-2">Season Stats</div>
      <div class="d-grid gap-2">
        <a class="btn btn-primary" href="/player/{pid}/season?group=hitting&season={season}">Hitting</a>
        <a class="btn btn-outline-primary" href="/player/{pid}/season?group=pitching&season={season}">Pitching</a>
      </div>
    </div>
  </div>

  <div class="col-12 col-md-6">
    <div class="card-dark">
      <div class="fw-semibold mb-2">Trends</div>
      <div class="d-grid gap-2">
        <a class="btn btn-warning" href="/player/{pid}/rolling?season={season}">Rolling 7/14/30</a>
        <a class="btn btn-success" href="/player/{pid}/zscores?season={season}">Z-Scores 7/14/30</a>
        <a class="btn btn-primary" href="/player/{pid}/splits?season={season}&group=hitting">Home/Away (Hitting)</a>
        <a class="btn btn-danger" href="/player/{pid}/splits?season={season}&group=pitching">Home/Away (Pitching)</a>
      </div>
    </div>
  </div>

  <div class="col-12">
    <div class="card-dark">
      <div class="fw-semibold mb-2">Betting Tools</div>
      <div class="d-grid gap-2">
        <a class="btn btn-dark" href="/leaderboard/hr-props">HR Props Board (Watchlist)</a>
        <a class="btn btn-danger" href="/player/{pid}/hr-prop-today?season={season}">Today HR Prop Score</a>
        <a class="btn btn-outline-light" href="/watchlist">Manage Watchlist</a>
      </div>
    </div>
  </div>
</div>
"""
    return layout("Player Dashboard", body)

@app.get("/player/{pid}/season", response_class=HTMLResponse)
def player_season(pid: int, group: str = "hitting", season: int = datetime.now().year):
    group = "pitching" if group == "pitching" else "hitting"
    st = eng.get_player_stats(pid, "season", group, season=season) or {}

    if group == "hitting":
        keys = [
            "gamesPlayed", "plateAppearances", "atBats", "hits", "homeRuns", "rbi",
            "avg", "obp", "slg", "ops", "strikeOuts", "baseOnBalls"
        ]
    else:
        keys = [
            "gamesPlayed", "gamesStarted", "wins", "losses", "era", "inningsPitched",
            "strikeOuts", "whip", "homeRuns", "baseOnBalls", "saves"
        ]

    rows = ""
    for k in keys:
        rows += f"<tr><td class='dark-muted'>{hs(k)}</td><td class='fw-semibold'>{hs(st.get(k, '-'))}</td></tr>"

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <div class="h5 fw-semibold mb-0">Season {season} - {hs(group.title())}</div>
      <div class="dark-muted">Player <span class="mono">{pid}</span></div>
    </div>
    <a class="btn btn-outline-light" href="/player/{pid}?season={season}">Back</a>
  </div>
</div>

<div class="card-dark">
  <table class="table mb-0">
    <tbody>{rows}</tbody>
  </table>
</div>
"""
    return layout("Season Stats", body)

@app.post("/watchlist/add")
def watchlist_add(pid: int = Form(...), name: str = Form(...), season: int = Form(...)):
    add_watch(pid=int(pid), name=str(name).strip() or f"ID {pid}", season=int(season), group="hitting")
    return RedirectResponse("/watchlist", status_code=303)

@app.get("/watchlist", response_class=HTMLResponse)
def watchlist():
    wl = load_watchlist()
    players = wl.get("players", [])

    rows = ""
    for i, p in enumerate(players):
        rows += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-start gap-2">
    <div>
      <div class="fw-semibold">{hs(p.get("name","-"))}</div>
      <div class="muted small">season {hs(p.get("season","-"))} - id {hs(p.get("id","-"))}</div>
    </div>
    <form action="/watchlist/remove" method="post">
      <input type="hidden" name="index" value="{i}">
      <button class="btn btn-outline-danger btn-sm" type="submit">Remove</button>
    </form>
  </div>
</div>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="h5 fw-semibold mb-1">Watchlist</div>
  <div class="muted">Add hitters, then use HR Board to rank them.</div>
</div>
{rows if rows else '<div class="p-3 soft-card muted">Watchlist is empty.</div>'}
"""
    return layout("Watchlist", body)

@app.post("/watchlist/remove")
def watchlist_remove(index: int = Form(...)):
    remove_watch(int(index))
    return RedirectResponse("/watchlist", status_code=303)

@app.post("/odds/set")
def odds_set(
    pid: int = Form(...),
    date: str = Form(...),
    odds: int = Form(...),
    next: str = Form("/today-edge"),
):
    date = (date or "").strip() or today_yyyy_mm_dd()
    set_odds(int(pid), date, int(odds))
    return RedirectResponse(next, status_code=303)

@app.post("/odds/clear")
def odds_clear(
    pid: int = Form(...),
    date: str = Form(...),
    next: str = Form("/today-edge"),
):
    date = (date or "").strip() or today_yyyy_mm_dd()
    clear_odds(int(pid), date)
    return RedirectResponse(next, status_code=303)

@app.get("/today-edge", response_class=HTMLResponse)
def today_edge_board(pa_proj: float = 4.2):
    wl = load_watchlist()
    odds_obj = load_odds()  # IMPORTANT: load once
    hitters = [p for p in wl.get("players", []) if p.get("group") == "hitting"]

    today = today_yyyy_mm_dd()
    default_season = datetime.now().year

    rows = []
    for p in hitters:
        pid = int(p["id"])
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or default_season)

        p_season, pa_season, hr_season = eng.season_hr_rate_from_season_stats(pid, season)
        if p_season is None:
            rows.append({
                "name": name, "pid": pid, "season": season,
                "model_p": None, "implied": None, "edge": None,
                "ctx": "no season baseline", "p_adj": None, "odds": None
            })
            continue

        ctx = None
        p_adj = float(p_season)

        if hasattr(eng, "hr_props_today_context"):
            try:
                ctx = eng.hr_props_today_context(pid, season, today)
            except Exception:
                ctx = None

        if ctx:
            park_mult = ctx.get("park_mult")
            sp_mult = ctx.get("sp_mult")
            if park_mult is not None:
                try:
                    p_adj *= float(park_mult)
                except Exception:
                    pass
            if sp_mult is not None:
                try:
                    p_adj *= float(sp_mult)
                except Exception:
                    pass

        p_adj = min(max(p_adj, 0.00001), 0.25)
        model_p = model_hr_game_prob(p_adj, pa_proj=pa_proj)

        amer = get_odds(pid, today, odds_obj=odds_obj)
        implied = american_to_implied_prob(amer)
        edge = (model_p - implied) if (implied is not None) else None

        if ctx:
            sp_name = ctx.get("sp_name", "tbd")
            venue = ctx.get("venue_name", "tbd")
            ctx_str = f"{sp_name} / {venue}"
        else:
            ctx_str = "no game context (tbd)"

        rows.append({
            "name": name, "pid": pid, "season": season,
            "model_p": model_p, "implied": implied, "edge": edge,
            "ctx": ctx_str, "p_adj": p_adj, "odds": amer
        })

    # IMPORTANT: None edge always goes to bottom
    rows.sort(key=lambda r: (r["edge"] is None, -(r["edge"] or -1e9), -(r["model_p"] or -1e9)))

    trs = ""
    for r in rows:
        odds_val = "" if r.get("odds") is None else str(r["odds"])
        edge_str = "n/a" if r["edge"] is None else f"{r['edge']*100:+.1f}%"

        trs += f"""
<tr class="edge-row" data-name="{lower_attr(r['name'])}">
  <td class="fw-semibold">{hs(r['name'])}</td>
  <td class="text-secondary small">{hs(r['ctx'])}</td>
  <td class="text-center">{fmt_pct(r['model_p'])}</td>
  <td class="text-center">{fmt_pct(r['implied'])}</td>
  <td class="text-center fw-semibold">{hs(edge_str)}</td>
  <td style="min-width:260px;">
    <div class="d-flex gap-2 flex-wrap">
      <form action="/odds/set" method="post" class="d-flex gap-2">
        <input type="hidden" name="pid" value="{r['pid']}">
        <input type="hidden" name="date" value="{hs(today)}">
        <input type="hidden" name="next" value="/today-edge?pa_proj={hs(pa_proj)}">
        <input class="form-control form-control-sm" name="odds" value="{hs(odds_val)}" placeholder="+320 / -110" style="max-width:120px;">
        <button class="btn btn-outline-secondary btn-sm" type="submit">Save</button>
      </form>

      <form action="/odds/clear" method="post">
        <input type="hidden" name="pid" value="{r['pid']}">
        <input type="hidden" name="date" value="{hs(today)}">
        <input type="hidden" name="next" value="/today-edge?pa_proj={hs(pa_proj)}">
        <button class="btn btn-outline-danger btn-sm" type="submit">Clear</button>
      </form>
    </div>
  </td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <div class="row g-2 align-items-end">
    <div class="col-12 col-md-4">
      <label class="form-label dark-muted small mb-0">Search</label>
      <input id="edgeSearch" class="form-control" placeholder="Type a player name...">
    </div>

    <div class="col-12 col-md-3">
      <label class="form-label dark-muted small mb-0">Projected PA</label>
      <form action="/today-edge" method="get" class="d-flex gap-2">
        <input class="form-control" name="pa_proj" value="{hs(pa_proj)}">
        <button class="btn btn-primary" type="submit">Apply</button>
      </form>
    </div>

    <div class="col-12 col-md-5 dark-muted small">
      Model% uses adjusted HR/PA and converts it to game HR probability: 1 - (1 - p)^PA.
      Enter American odds to compute implied% and edge%.
    </div>
  </div>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>Player</th>
          <th>Matchup</th>
          <th class="text-center">Model</th>
          <th class="text-center">Implied</th>
          <th class="text-center">Edge</th>
          <th>Odds</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="6" class="dark-muted">No hitters in watchlist.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {{
  const input = document.getElementById("edgeSearch");
  if (!input) return;
  input.addEventListener("keyup", function() {{
    const q = (input.value || "").toLowerCase();
    document.querySelectorAll(".edge-row").forEach(function(row) {{
      const name = row.getAttribute("data-name") || "";
      row.style.display = (name.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    return layout("Today Edge Board", body)

@app.get("/today", response_class=HTMLResponse)
def today_games(date: str = ""):
    day = _safe_date_yyyy_mm_dd(date)
    year = int(day.split("-")[0])

    # cache schedule for today page using mem
    k = f"page:today:{day}"
    cached = mem_get(k)
    if cached is not None:
        return HTMLResponse(cached)

    data = mlb_get("/api/v1/schedule", params={"sportId": 1, "date": day, "hydrate": "team,venue,probablePitcher"})
    dates = data.get("dates") or []
    games = (dates[0].get("games") if dates else []) or []

    cards = ""
    for g in games:
        home = ((g.get("teams") or {}).get("home") or {}).get("team") or {}
        away = ((g.get("teams") or {}).get("away") or {}).get("team") or {}
        home_name = home.get("name") or "Home"
        away_name = away.get("name") or "Away"

        venue = (g.get("venue") or {}).get("name") or "Venue tbd"
        start = fmt_time_pt(g.get("gameDate") or "")
        venue_obj = (g.get("venue") or {})
        venue = venue_obj.get("name") or "Venue tbd"
        venue_id = venue_obj.get("id")
        game_iso_utc = g.get("gameDate") or ""
        start = fmt_time_pt(game_iso_utc)

        wx_line = "" 
        
        if not venue_id:
            wx_line = ""
        if venue_id:
            vd = get_venue_detail_cached(int(venue_id))
            lat, lon = venue_lat_lon(venue_id, vd or {})
            if lat is not None and lon is not None:
                wx = open_meteo_hourly(lat, lon)
                w = pick_hourly_weather(wx, game_iso_utc) if wx else None
                if w:
                    wx_line = (
                        f"<div class='dark-muted small'>"
                        f"Weather: {hs(w.get('temp_f'))}°F | "
                        f"Wind {hs(w.get('wind_mph'))} mph | "
                        f"Rain {hs(w.get('precip_pct'))}%"
                        f"</div>"
                    )
            
        pp_home = g.get("teams", {}).get("home", {}).get("probablePitcher") or {}
        pp_away = g.get("teams", {}).get("away", {}).get("probablePitcher") or {}
        pp_home_name = pp_home.get("fullName") or "tbd"
        pp_away_name = pp_away.get("fullName") or "tbd"
        pp_home_id = pp_home.get("id")
        pp_away_id = pp_away.get("id")

        link_home = f'/player/{pp_home_id}?season={year}' if pp_home_id else None
        link_away = f'/player/{pp_away_id}?season={year}' if pp_away_id else None

        def pitcher_line(name: str, link: str | None) -> str:
            if link:
                return f'<a class="link-light" href="{hs(link)}">{hs(name)}</a>'
            return f'<span class="dark-muted">{hs(name)}</span>'

        cards += f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-1">{hs(away_name)} at {hs(home_name)}</div>
      <div class="dark-muted small">{hs(day)} - {hs(start)} - {hs(venue)}</div>
      
      {wx_line}
    </div>
    <div class="d-flex gap-2">
      <a class="btn btn-outline-light btn-sm" href="/search">Search players</a>
      <a class="btn btn-outline-light btn-sm" href="/watchlist">Watchlist</a>
    </div>
  </div>

  <hr class="border-light opacity-25">

  <div class="row g-2">
    <div class="col-12 col-md-6">
      <div class="dark-muted small">Away probable</div>
      <div class="fw-semibold">{pitcher_line(pp_away_name, link_away)}</div>
    </div>
    <div class="col-12 col-md-6">
      <div class="dark-muted small">Home probable</div>
      <div class="fw-semibold">{pitcher_line(pp_home_name, link_home)}</div>
    </div>
  </div>
</div>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/today" method="get">
    <div class="col-12 col-md-3">
      <label class="form-label dark-muted small mb-0">Date (YYYY-MM-DD)</label>
      <input class="form-control" name="date" value="{hs(day)}">
    </div>
    <div class="col-12 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Load</button>
    </div>
    <div class="col-12 col-md-7 dark-muted small">
      Shows schedule + probable pitchers. Times shown in Pacific Time.
    </div>
  </form>
</div>

{cards if cards else '<div class="card-dark dark-muted">No games found for this date.</div>'}
"""
    page = layout("Today Games", body)
    mem_set(k, page, ttl=45)
    return HTMLResponse(page)

@app.get("/today-hitters", response_class=HTMLResponse)
def today_hitters(date: str = ""):
    day = (date or "").strip() or today_yyyy_mm_dd()
    season = int(day.split("-")[0])

    k = f"page:today_hitters:{day}"
    cached = mem_get(k)
    if cached is not None:
        return HTMLResponse(cached)

    games = get_today_games(day)
    rows_html = ""
    total_hitters = 0

    for g in games:
        game_pk = g.get("gamePk")
        home = ((g.get("teams") or {}).get("home") or {}).get("team") or {}
        away = ((g.get("teams") or {}).get("away") or {}).get("team") or {}
        home_name = home.get("name") or "Home"
        away_name = away.get("name") or "Away"

        venue = (g.get("venue") or {}).get("name") or "Venue tbd"
        start_pt = fmt_time_pt(g.get("gameDate") or "")

        pp_home = g.get("teams", {}).get("home", {}).get("probablePitcher") or {}
        pp_away = g.get("teams", {}).get("away", {}).get("probablePitcher") or {}
        pp_home_name = pp_home.get("fullName") or "tbd"
        pp_away_name = pp_away.get("fullName") or "tbd"

        hitters_home, hitters_away = [], []
        lineup_status = "Lineups not posted yet"

        if game_pk:
            feed = get_feed_live_cached(int(game_pk))
            if feed:
                try:
                    hitters_home = extract_lineup_hitters(feed, "home")
                    hitters_away = extract_lineup_hitters(feed, "away")
                    if hitters_home or hitters_away:
                        lineup_status = "Lineups posted"
                except Exception:
                    pass

        def hitters_list_html(hitters: list[dict]) -> str:
            if not hitters:
                return f"<div class='dark-muted small'>{hs(lineup_status)}</div>"
            items = ""
            for hh in hitters:
                pid = hh["pid"]
                nm = hh["name"]
                total = "" if not hh["battingOrder"] else f" (BO {hh['battingOrder']})"
                items += f"""
<div class="d-flex justify-content-between align-items-center py-1 border-bottom border-light border-opacity-10">
  <div>
    <a class="link-light fw-semibold" href="/player/{pid}?season={season}">{hs(nm)}</a>
    <span class="dark-muted small">{hs(hh.get('pos',''))}{hs(total)}</span>
  </div>
  <form action="/watchlist/add" method="post" class="m-0">
    <input type="hidden" name="pid" value="{pid}">
    <input type="hidden" name="name" value="{hs(nm)}">
    <input type="hidden" name="season" value="{season}">
    <button class="btn btn-outline-light btn-sm" type="submit">+ Watch</button>
  </form>
</div>
"""
            return items

        total_hitters += len(hitters_home) + len(hitters_away)

        rows_html += f"""
<div class="card-dark mb-3 game-card" data-game="{lower_attr(away_name + ' ' + home_name)}">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-0">{hs(away_name)} at {hs(home_name)}</div>
      <div class="dark-muted small">{hs(day)} - {hs(start_pt)} - {hs(venue)}</div>
      <div class="dark-muted small">Probables: {hs(pp_away_name)} (away) - {hs(pp_home_name)} (home)</div>
    </div>
    <a class="btn btn-outline-light btn-sm" href="/today-edge">Today Edge</a>
  </div>

  <hr class="border-light opacity-25">

  <div class="row g-3">
    <div class="col-12 col-md-6">
      <div class="fw-semibold mb-1">{hs(away_name)} hitters</div>
      {hitters_list_html(hitters_away)}
    </div>
    <div class="col-12 col-md-6">
      <div class="fw-semibold mb-1">{hs(home_name)} hitters</div>
      {hitters_list_html(hitters_home)}
    </div>
  </div>
</div>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/today-hitters" method="get">
    <div class="col-12 col-md-3">
      <label class="form-label dark-muted small mb-0">Date</label>
      <input class="form-control" name="date" value="{hs(day)}">
    </div>
    <div class="col-12 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Load</button>
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label dark-muted small mb-0">Search games</label>
      <input id="gameSearch" class="form-control" placeholder="Dodgers, Yankees...">
    </div>
    <div class="col-12 col-md-3 dark-muted small">
      Hitters listed when lineups are posted.
      Added hitters go to Watchlist + Today Edge.
    </div>
  </form>
</div>

<div class="dark-muted small mb-2">Games: {len(games)} | Hitters found: {total_hitters}</div>

{rows_html if rows_html else "<div class='card-dark dark-muted'>No games found.</div>"}

<script>
document.addEventListener("DOMContentLoaded", function() {{
  const input = document.getElementById("gameSearch");
  if (!input) return;
  input.addEventListener("keyup", function() {{
    const q = (input.value || "").toLowerCase();
    document.querySelectorAll(".game-card").forEach(function(card) {{
      const t = card.getAttribute("data-game") || "";
      card.style.display = (t.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    page = layout("Today's Hitters", body)
    mem_set(k, page, ttl=30)
    return HTMLResponse(page)
    
@app.get("/leaderboard/hr-props", response_class=HTMLResponse)
def hr_props_leaderboard(window: int = 7, min_pa: int = 20):
    # normalize params
    try:
        window = int(window)
    except Exception:
        window = 7
    try:
        min_pa = int(min_pa)
    except Exception:
        min_pa = 20

    wl = load_watchlist()
    hitters = [p for p in wl.get("players", []) if p.get("group") == "hitting"]
    today = today_yyyy_mm_dd()

    if window not in (7, 14, 30):
        window = 7

    rows = []
    for p in hitters:
        pid = int(p.get("id", 0) or 0)
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or datetime.now().year)

        # baseline (guard against engine raising)
        try:
            p_season, _, _ = eng.season_hr_rate_from_season_stats(pid, season)
        except Exception as e:
            rows.append({"name": name, "season": season, "z": None, "detail": f"error season baseline: {type(e).__name__}"})
            continue

        if p_season is None:
            rows.append({"name": name, "season": season, "z": None, "detail": "no season baseline"})
            continue

        # game log (guard)
        try:
            games = eng.get_player_game_log(pid, season, "hitting") or []
        except Exception as e:
            rows.append({"name": name, "season": season, "z": None, "detail": f"error game log: {type(e).__name__}"})
            continue

        if len(games) < window:
            rows.append({"name": name, "season": season, "z": None, "detail": "not enough games"})
            continue

        pa_win = safe_int(sum(float(g.get("plateAppearances", 0) or 0) for g in games[:window]))
        hr_win = safe_int(sum(float(g.get("homeRuns", 0) or 0) for g in games[:window]))

        if pa_win < int(min_pa):
            rows.append({"name": name, "season": season, "z": None, "detail": f"PA too low ({pa_win} < {min_pa})"})
            continue

        # optional context (guard)
        ctx_str = ""
        p_adj = float(p_season)
        if hasattr(eng, "hr_props_today_context"):
            try:
                ctx = eng.hr_props_today_context(pid, season, today)
            except Exception:
                ctx = None
            if ctx:
                park_mult = ctx.get("park_mult")
                sp_mult = ctx.get("sp_mult")
                if park_mult is not None:
                    try:
                        p_adj *= float(park_mult)
                    except Exception:
                        pass
                if sp_mult is not None:
                    try:
                        p_adj *= float(sp_mult)
                    except Exception:
                        pass
                ctx_str = f" | SP {ctx.get('sp_name','?')} HR/9={ctx.get('sp_hr9','n/a')} | Park {ctx.get('venue_name','?')}"

        p_adj = min(max(p_adj, 0.00001), 0.25)

        # z calc (guard)
        try:
            z = eng.hr_binomial_z(hr_win, pa_win, p_adj)
        except Exception as e:
            rows.append({"name": name, "season": season, "z": None, "detail": f"error z-score: {type(e).__name__}"})
            continue

        detail = f"HR {hr_win}/PA {pa_win} | season HR/PA {p_season:.4f} | adj {p_adj:.4f}{ctx_str}"
        rows.append({"name": name, "season": season, "z": z, "detail": detail})

    # None z to bottom
    rows.sort(key=lambda r: (r["z"] is None, -(r["z"] or -1e9)))

    cards = ""
    for r in rows:
        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-start gap-2">
    <div>
      <div class="fw-semibold">{hs(r["name"])} <span class="text-secondary">({hs(r["season"])})</span></div>
      <div class="text-secondary small">{hs(r["detail"])}</div>
    </div>
    {badge_for_z(r["z"])}
  </div>
</div>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <form class="row g-2 align-items-end" action="/leaderboard/hr-props" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label muted small mb-0">Window</label>
      <select class="form-select" name="window">
        <option value="7" {"selected" if window==7 else ""}>7</option>
        <option value="14" {"selected" if window==14 else ""}>14</option>
        <option value="30" {"selected" if window==30 else ""}>30</option>
      </select>
    </div>
    <div class="col-6 col-md-2">
      <label class="form-label muted small mb-0">Min PA</label>
      <input class="form-control" name="min_pa" value="{hs(min_pa)}">
    </div>
    <div class="col-12 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Refresh</button>
    </div>
    <div class="col-12 col-md-6 text-secondary small">
      Guide: <span class="badge text-bg-success">z &gt;= +1.5</span> hot
      <span class="badge text-bg-danger">z &lt;= -1.5</span> cold
    </div>
  </form>
</div>
{cards if cards else '<div class="p-3 soft-card text-secondary">No hitters in watchlist yet.</div>'}
"""
    return layout("HR Props Board", body)
    
@app.get("/leaderboard/heat", response_class=HTMLResponse)
def heat_leaderboard(window: int = 7):
    try:
        window = int(window)
    except Exception:
        window = 7

    wl = load_watchlist()
    players = wl.get("players", [])
    if window not in (7, 14, 30):
        window = 7

    rows = []
    for p in players:
        pid = int(p.get("id", 0) or 0)
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or datetime.now().year)
        group = p.get("group", "hitting")

        # game log (guard)
        try:
            games = eng.get_player_game_log(pid, season, group) or []
        except Exception as e:
            rows.append({"name": name, "season": season, "group": group, "score": None, "detail": f"error game log: {type(e).__name__}"})
            continue

        if len(games) < window:
            rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "not enough games"})
            continue

        # hitter heat (guard)
        if group == "hitting" and hasattr(eng, "hitter_heat_score_z"):
            try:
                info = (eng.hitter_heat_score_z(games, windows=(window,)) or {}).get(window)
            except Exception as e:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": f"error hitter heat: {type(e).__name__}"})
                continue

            if info:
                score = info.get("score")
                comps = info.get("components") or {}
                detail = f"OPS {fmt_z(comps.get('OPS_z'))} | HR {fmt_z(comps.get('HR_z'))} | H {fmt_z(comps.get('H_z'))} | K {fmt_z(comps.get('K_z'))}"
                rows.append({"name": name, "season": season, "group": group, "score": score, "detail": detail})
            else:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "n/a"})
            continue

        # pitcher heat (guard)
        if group == "pitching" and hasattr(eng, "pitcher_heat_score_z"):
            try:
                info = (eng.pitcher_heat_score_z(games, windows=(window,)) or {}).get(window)
            except Exception as e:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": f"error pitcher heat: {type(e).__name__}"})
                continue

            if info:
                score = info.get("score")
                comps = info.get("components") or {}
                detail = f"K/IP {fmt_z(comps.get('KIP_z'))} | ERA {fmt_z(comps.get('ERA_z'))} | BB {fmt_z(comps.get('BB_z'))}"
                rows.append({"name": name, "season": season, "group": group, "score": score, "detail": detail})
            else:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "n/a"})
            continue

        rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "heat functions missing in engine"})

    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or -1e9)))

    cards = ""
    for r in rows:
        score_badge = (
            '<span class="badge text-bg-secondary fs-6">n/a</span>'
            if r["score"] is None
            else f'<span class="badge text-bg-warning fs-6">{float(r["score"]):+.2f}</span>'
        )
        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-start gap-2">
    <div>
      <div class="fw-semibold">{hs(r["name"])} <span class="text-secondary">({hs(r["season"])})</span></div>
      <div class="text-secondary small">{hs(r["group"])} - {hs(r["detail"])}</div>
    </div>
    {score_badge}
  </div>
</div>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <form class="row g-2 align-items-end" action="/leaderboard/heat" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label muted small mb-0">Window</label>
      <select class="form-select" name="window">
        <option value="7" {"selected" if window==7 else ""}>7</option>
        <option value="14" {"selected" if window==14 else ""}>14</option>
        <option value="30" {"selected" if window==30 else ""}>30</option>
      </select>
    </div>
    <div class="col-12 col-md-4 d-grid">
      <button class="btn btn-primary" type="submit">Refresh</button>
    </div>
    <div class="col-12 col-md-6 text-secondary small">
      Heat Score = weighted Z-score blend (higher = hotter).
    </div>
  </form>
</div>
{cards if cards else '<div class="p-3 soft-card text-secondary">Watchlist is empty.</div>'}
"""
    return layout("Heat Board", body)

@app.get("/leaderboard/parks", response_class=HTMLResponse)
def parks_board(window: int = 30):
    # normalize window defensively (handles querystring weirdness)
    try:
        window = int(window)
    except Exception:
        window = 30
    if window not in (7, 14, 30):
        window = 30

    rows = park_leaderboard(window_days=window) or []

    trs = ""
    for i, r in enumerate(rows, start=1):
        venue = r.get("venue", "Unknown")
        games = r.get("games", 0)
        hr_total = r.get("hr_total", 0)
        hrpg = r.get("hr_per_game", 0.0)

        try:
            games_i = int(games or 0)
        except Exception:
            games_i = 0
        try:
            hr_total_i = int(hr_total or 0)
        except Exception:
            hr_total_i = 0
        try:
            hrpg_f = float(hrpg or 0.0)
        except Exception:
            hrpg_f = 0.0

        trs += f"""
<tr>
  <td class="text-secondary">{i}</td>
  <td class="fw-semibold">{hs(venue)}</td>
  <td class="text-center">{games_i}</td>
  <td class="text-center">{hr_total_i}</td>
  <td class="text-center fw-semibold">{hrpg_f:.2f}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/leaderboard/parks" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Window</label>
      <select class="form-select" name="window">
        <option value="7" {"selected" if window==7 else ""}>7 days</option>
        <option value="14" {"selected" if window==14 else ""}>14 days</option>
        <option value="30" {"selected" if window==30 else ""}>30 days</option>
      </select>
    </div>
    <div class="col-6 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Refresh</button>
    </div>
    <div class="col-12 col-md-8 dark-muted small">
      Ranks parks by HR per game using completed MLB games in the selected window.
      Data is cached to keep page loads fast.
    </div>
  </form>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>#</th>
          <th>Park</th>
          <th class="text-center">Games</th>
          <th class="text-center">HR</th>
          <th class="text-center">HR/G</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="5" class="dark-muted">No data yet for this window.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""
    return layout("Park Leaderboard", body)
    
@app.get("/today-ks", response_class=HTMLResponse)
def today_ks_board(window: int = 14, ip_proj: float = 5.5):
    wl = load_watchlist()
    pitchers = [p for p in wl.get("players", []) if p.get("group") == "pitching"]

    try:
        window = int(window)
    except Exception:
        window = 14
    if window not in (7, 14, 30):
        window = 14

    try:
        ip_proj = float(ip_proj)
    except Exception:
        ip_proj = 5.5
    ip_proj = max(1.0, min(9.0, ip_proj))

    rows = []
    for p in pitchers:
        pid = int(p.get("id", 0) or 0)
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or datetime.now().year)

        # season baseline
        try:
            k_ip_season, ip_season, k_season = eng.season_k_per_ip_from_season_stats(pid, season)
        except Exception as e:
            rows.append({"name": name, "k_exp": None, "detail": f"error season baseline: {type(e).__name__}"})
            continue

        if k_ip_season is None:
            rows.append({"name": name, "k_exp": None, "detail": "missing season K/IP"})
            continue

        # recent form (optional blend)
        try:
            games = eng.get_player_game_log(pid, season, "pitching") or []
        except Exception:
            games = []

        k_ip_recent = None
        if games:
            try:
                k_ip_recent, ip_recent, k_recent = eng.last_n_k_per_ip_from_gamelog(games, window)
            except Exception:
                k_ip_recent = None

        k_ip_base = float(k_ip_season)
        if k_ip_recent is not None:
            # blend: emphasize recent a bit but keep stable
            k_ip_base = 0.60 * float(k_ip_recent) + 0.40 * float(k_ip_season)

        # expected Ks
        k_exp = k_ip_base * ip_proj

        detail = f"season K/IP {k_ip_season:.2f}"
        if k_ip_recent is not None:
            detail += f" | last{window} K/IP {k_ip_recent:.2f}"
        detail += f" | IPproj {ip_proj:.1f}"

        rows.append({"name": name, "k_exp": k_exp, "detail": detail})

    rows.sort(key=lambda r: (r["k_exp"] is None, -(r["k_exp"] or -1e9)))

    trs = ""
    for r in rows:
        k_str = "n/a" if r["k_exp"] is None else f"{r['k_exp']:.1f}"
        trs += f"""
<tr class="ks-row" data-name="{hs(r['name']).lower()}">
  <td class="fw-semibold">{hs(r['name'])}</td>
  <td class="text-secondary small">{hs(r['detail'])}</td>
  <td class="text-center fw-semibold">{k_str}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/today-ks" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Window</label>
      <select class="form-select" name="window">
        <option value="7" {"selected" if window==7 else ""}>7</option>
        <option value="14" {"selected" if window==14 else ""}>14</option>
        <option value="30" {"selected" if window==30 else ""}>30</option>
      </select>
    </div>
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Projected IP</label>
      <input class="form-control" name="ip_proj" value="{hs(ip_proj)}">
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label dark-muted small mb-0">Search</label>
      <input id="ksSearch" class="form-control" placeholder="Type a pitcher name...">
    </div>
    <div class="col-12 col-md-4 dark-muted small">
      Expected Ks = blended K/IP * projected IP. Add pitchers to watchlist (group=pitching).
    </div>
  </form>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>Pitcher</th>
          <th>Notes</th>
          <th class="text-center">Exp K</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="3" class="dark-muted">No pitchers in watchlist.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {{
  const input = document.getElementById("ksSearch");
  if (!input) return;
  input.addEventListener("keyup", function() {{
    const q = (input.value || "").toLowerCase();
    document.querySelectorAll(".ks-row").forEach(function(row) {{
      const name = row.getAttribute("data-name") || "";
      row.style.display = (name.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    return layout("Today Pitcher K Board", body)
    
@app.get("/today-hits", response_class=HTMLResponse)
def today_hits_board(ab_proj: float = 3.8):
    wl = load_watchlist()
    hitters = [p for p in wl.get("players", []) if p.get("group") == "hitting"]
    today = today_yyyy_mm_dd()
    default_season = datetime.now().year

    rows = []
    for p in hitters:
        pid = int(p["id"])
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or default_season)

        # Get season hitting stats (you already use get_player_stats elsewhere)
        try:
            st = eng.get_player_stats(pid, "season", "hitting", season=season) or {}
        except Exception:
            st = {}

        # Estimate p_hit_per_ab from season hits/AB
        ab = _to_int(st.get("atBats"))
        hits = _to_int(st.get("hits"))

        if not ab or ab <= 0 or hits is None:
            rows.append({"name": name, "pid": pid, "p": None, "detail": "missing hits/AB"})
            continue

        p_hit_ab = float(hits) / float(ab)
        p_game = model_hit_game_prob(p_hit_ab, ab_proj=ab_proj)

        # Optional: show matchup context if your engine provides it
        ctx_str = ""
        if hasattr(eng, "hr_props_today_context"):
            try:
                ctx = eng.hr_props_today_context(pid, season, today)
            except Exception:
                ctx = None
            if ctx:
                ctx_str = f"{ctx.get('sp_name','tbd')} / {ctx.get('venue_name','tbd')}"
            else:
                ctx_str = "tbd"
        else:
            ctx_str = "tbd"

        rows.append({
            "name": name,
            "pid": pid,
            "p": p_game,
            "detail": f"season H/AB {p_hit_ab:.3f} | ABproj {ab_proj:.1f} | {ctx_str}",
        })

    # Sort best hit prob first, None at bottom
    rows.sort(key=lambda r: (r["p"] is None, -(r["p"] or -1e9)))

    trs = ""
    for r in rows:
        trs += f"""
<tr class="hit-row" data-name="{hs(r['name']).lower()}">
  <td class="fw-semibold">{hs(r['name'])}</td>
  <td class="text-secondary small">{hs(r['detail'])}</td>
  <td class="text-center fw-semibold">{hs(fmt_pct2(r['p']))}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <div class="row g-2 align-items-end">
    <div class="col-12 col-md-4">
      <label class="form-label dark-muted small mb-0">Search</label>
      <input id="hitSearch" class="form-control" placeholder="Type a player name...">
    </div>

    <div class="col-12 col-md-3">
      <label class="form-label dark-muted small mb-0">Projected AB</label>
      <form action="/today-hits" method="get" class="d-flex gap-2">
        <input class="form-control" name="ab_proj" value="{hs(ab_proj)}">
        <button class="btn btn-primary" type="submit">Apply</button>
      </form>
    </div>

    <div class="col-12 col-md-5 dark-muted small">
      Model = 1 - (1 - H/AB)^AB. Uses season H/AB as baseline.
    </div>
  </div>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>Player</th>
          <th>Notes</th>
          <th class="text-center">Hit%</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="3" class="dark-muted">No hitters in watchlist.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {{
  const input = document.getElementById("hitSearch");
  if (!input) return;
  input.addEventListener("keyup", function() {{
    const q = (input.value || "").toLowerCase();
    document.querySelectorAll(".hit-row").forEach(function(row) {{
      const name = row.getAttribute("data-name") || "";
      row.style.display = (name.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    return layout("Today Hit Board", body)
@app.get("/suggest/hitters", response_class=HTMLResponse)
def suggest_hitters(date: str = "", per_team: int = 3, min_pa: int = 50):
    day = _safe_date_yyyy_mm_dd(date)

    # If no games on requested day, jump to next date with games
    games = get_today_games(day)
    if not games:
        nd = next_game_date(day, 30)
        if nd:
            day = nd
            games = get_today_games(day)

    banner = ""
    if date and date.strip() and day != date.strip():
        banner = (
            "<div class='card-dark mb-3 p-3 dark-muted'>"
            f"No games on {hs(date.strip())}. Showing next MLB date: <strong>{hs(day)}</strong>."
            "</div>"
        )

    try:
        per_team = int(per_team)
    except Exception:
        per_team = 3
    per_team = max(1, min(8, per_team))

    try:
        min_pa = int(min_pa)
    except Exception:
        min_pa = 50
    min_pa = max(1, min(300, min_pa))

    team_ids = get_today_team_ids(day)
    season = int(day.split("-")[0])

    cards_html = ""

    for tid in team_ids:
        # roster
        try:
            roster = get_active_roster(tid)
        except Exception:
            roster = []

        # STEP 1: Collect hitter IDs (skip pitchers)
        hitter_ids = []
        meta = {}  # pid -> {name,pos}

        for r in roster:
            person = (r.get("person") or {})
            pid = person.get("id")
            name = person.get("fullName") or ""
            pos = ((r.get("position") or {}).get("abbreviation") or "")

            if not pid or not name or pos == "P":
                continue

            pid = int(pid)
            hitter_ids.append(pid)
            meta[pid] = {"name": name, "pos": pos}

        if not hitter_ids:
            continue

        # STEP 2: Batch fetch stats once
        stats_map = batch_people_season_hitting_stats(hitter_ids, season)

        # STEP 3: Score players
        cand = []
        for pid in hitter_ids:
            st = stats_map.get(pid) or {}

            try:
                pa = int(st.get("plateAppearances") or 0)
                ops_val = st.get("ops")
                ops = float(ops_val) if ops_val is not None else None
            except Exception:
                continue

            if pa < min_pa or ops is None:
                continue

            cand.append({
                "pid": pid,
                "name": meta[pid]["name"],
                "pos": meta[pid]["pos"],
                "ops": st.get("ops"),
                "pa": st.get("plateAppearances"),
                "avg": st.get("avg"),
                "score": float(ops),
            })

        cand.sort(key=lambda x: x["score"], reverse=True)
        top = cand[:per_team]
        if not top:
            continue

        # Team name
        team_name = f"Team {tid}"
        try:
            tdata = mlb_get("/api/v1/teams", params={"teamId": tid})
            teams = tdata.get("teams") or []
            if teams:
                team_name = teams[0].get("name") or team_name
        except Exception:
            pass

        # Render card
        items = ""
        for p in top:
            items += f"""
<div class="d-flex justify-content-between align-items-center py-2 border-bottom border-light border-opacity-10">
  <div>
    <div class="fw-semibold">{hs(p["name"])} <span class="dark-muted small">{hs(p["pos"])}</span></div>
    <div class="dark-muted small">OPS {hs(p["ops"])} | PA {hs(p["pa"])} | AVG {hs(p["avg"])}</div>
  </div>
  <form action="/watchlist/add" method="post" class="m-0">
    <input type="hidden" name="pid" value="{hs(p["pid"])}">
    <input type="hidden" name="name" value="{hs(p["name"])}">
    <input type="hidden" name="season" value="{hs(season)}">
    <button class="btn btn-outline-light btn-sm" type="submit">+ Watch</button>
  </form>
</div>
"""

        cards_html += f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div class="h5 fw-semibold mb-0">{hs(team_name)}</div>
    <div class="dark-muted small">Top {hs(per_team)} by season OPS (min PA {hs(min_pa)})</div>
  </div>
  <hr class="border-light opacity-25">
  {items}
</div>
"""

    body = f"""
{banner}

<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/suggest/hitters" method="get">
    <div class="col-12 col-md-3">
      <label class="form-label dark-muted small mb-0">Date</label>
      <input class="form-control" name="date" value="{hs(day)}">
    </div>
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Per team</label>
      <input class="form-control" name="per_team" value="{hs(per_team)}">
    </div>
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Min PA</label>
      <input class="form-control" name="min_pa" value="{hs(min_pa)}">
    </div>
    <div class="col-12 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Suggest</button>
    </div>
    <div class="col-12 col-md-3 dark-muted small">
      Adds hitters to Watchlist so HR/Heat/Edge boards populate.
    </div>
  </form>
</div>

{cards_html if cards_html else "<div class='card-dark dark-muted p-3'>No suggestions found (try lowering Min PA or pick another date).</div>"}
"""
    return layout("Auto-Suggest Hitters", body)
    
@app.get("/leaderboard/hits", response_class=HTMLResponse)
def hits_board(season: int = 2025, limit: int = 50):
    rows = eng.hits_leaders(season=season, limit=limit) or []

    trs = ""
    for i, r in enumerate(rows, start=1):
        trs += f"""
<tr>
  <td class="text-secondary">{i}</td>
  <td class="fw-semibold">
    <a class="link-primary" href="/player/{hs(r['pid'])}?season={hs(season)}">{hs(r['name'])}</a>
    <span class="text-secondary small ms-2">{hs(r['team'])}</span>
  </td>
  <td class="text-center fw-semibold">{hs(r['hits'])}</td>
  <td class="text-end" style="min-width:140px;">
    <form action="/watchlist/add" method="post" class="m-0">
      <input type="hidden" name="pid" value="{hs(r['pid'])}">
      <input type="hidden" name="name" value="{hs(r['name'])}">
      <input type="hidden" name="season" value="{hs(season)}">
      <button class="btn btn-outline-dark btn-sm" type="submit">+ Watch</button>
    </form>
  </td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/leaderboard/hits" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Season</label>
      <input class="form-control" name="season" value="{hs(season)}">
    </div>
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Limit</label>
      <input class="form-control" name="limit" value="{hs(limit)}">
    </div>
    <div class="col-12 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Refresh</button>
    </div>
  </form>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0 bg-white text-dark">
      <thead>
        <tr>
          <th>#</th>
          <th>Player</th>
          <th class="text-center">Hits</th>
          <th class="text-end">Watch</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="4" class="text-secondary">No data.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""
    return layout(f"Hits Leaders ({hs(season)})", body) 
    
@app.get("/player/{pid}/hr-prop-today", response_class=HTMLResponse)
def player_hr_prop_today(pid: int, season: int = datetime.now().year, window: int = 14, pa_proj: float = 4.2):
    today = today_yyyy_mm_dd()

    # normalize
    try:
        season = int(season)
    except Exception:
        season = datetime.now().year

    try:
        window = int(window)
    except Exception:
        window = 14
    if window not in (7, 14, 30):
        window = 14

    try:
        pa_proj = float(pa_proj)
    except Exception:
        pa_proj = 4.2
    pa_proj = max(1.0, min(6.5, pa_proj))

    # name lookup (safe)
    name = f"Player {pid}"
    try:
        pdata = eng.api_get(f"/people/{pid}")
        people = pdata.get("people") or []
        if people:
            name = people[0].get("fullName") or name
    except Exception:
        pass

    # baseline season HR/PA
    p_season = None
    pa_season = None
    hr_season = None
    try:
        p_season, pa_season, hr_season = eng.season_hr_rate_from_season_stats(pid, season)
    except Exception:
        pass

    # context (SP + Park multipliers)
    ctx = None
    p_adj = float(p_season) if p_season is not None else None
    park_mult = None
    sp_mult = None

    if hasattr(eng, "hr_props_today_context"):
        try:
            ctx = eng.hr_props_today_context(pid, season, today) or None
        except Exception:
            ctx = None

    if ctx:
        park_mult = ctx.get("park_mult")
        sp_mult = ctx.get("sp_mult")

    if p_adj is not None:
        try:
            if park_mult is not None:
                p_adj *= float(park_mult)
        except Exception:
            pass
        try:
            if sp_mult is not None:
                p_adj *= float(sp_mult)
        except Exception:
            pass
        p_adj = min(max(p_adj, 0.00001), 0.25)

    model_p = model_hr_game_prob(p_adj, pa_proj=pa_proj) if p_adj is not None else None

    # recent window z-score (uses your engine binomial z)
    z = None
    hr_win = None
    pa_win = None
    try:
        games = eng.get_player_game_log(pid, season, "hitting") or []
    except Exception:
        games = []

    if p_adj is not None and games and len(games) >= window:
        try:
            pa_win = safe_int(sum(float(g.get("plateAppearances", 0) or 0) for g in games[:window]))
            hr_win = safe_int(sum(float(g.get("homeRuns", 0) or 0) for g in games[:window]))
            if pa_win > 0:
                z = eng.hr_binomial_z(hr_win, pa_win, p_adj)
        except Exception:
            z = None

    # odds + edge (optional)
    odds_obj = load_odds()
    amer = get_odds(pid, today, odds_obj=odds_obj)
    implied = american_to_implied_prob(amer)
    edge = (model_p - implied) if (model_p is not None and implied is not None) else None

    ctx_str = "No game context yet"
    if ctx:
        ctx_str = f"SP: {ctx.get('sp_name','tbd')} | Park: {ctx.get('venue_name','tbd')}"

    odds_val = "" if amer is None else str(amer)
    edge_str = "n/a" if edge is None else f"{edge*100:+.1f}%"

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-0">{hs(name)}</div>
      <div class="dark-muted small">Today HR Prop — {hs(today)} | season {hs(season)}</div>
      <div class="dark-muted small">{hs(ctx_str)}</div>
    </div>
    <div class="d-flex gap-2">
      <a class="btn btn-outline-light" href="/player/{pid}?season={season}">Back</a>
      <a class="btn btn-outline-light" href="/leaderboard/hr-props">HR Props Board</a>
    </div>
  </div>
</div>

<div class="row g-3">
  <div class="col-12 col-lg-6">
    <div class="card-dark p-3">
      <div class="fw-semibold mb-2">Baseline</div>
      <div class="dark-muted small">Season HR/PA: <span class="fw-semibold">{hs(f"{p_season:.4f}" if p_season is not None else "n/a")}</span></div>
      <div class="dark-muted small">Adjusted HR/PA: <span class="fw-semibold">{hs(f"{p_adj:.4f}" if p_adj is not None else "n/a")}</span></div>
      <div class="dark-muted small">Park mult: {hs(f"{float(park_mult):.2f}" if park_mult is not None else "n/a")} | SP mult: {hs(f"{float(sp_mult):.2f}" if sp_mult is not None else "n/a")}</div>
      <div class="dark-muted small">Season totals: HR {hs(hr_season)} | PA {hs(pa_season)}</div>
    </div>
  </div>

  <div class="col-12 col-lg-6">
    <div class="card-dark p-3">
      <div class="fw-semibold mb-2">Today HR%</div>
      <div class="display-6 fw-bold">{hs(fmt_pct(model_p))}</div>
      <div class="dark-muted small">Model = 1 - (1 - HR/PA)^PA (PAproj={hs(pa_proj)})</div>

      <hr class="border-light opacity-25">

      <div class="fw-semibold mb-1">Recent Form</div>
      <div class="dark-muted small">Last {hs(window)}: HR {hs(hr_win)} / PA {hs(pa_win)}</div>
      <div class="mt-2">Z-score: {badge_for_z(z)}</div>
    </div>
  </div>

  <div class="col-12">
    <div class="card-dark p-3">
      <div class="fw-semibold mb-2">Odds & Edge (optional)</div>

      <div class="row g-2 align-items-end">
        <div class="col-12 col-md-5">
          <form action="/odds/set" method="post" class="d-flex gap-2">
            <input type="hidden" name="pid" value="{pid}">
            <input type="hidden" name="date" value="{hs(today)}">
            <input type="hidden" name="next" value="/player/{pid}/hr-prop-today?season={season}&window={window}&pa_proj={hs(pa_proj)}">
            <input class="form-control" name="odds" value="{hs(odds_val)}" placeholder="+320 / -110">
            <button class="btn btn-outline-secondary" type="submit">Save</button>
          </form>
        </div>
        <div class="col-12 col-md-2">
          <form action="/odds/clear" method="post">
            <input type="hidden" name="pid" value="{pid}">
            <input type="hidden" name="date" value="{hs(today)}">
            <input type="hidden" name="next" value="/player/{pid}/hr-prop-today?season={season}&window={window}&pa_proj={hs(pa_proj)}">
            <button class="btn btn-outline-danger w-100" type="submit">Clear</button>
          </form>
        </div>
        <div class="col-12 col-md-5">
          <div class="dark-muted small">Implied: <span class="fw-semibold">{hs(fmt_pct(implied))}</span></div>
          <div class="dark-muted small">Edge: <span class="fw-semibold">{hs(edge_str)}</span></div>
        </div>
      </div>

      <hr class="border-light opacity-25">

      <form class="row g-2 align-items-end" action="/player/{pid}/hr-prop-today" method="get">
        <input type="hidden" name="season" value="{season}">
        <div class="col-6 col-md-2">
          <label class="form-label dark-muted small mb-0">Window</label>
          <select class="form-select" name="window">
            <option value="7" {"selected" if window==7 else ""}>7</option>
            <option value="14" {"selected" if window==14 else ""}>14</option>
            <option value="30" {"selected" if window==30 else ""}>30</option>
          </select>
        </div>
        <div class="col-6 col-md-2">
          <label class="form-label dark-muted small mb-0">PA proj</label>
          <input class="form-control" name="pa_proj" value="{hs(pa_proj)}">
        </div>
        <div class="col-12 col-md-2 d-grid">
          <button class="btn btn-primary" type="submit">Refresh</button>
        </div>
      </form>
    </div>
  </div>
</div>
"""
    return layout("Today HR Prop", body)
        
@app.get("/player/{pid}/rolling", response_class=HTMLResponse)
def player_rolling(pid: int, season: int = datetime.now().year):
    # name
    name = f"Player {pid}"
    try:
        pdata = eng.api_get(f"/people/{pid}")
        people = pdata.get("people") or []
        if people:
            name = people[0].get("fullName") or name
    except Exception:
        pass

    # game log
    try:
        games = eng.get_player_game_log(pid, season, "hitting") or []
    except Exception:
        games = []

    games = _sort_games_most_recent_first(games)

    windows = (7, 14, 30)
    rows = []
    for w in windows:
        rows.append((w, _rolling_metrics_for_window(games, w)))

    def fmt3(x):
        v = _to_float(x)
        return "n/a" if v is None else f"{v:.3f}"

    def fmt2(x):
        v = _to_float(x)
        return "n/a" if v is None else f"{v:.2f}"

    def fmtpct(x):
        v = _to_float(x)
        return "n/a" if v is None else f"{v*100:.1f}%"

    trs = ""
    for w, m in rows:
        if not m:
            trs += f"<tr><td class='fw-semibold'>{w}</td><td colspan='6' class='dark-muted'>not enough games</td></tr>"
            continue

        trs += f"""
<tr>
  <td class="fw-semibold">{w}</td>
  <td class="text-center">{fmt3(m.get("AVG"))}</td>
  <td class="text-center">{fmt3(m.get("OPS"))}</td>
  <td class="text-center">{safe_int(m.get("HR"))}</td>
  <td class="text-center">{fmt2(m.get("HR/G"))}</td>
  <td class="text-center">{safe_int(m.get("PA"))}</td>
  <td class="text-center">{fmtpct(m.get("K%"))}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <div class="h5 fw-semibold mb-0">{hs(name)}</div>
      <div class="dark-muted small">Rolling metrics — season {hs(season)} (most recent games)</div>
    </div>
    <a class="btn btn-outline-light" href="/player/{pid}?season={season}">Back</a>
  </div>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>Window</th>
          <th class="text-center">AVG</th>
          <th class="text-center">OPS</th>
          <th class="text-center">HR</th>
          <th class="text-center">HR/G</th>
          <th class="text-center">PA</th>
          <th class="text-center">K%</th>
        </tr>
      </thead>
      <tbody>{trs}</tbody>
    </table>
  </div>
</div>
"""
    return layout("Rolling 7/14/30", body)
    
@app.get("/player/{pid}/splits", response_class=HTMLResponse)
def player_splits(
    pid: int,
    season: int = datetime.now().year,
    group: str = "hitting",   # <--- add this
):
    group = "pitching" if group == "pitching" else "hitting"

    # name lookup (safe)
    name = f"Player {pid}"
    try:
        pdata = eng.api_get(f"/people/{pid}")
        people = pdata.get("people") or []
        if people:
            name = people[0].get("fullName") or name
    except Exception:
        pass

    # fetch splits (needs engine support for pitching too — step 2)
    try:
        spl = eng.home_away_splits(pid, season, group=group) or {}
    except Exception:
        spl = {}

    home = spl.get("home") or {}
    away = spl.get("away") or {}

    if group == "hitting":
        keys = [
            "gamesPlayed","plateAppearances","atBats","hits",
            "avg","obp","slg","ops",
            "homeRuns","rbi","strikeOuts","baseOnBalls"
        ]
    else:
        keys = [
            "gamesPlayed","gamesStarted",
            "inningsPitched","strikeOuts","baseOnBalls","homeRuns",
            "era","whip"
        ]

    rows = ""
    for k in keys:
        rows += f"""
<tr>
  <td class="text-secondary">{hs(k)}</td>
  <td class="fw-semibold text-center">{hs(home.get(k, "-"))}</td>
  <td class="fw-semibold text-center">{hs(away.get(k, "-"))}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-0">{hs(name)}</div>
      <div class="dark-muted small">Home vs Away splits — {hs(group)} — season {hs(season)}</div>
    </div>

    <div class="d-flex gap-2">
      <a class="btn btn-outline-light btn-sm" href="/player/{pid}/splits?season={season}&group=hitting">Hitting</a>
      <a class="btn btn-outline-light btn-sm" href="/player/{pid}/splits?season={season}&group=pitching">Pitching</a>
      <a class="btn btn-outline-light btn-sm" href="/player/{pid}?season={season}">Back</a>
    </div>
  </div>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm mb-0">
      <thead>
        <tr>
          <th>Stat</th>
          <th class="text-center">Home</th>
          <th class="text-center">Away</th>
        </tr>
      </thead>
      <tbody>
        {rows if (home or away) else "<tr><td colspan='3' class='dark-muted'>No splits found.</td></tr>"}
      </tbody>
    </table>
  </div>
</div>
"""
    return layout("Splits", body)
    
@app.get("/player/{pid}/zscores", response_class=HTMLResponse)
def player_zscores(pid: int, season: int = datetime.now().year):
    # name
    name = f"Player {pid}"
    try:
        pdata = eng.api_get(f"/people/{pid}")
        people = pdata.get("people") or []
        if people:
            name = people[0].get("fullName") or name
    except Exception:
        pass

    # game log
    try:
        games = eng.get_player_game_log(pid, season, "hitting") or []
    except Exception:
        games = []

    games = _sort_games_most_recent_first(games)

    # keep some history so z has something to compute from
    games_hist = games[:60]  # most recent 60
    windows = (7, 14, 30)
    metrics = ("OPS", "AVG", "HR/G", "K%")

    grid_rows = ""

    for w in windows:
        # latest window metrics
        latest = _rolling_metrics_for_window(games_hist, w)
        if not latest:
            grid_rows += f"<tr><td class='fw-semibold'>{w}</td><td colspan='{len(metrics)}' class='dark-muted'>not enough games</td></tr>"
            continue

        tds = ""
        for metric in metrics:
            series = _window_series(games_hist, w, metric)
            mu, sd = mean_std(series)
            z = z_score(_to_float(latest.get(metric)), mu, sd)
            tds += f"<td class='text-center'>{badge_for_z(z)}</td>"

        grid_rows += f"<tr><td class='fw-semibold'>{w}</td>{tds}</tr>"

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <div class="h5 fw-semibold mb-0">{hs(name)}</div>
      <div class="dark-muted small">
        Z-scores compare the most recent window vs the player’s rolling-window history (last 60 games).
      </div>
    </div>
    <a class="btn btn-outline-light" href="/player/{pid}?season={season}">Back</a>
  </div>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>Window</th>
          <th class="text-center">OPS z</th>
          <th class="text-center">AVG z</th>
          <th class="text-center">HR/G z</th>
          <th class="text-center">K% z</th>
        </tr>
      </thead>
      <tbody>{grid_rows}</tbody>
    </table>
  </div>
</div>
"""
    return layout("Z-Scores 7/14/30", body)
    
@app.get("/leaderboard/teams-hot", response_class=HTMLResponse)
def teams_hot_board(window: int = 14):
    if window not in (7, 14, 30):
        window = 14

    rows = hot_teams(window_days=window)

    trs = ""
    for i, r in enumerate(rows, start=1):
        ops_str = "n/a" if r["ops"] is None else f"{r['ops']:.3f}"
        trs += f"""
<tr>
  <td class="text-secondary">{i}</td>
  <td class="fw-semibold">{hs(r['team'])}</td>
  <td class="text-center">{int(r['games'])}</td>
  <td class="text-center">{float(r['hr_g']):.2f}</td>
  <td class="text-center">{float(r['r_g']):.2f}</td>
  <td class="text-center">{hs(ops_str)}</td>
</tr>
"""

    body = f"""
<div class="card-dark mb-3">
  <form class="row g-2 align-items-end" action="/leaderboard/teams-hot" method="get">
    <div class="col-6 col-md-2">
      <label class="form-label dark-muted small mb-0">Window</label>
      <select class="form-select" name="window">
        <option value="7" {"selected" if window==7 else ""}>7 days</option>
        <option value="14" {"selected" if window==14 else ""}>14 days</option>
        <option value="30" {"selected" if window==30 else ""}>30 days</option>
      </select>
    </div>
    <div class="col-6 col-md-2 d-grid">
      <button class="btn btn-primary" type="submit">Refresh</button>
    </div>
    <div class="col-12 col-md-8 dark-muted small">
      Teams ranked by HR per game (then OPS, then runs per game) over the selected window.
      Uses completed games and caches boxscores for speed.
    </div>
  </form>
</div>

<div class="card-dark">
  <div class="table-responsive">
    <table class="table table-sm align-middle mb-0">
      <thead>
        <tr>
          <th>#</th>
          <th>Team</th>
          <th class="text-center">Games</th>
          <th class="text-center">HR/G</th>
          <th class="text-center">R/G</th>
          <th class="text-center">OPS</th>
        </tr>
      </thead>
      <tbody>
        {trs if trs else '<tr><td colspan="6" class="dark-muted">No data found.</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
"""
    return layout("Hot Teams", body)
