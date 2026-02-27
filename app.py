# -*- coding: utf-8 -*-
"""
MLB Analytics (FastAPI) - Render-safe single-file app
- Mobile-friendly layout (Bootstrap offcanvas)
- Watchlist add/remove (POST)
- ASCII-only strings (no Unicode bullets/emoji) to avoid Render/Python parsing issues
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

import mlb_engine as eng  # your engine module

# ----------------------------
# App + storage
# ----------------------------
app = FastAPI(title="MLB HR Props App")

# NOTE:
# - /tmp is writable on Render but NOT persistent across deploys/restarts.
# - If you add a Render Persistent Disk, set this to something like /var/data/watchlist.json
WATCHLIST_PATH = Path("/tmp/watchlist.json")


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


# ----------------------------
# UI helpers
# ----------------------------
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


# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    wl = load_watchlist()
    body = f"""
<div class="p-3 soft-card mb-3">
  <div class="h4 fw-semibold mb-1">Search a player</div>
  <div class="muted mb-3">Add hitters to your watchlist, then use the HR Board.</div>

  <form class="d-flex gap-2" action="/search" method="get">
    <input class="form-control form-control-lg" name="q" placeholder="Type Here" autofocus>
    <button class="btn btn-primary btn-lg" type="submit">Search</button>
  </form>

  <div class="mt-3 muted">Watchlist players: <span class="fw-semibold">{len(wl.get("players", []))}</span></div>
</div>
"""
    return layout("MLB HR Props", body)


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
