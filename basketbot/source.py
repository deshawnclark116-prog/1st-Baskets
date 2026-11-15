"""NBA CDN ingestion and leak-safe normalization of completed games."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
BASE = "https://cdn.nba.com/static/json"
SCHEDULE_URLS = (
    f"{BASE}/staticData/scheduleLeagueV2_1.json",
    f"{BASE}/staticData/scheduleLeagueV2.json",
)


def fetch_json(url: str, attempts: int = 3) -> dict:
    """Try finite retries. Never synthesize missing game information."""
    last = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FirstBasketResearch/0.1)", "Accept": "application/json"})
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Unexpected non-object NBA response")
            return payload
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"NBA source unavailable: {url}: {last}")


def parse_datetime(value: str) -> datetime:
    if not value:
        raise ValueError("Game has no tipoff time")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def schedule(payload: dict) -> list[dict]:
    days = payload.get("leagueSchedule", {}).get("gameDates", [])
    result = []
    for day in days:
        for row in day.get("games", []):
            home, away = row.get("homeTeam") or {}, row.get("awayTeam") or {}
            gid = str(row.get("gameId") or "")
            if len(gid) != 10 or not gid.startswith("002"):
                continue  # regular-season research only; avoid mixing cup/preseason rules
            if not home.get("teamId") or not away.get("teamId"):
                continue
            try:
                dt = parse_datetime(row.get("gameDateTimeUTC") or "")
            except ValueError:
                continue
            result.append({"game_id": gid, "tipoff_utc": dt.isoformat(), "status": int(row.get("gameStatus") or 0),
                           "home": {"id": str(home["teamId"]), "code": home.get("teamTricode", "HOME")},
                           "away": {"id": str(away["teamId"]), "code": away.get("teamTricode", "AWAY")}})
    return sorted(result, key=lambda r: (r["tipoff_utc"], r["game_id"]))


def get_schedule() -> list[dict]:
    errors = []
    for url in SCHEDULE_URLS:
        try:
            found = schedule(fetch_json(url))
            if found:
                return found
            errors.append(f"{url}: empty schedule")
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError("No usable NBA schedule. " + "; ".join(errors))


def is_starter(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true", "yes")) or value == 1


def parse_players(box: dict, metadata: dict) -> list[dict]:
    game = box.get("game") or {}
    players = []
    for side in ("home", "away"):
        team = game.get(side + "Team") or {}
        team_id = str(team.get("teamId") or metadata[side]["id"])
        for p in team.get("players") or []:
            pid = str(p.get("personId") or "")
            if not pid:
                continue
            stats = p.get("statistics") or {}
            players.append({"id": pid, "team_id": team_id,
                            "name": p.get("name") or f"Player {pid}",
                            "starter": is_starter(p.get("starter")),
                            "fga": max(0, int(stats.get("fieldGoalsAttempted") or 0)),
                            "fgm": max(0, int(stats.get("fieldGoalsMade") or 0)),
                            "minutes": str(stats.get("minutes") or "")})
    return players


def made_fg(action: dict, prev_home: int, prev_away: int) -> bool:
    result = str(action.get("shotResult") or "").lower()
    if result == "made":
        return True
    if result == "missed":
        return False
    # Recover if the feed omitted shotResult; never use description alone to count free throws.
    try:
        current_home = int(action.get("scoreHome") or 0)
        current_away = int(action.get("scoreAway") or 0)
        return max(current_home - prev_home, current_away - prev_away) >= 2
    except (TypeError, ValueError):
        return False


def normalize_game(metadata: dict, box: dict, pbp: dict) -> dict:
    """Label first successful FIELD GOAL, not first free throw / first shot attempt."""
    players = parse_players(box, metadata)
    teams = {metadata["home"]["id"], metadata["away"]["id"]}
    starter_counts = {team_id: sum(p["starter"] and p["team_id"] == team_id for p in players) for team_id in teams}
    if any(x != 5 for x in starter_counts.values()):
        raise ValueError(f"Cannot verify 5 historical starters for {metadata['game_id']}: {starter_counts}")
    actions = (pbp.get("game") or {}).get("actions") or []
    actions = sorted(actions, key=lambda a: (int(a.get("orderNumber") or a.get("actionNumber") or 0), int(a.get("actionNumber") or 0)))
    shots, first_fg, first_shot, tip_team_id = [], None, {}, None
    home_score = away_score = 0
    for a in actions:
        if int(a.get("period") or 0) != 1:
            continue
        typ = str(a.get("actionType") or "").lower()
        if typ == "jumpball" and tip_team_id is None:
            possession = str(a.get("possession") or "")
            if possession in teams:
                tip_team_id = possession
        is_fg = a.get("isFieldGoal") in (1, True, "1") or typ in ("2pt", "3pt")
        if is_fg:
            pid = str(a.get("personId") or "")
            team = str(a.get("teamId") or "")
            if pid and team in teams:
                made = made_fg(a, home_score, away_score)
                shot = {"player_id": pid, "team_id": team, "made": made}
                if len([x for x in shots if x["team_id"] == team]) < 4:
                    shots.append(shot)
                if team not in first_shot:
                    first_shot[team] = pid
                if made and first_fg is None:
                    first_fg = {"player_id": pid, "team_id": team}
        try:
            home_score = int(a.get("scoreHome") if a.get("scoreHome") is not None else home_score)
            away_score = int(a.get("scoreAway") if a.get("scoreAway") is not None else away_score)
        except (TypeError, ValueError):
            pass
    if first_fg is None:
        raise ValueError(f"No identifiable first field goal for {metadata['game_id']}")
    if first_fg["player_id"] not in {p["id"] for p in players}:
        raise ValueError(f"First FG scorer missing from boxscore {metadata['game_id']}")
    return {**{k: metadata[k] for k in ("game_id", "tipoff_utc", "home", "away")},
            "players": players, "early_shots": shots, "first_shot": first_shot,
            "first_fg": first_fg, "tip_team_id": tip_team_id}


def get_completed_game(meta: dict) -> dict:
    game_id = meta["game_id"]
    box = fetch_json(f"{BASE}/liveData/boxscore/boxscore_{game_id}.json")
    pbp = fetch_json(f"{BASE}/liveData/playbyplay/playbyplay_{game_id}.json")
    return normalize_game(meta, box, pbp)


def get_reported_starters(meta: dict) -> dict[str, list[dict]] | None:
    """Use only current NBA game feed; five explicit starters per team required."""
    try:
        box = fetch_json(f"{BASE}/liveData/boxscore/boxscore_{meta['game_id']}.json", attempts=1)
    except RuntimeError:
        return None
    players = parse_players(box, meta)
    groups = {meta[side]["id"]: [p for p in players if p["starter"] and p["team_id"] == meta[side]["id"]] for side in ("home", "away")}
    return groups if all(len(group) == 5 for group in groups.values()) else None
