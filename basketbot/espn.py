"""ESPN-backed NBA regular-season ingestion, isolated from NBA-CDN identifiers.

ESPN game/team/athlete IDs are never silently merged with NBA-CDN IDs.
The feed is unofficial and may change; fail closed on absent or ambiguous labels.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
SOURCE = "espn_site_v2"


def fetch_json(url: str, attempts: int = 3) -> dict:
    last = None
    for retry in range(attempts):
        try:
            # Deliberately omit a spoofed browser User-Agent: ESPN has blocked it.
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=25) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
            last = error
            if retry + 1 < attempts:
                time.sleep(1.5 * (retry + 1))
    raise RuntimeError(f"ESPN request failed ({url}): {last}")


def _regular_season_months(now: datetime):
    """Only October-April for the current and preceding season; no playoffs."""
    current_start = now.year if now.month >= 9 else now.year - 1
    cutoff = now + timedelta(days=35)
    for season_start in (current_start - 1, current_start):
        for month in (10, 11, 12, 1, 2, 3, 4):
            year = season_start if month >= 10 else season_start + 1
            date = datetime(year, month, 1, tzinfo=timezone.utc)
            if date <= cutoff:
                yield f"{year}{month:02d}"


def parse_schedule(payload: dict) -> list[dict]:
    result = []
    for event in payload.get("events") or []:
        if str((event.get("season") or {}).get("type") or "2") != "2":
            continue
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        teams = {}
        for item in competitions[0].get("competitors") or []:
            side = item.get("homeAway")
            team = item.get("team") or {}
            if side in ("home", "away") and team.get("id"):
                teams[side] = {"id": str(team["id"]),
                               "code": str(team.get("abbreviation") or team.get("shortDisplayName") or side).upper()}
        if set(teams) != {"home", "away"}:
            continue
        raw_id = str(event.get("id") or "")
        if not raw_id.isdigit():
            continue
        try:
            start = datetime.fromisoformat(str(event["date"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        status = (event.get("status") or {}).get("type") or {}
        completed = bool(status.get("completed", False))
        # Do not forecast an ongoing game.
        in_progress = status.get("state") == "in"
        result.append({"game_id": raw_id, "tipoff_utc": start.isoformat(),
                       "status": 3 if completed else (2 if in_progress else 1),
                       "home": teams["home"], "away": teams["away"], "data_source": SOURCE})
    return result


def get_schedule(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    # Historical prior-season regular season enables meaningful walk-forward research.
    # Season dates are discovered only by event metadata; playoff events are excluded.
    all_games = {}
    problems = []
    for month in _regular_season_months(now):
        url = f"{API}/scoreboard?dates={month}&seasontype=2&limit=500"
        try:
            payload = fetch_json(url)
            games = parse_schedule(payload)
            for row in games:
                all_games[row["game_id"]] = row
        except RuntimeError as exc:
            problems.append(str(exc))
        time.sleep(0.3)
    if problems:
        # A partial calendar biases training and may omit today's games.
        raise RuntimeError("ESPN calendar incomplete: " + "; ".join(problems))
    if not all_games:
        raise RuntimeError("ESPN returned zero regular-season games; refusing empty schedule")
    return sorted(all_games.values(), key=lambda g: (g["tipoff_utc"], g["game_id"]))


def _field_goal(row: dict) -> tuple[int, int]:
    # ESPN statistics parallel arrays use FG = "made-attempted".
    match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(row.get("FG") or row.get("FGM-A") or ""))
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def parse_players(summary: dict, meta: dict) -> list[dict]:
    players = []
    for team_group in (summary.get("boxscore") or {}).get("players") or []:
        tid = str((team_group.get("team") or {}).get("id") or "")
        if tid not in (meta["home"]["id"], meta["away"]["id"]):
            continue
        for group in team_group.get("statistics") or []:
            headers = [str(x).upper() for x in (group.get("names") or group.get("labels") or [])]
            # Some ESPN payloads explicitly label starters as a group; others flag athletes.
            starter_group = str(group.get("name") or "").lower() == "starters"
            for entry in group.get("athletes") or []:
                person = entry.get("athlete") or {}
                pid = str(person.get("id") or "")
                if not pid:
                    continue
                vals = entry.get("stats") or []
                stats = dict(zip(headers, vals))
                made, attempted = _field_goal(stats)
                starter_flag = entry.get("starter")
                starter = starter_flag is True or (starter_flag is None and starter_group)
                players.append({"id": pid, "name": person.get("displayName") or person.get("fullName") or f"Player {pid}",
                                "team_id": tid, "starter": bool(starter), "fga": attempted,
                                "fgm": made, "minutes": str(stats.get("MIN") or "0")})
    # Disallow duplicate team/player entries: avoid fabricated or ambiguous rosters.
    keys = [(p["team_id"], p["id"]) for p in players]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate ESPN player entries in boxscore")
    return players


def _actor(action: dict) -> str:
    for participant in action.get("participants") or []:
        athlete = participant.get("athlete") or {}
        if athlete.get("id"):
            return str(athlete["id"])
    if (action.get("athlete") or {}).get("id"):
        return str(action["athlete"]["id"])
    return ""


def normalize_game(meta: dict, summary: dict) -> dict:
    players = parse_players(summary, meta)
    teams = {meta["home"]["id"], meta["away"]["id"]}
    if any(sum(p["starter"] and p["team_id"] == team for p in players) != 5 for team in teams):
        raise ValueError(f"ESPN did not identify five starters for each team: {meta['game_id']}")
    plays = summary.get("plays") or []
    if not plays:
        raise ValueError("ESPN play-by-play unavailable")
    plays = sorted(plays, key=lambda p: (int(p.get("sequenceNumber") or 0), int(p.get("id") or 0)))
    first_fg = None
    first_shot = {}
    early_shots = []
    shots_by_team = {t: 0 for t in teams}
    tip_team_id = None
    for play in plays:
        period = play.get("period") or {}
        if int(period.get("number") or 0) != 1:
            continue
        action_type = str((play.get("type") or {}).get("text") or "").lower()
        description = str(play.get("text") or "").lower()
        team = str((play.get("team") or {}).get("id") or "")
        pid = _actor(play)
        # A scoreboard's first made FG MUST NOT be inferred from free throws.
        if "free throw" in action_type or "free throw" in description:
            continue
        points = int(play.get("scoreValue") or 0)
        made = bool(play.get("scoringPlay") and points in (2, 3))
        # ESPN provides shootingPlay; alternative text classification handles its omission.
        shot = bool(play.get("shootingPlay")) or made or (
            ("misses" in description or "makes" in description) and
            any(term in description for term in ("jump shot", "jumper", "layup", "dunk",
                                                  "three point", "3-point", "hook shot", "tip shot", "floating"))
        )
        if not shot or team not in teams or not pid:
            continue
        if team not in first_shot:
            first_shot[team] = pid
        if shots_by_team[team] < 4:
            early_shots.append({"player_id": pid, "team_id": team, "made": made})
            shots_by_team[team] += 1
        if made and first_fg is None:
            first_fg = {"player_id": pid, "team_id": team}
    if first_fg is None:
        raise ValueError(f"No verified first made field goal for {meta['game_id']}")
    if first_fg["player_id"] not in {p["id"] for p in players}:
        raise ValueError("First scorer is not in ESPN boxscore")
    return {**{k: meta[k] for k in ("game_id", "tipoff_utc", "home", "away")},
            "data_source": SOURCE, "players": players, "early_shots": early_shots,
            "first_shot": first_shot, "first_fg": first_fg, "tip_team_id": tip_team_id}


def get_completed_game(meta: dict) -> dict:
    summary = fetch_json(f"{API}/summary?event={meta['game_id']}")
    return normalize_game(meta, summary)


def get_reported_starters(meta: dict) -> dict[str, list[dict]] | None:
    try:
        summary = fetch_json(f"{API}/summary?event={meta['game_id']}", attempts=1)
        players = parse_players(summary, meta)
    except (RuntimeError, ValueError, KeyError, TypeError):
        return None
    groups = {meta[side]["id"]: [p for p in players if p["starter"] and p["team_id"] == meta[side]["id"]]
              for side in ("home", "away")}
    return groups if all(len(v) == 5 for v in groups.values()) else None
