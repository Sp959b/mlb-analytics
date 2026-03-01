import requests
from datetime import datetime
import time
import json
import math

BASE = "https://statsapi.mlb.com/api/v1"

WATCHLIST_PATH = "watchlist.json"
PARK_HR_PATH = "park_hr_factors.json"


# -----------------------------
# HTTP helpers
# -----------------------------
def api_get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def safe_first_stat(payload: dict) -> dict:
    """
    MLB stats payload shape often: {"stats":[{"splits":[{"stat":{...}}]}]}
    Return {} safely if empty.
    """
    stats_list = payload.get("stats") or []
    if not stats_list:
        return {}
    splits = stats_list[0].get("splits") or []
    if not splits:
        return {}
    return splits[0].get("stat") or {}


# -----------------------------
# Park factors
# -----------------------------
def load_park_hr_factors() -> dict[int, float]:
    """
    park_hr_factors.json format:
    {
      "3313": 112,
      "2392": 98
    }
    where key = venueId, value = HR factor (100 = average)
    """
    try:
        with open(PARK_HR_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def park_hr_multiplier(venue_id: int | None, park_map: dict[int, float]) -> float | None:
    if venue_id is None:
        return None
    pf = park_map.get(int(venue_id))
    if pf is None:
        return None
    return pf / 100.0


# -----------------------------
# Lookups
# -----------------------------
def search_players(name: str) -> list[dict]:
    data = api_get("/people/search", {"names": name})
    people = data.get("people") or []
    results = []
    for p in people:
        results.append({
            "id": p.get("id"),
            "fullName": p.get("fullName"),
            "primaryPosition": (p.get("primaryPosition") or {}).get("abbreviation"),
            "team": (p.get("currentTeam") or {}).get("name"),
            "birthDate": p.get("birthDate"),
        })
    return results

def hits_leaders(season: int = 2025, limit: int = 50) -> list[dict]:
    """
    Correct MLB season hits leaders via stats/leaders endpoint.
    Returns: [{"pid": int, "name": str, "team": str, "hits": int}, ...]
    """
    try:
        season = int(season)
    except Exception:
        season = datetime.now().year

    try:
        limit = int(limit)
    except Exception:
        limit = 50
    limit = max(1, min(200, limit))

    data = api_get("/stats/leaders", {
        "leaderCategories": "hits",
        "season": season,
        "sportId": 1,
        "limit": limit,
        "leaderGameTypes": "R",  # Regular season
    })

    ll = data.get("leagueLeaders") or []
    leaders = (ll[0].get("leaders") if ll else []) or []

    rows = []
    for r in leaders:
        person = r.get("person") or {}
        team = r.get("team") or {}
        val = r.get("value")

        try:
            hits = int(val)
        except Exception:
            continue  # skip weird rows

        rows.append({
            "pid": person.get("id"),
            "name": person.get("fullName") or "Unknown",
            "team": team.get("name") or "",
            "hits": hits,
        })

    return rows
    
def choose_player(matches: list[dict]) -> dict | None:
    if not matches:
        print("No players found.")
        return None

    print("\nMatches:")
    for i, m in enumerate(matches, start=1):
        print(f"{i:>2}) {m['fullName']} | ID={m['id']} | Pos={m['primaryPosition']} | Team={m['team']} | DOB={m['birthDate']}")

    while True:
        choice = input("\nPick a number (or 'q' to cancel): ").strip().lower()
        if choice == "q":
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(matches):
                return matches[idx - 1]
        print("Invalid choice.")


def get_player_stats(
    player_id: int,
    stats_type: str,
    group: str,
    season: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
) -> dict:
    """
    stats_type: season | byDateRange | lastXGames | gameLog
    group: hitting | pitching
    """
    params = {"stats": stats_type, "group": group}
    if season is not None:
        params["season"] = season
    if start_date and end_date:
        params["startDate"] = start_date
        params["endDate"] = end_date
    if limit is not None:
        params["limit"] = limit

    payload = api_get(f"/people/{player_id}/stats", params)
    return safe_first_stat(payload)


def pretty_print_stat(stat: dict, keys: list[str]):
    if not stat:
        print("No stats returned for that query (player may have 0 games in that time range/season).")
        return
    for k in keys:
        if k in stat:
            print(f"{k:>18}: {stat[k]}")


# -----------------------------
# Teams / standings
# -----------------------------
def list_teams() -> list[dict]:
    data = api_get("/teams", {"sportId": 1})
    teams = data.get("teams") or []
    return [{"id": t["id"], "name": t["name"]} for t in teams]


def standings_team_row(team_id: int, season: int) -> dict:
    data = api_get("/standings", {"leagueId": "103,104", "season": season})
    records = data.get("records") or []
    for rec in records:
        for tr in rec.get("teamRecords") or []:
            if (tr.get("team") or {}).get("id") == team_id:
                return {
                    "team": tr["team"]["name"],
                    "w": tr.get("wins"),
                    "l": tr.get("losses"),
                    "pct": tr.get("winningPercentage"),
                    "streak": (tr.get("streak") or {}).get("streakCode"),
                    "runs_scored": tr.get("runsScored"),
                    "runs_allowed": tr.get("runsAllowed"),
                }
    return {}


def pick_team_interactive() -> dict | None:
    teams = list_teams()
    print("\nTeams:")
    for i, t in enumerate(teams, start=1):
        print(f"{i:>2}) {t['name']} (ID={t['id']})")

    pick = input("\nPick a number (or enter team id, or 'q'): ").strip().lower()
    if pick == "q":
        return None

    if pick.isdigit():
        n = int(pick)
        if 1 <= n <= len(teams):
            return {"id": teams[n - 1]["id"], "name": teams[n - 1]["name"]}
        # treat as team id
        team_id = n
        for t in teams:
            if t["id"] == team_id:
                return {"id": team_id, "name": t["name"]}
        return {"id": team_id, "name": f"team:{team_id}"}

    print("Invalid selection.")
    return None


# -----------------------------
# Watch mode
# -----------------------------
def watch_mode(selected_player: dict | None):
    current_year = datetime.now().year

    if not selected_player:
        print("No player selected yet.")
        if input("Search/select a player now? (y/n): ").strip().lower().startswith("y"):
            name = input("Enter player name: ").strip()
            matches = search_players(name)
            picked = choose_player(matches)
            if picked:
                selected_player = picked

    team_choice = None
    if input("Track a team’s wins too? (y/n): ").strip().lower().startswith("y"):
        team_choice = pick_team_interactive()

    season = input_year("Season year for watch mode", current_year)

    while True:
        s = input("Poll every how many seconds? (>=10): ").strip()
        if s.isdigit() and int(s) >= 10:
            poll_seconds = int(s)
            break
        print("Enter a number >= 10.")

    last_hr = last_hits = last_k = None
    last_wins = None

    player_id = int(selected_player["id"]) if selected_player else None
    player_name = selected_player["fullName"] if selected_player else None

    def to_int(x):
        try:
            return int(x)
        except Exception:
            return None

    print("\n=== WATCH MODE STARTED ===")
    if selected_player:
        print(f"Watching player: {player_name} (ID={player_id}) season={season}")
        print("  Alerts: HR increase, Hits increase, Strikeouts increase")
    if team_choice:
        print(f"Watching team:   {team_choice['name']} (ID={team_choice['id']}) season={season}")
        print("  Alerts: Team wins change")
    print(f"Polling every {poll_seconds}s. Press Ctrl+C to stop.\n")

    try:
        while True:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if player_id:
                hit_stat = get_player_stats(player_id, "season", "hitting", season=season)
                pit_stat = get_player_stats(player_id, "season", "pitching", season=season)

                hr_i = to_int(hit_stat.get("homeRuns"))
                hits_i = to_int(hit_stat.get("hits"))
                ks_i = to_int(pit_stat.get("strikeOuts"))

                if hr_i is not None and last_hr is not None and hr_i > last_hr:
                    print(f"[{ts}] 🔥 {player_name} HR increased: {last_hr} -> {hr_i}")
                if hits_i is not None and last_hits is not None and hits_i > last_hits:
                    print(f"[{ts}] ✅ {player_name} Hits increased: {last_hits} -> {hits_i}")
                if ks_i is not None and last_k is not None and ks_i > last_k:
                    print(f"[{ts}] 🧤 {player_name} Strikeouts increased: {last_k} -> {ks_i}")

                if hr_i is not None:
                    last_hr = hr_i
                if hits_i is not None:
                    last_hits = hits_i
                if ks_i is not None:
                    last_k = ks_i

            if team_choice:
                row = standings_team_row(int(team_choice["id"]), season)
                wins_i = to_int(row.get("w"))
                if wins_i is not None and last_wins is not None and wins_i != last_wins:
                    print(f"[{ts}] 🏆 {team_choice['name']} wins changed: {last_wins} -> {wins_i}")
                if wins_i is not None:
                    last_wins = wins_i

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print("\nWatch mode stopped.")


# -----------------------------
# Rolling deltas
# -----------------------------
def _to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _to_int(x):
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _delta(a, b):
    if a is None or b is None:
        return None
    return a - b


def _fmt_delta(x, decimals=3):
    if x is None:
        return "n/a"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.{decimals}f}"


