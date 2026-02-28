# -*- coding: utf-8 -*-
"""
MLB Analytics (FastAPI) - Render-safe single-file app
- Mobile-friendly layout (Bootstrap offcanvas)
- Watchlist add/remove (POST)
- ASCII-only strings (no Unicode bullets/emoji) to avoid Render/Python parsing issues
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

import mlb_engine as eng  # your engine module
import requests
from zoneinfo import ZoneInfo

# ----------------------------
# App + storage
# ----------------------------
app = FastAPI(title="MLB HR Props App")

# NOTE:
# - /tmp is writable on Render but NOT persistent across deploys/restarts.
# - If you add a Render Persistent Disk, set this to something like /var/data/watchlist.json
WATCHLIST_PATH = Path("/tmp/watchlist.json")

TEAM_CACHE_DIR = Path("/tmp/team_cache")
TEAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)

GAME_CACHE_DIR = Path("/tmp/game_cache")
GAME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def cache_read(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def cache_write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    
def load_watchlist() -> Dict[str, List[Dict[str, Any]]]:
    try:
        if WATCHLIST_PATH.exists():
            return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"players": []}  # each: {"id": int, "name": str, "season": int, "group": "hitting"}


def save_watchlist(wl: Dict[str, Any]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(wl, indent=2), encoding="utf-8")


def add_watch(pid: int, name: str, season: int, group: str = "hitting") -> None:
    wl = load_watchlist()
    players = wl.get("players", [])
    for p in players:
        if int(p.get("id", -1)) == int(pid) and int(p.get("season", -1)) == int(season) and p.get("group") == group:
            return
    players.append({"id": int(pid), "name": name, "season": int(season), "group": group})
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
ODDS_PATH = Path("/tmp/odds.json")  # change to /var/data/odds.json if you later add a Render disk

def load_odds() -> dict:
    try:
        if ODDS_PATH.exists():
            return json.loads(ODDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"odds": {}}

def save_odds(obj: dict) -> None:
    ODDS_PATH.write_text(json.dumps(obj, indent=2), encoding="utf-8")

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

def get_odds(pid: int, date: str) -> int | None:
    obj = load_odds()
    rec = obj.get("odds", {}).get(odds_key(pid, date))
    if not rec:
        return None
    try:
        return int(rec.get("odds"))
    except Exception:
        return None

def american_to_implied_prob(odds: int | None) -> float | None:
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

def fmt_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{p*100:.1f}%"

def model_hr_game_prob(p_hr_per_pa: float, pa_proj: float = 4.2) -> float:
    # P(HR >= 1) = 1 - (1 - p)^PA
    p = max(0.0000001, min(0.25, float(p_hr_per_pa)))
    pa = max(1.0, float(pa_proj))
    return 1.0 - (1.0 - p) ** pa

PARK_CACHE_DIR = Path("/tmp/park_cache")
PARK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def cache_read(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def cache_write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

# ----------------------------
# UI helpers
# ----------------------------
def get_boxscore_cached(game_pk: int) -> dict | None:
    p = GAME_CACHE_DIR / f"box_{int(game_pk)}.json"
    cached = cache_read(p)
    if cached:
        return cached
    try:
        data = mlb_get(f"/api/v1/game/{int(game_pk)}/boxscore")
        cache_write(p, data)
        return data
    except Exception:
        return None

def extract_team_batting_stats(box: dict, side: str) -> dict | None:
    """
    side: 'home' or 'away'
    Returns: {"team": str, "team_id": int, "hr": int, "r": int, "pa": int|None, "ops": float|None}
    """
    try:
        teams = (box.get("teams") or {})
        t = teams.get(side) or {}
        team = (t.get("team") or {})
        team_name = team.get("name") or side.title()
        team_id = team.get("id")

        batting = (t.get("teamStats") or {}).get("batting") or {}
        hr = batting.get("homeRuns")
        r = batting.get("runs")
        ops = batting.get("ops")
        pa = batting.get("plateAppearances")

        # Coerce types safely
        hr = int(hr) if hr is not None else None
        r = int(r) if r is not None else None
        pa = int(pa) if pa is not None else None
        ops = float(ops) if ops is not None else None

        if hr is None or r is None:
            return None

        return {"team": team_name, "team_id": team_id, "hr": hr, "r": r, "pa": pa, "ops": ops}
    except Exception:
        return None
        
def _date_str(dt_obj: datetime) -> str:
    return dt_obj.strftime("%Y-%m-%d")

def fetch_game_total_hr(game_pk: int) -> int | None:
    """
    Returns total HR in game (away HR + home HR), or None if missing.
    """
    try:
        box = mlb_get(f"/api/v1/game/{int(game_pk)}/boxscore")
        teams = (box.get("teams") or {})
        away = (teams.get("away") or {}).get("teamStats", {}).get("batting", {})
        home = (teams.get("home") or {}).get("teamStats", {}).get("batting", {})
        hr_away = away.get("homeRuns")
        hr_home = home.get("homeRuns")
        if hr_away is None or hr_home is None:
            return None
        return int(hr_away) + int(hr_home)
    except Exception:
        return None

def park_leaderboard(window_days: int = 30) -> list[dict]:
    """
    Aggregates completed games in the last window_days by venue.
    Uses caching so it does not hammer the API on every page load.
    """
    if window_days not in (7, 14, 30):
        window_days = 30

    today = datetime.now(LA_TZ).date()
    start = today - timedelta(days=window_days)
    end = today

    cache_key = f"parks_{_date_str(datetime.combine(start, datetime.min.time()))}_{_date_str(datetime.combine(end, datetime.min.time()))}.json"
    cache_path = PARK_CACHE_DIR / cache_key

    cached = cache_read(cache_path)
    if cached and isinstance(cached.get("rows"), list):
        return cached["rows"]

    # Pull schedule for date range
    sched = mlb_get(
        "/api/v1/schedule",
        params={
            "sportId": 1,
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "hydrate": "venue",
        },
    )

    dates = sched.get("dates") or []
    venue_map: dict[str, dict] = {}

    # Loop through games; only count Final games
    for d in dates:
        games = d.get("games") or []
        for g in games:
            status = ((g.get("status") or {}).get("detailedState") or "")
            if status != "Final":
                continue

            game_pk = g.get("gamePk")
            venue = (g.get("venue") or {})
            venue_name = venue.get("name") or "Unknown Park"
            venue_id = venue.get("id") or ""

            total_hr = fetch_game_total_hr(game_pk) if game_pk else None
            if total_hr is None:
                continue

            k = f"{venue_id}|{venue_name}"
            rec = venue_map.get(k)
            if not rec:
                rec = {"venue": venue_name, "venue_id": venue_id, "games": 0, "hr_total": 0}
                venue_map[k] = rec

            rec["games"] += 1
            rec["hr_total"] += int(total_hr)

    rows = []
    for rec in venue_map.values():
        games = rec["games"]
        hr_total = rec["hr_total"]
        hr_per_game = (hr_total / games) if games > 0 else 0.0
        rows.append({
            "venue": rec["venue"],
            "games": games,
            "hr_total": hr_total,
            "hr_per_game": hr_per_game,
        })

    rows.sort(key=lambda r: (-r["hr_per_game"], -r["games"], r["venue"]))

    cache_write(cache_path, {"rows": rows})
    return rows
    
def layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">

  <style>
    body {{
      font-family: 'Inter', sans-serif;
      background: #0f1115;
      color: #e6e6e6;
    }}

    .sidebar {{
      background: #161a22;
    }}
    .sidebar a {{
      color: #aaa;
      text-decoration: none;
      display: block;
      padding: 10px 0;
    }}
    .sidebar a:hover {{
      color: #fff;
    }}

    .card-dark {{
      background: #1b2029;
      border-radius: 16px;
      padding: 16px;
      border: 1px solid rgba(255,255,255,0.06);
    }}

    /* "soft-card" = light card used in a dark app */
    .soft-card {{
      background: #ffffff;
      color: #111;
      border-radius: 16px;
      border: 1px solid rgba(0,0,0,0.08);
    }}

    .muted {{
      color: rgba(0,0,0,0.55);
    }}
    /* dark-muted for dark cards */
    .dark-muted {{
      color: rgba(255,255,255,0.65);
    }}

    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }}

    /* Make bootstrap table readable on dark background when inside card-dark */
    .card-dark .table {{
      color: #e6e6e6;
    }}
    .card-dark .table td {{
      border-color: rgba(255,255,255,0.08);
    }}
  </style>
</head>

<body>

<!-- Mobile Navbar -->
<nav class="navbar navbar-dark bg-dark d-lg-none">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold">MLB Analytics</span>
    <button class="navbar-toggler" type="button" data-bs-toggle="offcanvas" data-bs-target="#mobileSidebar">
      <span class="navbar-toggler-icon"></span>
    </button>
  </div>
</nav>

<!-- Mobile Offcanvas -->
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
    <a href="/leaderboard/parks">Parks</a>
    <a href="/leaderboard/teams-hot">Hot Teams</a>
    <a href="/watchlist">Watchlist</a>
    <a href="/leaderboard/hr-props">HR Board</a>
    <a href="/leaderboard/heat">Heat Board</a>
  </div>
</div>

<div class="container-fluid">
  <div class="row">

    <!-- Desktop Sidebar -->
    <nav class="col-lg-2 d-none d-lg-block sidebar min-vh-100 p-4">
      <h4 class="fw-bold mb-4">MLB Analytics</h4>
      <a href="/">Dashboard</a>
      <a href="/today-edge">Today Edge</a>
      <a href="/today">Today</a>
      <a href="/today-hitters">Today's Hitters</a>
      <a href="/leaderboard/parks">Parks</a>
      <a href="/leaderboard/teams-hot">Hot Teams</a>
      <a href="/watchlist">Watchlist</a>
      <a href="/leaderboard/hr-props">HR Board</a>
      <a href="/leaderboard/heat">Heat Board</a>
    </nav>

    <!-- Main -->
    <main class="col-12 col-lg-10 p-4">
      <h2 class="fw-bold mb-4">{title}</h2>
      {body}
    </main>

  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


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


def _mean(vals: List[Optional[float]]) -> Optional[float]:
    v = [x for x in vals if x is not None]
    if not v:
        return None
    return sum(v) / len(v)


def mean_std(values: List[Optional[float]]) -> (Optional[float], Optional[float]):
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
    sign = "+" if z > 0 else ""
    return f"{sign}{z:.2f}"


def _fmt(x: Optional[float], d: int = 3) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{d}f}"


def _fmt_delta(x: Optional[float], d: int = 3) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.{d}f}"
LA_TZ = ZoneInfo("America/Los_Angeles")

def _safe_date_yyyy_mm_dd(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return datetime.now(LA_TZ).strftime("%Y-%m-%d")
    # very small validation
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return datetime.now(LA_TZ).strftime("%Y-%m-%d")

def mlb_api_get(path: str, params: dict | None = None) -> dict:
    url = f"https://statsapi.mlb.com{path}"
    r = requests.get(url, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()

def parse_mlb_utc_to_la(iso_utc: str) -> str:
    # MLB often returns "2026-02-27T20:10:00Z"
    if not iso_utc:
        return "tbd"
    try:
        dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        dt_la = dt_utc.astimezone(LA_TZ)
        return dt_la.strftime("%-I:%M %p PT")
    except Exception:
        return "tbd"

MLB_BASE = "https://statsapi.mlb.com"
LA_TZ = ZoneInfo("America/Los_Angeles")

HTTP = requests.Session()

def mlb_get(path: str, params: dict | None = None) -> dict:
    r = HTTP.get(f"{MLB_BASE}{path}", params=params or {}, timeout=8)
    r.raise_for_status()
    return r.json()

def fetch_people_names(person_ids: list[int]) -> dict[int, str]:
    ids = [i for i in sorted(set(person_ids)) if i not in NAME_CACHE]
    if not ids:
        return {}

    try:
        pdata = mlb_get(
            "/api/v1/people",
            params={"personIds": ",".join(map(str, ids))}
        )
        people = pdata.get("people") or []
        out = {}
        for p in people:
            pid = p.get("id")
            nm = p.get("fullName")
            if pid and nm:
                out[int(pid)] = nm
        return out
    except Exception:
        return {}
        
def today_yyyy_mm_dd() -> str:
    return datetime.now(LA_TZ).strftime("%Y-%m-%d")

def fmt_time_pt(iso_utc: str) -> str:
    if not iso_utc:
        return "tbd"
    try:
        dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        dt_pt = dt_utc.astimezone(LA_TZ)
        return dt_pt.strftime("%-I:%M %p PT")
    except Exception:
        return "tbd"

def get_today_games(day: str) -> list[dict]:
    sched = mlb_get("/api/v1/schedule", params={"sportId": 1, "date": day, "hydrate": "venue,probablePitcher"})
    dates = sched.get("dates") or []
    return (dates[0].get("games") if dates else []) or []

NAME_CACHE: dict[int, str] = {}

def extract_lineup_hitters(feed: dict, side: str) -> list[dict]:
    """
    side: 'home' or 'away'
    Returns list of hitters with pid/name/battingOrder/pos.
    """
    out = []

    box = (feed.get("liveData") or {}).get("boxscore") or {}
    teams = box.get("teams") or {}
    t = teams.get(side) or {}
    batters = t.get("batters") or []
    players = box.get("players") or {}

    missing: list[int] = []

    # First pass: take names from boxscore if present, collect missing ids
    for pid in batters:
        pid_int = int(pid)
        p = players.get(f"ID{pid_int}") or {}
        person = p.get("person") or {}
        name = person.get("fullName")

        if name:
            NAME_CACHE[pid_int] = name
        else:
            # maybe already cached from earlier games
            if pid_int not in NAME_CACHE:
                missing.append(pid_int)

    # Batch fetch missing names (ONE request)
    fetched = fetch_people_names(missing)
    for k, v in fetched.items():
        NAME_CACHE[k] = v

    # Build output
    for pid in batters:
        pid_int = int(pid)
        p = players.get(f"ID{pid_int}") or {}

        name = NAME_CACHE.get(pid_int) or f"ID {pid_int}"
        bo = p.get("battingOrder")
        pos = (p.get("position") or {}).get("abbreviation") or ""

        out.append({
            "pid": pid_int,
            "name": name,
            "battingOrder": bo or "",
            "pos": pos,
        })

    return out
def hot_teams(window_days: int = 14) -> list[dict]:
    if window_days not in (7, 14, 30):
        window_days = 14

    today = datetime.now(LA_TZ).date()
    start = today - timedelta(days=window_days)
    end = today

    cache_path = TEAM_CACHE_DIR / f"hot_teams_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.json"
    cached = cache_read(cache_path)
    if cached and isinstance(cached.get("rows"), list):
        return cached["rows"]

    sched = mlb_get(
        "/api/v1/schedule",
        params={
            "sportId": 1,
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
        },
    )

    agg: dict[int, dict] = {}  # team_id -> totals

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

            for side in ("away", "home"):
                st = extract_team_batting_stats(box, side)
                if not st:
                    continue

                tid = int(st.get("team_id") or 0)
                if tid == 0:
                    continue

                rec = agg.get(tid)
                if not rec:
                    rec = {
                        "team": st["team"],
                        "team_id": tid,
                        "games": 0,
                        "hr_total": 0,
                        "r_total": 0,
                        "pa_total": 0,
                        "ops_pa_sum": 0.0,  # ops * pa
                        "ops_games_sum": 0.0,  # fallback: ops per game
                        "ops_games_n": 0,
                    }
                    agg[tid] = rec

                rec["games"] += 1
                rec["hr_total"] += int(st["hr"])
                rec["r_total"] += int(st["r"])

                ops = st.get("ops")
                pa = st.get("pa")

                if ops is not None and pa is not None and pa > 0:
                    rec["pa_total"] += int(pa)
                    rec["ops_pa_sum"] += float(ops) * int(pa)
                elif ops is not None:
                    rec["ops_games_sum"] += float(ops)
                    rec["ops_games_n"] += 1

    rows = []
    for rec in agg.values():
        g = rec["games"]
        hr_g = rec["hr_total"] / g if g else 0.0
        r_g = rec["r_total"] / g if g else 0.0

        ops = None
        if rec["pa_total"] > 0:
            ops = rec["ops_pa_sum"] / rec["pa_total"]
        elif rec["ops_games_n"] > 0:
            ops = rec["ops_games_sum"] / rec["ops_games_n"]

        rows.append({
            "team": rec["team"],
            "games": g,
            "hr_total": rec["hr_total"],
            "r_total": rec["r_total"],
            "hr_g": hr_g,
            "r_g": r_g,
            "ops": ops,
        })

    # Sort by HR/G, then OPS, then R/G
    rows.sort(key=lambda r: (-(r["hr_g"]), -(r["ops"] if r["ops"] is not None else -999), -(r["r_g"])))

    cache_write(cache_path, {"rows": rows})
    return rows
    
# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def home():

    # Pull previews
    try:
        edge_preview = today_edge_board_data(limit=5)
    except:
        edge_preview = []

    try:
        hot_teams_preview = hot_teams(window_days=7)[:5]
    except:
        hot_teams_preview = []

    body = f"""
