# 🏀 First Basket Lab

An autonomous, **research-stage NBA first made field goal** forecast engine. The target is the first **made field goal**, not the first point (which could be a free throw). Betting odds are not used to generate the player probabilities.

## Current operational status

- **Data source:** ESPN Site API v2 scoreboard and game summaries, including play-by-play and box scores. This unofficial feed replaced the NBA CDN after GitHub Actions received HTTP 403 responses. ESPN team/player/game IDs are isolated; never merge provider histories without an explicit verified mapping.
- **Backfill:** ESPN regular-season events from the current and previous NBA seasons (October-April), plus the next 35 days. Completed-game ingestion is incremental, most recent first. Source failures fail the run; fabricated games are prohibited.
- **Research engine:** Recent starter probability, first-shot allocation, first-four-shot data, shooting efficiency, and opening possession simulator. **40,000** seeded scenarios per upcoming game produce a distribution across ten projected or reported starters.
- **Forecast provenance:** An explicit five-starter pre-tip feed listing is labeled `espn_feed_reported`; otherwise the board says `projected` and `watchlist_only`. ESPN reporting is not the same as independent verified lineups.
- **Automatic workflow:** GitHub Actions scheduled twice per hour (subject to scheduler delays), with incremental game ingestion, prediction snapshots, postgame grading, and weekly chronological backtesting. Code pushes also run a research backtest.
- **Evaluation:** Every historical game is processed in time order. Predictions never receive the target game's actual starters. Real pre-tip predictions are saved and graded only against the actual outcome; out-of-lineup first scorers count as top-1/top-3 failures and incur a proper-score penalty.

### Known limitations

**Do not interpret the displayed simulated percentages as calibrated winning probabilities.** An increase in Monte Carlo simulations reduces simulation noise, not model bias.

- Predicted lineups can be wrong; any opponent, substitution or starting-five change can invalidate a candidate.
- Opening tip data is missing in the current ESPN normalization, so the tip prior remains essentially neutral (0.5). Turnover and offensive rebound parameters are provisional fixed assumptions, not team-specific models.
- First-four-shot tendencies capture each team's early shot attempts, not necessarily only shots before the game's first made field goal.
- The historical archive currently grows incrementally backward into 2025–26; until finished, training coverage is incomplete. Season-boundary shifts must be assessed separately.
- The current model is a **baseline**. A reported success rate on a short historical subset does not establish predictive value, calibration, or profitability.

## Repository

- `basketbot/espn.py`: ESPN event discovery, completed-game normalization, boxscore and PBP validation.
- `basketbot/source.py`: legacy NBA CDN parser retained for comparison/offline tests, not active in the live pipeline.
- `basketbot/model.py`: leak-safe starter inference, shot and tip modeling, seeded simulator, outcome scoring.
- `basketbot/__main__.py`: command line, point-in-time pregame snapshots, grading, source schedule manifest and expanding-window backtest.
- `basketbot/research.py`: four fixed challenger formulas versus incumbent; 60/20/20 chronological selection and a hard, nonautomatic promotion gate.
- `tests/test_research.py`: deterministic scoring and promotion-safety unit tests.
- `tests/test_engine.py`, `tests/test_espn.py`: deterministic offline tests, including source and grade-miss behavior.
- `.github/workflows/bot.yml`: scheduled and manual runs.
- `docs/index.html`: static GitHub Pages research board (enable Pages from `/docs` on `main`).
- `data/history.json`: verified, completed game samples, containing no invented results.
- `data/snapshots.json`: retained predictions with timestamps, never overwritten after tip.
- `data/graded.json`: latest genuine pre-tip snapshot per finished game.
- `data/backtest.json`: expanding-window backtest (last 25% reported as chronological holdout), with an archive-coverage diagnostic.
- `data/schedule.json`: provider-derived expected schedule; exposes missing historical games.
- `data/research.json`: incumbent-versus-challenger results; provisional OTHER-scorer mass is applied equally across methods, with insufficient-data outcomes visible.

## Reproduce

Python 3.10+; no third-party Python package dependencies.

```bash
python -m unittest discover -s tests -v
python -m basketbot run --max-new 150 --simulations 40000
python -m basketbot backfill --max-new 350
python -m basketbot backtest --simulations 5000
python -m basketbot.research
```

GitHub: **Actions → First Basket Bot → Run workflow**. Select `run`, `backfill`, or `backtest`. The schedule may be delayed by GitHub and the unofficial ESPN service can throttle or change formats.

## Promotion rules

The candidate model stays experimental until independent chronological holdouts show reproducible improvements over **uniform** and **empirical first-FG** baselines on comparable eligible games, including lineup coverage misses. All changes should be compared with a fixed, previously archived champion; do not tune repeatedly against an already-used holdout. Report sample size, source gaps, coverage, top-1/top-3, log loss, Brier, and model calibration before claiming superiority.

The data feed and predictions are not guaranteed. No betting edge or profit is established by simulations alone.