def get_last_x_games_stats(player_id: int, season: int, group: str, x: int) -> dict:
    return get_player_stats(player_id, "lastXGames", group, season=season, limit=x)


def show_rolling_deltas(selected_player: dict):
    pid = int(selected_player["id"])
    name = selected_player["fullName"]
    current_year = datetime.now().year
    season = input_year("Season year", current_year)

    while True:
        mode = input("Type 'h' for hitting or 'p' for pitching: ").strip().lower()
        if mode in {"h", "p"}:
            break
        print("Enter 'h' or 'p'.")

    group = "hitting" if mode == "h" else "pitching"
    season_stat = get_player_stats(pid, "season", group, season=season)
    if not season_stat:
        print("No season stats found for that player/season.")
        return

    windows = [7, 14, 30]
    roll_stats = {w: get_last_x_games_stats(pid, season, group, w) for w in windows}

    print(f"\n=== Rolling {group.upper()} (Last 7/14/30 Games) vs Season ===")
    print(f"Player: {name} | Season: {season}\n")

    if group == "hitting":
        count_keys = ["hits", "homeRuns", "rbi", "strikeOuts", "baseOnBalls"]
        rate_keys = ["avg", "obp", "slg", "ops"]

        s_counts = {k: _to_int(season_stat.get(k)) for k in count_keys}
        s_rates = {k: _to_float(season_stat.get(k)) for k in rate_keys}
        s_gp = _to_int(season_stat.get("gamesPlayed")) or 0

        season_pg = {}
        for k in count_keys:
            season_pg[k] = (s_counts[k] / s_gp) if (s_counts[k] is not None and s_gp > 0) else None

        print(f"  Season games: {s_gp}")
        for k in count_keys:
            v = season_pg[k]
            print(f"  Season {k}/G: {v:.3f}" if v is not None else f"  Season {k}/G: n/a")

        print("\nROLLING WINDOWS:")
        for w in windows:
            st = roll_stats[w] or {}
            gp = _to_int(st.get("gamesPlayed")) or w
            print(f"\n  Last {w} games (GP={gp}):")

            for k in count_keys:
                v = _to_int(st.get(k))
                v_pg = (v / gp) if (v is not None and gp > 0) else None
                d = _delta(v_pg, season_pg.get(k))
                v_pg_str = f"{v_pg:.3f}" if v_pg is not None else "n/a"
                print(f"    {k}/G: {v_pg_str}  (Δ vs season: {_fmt_delta(d, 3)})")

            print("    Rates:")
            for k in rate_keys:
                rv = _to_float(st.get(k))
                d = _delta(rv, s_rates.get(k))
                rv_str = f"{rv:.3f}" if rv is not None else "n/a"
                print(f"    {k.upper():>3}: {rv_str}  (Δ vs season: {_fmt_delta(d, 3)})")
    else:
        count_keys = ["strikeOuts", "baseOnBalls", "hits", "homeRuns"]
        rate_keys = ["era", "whip"]
        ip_key = "inningsPitched"

        s_counts = {k: _to_int(season_stat.get(k)) for k in count_keys}
        s_rates = {k: _to_float(season_stat.get(k)) for k in rate_keys}
        s_ip = ip_str_to_float(season_stat.get(ip_key))
        s_gp = _to_int(season_stat.get("gamesPlayed")) or 0

        season_pg = {}
        for k in count_keys:
            season_pg[k] = (s_counts[k] / s_gp) if (s_counts[k] is not None and s_gp > 0) else None

        season_per_ip = {
            "K_per_IP": (s_counts["strikeOuts"] / s_ip) if (s_counts["strikeOuts"] is not None and s_ip and s_ip > 0) else None,
            "BB_per_IP": (s_counts["baseOnBalls"] / s_ip) if (s_counts["baseOnBalls"] is not None and s_ip and s_ip > 0) else None,
        }

        print("SEASON BASELINE:")
        print(f"  Season GP: {s_gp} | IP: {s_ip if s_ip is not None else 'n/a'}")
        for k in rate_keys:
            print(f"  Season {k.upper():>4}: {s_rates[k]:.3f}" if s_rates.get(k) is not None else f"  Season {k.upper():>4}: n/a")
        if season_per_ip["K_per_IP"] is not None:
            print(f"  Season K/IP: {season_per_ip['K_per_IP']:.3f}")
        if season_per_ip["BB_per_IP"] is not None:
            print(f"  Season BB/IP: {season_per_ip['BB_per_IP']:.3f}")

        print("\nROLLING WINDOWS:")
        for w in windows:
            st = roll_stats[w] or {}
            gp = _to_int(st.get("gamesPlayed")) or w
            ip = ip_str_to_float(st.get(ip_key))

            print(f"\n  Last {w} games (GP={gp}, IP={ip if ip is not None else 'n/a'}):")

            for k in count_keys:
                v = _to_int(st.get(k))
                v_pg = (v / gp) if (v is not None and gp > 0) else None
                d = _delta(v_pg, season_pg.get(k))
                v_pg_str = f"{v_pg:.3f}" if v_pg is not None else "n/a"
                print(f"    {k}/G: {v_pg_str}  (Δ vs season: {_fmt_delta(d, 3)})")

            print("    Rates:")
            for k in rate_keys:
                rv = _to_float(st.get(k))
                d = _delta(rv, s_rates.get(k))
                rv_str = f"{rv:.3f}" if rv is not None else "n/a"
                print(f"    {k.upper():>4}: {rv_str}  (Δ vs season: {_fmt_delta(d, 3)})")

            k_total = _to_int(st.get("strikeOuts"))
            bb_total = _to_int(st.get("baseOnBalls"))
            k_per_ip = (k_total / ip) if (k_total is not None and ip and ip > 0) else None
            bb_per_ip = (bb_total / ip) if (bb_total is not None and ip and ip > 0) else None

            dk = _delta(k_per_ip, season_per_ip.get("K_per_IP"))
            dbb = _delta(bb_per_ip, season_per_ip.get("BB_per_IP"))

            if k_per_ip is not None:
                print(f"    K/IP: {k_per_ip:.3f}  (Δ vs season: {_fmt_delta(dk, 3)})")
            if bb_per_ip is not None:
                print(f"    BB/IP: {bb_per_ip:.3f}  (Δ vs season: {_fmt_delta(dbb, 3)})")