<div class="card-dark mb-4 p-4">
  <div class="display-6 fw-bold">MLB Betting Analytics</div>
  <div class="dark-muted mt-2">
    Identify HR edges, hot offenses, favorable parks, and sharp betting spots.
  </div>

  <div class="mt-4 d-flex gap-3 flex-wrap">
    <a class="btn btn-danger btn-lg" href="/leaderboard/today-edge">Today Edge Board</a>
    <a class="btn btn-primary btn-lg" href="/leaderboard/hr-props">HR Props Board</a>
    <a class="btn btn-warning btn-lg" href="/leaderboard/teams-hot">Hot Teams</a>
    <a class="btn btn-outline-light btn-lg" href="/leaderboard/parks">Park Board</a>
  </div>
</div>

<div class="card-dark mb-4 p-3">
  <form class="d-flex gap-2" action="/search" method="get">
    <input class="form-control form-control-lg" name="q"
           placeholder="Search player (e.g., Aaron Judge)" autocomplete="off">
    <button class="btn btn-primary btn-lg" type="submit">Search</button>
  </form>
</div>

<div class="row g-3">

  <div class="col-12 col-lg-6">
    <div class="card-dark p-3">
      <div class="fw-semibold mb-2">Top HR Edges Today</div>
      {
        "".join(
            f"<div>{r.get('name','')} <span class='dark-muted small'>{r.get('edge','')}</span></div>"
            for r in edge_preview
        ) if edge_preview else "<div class='dark-muted'>No data yet.</div>"
      }
    </div>
  </div>

  <div class="col-12 col-lg-6">
    <div class="card-dark p-3">
      <div class="fw-semibold mb-2">Hottest Teams (7d)</div>
      {
        "".join(
            f"<div>{r.get('team','')} — HR/G {r.get('hr_g',0):.2f}</div>"
            for r in hot_teams_preview
        ) if hot_teams_preview else "<div class='dark-muted'>No data yet.</div>"
      }
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
   href="/player/{pid}">
  <div>
    <div class="fw-semibold">{full}</div>
    <div class="small text-secondary">{pos} - {team}</div>
  </div>
  <span class="badge text-bg-secondary mono">ID {pid}</span>
