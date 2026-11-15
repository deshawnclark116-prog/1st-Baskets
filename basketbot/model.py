"""Pregame first-made-FG distribution from earlier boxscores and opening PBP only."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
import math
import random

MODEL_VERSION = "opening-possession-v2-espn"


def games_before(history: list[dict], tipoff: str) -> list[dict]:
    # No current/future game or same-tipoff game may enter a prediction.
    return [g for g in history if g["tipoff_utc"] < tipoff]


def team_games(history: list[dict], team_id: str, n: int = 24) -> list[dict]:
    return [g for g in history if team_id in (g["home"]["id"], g["away"]["id"])][-n:]


def infer_lineup(team_id: str, history: list[dict]) -> list[dict]:
    """Conservative five-starter projection; stale players are not eligible."""
    games = team_games(history, team_id, 9)
    if len(games) < 3:
        return []
    def logged_minutes(value):
        raw = str(value or "").strip()
        if raw.startswith("PT"):
            import re
            match = re.fullmatch(r"PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+(?:\\.\\d+)?)S)?", raw)
            return (int(match[1] or 0) * 60 + int(match[2] or 0) + float(match[3] or 0) / 60) if match else 0
        try:
            return float(raw.split(":")[0])
        except (TypeError, ValueError):
            return 0
    recent_ids = {p["id"] for g in games[-2:] for p in g["players"]
                  if p["team_id"] == team_id and logged_minutes(p.get("minutes")) > 0}
    scores = defaultdict(float)
    latest = {}
    for age, game in enumerate(reversed(games)):
        weight = math.exp(-age / 2.4)
        for p in game["players"]:
            if p["team_id"] != team_id:
                continue
            latest.setdefault(p["id"], p)
            if p["starter"]:
                scores[p["id"]] += 4.0 * weight
            if p["fga"]:
                scores[p["id"]] += 0.02 * p["fga"] * weight
    ranked = sorted((pid for pid in recent_ids if pid in scores), key=lambda pid: (-scores[pid], pid))
    return [{**latest[pid], "starter": True} for pid in ranked[:5]] if len(ranked) >= 5 else []


def player_params(starters: list[dict], team_id: str, history: list[dict]) -> list[dict]:
    games = team_games(history, team_id, 24)
    profiles = []
    for starter in starters:
        pid = starter["id"]
        stats_fga = stats_fgm = 0.0
        opens = makes = first_shots = 0.0
        for age, game in enumerate(reversed(games)):
            decay = math.exp(-age / 10)
            for p in game["players"]:
                if p["id"] == pid and p["team_id"] == team_id:
                    stats_fga += decay * p["fga"]
                    stats_fgm += decay * p["fgm"]
            if game.get("first_shot", {}).get(team_id) == pid:
                first_shots += decay
            for shot in game.get("early_shots", []):
                if shot["player_id"] == pid and shot["team_id"] == team_id:
                    opens += decay
                    makes += decay * int(shot["made"])
        # Rate prior ~44%; whole-game FG% provides regularized evidence, not a hard guarantee.
        hit = (8.8 + 0.15 * stats_fgm + 1.2 * makes) / (20.0 + 0.15 * stats_fga + 1.2 * opens)
        usage = max(0.2, stats_fga) ** 0.78
        profiles.append({"id": pid, "name": starter["name"], "team_id": team_id,
                         "make_prob": min(0.72, max(0.27, hit)), "usage": usage,
                         "first_shots": first_shots, "early_shots": opens})
    # Dirichlet-like smoothing: opening-shot history matters but does not dominate 2-3 games.
    base_sum = sum(p["usage"] for p in profiles)
    for p in profiles:
        prior = 7.0 * p["usage"] / base_sum
        p["weight"] = prior + 1.7 * p["first_shots"] + 0.35 * p["early_shots"]
    total = sum(p["weight"] for p in profiles)
    for p in profiles:
        p["shot_share"] = p["weight"] / total
        del p["weight"]
        del p["usage"]
    return profiles


def tip_home_probability(home_id: str, away_id: str, history: list[dict]) -> float:
    def prior(team_id: str):
        gs = team_games(history, team_id, 30)
        known = [g for g in gs if g.get("tip_team_id")]
        return (8.0 + sum(g["tip_team_id"] == team_id for g in known)) / (16.0 + len(known))
    return max(0.32, min(0.68, 0.5 + 0.65 * (prior(home_id) - prior(away_id))))


def simulate(home: list[dict], away: list[dict], p_home_tip: float,
             game_id: str, n: int = 40000) -> dict[str, float]:
    """Simulate tip -> possession -> shooter -> miss/ORB or turnover -> repeat."""
    if n < 1000 or len(home) != 5 or len(away) != 5:
        raise ValueError("Need 5 players per team and >=1,000 simulations")
    seed = int.from_bytes(sha256(f"{MODEL_VERSION}:{game_id}:{n}".encode()).digest()[:8], "big")
    rng = random.Random(seed)
    counts = {p["id"]: 0 for p in home + away}
    rosters = (home, away)
    for _ in range(n):
        side = 0 if rng.random() < p_home_tip else 1
        for attempt in range(80):
            # A no-shot turnover / pre-shot foul returns possession to the other team.
            if rng.random() < 0.085:
                side = 1 - side
                continue
            roster = rosters[side]
            r = rng.random()
            cumulative = 0.0
            shooter = roster[-1]
            for player in roster:
                cumulative += player["shot_share"]
                if r <= cumulative:
                    shooter = player
                    break
            if rng.random() < shooter["make_prob"]:
                counts[shooter["id"]] += 1
                break
            # Small chance of retaining possession via offensive board.
            if rng.random() >= 0.23:
                side = 1 - side
        else:
            raise RuntimeError("Too many simulated possessions without a made FG")
    return {pid: count / n for pid, count in counts.items()}


def predict_game(meta: dict, history: list[dict], confirmed: dict | None = None, n: int = 40000) -> dict:
    past = games_before(history, meta["tipoff_utc"])
    rosters = {}
    for side in ("home", "away"):
        team = meta[side]["id"]
        starters = confirmed.get(team) if confirmed else infer_lineup(team, past)
        if not starters or len(starters) != 5:
            raise ValueError(f"No reliable 5-man lineup for {meta[side]['code']}")
        rosters[side] = player_params(starters, team, past)
    p_tip = tip_home_probability(meta["home"]["id"], meta["away"]["id"], past)
    probabilities = simulate(rosters["home"], rosters["away"], p_tip, meta["game_id"], n)
    entries = [{"id": p["id"], "name": p["name"], "team_id": p["team_id"],
                "team": meta[side]["code"], "probability": round(probabilities[p["id"]], 5),
                "shot_share": round(p["shot_share"], 4), "fg_probability": round(p["make_prob"], 4)}
               for side in ("home", "away") for p in rosters[side]]
    entries.sort(key=lambda p: (-p["probability"], p["id"]))
    return {"game_id": meta["game_id"], "tipoff_utc": meta["tipoff_utc"],
            "matchup": f"{meta['away']['code']} @ {meta['home']['code']}",
            "model": MODEL_VERSION, "lineup_status": "espn_feed_reported" if confirmed else "projected",
            "quality": "official_candidate" if confirmed else "watchlist_only",
            "historical_games_available": len(past), "tip_home_probability": round(p_tip, 4),
            "simulations": n, "predictions": entries}


def score_predictions(pred: dict, actual_id: str) -> dict:
    players = pred["predictions"]
    if actual_id not in {p["id"] for p in players}:
        return {"graded": False, "reason": "actual_scorer_not_in_predicted_lineup"}
    probs = {p["id"]: p["probability"] for p in players}
    return {"graded": True, "top1": int(players[0]["id"] == actual_id),
            "top3": int(actual_id in [p["id"] for p in players[:3]]),
            "log_loss": -math.log(max(1e-8, probs[actual_id])),
            "brier": sum((p - (1 if pid == actual_id else 0)) ** 2 for pid, p in probs.items()) / len(probs),
            "uniform_log_loss": math.log(10), "uniform_brier": 0.09}