# -----------------------------
# Game log + z-scores utilities
# -----------------------------
def ip_str_to_float(ip_str):
    """
    MLB inningsPitched sometimes is '5.2' meaning 5 and 2/3 innings.
    """
    if ip_str is None:
        return None
    try:
        s = str(ip_str)
        if "." not in s:
            return float(s)
        whole, frac = s.split(".", 1)
        whole = int(whole) if whole else 0
        frac = int(frac) if frac else 0
        if frac == 0:
            return float(whole)
        if frac == 1:
            return whole + (1.0 / 3.0)
        if frac == 2:
            return whole + (2.0 / 3.0)
        return float(s)
    except Exception:
        return None


def get_player_game_log(player_id: int, season: int, group: str) -> list[dict]:
    params = {"stats": "gameLog", "group": group, "season": season}
    payload = api_get(f"/people/{player_id}/stats", params)
    stats_list = payload.get("stats") or []
    if not stats_list:
        return []
    splits = stats_list[0].get("splits") or []
    return [(sp.get("stat") or {}) for sp in splits]


def mean_std(values: list[float]) -> tuple[float | None, float | None]:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(vals)
    if n < 2:
        return (None, None)
    mu = sum(vals) / n
    var = sum((v - mu) ** 2 for v in vals) / (n - 1)
    return (mu, math.sqrt(var))


def z_score(value: float | None, mu: float | None, sd: float | None) -> float | None:
    if value is None or mu is None or sd is None or sd == 0:
        return None
    return (value - mu) / sd


def fmt_z(z: float | None) -> str:
    if z is None:
        return "n/a"
    sign = "+" if z > 0 else ""
    return f"{sign}{z:.2f}"


def rolling_mean_from_gamelog(games: list[dict], key: str, last_n: int) -> float | None:
    if not games:
        return None
    chunk = games[:last_n]
    vals = []
    for g in chunk:
        try:
            vals.append(float(g.get(key)))
        except Exception:
            vals.append(None)
    mu, _ = mean_std(vals)
    if mu is None and len([v for v in vals if v is not None]) == 1:
        return [v for v in vals if v is not None][0]
    return mu


def rolling_rate_from_gamelog(games: list[dict], num_key: str, denom_key: str, last_n: int) -> float | None:
    if not games:
        return None
    chunk = games[:last_n]
    num_sum = 0.0
    denom_sum = 0.0
    for g in chunk:
        try:
            n = float(g.get(num_key))
        except Exception:
            n = None

        if denom_key == "inningsPitched":
            d = ip_str_to_float(g.get(denom_key))
        else:
            try:
                d = float(g.get(denom_key))
            except Exception:
                d = None

        if n is not None:
            num_sum += n
        if d is not None:
            denom_sum += d

    if denom_sum <= 0:
        return None
    return num_sum / denom_sum


