"""Locked-formula first-basket challenger evaluation; research only, never publishes picks.

Candidates are chosen on the validation partition and evaluated ONCE on a later
chronological test partition. There is no parameter optimization on the test set.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .model import MODEL_VERSION, games_before, predict_game, team_games
from .__main__ import DATA, read_json, write_json

UNKNOWN_MASS = 0.12
RESEARCH_VERSION = "baseline-challengers-v1"
CHALLENGERS = ("uniform", "shot_allocation", "shot_x_fg", "first_fg_empirical")


def normalize(values):
    total = sum(max(v, 0) for v in values)
    if total <= 0:
        raise ValueError("Invalid candidate weights")
    return [max(v, 0) / total for v in values]


def weights(pred: dict, prior: list[dict], method: str) -> list[float]:
    ps = pred["predictions"]
    if method == "uniform":
        return [0.1] * 10
    # Split team first-FG chance equally, then allocate conditionally within team.
    out = [0.0] * 10
    for team_id in {p["team_id"] for p in ps}:
        ix = [i for i, p in enumerate(ps) if p["team_id"] == team_id]
        if len(ix) != 5:
            raise ValueError("Expected five forecast starters per team")
        if method == "shot_allocation":
            raw = [ps[i]["shot_share"] for i in ix]
        elif method == "shot_x_fg":
            raw = [ps[i]["shot_share"] * ps[i]["fg_probability"] for i in ix]
        elif method == "first_fg_empirical":
            games = team_games(prior, team_id, 24)
            counts = {}
            for age, g in enumerate(reversed(games)):
                pid = g["first_fg"]["player_id"]
                counts[pid] = counts.get(pid, 0) + math.exp(-age / 8)
            # Strong equal prior guards against a handful of observed early scores.
            raw = [1.0 + counts.get(ps[i]["id"], 0.0) for i in ix]
        else:
            raise ValueError("Unknown research challenger")
        for i, w in zip(ix, normalize(raw)):
            out[i] = 0.5 * w
    return out


def grade_one(probabilities: list[float], ids: list[str], actual: str):
    if len(probabilities) != 10 or len(set(ids)) != 10:
        raise ValueError("Expected ten distinct candidates")
    p = [(1 - UNKNOWN_MASS) * v for v in probabilities] + [UNKNOWN_MASS]
    if abs(sum(p) - 1.0) > 0.001:
        raise ValueError("Probabilities failed normalization")
    actual_idx = ids.index(actual) if actual in ids else 10
    ordered = sorted(range(10), key=lambda i: (-p[i], ids[i]))
    return {
        "log_loss": -math.log(max(1e-9, p[actual_idx])),
        "brier": sum((v - int(i == actual_idx)) ** 2 for i, v in enumerate(p)) / 11,
        "top1": int(ordered[0] == actual_idx),
        "top3": int(actual_idx in ordered[:3]),
        "lineup_miss": int(actual_idx == 10)
    }


def aggregate(results):
    n = len(results)
    if not n:
        return {"n": 0, "log_loss": None, "brier": None, "top1": None, "top3": None,
                "lineup_misses": 0}
    return {"n": n, "log_loss": round(sum(x["log_loss"] for x in results) / n, 5),
            "brier": round(sum(x["brier"] for x in results) / n, 5),
            "top1": round(sum(x["top1"] for x in results) / n, 5),
            "top3": round(sum(x["top3"] for x in results) / n, 5),
            "lineup_misses": sum(x["lineup_miss"] for x in results)}


def research(history: list[dict], simulations=2000):
    ordered = sorted(history, key=lambda g: (g["tipoff_utc"], g["game_id"]))
    models = ("incumbent",) + CHALLENGERS
    scored = {m: [] for m in models}
    unavailable = 0
    for game in ordered:
        past = games_before(ordered, game["tipoff_utc"])
        if len(past) < 40:
            continue
        try:
            prediction = predict_game(game, past, confirmed=None, n=simulations)
        except (ValueError, RuntimeError):
            unavailable += 1
            continue
        ids = [p["id"] for p in prediction["predictions"]]
        actual = game["first_fg"]["player_id"]
        incumbent = normalize([p["probability"] for p in prediction["predictions"]])
        scored["incumbent"].append(grade_one(incumbent, ids, actual))
        for method in CHALLENGERS:
            challenger = weights(prediction, past, method)
            scored[method].append(grade_one(challenger, ids, actual))
    N = len(scored["incumbent"])
    # Chronological split across eligible matches, shared for every method.
    a, b = int(N * 0.6), int(N * 0.8)
    portions = {
        "development": {name: aggregate(rows[:a]) for name, rows in scored.items()},
        "validation": {name: aggregate(rows[a:b]) for name, rows in scored.items()},
        "test": {name: aggregate(rows[b:]) for name, rows in scored.items()},
    }
    if a < b:
        selected = min(CHALLENGERS, key=lambda m: portions["validation"][m]["log_loss"])
    else:
        selected = None
    # Validation and test sizes are genuine sample-size gates, not tunable excuses.
    ready = b - a >= 200 and N - b >= 200
    incumbent_test = portions["test"]["incumbent"]
    baseline_test = portions["test"]["uniform"]
    challenger_test = portions["test"][selected] if selected else None
    earned = bool(ready and challenger_test and
                  challenger_test["log_loss"] + 0.01 < min(
                      incumbent_test["log_loss"], baseline_test["log_loss"]) and
                  challenger_test["top1"] >= incumbent_test["top1"])
    report = {
        "research_version": RESEARCH_VERSION, "production_champion": MODEL_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "history_games": len(ordered), "forecastable_games": N,
        "unavailable_lineups": unavailable, "unknown_outcome_mass_assumed": UNKNOWN_MASS,
        "split": "chronological 60% development / 20% validation / 20% test",
        "selection": "best validation log loss among fixed challenger formulas",
        "selected_challenger": selected, "promotion_earned": earned,
        "status": "eligible_for_review_not_auto_promoted" if earned else (
            "insufficient_out_of_sample_data" if not ready else "failed_promotion_gate"),
        "promotion_gate": "at least 200 validation and 200 test games; test log-loss improvement >0.01 over incumbent and uniform; no top1 degradation",
        "note": "Fixed provisional 12% other-scorer bucket is applied equally to ALL methods for comparison; these are not calibrated live probabilities.",
        "results": portions
    }
    return report


def main():
    history = read_json(DATA / "history.json", [])
    report = research(history)
    write_json(DATA / "research.json", report)
    print({k: v for k, v in report.items() if k != "results"})
    print("TEST", report["results"]["test"])


if __name__ == "__main__":
    main()
