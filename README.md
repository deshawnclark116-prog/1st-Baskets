# 🏀 First Basket Lab

An automated **first made field goal** research bot for NBA regular-season games. It does **not** predict the first point (which may be a free throw) or rely on betting odds as a signal.

## What the bot actually does

1. Downloads the official NBA CDN schedule, completed-game play-by-play, and box scores.
2. Extracts the first made field goal, first-shot and first-four-shot tendencies, tip winner when identifiable, and five historical starters.
3. Infers the likely starting five from *previous games only*, unless the current NBA boxscore explicitly reports five starters per team before tipoff. The board labels inferred lineups `projected`.
4. Combines recent opening-shot allocation with individual shot-making, opening-tip tendencies, turnover risk, and offensive rebounds. Simulates 40,000 opening sequences per matchup, returning probabilities across all ten starters (not just a single name).
5. Saves `data/latest.json` (upcoming board), `data/snapshots.json` (time-stamped predictions), `data/graded.json` (real pregame grades) and `data/backtest.json` (chronological research results).
6. Runs through GitHub Actions twice an hour, with a weekly backtest. The workflow commits updated files automatically.

### Files

- `basketbot/source.py`: source fetch, PBP/boxscore normalization and strict labeling.
- `basketbot/model.py`: projected lineups, shot allocation, tip model, simulation, proper scoring.
- `basketbot/__main__.py`: backfill / prediction / grading / backtest command-line tool.
- `tests/test_engine.py`: deterministic offline unit tests (no network or fabricated production results).
- `.github/workflows/bot.yml`: scheduled or manual runner.
- `docs/index.html`: optional GitHub Pages board; enable Pages from `/docs` on the `main` branch.

## Run it yourself

Requires Python 3.10+; no pip dependencies.

```bash
python -m unittest discover -s tests -v
python -m basketbot backfill --max-new 350
python -m basketbot run --max-new 150 --simulations 40000
python -m basketbot backtest --simulations 5000
```

In GitHub, choose **Actions → First Basket Bot → Run workflow**. Run **backfill** until enough historical games are ingested, then **run**. GitHub's scheduled workflows may be delayed, so they are not guaranteed to execute at a precise minute. Data ingestion can be rate-limited or blocked by the source.

## Methodological safeguards

- **No leakage:** predictions use games with strictly earlier tipoff dates; backtests never use the historical target game's actual starting lineup.
- **No invented data:** if the schedule, PBP, or five-man lineup cannot be verified, the game is skipped and a reason is provided.
- **Only actual made FG counts:** missed shots, foul shots, and rebounds do not qualify.
- **Model uncertainty:** published percentages are outputs of a provisional simulator, **not validated real-world confidence levels**. They may be miscalibrated; do not mistake Monte Carlo repetitions for independent evidence.
- **Grade honestly:** actual scorers outside projected lineups count as failures for top-1/top-3 rates; log/Brier scores are reported separately on covered candidates, with lineup-miss coverage displayed.
- **Proper validation:** `backtest` uses an expanding historical window and reports the latest 25% of predictions as a chronological holdout. The model formula is fixed before those results. A good outcome on synthetic unit tests does not establish forecast accuracy.
- **Research first:** do not call it a profitable betting system until it consistently beats a simple uniform and empirical baseline on out-of-sample data, including all lineup misses, ideally across several seasons.

## Data sources / reliability

NBA public feeds (may change or deny access):
- `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json`
- `https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json`
- `https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json`

The official schedule CDN contains the **current season**; historical data accumulates in GitHub as games complete. Backfill does **not** claim to fetch multiple past seasons without a separate historical schedule source. No sportsbook prices or guarantees are included. The current v1 is an experimental statistical baseline and has **not** yet been live-data validated on GitHub Actions.