def show_zscores(selected_player: dict):
    pid = int(selected_player["id"])
    name = selected_player["fullName"]
    current_year = datetime.now().year
    season = input_year("Season year", current_year)

    while True:
        mode = input("Type 'h' for hitting or 'p' for pitching: ").strip().lower()
        if mode in {"h", "p"}:
            break
        print("Enter 'h' or 'p'.")

    group = "hitting" if mode == "h" else "pitching"
    games = get_player_game_log(pid, season, group)
    if len(games) < 5:
        print("Not enough game log data to compute meaningful Z-scores.")
        return

    windows = [7, 14, 30]
    print(f"\n=== Z-SCORES from Game Log (Player vs own season distribution) ===")
    print(f"Player: {name} | {group.upper()} | Season: {season}")
    print("Z-score meaning: +2.0 = very hot, 0 = normal, -2.0 = very cold\n")

    if group == "hitting":
        dist_ops, dist_hr, dist_hits, dist_k = [], [], [], []
        for g in games:
            try: dist_ops.append(float(g.get("ops")))
            except Exception: dist_ops.append(None)
            try: dist_hr.append(float(g.get("homeRuns")))
            except Exception: dist_hr.append(None)
            try: dist_hits.append(float(g.get("hits")))
            except Exception: dist_hits.append(None)
            try: dist_k.append(float(g.get("strikeOuts")))
            except Exception: dist_k.append(None)

        ops_mu, ops_sd = mean_std(dist_ops)
        hr_mu, hr_sd = mean_std(dist_hr)
        hits_mu, hits_sd = mean_std(dist_hits)
        k_mu, k_sd = mean_std(dist_k)

        for w in windows:
            ops_last = rolling_mean_from_gamelog(games, "ops", w)
            hr_last = rolling_mean_from_gamelog(games, "homeRuns", w)
            hits_last = rolling_mean_from_gamelog(games, "hits", w)
            k_last = rolling_mean_from_gamelog(games, "strikeOuts", w)

            print(f"Last {w} games:")
            print(f"  OPS   mean: {ops_last if ops_last is not None else 'n/a'} | Z: {fmt_z(z_score(ops_last, ops_mu, ops_sd))}")
            print(f"  HR/G  mean: {hr_last if hr_last is not None else 'n/a'} | Z: {fmt_z(z_score(hr_last, hr_mu, hr_sd))}")
            print(f"  H/G   mean: {hits_last if hits_last is not None else 'n/a'} | Z: {fmt_z(z_score(hits_last, hits_mu, hits_sd))}")
            print(f"  K/G   mean: {k_last if k_last is not None else 'n/a'} | Z: {fmt_z(z_score(k_last, k_mu, k_sd))}")
            print()
    else:
        dist_k, dist_bb, dist_era, dist_kip = [], [], [], []
        for g in games:
            try: dist_k.append(float(g.get("strikeOuts")))
            except Exception: dist_k.append(None)
            try: dist_bb.append(float(g.get("baseOnBalls")))
            except Exception: dist_bb.append(None)
            try: dist_era.append(float(g.get("era")))
            except Exception: dist_era.append(None)

            ip = ip_str_to_float(g.get("inningsPitched"))
            try:
                k = float(g.get("strikeOuts"))
            except Exception:
                k = None
            if k is not None and ip is not None and ip > 0:
                dist_kip.append(k / ip)
            else:
                dist_kip.append(None)

        k_mu, k_sd = mean_std(dist_k)
        bb_mu, bb_sd = mean_std(dist_bb)
        era_mu, era_sd = mean_std(dist_era)
        kip_mu, kip_sd = mean_std(dist_kip)

        for w in windows:
            k_last = rolling_mean_from_gamelog(games, "strikeOuts", w)
            bb_last = rolling_mean_from_gamelog(games, "baseOnBalls", w)
            era_last = rolling_mean_from_gamelog(games, "era", w)
            kip_last = rolling_rate_from_gamelog(games, "strikeOuts", "inningsPitched", w)

            print(f"Last {w} games:")
            print(f"  K/G   mean: {k_last if k_last is not None else 'n/a'} | Z: {fmt_z(z_score(k_last, k_mu, k_sd))}")
            print(f"  BB/G  mean: {bb_last if bb_last is not None else 'n/a'} | Z: {fmt_z(z_score(bb_last, bb_mu, bb_sd))}")
            print(f"  ERA   mean: {era_last if era_last is not None else 'n/a'} | Z: {fmt_z(z_score(era_last, era_mu, era_sd))}")
            print(f"  K/IP  rate: {kip_last if kip_last is not None else 'n/a'} | Z: {fmt_z(z_score(kip_last, kip_mu, kip_sd))}")
            print()


# -----------------------------
# Watchlist + heat leaderboard
# -----------------------------
def load_watchlist():
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"players": []}


def save_watchlist(wl):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(wl, f, indent=2)