</a>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <form class="d-flex gap-2" action="/search" method="get">
    <input class="form-control form-control-lg" name="q" value="{q}" placeholder="Aaron Judge">
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
  <input type="hidden" name="name" value="{name}">
  <input type="hidden" name="season" value="{season}">
  <button class="btn btn-primary" type="submit">+ Watchlist</button>
</form>
"""
    )

    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h4 mb-0 fw-semibold">{name}</div>
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
        rows += f"<tr><td class='dark-muted'>{k}</td><td class='fw-semibold'>{st.get(k, '-')}</td></tr>"

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <div class="h5 fw-semibold mb-0">Season {season} - {group.title()}</div>
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
      <div class="fw-semibold">{p.get("name","-")}</div>
      <div class="muted small">season {p.get("season","-")} - id {p.get("id","-")}</div>
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


@app.get("/leaderboard/hr-props", response_class=HTMLResponse)
def hr_props_leaderboard(window: int = 7, min_pa: int = 20):
    wl = load_watchlist()
    hitters = [p for p in wl.get("players", []) if p.get("group") == "hitting"]
    today = datetime.now().strftime("%Y-%m-%d")

    if window not in (7, 14, 30):
        window = 7

    rows = []
    for p in hitters:
        pid = int(p["id"])
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or datetime.now().year)

        p_season, pa_season, _ = eng.season_hr_rate_from_season_stats(pid, season)
        if p_season is None:
            rows.append({"name": name, "season": season, "z": None, "detail": "no season baseline"})
            continue

        games = eng.get_player_game_log(pid, season, "hitting") or []
        if len(games) < window:
            rows.append({"name": name, "season": season, "z": None, "detail": "not enough games"})
            continue

        pa_win = safe_int(sum(float(g.get("plateAppearances", 0) or 0) for g in games[:window]))
        hr_win = safe_int(sum(float(g.get("homeRuns", 0) or 0) for g in games[:window]))

        if pa_win < int(min_pa):
            rows.append({"name": name, "season": season, "z": None, "detail": f"PA too low ({pa_win} < {min_pa})"})
            continue

        # optional context
        ctx_str = ""
        p_adj = p_season
        if hasattr(eng, "hr_props_today_context"):
            try:
                ctx = eng.hr_props_today_context(pid, season, today)
            except Exception:
                ctx = None
            if ctx:
                park_mult = ctx.get("park_mult")
                sp_mult = ctx.get("sp_mult")
                if park_mult is not None:
                    p_adj *= park_mult
                if sp_mult is not None:
                    p_adj *= sp_mult
                ctx_str = f" | SP {ctx.get('sp_name','?')} HR/9={ctx.get('sp_hr9','n/a')} | Park {ctx.get('venue_name','?')}"

        p_adj = min(max(p_adj, 0.00001), 0.25)
        z = eng.hr_binomial_z(hr_win, pa_win, p_adj)
        detail = f"HR {hr_win}/PA {pa_win} | season HR/PA {p_season:.4f} | adj {p_adj:.4f}{ctx_str}"
        rows.append({"name": name, "season": season, "z": z, "detail": detail})

    rows.sort(key=lambda r: (-r["z"]) if r["z"] is not None else 10**9)

    cards = ""
    for r in rows:
        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-start gap-2">
    <div>
      <div class="fw-semibold">{r["name"]} <span class="text-secondary">({r["season"]})</span></div>
      <div class="text-secondary small">{r["detail"]}</div>
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
      <input class="form-control" name="min_pa" value="{min_pa}">
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
    wl = load_watchlist()
    players = wl.get("players", [])
    if window not in (7, 14, 30):
        window = 7

    rows = []
    for p in players:
        pid = int(p["id"])
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or datetime.now().year)
        group = p.get("group", "hitting")

        games = eng.get_player_game_log(pid, season, group) or []
        if len(games) < window:
            rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "not enough games"})
            continue

        if group == "hitting" and hasattr(eng, "hitter_heat_score_z"):
            info = (eng.hitter_heat_score_z(games, windows=(window,)) or {}).get(window)
            if info:
                score = info.get("score")
                comps = info.get("components") or {}
                detail = f"OPS {fmt_z(comps.get('OPS_z'))} | HR {fmt_z(comps.get('HR_z'))} | H {fmt_z(comps.get('H_z'))} | K {fmt_z(comps.get('K_z'))}"
                rows.append({"name": name, "season": season, "group": group, "score": score, "detail": detail})
            else:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "n/a"})
            continue

        if group == "pitching" and hasattr(eng, "pitcher_heat_score_z"):
            info = (eng.pitcher_heat_score_z(games, windows=(window,)) or {}).get(window)
            if info:
                score = info.get("score")
                comps = info.get("components") or {}
                detail = f"K/IP {fmt_z(comps.get('KIP_z'))} | ERA {fmt_z(comps.get('ERA_z'))} | BB {fmt_z(comps.get('BB_z'))}"
                rows.append({"name": name, "season": season, "group": group, "score": score, "detail": detail})
            else:
                rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "n/a"})
            continue

        rows.append({"name": name, "season": season, "group": group, "score": None, "detail": "heat functions missing in engine"})

    rows.sort(key=lambda r: (-r["score"]) if r["score"] is not None else 10**9)

    cards = ""
    for r in rows:
        score_badge = (
            '<span class="badge text-bg-secondary fs-6">n/a</span>'
            if r["score"] is None
            else f'<span class="badge text-bg-warning fs-6">{r["score"]:+.2f}</span>'
        )
        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-start gap-2">
    <div>
      <div class="fw-semibold">{r["name"]} <span class="text-secondary">({r["season"]})</span></div>
      <div class="text-secondary small">{r["group"]} - {r["detail"]}</div>
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


@app.get("/player/{pid}/rolling", response_class=HTMLResponse)
def player_rolling(pid: int, season: int = datetime.now().year):
    group = "hitting"
    games = eng.get_player_game_log(pid, season, group) or []
    if not games:
        return layout("Rolling", "<div class='p-3 soft-card text-secondary'>No game log found.</div>")

    season_stat = eng.get_player_stats(pid, "season", group, season=season) or {}
    ops_season = _to_float(season_stat.get("ops"))

    windows = [7, 14, 30]
    cards = ""

    for w in windows:
        chunk = games[:w]
        ops_m = _mean([_to_float(g.get("ops")) for g in chunk])
        hr_m = _mean([float(_to_int(g.get("homeRuns")) or 0) for g in chunk])
        h_m = _mean([float(_to_int(g.get("hits")) or 0) for g in chunk])
        k_m = _mean([float(_to_int(g.get("strikeOuts")) or 0) for g in chunk])

        d_ops = (ops_m - ops_season) if (ops_m is not None and ops_season is not None) else None

        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="d-flex justify-content-between align-items-center">
    <div class="fw-semibold">Last {w} games</div>
    <a class="btn btn-outline-secondary btn-sm" href="/player/{pid}?season={season}">Back</a>
  </div>
  <div class="text-secondary small mt-1">Means per game (using game log)</div>
  <div class="mt-2">
    <div>OPS: <strong>{_fmt(ops_m,3)}</strong> <span class="text-secondary"> (delta vs season: {_fmt_delta(d_ops,3)})</span></div>
    <div>HR/G: <strong>{_fmt(hr_m,3)}</strong></div>
    <div>H/G: <strong>{_fmt(h_m,3)}</strong></div>
    <div>K/G: <strong>{_fmt(k_m,3)}</strong></div>
  </div>
</div>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="h5 fw-semibold mb-0">Rolling 7/14/30 (Hitting)</div>
  <div class="text-secondary">Player <span class="mono">{pid}</span> - Season {season}</div>
</div>
{cards}
"""
    return layout("Rolling 7/14/30", body)

