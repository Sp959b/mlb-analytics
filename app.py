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

# ----------------------------
# App + UI layout
# ----------------------------
app = FastAPI(title="MLB Analytics")

def layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{hs(title)}</title>

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