def hitter_heat_score_z(games: list[dict], windows=(7, 14, 30)):
    if len(games) < 8:
        return {}

    def dist_float(key):
        out = []
        for g in games:
            try:
                out.append(float(g.get(key)))
            except Exception:
                out.append(None)
        return out

    dist_ops = dist_float("ops")
    dist_hr = dist_float("homeRuns")
    dist_hits = dist_float("hits")
    dist_k = dist_float("strikeOuts")
    dist_bb = dist_float("baseOnBalls")

    ops_mu, ops_sd = mean_std(dist_ops)
    hr_mu, hr_sd = mean_std(dist_hr)
    hits_mu, hits_sd = mean_std(dist_hits)
    k_mu, k_sd = mean_std(dist_k)
    bb_mu, bb_sd = mean_std(dist_bb)

    results = {}
    for w in windows:
        ops_m = rolling_mean_from_gamelog(games, "ops", w)
        hr_m = rolling_mean_from_gamelog(games, "homeRuns", w)
        hits_m = rolling_mean_from_gamelog(games, "hits", w)
        k_m = rolling_mean_from_gamelog(games, "strikeOuts", w)
        bb_m = rolling_mean_from_gamelog(games, "baseOnBalls", w)

        ops_z = z_score(ops_m, ops_mu, ops_sd)
        hr_z = z_score(hr_m, hr_mu, hr_sd)
        hits_z = z_score(hits_m, hits_mu, hits_sd)
        k_z = z_score(k_m, k_mu, k_sd)
        bb_z = z_score(bb_m, bb_mu, bb_sd)

        score = 0.0

        def add(wt, val):
            nonlocal score
            if val is not None:
                score += wt * val

        add(0.45, ops_z)
        add(0.25, hr_z)
        add(0.15, hits_z)
        add(0.10, bb_z)
        add(-0.15, k_z)

        results[w] = {
            "score": score,
            "components": {"OPS_z": ops_z, "HR_z": hr_z, "H_z": hits_z, "BB_z": bb_z, "K_z": k_z},
        }
    return results


def pitcher_heat_score_z(games: list[dict], windows=(7, 14, 30)):
    if len(games) < 8:
        return {}

    dist_bb, dist_era, dist_kip = [], [], []
    for g in games:
        try: dist_bb.append(float(g.get("baseOnBalls")))
        except Exception: dist_bb.append(None)
        try: dist_era.append(float(g.get("era")))
        except Exception: dist_era.append(None)

        ip = ip_str_to_float(g.get("inningsPitched"))
        try:
            k = float(g.get("strikeOuts"))
        except Exception:
            k = None
        if k is not None and ip is not None and ip > 0:
            dist_kip.append(k / ip)
        else:
            dist_kip.append(None)

    bb_mu, bb_sd = mean_std(dist_bb)
    era_mu, era_sd = mean_std(dist_era)
    kip_mu, kip_sd = mean_std(dist_kip)

    results = {}
    for w in windows:
        bb_m = rolling_mean_from_gamelog(games, "baseOnBalls", w)
        era_m = rolling_mean_from_gamelog(games, "era", w)
        kip_r = rolling_rate_from_gamelog(games, "strikeOuts", "inningsPitched", w)

        bb_z = z_score(bb_m, bb_mu, bb_sd)
        era_z = z_score(era_m, era_mu, era_sd)
        kip_z = z_score(kip_r, kip_mu, kip_sd)

        score = 0.0

        def add(wt, val):
            nonlocal score
            if val is not None:
                score += wt * val

        add(0.55, kip_z)
        add(-0.20, bb_z)
        add(-0.35, era_z)

        results[w] = {
            "score": score,
            "components": {"KIP_z": kip_z, "BB_z": bb_z, "ERA_z": era_z},
        }
    return results


def add_selected_player_to_watchlist(selected_player: dict):
    wl = load_watchlist()
    pid = int(selected_player["id"])
    name = selected_player["fullName"]
    current_year = datetime.now().year
    season = input_year("Season year", current_year)

    while True:
        mode = input("Track as hitter or pitcher? (h/p): ").strip().lower()
        if mode in {"h", "p"}:
            break
        print("Enter 'h' or 'p'.")

    group = "hitting" if mode == "h" else "pitching"

    for p in wl["players"]:
        if int(p["id"]) == pid and p["group"] == group and int(p["season"]) == season:
            print("Already in watchlist.")
            return

    wl["players"].append({"id": pid, "name": name, "group": group, "season": season})
    save_watchlist(wl)
    print(f"Added to watchlist: {name} ({group}) season {season}")


def remove_from_watchlist():
    wl = load_watchlist()
    players = wl.get("players", [])
    if not players:
        print("Watchlist is empty.")
        return

    print("\nWatchlist:")
    for i, p in enumerate(players, start=1):
        print(f"{i:>2}) {p['name']} | {p['group']} | season {p['season']} | id={p['id']}")

    s = input("\nRemove which number? (or 'q'): ").strip().lower()
    if s == "q":
        return
    if not s.isdigit():
        print("Invalid.")
        return
    idx = int(s)
    if not (1 <= idx <= len(players)):
        print("Out of range.")
        return

    removed = players.pop(idx - 1)
    wl["players"] = players
    save_watchlist(wl)
    print(f"Removed: {removed['name']} ({removed['group']}) season {removed['season']}")


def show_heat_leaderboard():
    wl = load_watchlist()
    players = wl.get("players", [])
    if not players:
        print("Watchlist is empty. Add players first.")
        return

    while True:
        s = input("Leaderboard window (7/14/30): ").strip()
        if s in {"7", "14", "30"}:
            window = int(s)
            break
        print("Enter 7, 14, or 30.")

    rows = []
    for p in players:
        pid = int(p["id"])
        season = int(p["season"])
        group = p["group"]
        name = p["name"]

        games = get_player_game_log(pid, season, group)
        if len(games) < window:
            rows.append((name, group, season, None, "not enough games"))
            continue

        if group == "hitting":
            info = hitter_heat_score_z(games, windows=(window,)).get(window)
        else:
            info = pitcher_heat_score_z(games, windows=(window,)).get(window)

        if not info:
            rows.append((name, group, season, None, "n/a"))
            continue

        score = info["score"]
        comps = info["components"]
        if group == "hitting":
            comp_str = f"OPS {fmt_z(comps.get('OPS_z'))} | HR {fmt_z(comps.get('HR_z'))} | H {fmt_z(comps.get('H_z'))} | K {fmt_z(comps.get('K_z'))}"
        else:
            comp_str = f"K/IP {fmt_z(comps.get('KIP_z'))} | ERA {fmt_z(comps.get('ERA_z'))} | BB {fmt_z(comps.get('BB_z'))}"

        rows.append((name, group, season, score, comp_str))

    rows.sort(key=lambda r: (-r[3]) if r[3] is not None else 10**9)

    print(f"\n=== HEAT LEADERBOARD (last {window} games) ===")
    print("Higher = hotter. Negative = cold.\n")
    for i, (name, group, season, score, comp) in enumerate(rows, start=1):
        if score is None:
            print(f"{i:>2}. {name} | {group:<8} | {season} | score: n/a | {comp}")
        else:
            print(f"{i:>2}. {name} | {group:<8} | {season} | score: {score:+.2f} | {comp}")