@app.get("/today", response_class=HTMLResponse)
def today_games(date: str = ""):
    day = _safe_date_yyyy_mm_dd(date)
    year = int(day.split("-")[0])

    data = mlb_api_get(
        "/api/v1/schedule",
        params={
            "sportId": 1,
            "date": day,
            "hydrate": "team,venue,probablePitcher"
        },
    )

    dates = data.get("dates") or []
    games = (dates[0].get("games") if dates else []) or []

    cards = ""
    for g in games:
        home = ((g.get("teams") or {}).get("home") or {}).get("team") or {}
        away = ((g.get("teams") or {}).get("away") or {}).get("team") or {}
        home_name = home.get("name") or "Home"
        away_name = away.get("name") or "Away"

        venue = (g.get("venue") or {}).get("name") or "Venue tbd"
        start = parse_mlb_utc_to_la(g.get("gameDate") or "")

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
                return f'<a class="link-light" href="{link}">{name}</a>'
            return f'<span class="dark-muted">{name}</span>'

        cards += f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-1">{away_name} at {home_name}</div>
      <div class="dark-muted small">{day} - {start} - {venue}</div>
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
      <input class="form-control" name="date" value="{day}">
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
    return layout("Today Games", body)
    
@app.get("/player/{pid}/zscores", response_class=HTMLResponse)
def player_zscores(pid: int, season: int = datetime.now().year):
    group = "hitting"
    games = eng.get_player_game_log(pid, season, group) or []
    if len(games) < 5:
        return layout("Z-Scores", "<div class='p-3 soft-card text-secondary'>Not enough game log data for Z-scores.</div>")

    dist_ops = [_to_float(g.get("ops")) for g in games]
    dist_hr = [float(_to_int(g.get("homeRuns")) or 0) for g in games]
    dist_hits = [float(_to_int(g.get("hits")) or 0) for g in games]
    dist_k = [float(_to_int(g.get("strikeOuts")) or 0) for g in games]

    ops_mu, ops_sd = mean_std(dist_ops)
    hr_mu, hr_sd = mean_std(dist_hr)
    h_mu, h_sd = mean_std(dist_hits)
    k_mu, k_sd = mean_std(dist_k)

    def rolling_mean(key: str, n: int) -> Optional[float]:
        chunk = games[:n]
        vals: List[Optional[float]] = []
        for g in chunk:
            if key in ("homeRuns", "hits", "strikeOuts"):
                vals.append(float(_to_int(g.get(key)) or 0))
            else:
                vals.append(_to_float(g.get(key)))
        return _mean(vals)

    windows = [7, 14, 30]
    cards = ""

    for w in windows:
        ops_m = rolling_mean("ops", w)
        hr_m = rolling_mean("homeRuns", w)
        h_m = rolling_mean("hits", w)
        k_m = rolling_mean("strikeOuts", w)

        ops_z = z_score(ops_m, ops_mu, ops_sd)
        hr_zv = z_score(hr_m, hr_mu, hr_sd)
        h_zv = z_score(h_m, h_mu, h_sd)
        k_zv = z_score(k_m, k_mu, k_sd)

        cards += f"""
<div class="p-3 soft-card mb-2">
  <div class="fw-semibold">Last {w} games</div>
  <div class="text-secondary small">Z-score: +2 hot, 0 normal, -2 cold</div>

  <div class="mt-2">
    <div>OPS mean: <strong>{_fmt(ops_m,3)}</strong> <span class="badge text-bg-primary">{fmt_z(ops_z)}</span></div>
    <div>HR/G mean: <strong>{_fmt(hr_m,3)}</strong> <span class="badge text-bg-primary">{fmt_z(hr_zv)}</span></div>
    <div>H/G mean: <strong>{_fmt(h_m,3)}</strong> <span class="badge text-bg-primary">{fmt_z(h_zv)}</span></div>
    <div>K/G mean: <strong>{_fmt(k_m,3)}</strong> <span class="badge text-bg-primary">{fmt_z(k_zv)}</span></div>
  </div>
</div>
"""

    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="d-flex justify-content-between align-items-center">
    <div>
      <div class="h5 fw-semibold mb-0">Z-Scores 7/14/30 (Hitting)</div>
      <div class="text-secondary">Player <span class="mono">{pid}</span> - Season {season}</div>
    </div>
    <a class="btn btn-outline-secondary" href="/player/{pid}?season={season}">Back</a>
  </div>
