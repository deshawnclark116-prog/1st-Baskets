"""GitHub Actions-ready CLI: run, backfill, validate."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .model import MODEL_VERSION, predict_game, score_predictions
from .espn import get_completed_game, get_reported_starters, get_schedule, SOURCE

LOG = logging.getLogger("first_baskets")
DATA = Path("data")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_history(history):
    history.sort(key=lambda g: (g["tipoff_utc"], g["game_id"]))
    write_json(DATA / "history.json", history)


def backfill(schedule: list[dict], history: list[dict], max_new: int) -> dict:
    sources = {g.get("data_source") for g in history}
    if sources and sources != {SOURCE}:
        raise RuntimeError(f"Mixed or unverified historical providers: {sources}. Refusing cross-ID training.")
    seen = {g["game_id"] for g in history}
    candidates = [g for g in schedule if g["status"] == 3 and g["game_id"] not in seen and
                  datetime.fromisoformat(g["tipoff_utc"]) < now_utc()]
    newly = 0
    failures = []
    # Seed recent training first so initial runs do not forecast from stale October-only data.
    candidates.reverse()
    for g in candidates[:max_new]:
        try:
            game = get_completed_game(g)
            history.append(game)
            seen.add(g["game_id"])
            newly += 1
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("Skipped completed game %s: %s", g["game_id"], exc)
            failures.append({"game_id": g["game_id"], "reason": str(exc)[:220]})
    save_history(history)
    return {"new_games": newly, "remaining_games": max(0, len(candidates) - max_new),
            "failed": failures[:25], "historical_games": len(history)}


def upcoming_predictions(schedule: list[dict], history: list[dict], n: int) -> dict:
    now = now_utc()
    upcoming = [g for g in schedule if g["status"] == 1 and now < datetime.fromisoformat(g["tipoff_utc"]) <= now + timedelta(hours=30)]
    predictions, skipped = [], []
    for g in upcoming:
        try:
            # Do not pretend that a projected starting five is confirmed.
            # Only query current-game starters near tipoff and report provenance.
            near_tip = datetime.fromisoformat(g["tipoff_utc"]) - now <= timedelta(hours=2)
            confirmed = get_reported_starters(g) if near_tip else None
            p = predict_game(g, history, confirmed=confirmed, n=n)
            p["generated_at_utc"] = now.isoformat()
            p["data_source"] = SOURCE
            predictions.append(p)
        except (ValueError, RuntimeError, KeyError) as exc:
            skipped.append({"game_id": g["game_id"], "matchup": f"{g['away']['code']} @ {g['home']['code']}", "reason": str(exc)})
    return {"generated_at_utc": now.isoformat(), "model": MODEL_VERSION,
            "version": __version__, "disclaimer": "Unvalidated research probabilities; not guaranteed or calibrated betting edges.",
            "games": predictions, "skipped": skipped}


def persist_snapshots(board: dict):
    path = DATA / "snapshots.json"
    snapshots = read_json(path, [])
    for prediction in board["games"]:
        previous = next((p for p in reversed(snapshots) if p["game_id"] == prediction["game_id"]), None)
        current_ids = [p["id"] for p in prediction["predictions"]]
        prior_ids = [p["id"] for p in previous["predictions"]] if previous else []
        # One snapshot when new/lineups change; retain periodic latest pre-tip for grading.
        elapsed = (datetime.fromisoformat(prediction["generated_at_utc"]) -
                   datetime.fromisoformat(previous["generated_at_utc"])).total_seconds() if previous else float("inf")
        changed_top = bool(previous and previous["predictions"][0]["id"] != prediction["predictions"][0]["id"])
        changed_probability = bool(previous and abs(previous["predictions"][0]["probability"] - prediction["predictions"][0]["probability"]) >= 0.015)
        if previous is None or previous["lineup_status"] != prediction["lineup_status"] or current_ids != prior_ids or changed_top or changed_probability or elapsed >= 900:
            snapshots.append(prediction)
    write_json(path, snapshots)


def grade_live(history: list[dict]) -> dict:
    snapshots = read_json(DATA / "snapshots.json", [])
    actual = {g["game_id"]: g for g in history}
    by_game = {}
    for p in snapshots:
        g = actual.get(p["game_id"])
        # Explicit point-in-time restriction: no forecast made at/after tipoff.
        if g and p["generated_at_utc"] < g["tipoff_utc"]:
            if p["game_id"] not in by_game or p["generated_at_utc"] > by_game[p["game_id"]]["generated_at_utc"]:
                by_game[p["game_id"]] = p
    rows = []
    for gid, p in sorted(by_game.items()):
        result = score_predictions(p, actual[gid]["first_fg"]["player_id"])
        rows.append({"game_id": gid, "tipoff_utc": actual[gid]["tipoff_utc"],
                     "lineup_status": p["lineup_status"], "first_scorer": actual[gid]["first_fg"]["player_id"],
                     "top_prediction": p["predictions"][0]["name"], **result})
    summary = aggregate(rows)
    report = {"model": MODEL_VERSION, "generated_at_utc": now_utc().isoformat(),
              "scope": "latest genuine pre-tip snapshot per game (no hindsight)", **summary, "rows": rows}
    write_json(DATA / "graded.json", report)
    return report


def aggregate(rows):
    valid = [r for r in rows if r.get("graded")]
    covered = [r for r in valid if not r.get("lineup_miss")]
    def avg(key):
        return round(sum(r[key] for r in covered) / len(covered), 5) if covered else None
    def avg_all(key):
        present = [r[key] for r in rows if key in r]
        return round(sum(present) / len(present), 5) if present else None
    return {"eligible_predictions": len(rows), "graded_predictions": len(valid),
            "lineup_misses": sum(bool(r.get("lineup_miss")) for r in valid) + len(rows) - len(valid),
            "top1_hits": sum(r.get("top1", 0) for r in valid),
            "top3_hits": sum(r.get("top3", 0) for r in valid),
            "top1_rate_all_predictions": round(sum(r.get("top1", 0) for r in valid) / len(rows), 5) if rows else None,
            "top3_rate_all_predictions": round(sum(r.get("top3", 0) for r in valid) / len(rows), 5) if rows else None,
            "mean_log_loss_on_covered": avg("log_loss"),
            "uniform_log_loss_on_covered": avg("uniform_log_loss"),
            "mean_brier_on_covered": avg("brier"), "uniform_brier_on_covered": avg("uniform_brier"),
            "mean_log_loss_all": avg_all("log_loss_all"), "uniform_log_loss_all": avg_all("uniform_log_loss_all"),
            "mean_brier_all": avg_all("brier_all"), "uniform_brier_all": avg_all("uniform_brier_all")}


def backtest(history: list[dict], n: int) -> dict:
    """Expanding-window, chronological, model fixed before holdout (not in-sample)."""
    ordered = sorted(history, key=lambda g: (g["tipoff_utc"], g["game_id"]))
    rows = []
    for game in ordered:
        prior = [g for g in ordered if g["tipoff_utc"] < game["tipoff_utc"]]
        if len(prior) < 40:
            continue
        try:
            # We deliberately DO NOT provide the historical game's actual starters.
            predicted = predict_game(game, prior, confirmed=None, n=n)
            score = score_predictions(predicted, game["first_fg"]["player_id"])
            rows.append({"game_id": game["game_id"], "tipoff_utc": game["tipoff_utc"],
                         "actual": game["first_fg"]["player_id"],
                         "predicted": predicted["predictions"][0]["id"], **score})
        except (ValueError, RuntimeError) as exc:
            rows.append({"game_id": game["game_id"], "tipoff_utc": game["tipoff_utc"],
                         "graded": False, "reason": "unavailable_lineup: " + str(exc)})
    cut = max(0, int(len(rows) * 0.75))
    catalog = read_json(DATA / "schedule.json", [])
    finished = {g["game_id"] for g in catalog if g.get("status") == 3}
    archived = {g["game_id"] for g in ordered}
    coverage = round(len(finished & archived) / len(finished), 5) if finished else None
    report = {"model": MODEL_VERSION, "generated_at_utc": now_utc().isoformat(),
              "catalog_finished_games": len(finished), "archive_coverage": coverage,
              "missing_finished_games": len(finished - archived),
              "scope": "walk-forward projected lineups; never actual same-game starting lineup",
              "history_games": len(ordered), "all_games": aggregate(rows),
              "last_25_percent_holdout": aggregate(rows[cut:]), "holdout_start": rows[cut]["tipoff_utc"] if cut < len(rows) else None,
              "rows": rows}
    write_json(DATA / "backtest.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="NBA first made field goal prediction bot")
    parser.add_argument("command", choices=("run", "backfill", "backtest"))
    parser.add_argument("--max-new", type=int, default=120, help="Max games to ingest per run")
    parser.add_argument("--simulations", type=int, default=40000)
    args = parser.parse_args()
    if args.max_new < 0 or args.simulations < 1000:
        parser.error("--max-new must be >=0 and --simulations >=1000")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DATA.mkdir(exist_ok=True)
    history = read_json(DATA / "history.json", [])
    if args.command != "backtest":
        games = get_schedule()  # Fail loudly if source unavailable; no synthetic replacement.
        write_json(DATA / "schedule.json", games)  # Source-derived completeness manifest.
        ingest = backfill(games, history, args.max_new)
        print("Ingestion:", json.dumps(ingest))
        if args.command == "run":
            board = upcoming_predictions(games, history, args.simulations)
            write_json(DATA / "latest.json", board)
            write_json(Path("docs/latest.json"), board)
            persist_snapshots(board)
            graded = grade_live(history)
            print("Upcoming predictions:", len(board["games"]), "skipped:", len(board["skipped"]))
            print("Graded pregame predictions:", graded["eligible_predictions"])
    else:
        report = backtest(history, args.simulations)
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