# -----------------------------
# HR props leaderboard + SP/park context
# -----------------------------
def sum_last_n_from_gamelog(games: list[dict], key: str, last_n: int) -> float:
    total = 0.0
    for g in games[:last_n]:
        try:
            total += float(g.get(key))
        except Exception:
            total += 0.0
    return total


def season_hr_rate_from_season_stats(player_id: int, season: int) -> tuple[float | None, int | None, int | None]:
    st = get_player_stats(player_id, "season", "hitting", season=season)
    if not st:
        return (None, None, None)
    try:
        pa = int(st.get("plateAppearances"))
        hr = int(st.get("homeRuns"))
        if pa <= 0:
            return (None, pa, hr)
        return (hr / pa, pa, hr)
    except Exception:
        return (None, None, None)


def hr_binomial_z(hr_window: int, pa_window: int, p_rate: float) -> float | None:
    if pa_window <= 0 or p_rate is None:
        return None
    p = max(1e-6, min(1.0 - 1e-6, p_rate))
    var = pa_window * p * (1 - p)
    if var <= 0:
        return None
    exp = pa_window * p
    return (hr_window - exp) / math.sqrt(var)


def get_player_team_id(player_id: int) -> int | None:
    data = api_get(f"/people/{player_id}", {"hydrate": "currentTeam"})
    people = data.get("people") or []
    if not people:
        return None
    team = (people[0].get("currentTeam") or {}).get("id")
    return int(team) if team else None


def get_team_game_on_date(team_id: int, date_yyyy_mm_dd: str) -> dict | None:
    data = api_get("/schedule", {
        "sportId": 1,
        "teamId": team_id,
        "date": date_yyyy_mm_dd,
        "hydrate": "probablePitcher,venue,teams"
    })
    dates = data.get("dates") or []
    if not dates:
        return None
    games = dates[0].get("games") or []
    return games[0] if games else None


def extract_opponent_probable_pitcher(game: dict, team_id: int) -> tuple[int | None, str | None]:
    teams = game.get("teams") or {}
    home = (teams.get("home") or {}).get("team", {})
    away = (teams.get("away") or {}).get("team", {})

    home_id = home.get("id")
    away_id = away.get("id")

    if home_id == team_id:
        opp_side = teams.get("away") or {}
    elif away_id == team_id:
        opp_side = teams.get("home") or {}
    else:
        return (None, None)

    pp = opp_side.get("probablePitcher") or {}
    pid = pp.get("id")
    pname = pp.get("fullName")
    return (int(pid) if pid else None, pname)


def get_venue_info(game: dict) -> tuple[int | None, str | None]:
    v = game.get("venue") or {}
    vid = v.get("id")
    vname = v.get("name")
    return (int(vid) if vid else None, vname)


def pitcher_hr9(pitcher_id: int, season: int) -> float | None:
    st = get_player_stats(pitcher_id, "season", "pitching", season=season)
    if not st:
        return None
    try:
        hr = float(st.get("homeRuns"))
        ip = ip_str_to_float(st.get("inningsPitched"))
        if not ip or ip <= 0:
            return None
        return (hr * 9.0) / ip
    except Exception:
        return None


def pitcher_hr_multiplier(sp_hr9: float | None, baseline_hr9: float = 1.20) -> float | None:
    if sp_hr9 is None or baseline_hr9 <= 0:
        return None
    return max(0.6, min(1.6, sp_hr9 / baseline_hr9))

# -----------------------------
# HIT props helpers (>=1 hit)
# -----------------------------
def season_hit_rate_from_season_stats(player_id: int, season: int) -> tuple[float | None, int | None, int | None]:
    """
    Returns (H/AB, AB, H) from season hitting stats.
    """
    st = get_player_stats(player_id, "season", "hitting", season=season)
    if not st:
        return (None, None, None)
    try:
        ab = int(st.get("atBats"))
        h = int(st.get("hits"))
        if ab <= 0:
            return (None, ab, h)
        return (h / ab, ab, h)
    except Exception:
        return (None, None, None)


def last_n_hit_rate_from_gamelog(games: list[dict], last_n: int) -> tuple[float | None, int | None, int | None]:
    """
    Returns (H/AB, AB, H) over last_n games using gameLog stats.
    """
    if not games:
        return (None, None, None)

    h_sum = 0.0
    ab_sum = 0.0
    for g in games[:last_n]:
        try:
            h_sum += float(g.get("hits") or 0)
        except Exception:
            pass
        try:
            ab_sum += float(g.get("atBats") or 0)
        except Exception:
            pass

    if ab_sum <= 0:
        return (None, int(ab_sum), int(h_sum))
    return (h_sum / ab_sum, int(ab_sum), int(h_sum))


def pitcher_whip(pitcher_id: int, season: int) -> float | None:
    st = get_player_stats(pitcher_id, "season", "pitching", season=season)
    if not st:
        return None
    try:
        w = float(st.get("whip"))
        # sanity clamp
        if w <= 0:
            return None
        return w
    except Exception:
        return None


def pitcher_hit_multiplier_from_whip(whip: float | None, baseline_whip: float = 1.30) -> float | None:
    """
    crude multiplier: WHIP above baseline -> more hits allowed
    clamped so it can't explode
    """
    if whip is None or baseline_whip <= 0:
        return None
    m = whip / baseline_whip
    return max(0.75, min(1.25, m))