</div>
{cards}
"""
    return layout("Z-Scores", body)
@app.post("/odds/set")
def odds_set(
    pid: int = Form(...),
    date: str = Form(...),
    odds: int = Form(...),
    next: str = Form("/today-edge"),
):
    date = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    set_odds(int(pid), date, int(odds))
    return RedirectResponse(next, status_code=303)

@app.post("/odds/clear")
def odds_clear(
    pid: int = Form(...),
    date: str = Form(...),
    next: str = Form("/today-edge"),
):
    date = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    clear_odds(int(pid), date)
    return RedirectResponse(next, status_code=303)
@app.get("/today-edge", response_class=HTMLResponse)
def today_edge_board(pa_proj: float = 4.2):
    wl = load_watchlist()
    hitters = [p for p in wl.get("players", []) if p.get("group") == "hitting"]

    today = datetime.now().strftime("%Y-%m-%d")
    season = datetime.now().year

    rows = []
    for p in hitters:
        pid = int(p["id"])
        name = p.get("name") or f"ID {pid}"
        season = int(p.get("season") or season)

        # baseline HR/PA from season stats
        p_season, pa_season, hr_season = eng.season_hr_rate_from_season_stats(pid, season)
        if p_season is None:
            rows.append({
                "name": name, "pid": pid, "season": season,
                "model_p": None, "implied": None, "edge": None,
                "ctx": "no season baseline", "p_adj": None
            })
            continue

        # context (park + opposing pitcher multipliers) if available
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
                try: p_adj *= float(park_mult)
                except Exception: pass
            if sp_mult is not None:
                try: p_adj *= float(sp_mult)
                except Exception: pass

        # clamp to sane range
        p_adj = min(max(p_adj, 0.00001), 0.25)

        model_p = model_hr_game_prob(p_adj, pa_proj=pa_proj)

        # odds + implied + edge
        amer = get_odds(pid, today)
        implied = american_to_implied_prob(amer)
        edge = (model_p - implied) if (implied is not None) else None

        # readable context line
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

    # Sort: best edge first, then model_p
    def sort_key(r):
        e = r["edge"]
        mp = r["model_p"]
        return (-(e if e is not None else -999), -(mp if mp is not None else -999))

    rows.sort(key=sort_key)

    # Build table rows
    trs = ""
    for r in rows:
        odds_val = "" if r.get("odds") is None else str(r["odds"])
        edge_str = "n/a" if r["edge"] is None else f"{r['edge']*100:+.1f}%"

        trs += f"""