def hits_today_context(player_id: int, season: int, date_yyyy_mm_dd: str) -> dict | None:
    """
    Reuses your existing schedule/venue/probablePitcher extraction.
    Adds opponent SP WHIP and a multiplier.
    """
    team_id = get_player_team_id(player_id)
    if not team_id:
        return None

    game = get_team_game_on_date(team_id, date_yyyy_mm_dd)
    if not game:
        return None

    sp_id, sp_name = extract_opponent_probable_pitcher(game, team_id)
    venue_id, venue_name = get_venue_info(game)

    sp_whip = pitcher_whip(sp_id, season) if sp_id else None
    sp_mult = pitcher_hit_multiplier_from_whip(sp_whip, baseline_whip=1.30)

    # NOTE: you only have HR park factors. For hits, either omit park entirely,
    # or apply a tiny damped adjustment (optional).
    park_map = load_park_hr_factors()
    park_mult_hr = park_hr_multiplier(venue_id, park_map)  # HR-based
    park_mult = None
    if park_mult_hr is not None:
        # damp it heavily so it's not pretending to be a true hit factor
        park_mult = 1.0 + 0.20 * (park_mult_hr - 1.0)

    return {
        "sp_id": sp_id,
        "sp_name": sp_name,
        "sp_whip": sp_whip,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "sp_mult": sp_mult,
        "park_mult": park_mult,
    }
    
def hr_props_today_context(player_id: int, season: int, date_yyyy_mm_dd: str) -> dict | None:
    team_id = get_player_team_id(player_id)
    if not team_id:
        return None

    game = get_team_game_on_date(team_id, date_yyyy_mm_dd)
    if not game:
        return None

    sp_id, sp_name = extract_opponent_probable_pitcher(game, team_id)
    venue_id, venue_name = get_venue_info(game)

    sp_hr9 = pitcher_hr9(sp_id, season) if sp_id else None

    park_map = load_park_hr_factors()
    park_mult = park_hr_multiplier(venue_id, park_map)
    sp_mult = pitcher_hr_multiplier(sp_hr9, baseline_hr9=1.20)

    return {
        "sp_id": sp_id,
        "sp_name": sp_name,
        "sp_hr9": sp_hr9,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "park_hr_factor": (park_map.get(venue_id) if venue_id else None),
        "park_mult": park_mult,
        "sp_mult": sp_mult,
    }

def season_k_per_ip_from_season_stats(pitcher_id: int, season: int) -> tuple[float | None, float | None, int | None]:
    """
    Returns (K/IP, IP_float, K_total) from season pitching stats.
    """
    st = get_player_stats(pitcher_id, "season", "pitching", season=season)
    if not st:
        return (None, None, None)
    try:
        k = int(st.get("strikeOuts"))
        ip = ip_str_to_float(st.get("inningsPitched"))
        if ip is None or ip <= 0:
            return (None, ip, k)
        return (k / ip, ip, k)
    except Exception:
        return (None, None, None)

def last_n_k_per_ip_from_gamelog(games: list[dict], last_n: int) -> tuple[float | None, float | None, int | None]:
    """
    Returns (K/IP, IP_float, K_total) over last_n games using gameLog pitching stats.
    """
    if not games:
        return (None, None, None)

    k_sum = 0.0
    ip_sum = 0.0
    for g in games[:last_n]:
        try:
            k_sum += float(g.get("strikeOuts") or 0)
        except Exception:
            pass
        ip = ip_str_to_float(g.get("inningsPitched"))
        if ip is not None:
            ip_sum += float(ip)

    if ip_sum <= 0:
        return (None, ip_sum, int(k_sum))
    return (k_sum / ip_sum, ip_sum, int(k_sum))
def show_hr_props_leaderboard():
    wl = load_watchlist()
    players = [p for p in wl.get("players", []) if p.get("group") == "hitting"]
    if not players:
        print("Watchlist has no hitters. Add hitters with option 12 first.")
        return

    while True:
        s = input("Leaderboard window (7/14/30): ").strip()
        if s in {"7", "14", "30"}:
            window = int(s)
            break
        print("Enter 7, 14, or 30.")

    while True:
        s = input("Minimum PA in window (recommended 20): ").strip()
        if not s:
            min_pa = 20
            break
        if s.isdigit() and int(s) >= 1:
            min_pa = int(s)
            break
        print("Enter a positive integer (or blank for 20).")

    rows = []
    for p in players:
        pid = int(p["id"])
        season = int(p["season"])
        name = p["name"]

        p_season, pa_season, _hr_season = season_hr_rate_from_season_stats(pid, season)

        today = datetime.now().strftime("%Y-%m-%d")
        ctx = hr_props_today_context(pid, season, today)

        park_mult = None
        sp_mult = None
        if ctx:
            park_mult = ctx.get("park_mult")
            sp_mult = ctx.get("sp_mult")

        if p_season is None or pa_season is None:
            rows.append((name, season, None, "no season baseline"))
            continue

        games = get_player_game_log(pid, season, "hitting")
        if len(games) < window:
            rows.append((name, season, None, "not enough games"))
            continue

        pa_win = int(sum_last_n_from_gamelog(games, "plateAppearances", window))
        hr_win = int(sum_last_n_from_gamelog(games, "homeRuns", window))

        if pa_win < min_pa:
            rows.append((name, season, None, f"PA too low ({pa_win} < {min_pa})"))
            continue

        # Adjust baseline probability using context
        p_adj = p_season
        if park_mult is not None:
            p_adj *= park_mult
        if sp_mult is not None:
            p_adj *= sp_mult
        p_adj = min(max(p_adj, 0.00001), 0.25)

        z = hr_binomial_z(hr_win, pa_win, p_adj)

        comp = f"HR {hr_win}/PA {pa_win} | season HR/PA {p_season:.4f}"
        if ctx:
            comp += f" | SP: {ctx.get('sp_name')} HR/9={ctx.get('sp_hr9') if ctx.get('sp_hr9') is not None else 'n/a'}"
            comp += f" | Park: {ctx.get('venue_name')} HRfac={ctx.get('park_hr_factor') if ctx.get('park_hr_factor') is not None else 'n/a'}"

        rows.append((name, season, z, comp))

    rows.sort(key=lambda r: (-r[2]) if r[2] is not None else 10**9)

    print(f"\n=== HR PROPS HEAT LEADERBOARD (last {window} games) ===")
    print("Metric: binomial z-score on HR/PA vs adjusted baseline (season × park × SP HR/9)")
    print("Rule of thumb: z >= +1.5 hot, z >= +2.0 very hot | z <= -1.5 cold\n")

    for i, (name, season, z, comp) in enumerate(rows, start=1):
        if z is None:
            print(f"{i:>2}. {name} | {season} | z: n/a | {comp}")
        else:
            print(f"{i:>2}. {name} | {season} | z: {z:+.2f} | {comp}")


# -----------------------------
# CLI helpers
# -----------------------------
def input_year(prompt: str, default: int) -> int:
    s = input(f"{prompt} [{default}]: ").strip()
    if not s:
        return default
    return int(s)


def input_date(prompt: str) -> str:
    while True:
        s = input(prompt).strip()
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            print("Use YYYY-MM-DD (example: 2025-04-01).")


# -----------------------------
# Main
# -----------------------------
def main():
    current_year = datetime.now().year
    selected_player = None

    while True:
        print("\n=== MLB CLI Stat Bot ===")
        print("1) Search & select player")
        print("2) Player season stats (hitting)")
        print("3) Player season stats (pitching)")
        print("4) Player date-range stats (hitting)")
        print("5) Player date-range stats (pitching)")
        print("6) Player last X games (hitting)")
        print("7) Player last X games (pitching)")
        print("8) Team standings (optional)")
        print("9) Watch mode (alerts)")
        print("10) Rolling last 7/14/30 + deltas (vs season)")
        print("11) Z-scores (last 7/14/30 vs season distribution)")
        print("12) Add selected player to watchlist (for Heat Score)")
        print("13) Remove player from watchlist")
        print("14) Heat Score leaderboard (rank watchlist)")
        print("15) HR props leaderboard (hot/cold + SP + park)")
        print("q) Quit")

        if selected_player:
            print(f"\nSelected: {selected_player['fullName']} (ID={selected_player['id']})")

        choice = input("\nChoose: ").strip().lower()

        if choice == "q":
            break

        if choice == "1":
            name = input("Enter player name (e.g., 'Aaron Judge'): ").strip()
            matches = search_players(name)
            picked = choose_player(matches)
            if picked:
                selected_player = picked
            continue

        # player-required actions
        if choice in {"2", "3", "4", "5", "6", "7", "9", "10", "11", "12"} and not selected_player:
            # watch mode (9) can work without player, but we still allow it:
            if choice == "9":
                watch_mode(None)
                continue
            print("Select a player first (option 1).")
            continue

        pid = int(selected_player["id"]) if selected_player else None

        if choice in {"2", "3"}:
            season = input_year("Season year", current_year)
            group = "hitting" if choice == "2" else "pitching"
            stat = get_player_stats(pid, "season", group, season=season)
            print(f"\n{selected_player['fullName']} | {group.upper()} | Season {season}")
            keys = (
                ["gamesPlayed", "plateAppearances", "atBats", "hits", "homeRuns", "rbi", "avg", "obp", "slg", "ops"]
                if group == "hitting"
                else ["gamesPlayed", "gamesStarted", "wins", "losses", "era", "inningsPitched", "strikeOuts", "whip", "saves"]
            )
            pretty_print_stat(stat, keys)
            continue

        if choice in {"4", "5"}:
            group = "hitting" if choice == "4" else "pitching"
            start = input_date("Start date (YYYY-MM-DD): ")
            end = input_date("End date   (YYYY-MM-DD): ")
            stat = get_player_stats(pid, "byDateRange", group, start_date=start, end_date=end)
            print(f"\n{selected_player['fullName']} | {group.upper()} | {start} to {end}")
            keys = (
                ["gamesPlayed", "plateAppearances", "atBats", "hits", "homeRuns", "rbi", "avg", "obp", "slg", "ops"]
                if group == "hitting"
                else ["gamesPlayed", "gamesStarted", "wins", "losses", "era", "inningsPitched", "strikeOuts", "whip", "saves"]
            )
            pretty_print_stat(stat, keys)
            continue

        if choice in {"6", "7"}:
            group = "hitting" if choice == "6" else "pitching"
            season = input_year("Season year", current_year)
            while True:
                s = input("Last how many games? (e.g., 5, 10, 20): ").strip()
                if s.isdigit() and int(s) > 0:
                    limit = int(s)
                    break
                print("Enter a positive integer.")
            stat = get_player_stats(pid, "lastXGames", group, season=season, limit=limit)
            print(f"\n{selected_player['fullName']} | {group.upper()} | Last {limit} games (Season {season})")
            keys = (
                ["gamesPlayed", "plateAppearances", "atBats", "hits", "homeRuns", "rbi", "avg", "obp", "slg", "ops"]
                if group == "hitting"
                else ["gamesPlayed", "gamesStarted", "wins", "losses", "era", "inningsPitched", "strikeOuts", "whip", "saves"]
            )
            pretty_print_stat(stat, keys)
            continue

        if choice == "8":
            season = input_year("Season year", current_year)
            team_choice = pick_team_interactive()
            if not team_choice:
                continue
            row = standings_team_row(int(team_choice["id"]), season)
            if not row:
                print("No standings found (season/team may be invalid).")
                continue
            print(f"\n{row['team']} | Season {season}")
            print(f"{'W-L':>18}: {row['w']}-{row['l']}")
            print(f"{'PCT':>18}: {row['pct']}")
            print(f"{'Streak':>18}: {row['streak']}")
            print(f"{'Runs Scored':>18}: {row['runs_scored']}")
            print(f"{'Runs Allowed':>18}: {row['runs_allowed']}")
            continue

        if choice == "9":
            watch_mode(selected_player)
            continue

        if choice == "10":
            show_rolling_deltas(selected_player)
            continue

        if choice == "11":
            show_zscores(selected_player)
            continue

        if choice == "12":
            add_selected_player_to_watchlist(selected_player)
            continue

        if choice == "13":
            remove_from_watchlist()
            continue

        if choice == "14":
            show_heat_leaderboard()
            continue

        if choice == "15":
            show_hr_props_leaderboard()
            continue

        print("Unknown option.")


if __name__ == "__main__":
    main()