<tr class="edge-row" data-name="{r['name'].lower()}">
  <td class="fw-semibold">{r['name']}</td>
  <td class="text-secondary small">{r['ctx']}</td>
  <td class="text-center">{fmt_pct(r['model_p'])}</td>
  <td class="text-center">{fmt_pct(r['implied'])}</td>
  <td class="text-center fw-semibold">{edge_str}</td>
  <td style="min-width:260px;">
    <div class="d-flex gap-2 flex-wrap">
      <form action="/odds/set" method="post" class="d-flex gap-2">
        <input type="hidden" name="pid" value="{r['pid']}">
        <input type="hidden" name="date" value="{today}">
        <input type="hidden" name="next" value="/today-edge?pa_proj={pa_proj}">
        <input class="form-control form-control-sm" name="odds" value="{odds_val}" placeholder="+320 / -110" style="max-width:120px;">
        <button class="btn btn-outline-secondary btn-sm" type="submit">Save</button>
      </form>

      <form action="/odds/clear" method="post">
        <input type="hidden" name="pid" value="{r['pid']}">
        <input type="hidden" name="date" value="{today}">
        <input type="hidden" name="next" value="/today-edge?pa_proj={pa_proj}">
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
        <input class="form-control" name="pa_proj" value="{pa_proj}">
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
    const q = input.value.toLowerCase();
    document.querySelectorAll(".edge-row").forEach(function(row) {{
      const name = row.getAttribute("data-name") || "";
      row.style.display = (name.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    return layout("Today Edge Board", body)
    
@app.get("/today-hitters", response_class=HTMLResponse)
def today_hitters(date: str = ""):
    day = (date or "").strip() or today_yyyy_mm_dd()
    season = int(day.split("-")[0])

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

        # probable pitchers (optional)
        pp_home = g.get("teams", {}).get("home", {}).get("probablePitcher") or {}
        pp_away = g.get("teams", {}).get("away", {}).get("probablePitcher") or {}
        pp_home_name = pp_home.get("fullName") or "tbd"
        pp_away_name = pp_away.get("fullName") or "tbd"

        hitters_home, hitters_away = [], []
        lineup_status = "Lineups not posted yet"

        if game_pk:
            try:
                feed = mlb_get(f"/api/v1.1/game/{game_pk}/feed/live")
                hitters_home = extract_lineup_hitters(feed, "home")
                hitters_away = extract_lineup_hitters(feed, "away")
                if hitters_home or hitters_away:
                    lineup_status = "Lineups posted"
            except Exception:
                pass

        def hitters_list_html(hitters: list[dict]) -> str:
            if not hitters:
                return f"<div class='dark-muted small'>{lineup_status}</div>"
            items = ""
            for h in hitters:
                pid = h["pid"]
                nm = h["name"]
                total = "" if not h["battingOrder"] else f" (BO {h['battingOrder']})"
                items += f"""
<div class="d-flex justify-content-between align-items-center py-1 border-bottom border-light border-opacity-10">
  <div>
    <a class="link-light fw-semibold" href="/player/{pid}?season={season}">{nm}</a>
    <span class="dark-muted small">{h.get('pos','')}{total}</span>
  </div>
  <form action="/watchlist/add" method="post" class="m-0">
    <input type="hidden" name="pid" value="{pid}">
    <input type="hidden" name="name" value="{nm}">
    <input type="hidden" name="season" value="{season}">
    <button class="btn btn-outline-light btn-sm" type="submit">+ Watch</button>
  </form>
</div>
"""
            return items

        total_hitters += len(hitters_home) + len(hitters_away)

        rows_html += f"""
<div class="card-dark mb-3 game-card" data-game="{away_name.lower()} {home_name.lower()}">
  <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
    <div>
      <div class="h5 fw-semibold mb-0">{away_name} at {home_name}</div>
      <div class="dark-muted small">{day} - {start_pt} - {venue}</div>
      <div class="dark-muted small">Probables: {pp_away_name} (away) - {pp_home_name} (home)</div>
    </div>
    <a class="btn btn-outline-light btn-sm" href="/today-edge">Today Edge</a>
  </div>

  <hr class="border-light opacity-25">

  <div class="row g-3">
    <div class="col-12 col-md-6">
      <div class="fw-semibold mb-1">{away_name} hitters</div>
      {hitters_list_html(hitters_away)}
    </div>
    <div class="col-12 col-md-6">
      <div class="fw-semibold mb-1">{home_name} hitters</div>
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
      <input class="form-control" name="date" value="{day}">
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
    const q = input.value.toLowerCase();
    document.querySelectorAll(".game-card").forEach(function(card) {{
      const t = card.getAttribute("data-game") || "";
      card.style.display = (t.indexOf(q) >= 0) ? "" : "none";
    }});
  }});
}});
</script>
"""
    return layout("Today's Hitters", body)
    
@app.get("/player/{pid}/hr-prop-today", response_class=HTMLResponse)
def player_hr_prop_today(pid: int, season: int = datetime.now().year, window: int = 14, min_pa: int = 20):
    missing = []
    for fn in ("season_hr_rate_from_season_stats", "get_player_game_log", "hr_binomial_z"):
        if not hasattr(eng, fn):
            missing.append(fn)

    if missing:
        body = f"""
<div class="card-dark">
  <div class="h5 fw-semibold mb-2">Today HR Prop Score</div>
  <div class="dark-muted">Player <span class="mono">{pid}</span> - Season {season}</div>
  <hr>
  <div class="text-danger fw-semibold">Missing engine functions:</div>
  <div class="mono">{", ".join(missing)}</div>
  <a class="btn btn-outline-light mt-3" href="/player/{pid}?season={season}">Back</a>
</div>
"""
        return layout("Today HR Prop Score", body)

    p_season, pa_season, hr_season = eng.season_hr_rate_from_season_stats(pid, season)
    if p_season is None:
        body = f"""
<div class="card-dark">
  <div class="h5 fw-semibold mb-2">Today HR Prop Score</div>
  <div class="text-danger">No season baseline available.</div>
  <a class="btn btn-outline-light mt-3" href="/player/{pid}?season={season}">Back</a>
</div>
"""
        return layout("Today HR Prop Score", body)

    games = eng.get_player_game_log(pid, season, "hitting") or []
    if len(games) < window:
        body = f"""
<div class="card-dark">
  <div class="h5 fw-semibold mb-2">Today HR Prop Score</div>
  <div class="dark-muted">Need at least {window} games in game log.</div>
  <a class="btn btn-outline-light mt-3" href="/player/{pid}?season={season}">Back</a>
</div>
"""
        return layout("Today HR Prop Score", body)

    def sum_last_n(key: str, n: int) -> int:
        total = 0
        for g in games[:n]:
            total += safe_int(g.get(key, 0) or 0)
        return total

    pa_win = sum_last_n("plateAppearances", window)
    hr_win = sum_last_n("homeRuns", window)

    if pa_win < min_pa:
        body = f"""
<div class="card-dark">
  <div class="h5 fw-semibold mb-2">Today HR Prop Score</div>
  <div class="dark-muted">PA in last {window}: {pa_win} (min {min_pa}). Too small to score.</div>
  <a class="btn btn-outline-light mt-3" href="/player/{pid}?season={season}">Back</a>
</div>
"""
        return layout("Today HR Prop Score", body)

    today = datetime.now().strftime("%Y-%m-%d")
    ctx = None
    if hasattr(eng, "hr_props_today_context"):
        try:
            ctx = eng.hr_props_today_context(pid, season, today)
        except Exception:
            ctx = None

    park_mult = (ctx or {}).get("park_mult")
    sp_mult = (ctx or {}).get("sp_mult")

    p_adj = p_season
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

    hr_heat_z = eng.hr_binomial_z(hr_win, pa_win, p_adj)

    def boost_from_mult(mult: Any) -> float:
        if mult is None:
            return 0.0
        try:
            return 2.0 * (float(mult) - 1.0)
        except Exception:
            return 0.0

    sp_boost = boost_from_mult(sp_mult)
    park_boost = boost_from_mult(park_mult)

    today_score = None
    if hr_heat_z is not None:
        today_score = float(hr_heat_z) + sp_boost + park_boost

    def fmt_mult(m: Any) -> str:
        if m is None:
            return "n/a"
        try:
            return f"{float(m):.3f}x"
        except Exception:
            return "n/a"

    heat_badge = badge_for_z(hr_heat_z)
    if today_score is None:
        score_badge = '<span class="badge bg-secondary badge-score">N/A</span>'
    elif today_score >= 2.5:
        score_badge = f'<span class="badge bg-danger badge-score">ELITE {today_score:+.2f}</span>'
    elif today_score >= 1.5:
        score_badge = f'<span class="badge bg-warning text-dark badge-score">STRONG {today_score:+.2f}</span>'
    elif today_score >= 0.5:
        score_badge = f'<span class="badge bg-primary badge-score">PLAYABLE {today_score:+.2f}</span>'
    else:
        score_badge = f'<span class="badge bg-secondary badge-score">COLD {today_score:+.2f}</span>'

    sp_name = (ctx or {}).get("sp_name", "n/a")
    sp_hr9 = (ctx or {}).get("sp_hr9", None)
    sp_hr9_str = "n/a"
    if sp_hr9 is not None:
        try:
            sp_hr9_str = f"{float(sp_hr9):.2f}"
        except Exception:
            sp_hr9_str = "n/a"

    venue_name = (ctx or {}).get("venue_name", "n/a")
    park_factor = (ctx or {}).get("park_hr_factor", None)
    park_factor_str = "n/a"
    if park_factor is not None:
        try:
            park_factor_str = f"{float(park_factor):.0f}"
        except Exception:
            park_factor_str = "n/a"

    body = f"""
<div class="card-dark mb-3">
  <div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">
    <div>
      <div class="h5 fw-semibold mb-0">Today HR Prop Score</div>
      <div class="dark-muted">Player <span class="mono">{pid}</span> - Season {season} - Window {window} games</div>
    </div>
    <a class="btn btn-outline-light" href="/player/{pid}?season={season}">Back</a>
  </div>
</div>

<div class="row g-3">
  <div class="col-12 col-md-6">
    <div class="card-dark">
      <div class="fw-semibold mb-2">HR Heat (binomial z)</div>
      <div class="d-flex justify-content-between align-items-center">
        <div class="dark-muted">HR {hr_win} / PA {pa_win}</div>
        {heat_badge}
      </div>
      <div class="dark-muted small mt-2">
        Baseline HR/PA: {p_season:.4f} - Adjusted: {p_adj:.4f}
      </div>
    </div>
  </div>

  <div class="col-12 col-md-6">
    <div class="card-dark">
      <div class="fw-semibold mb-2">Today Score</div>
      <div class="d-flex justify-content-between align-items-center">
        <div class="dark-muted">Heat + SP + Park</div>
        {score_badge}
      </div>
      <div class="dark-muted small mt-2">
        SP boost: {sp_boost:+.2f} - Park boost: {park_boost:+.2f}
      </div>
    </div>
  </div>

  <div class="col-12">
    <div class="card-dark">
      <div class="fw-semibold mb-2">Matchup Context (today)</div>

      <div class="mb-2">
        <div class="dark-muted small">Opposing Probable SP</div>
        <div><strong>{sp_name}</strong> - HR/9: <strong>{sp_hr9_str}</strong> - Mult: <strong>{fmt_mult(sp_mult)}</strong></div>
      </div>

      <div>
        <div class="dark-muted small">Park</div>
        <div><strong>{venue_name}</strong> - HR Factor: <strong>{park_factor_str}</strong> - Mult: <strong>{fmt_mult(park_mult)}</strong></div>
      </div>

      <hr>
      <div class="dark-muted small">
        Notes: If there is no game / no probable pitcher, SP/Park may show as n/a.
      </div>
    </div>
  </div>
</div>
"""
    return layout("Today HR Prop Score", body)
@app.get("/leaderboard/parks", response_class=HTMLResponse)
def parks_board(window: int = 30):
    if window not in (7, 14, 30):
        window = 30

    rows = park_leaderboard(window_days=window)

    trs = ""
    for i, r in enumerate(rows, start=1):
        trs += f"""
<tr>
  <td class="text-secondary">{i}</td>
  <td class="fw-semibold">{r['venue']}</td>
  <td class="text-center">{r['games']}</td>
  <td class="text-center">{r['hr_total']}</td>
  <td class="text-center fw-semibold">{r['hr_per_game']:.2f}</td>
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
  <td class="fw-semibold">{r['team']}</td>
  <td class="text-center">{r['games']}</td>
  <td class="text-center">{r['hr_g']:.2f}</td>
  <td class="text-center">{r['r_g']:.2f}</td>
  <td class="text-center">{ops_str}</td>
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
    
