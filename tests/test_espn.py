"""Fixture-driven ESPN adapter tests: no live requests or fabricated game results."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from basketbot.espn import get_schedule, normalize_game, parse_players, parse_schedule
from basketbot.model import score_predictions


def event(game_id="401999999", state="post"):
    return {
        "id": game_id, "date": "2026-11-15T20:00Z", "season": {"type": 2},
        "status": {"type": {"state": state, "completed": state == "post"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"id": "1", "abbreviation": "ATL"}},
            {"homeAway": "away", "team": {"id": "2", "abbreviation": "BOS"}}
        ]}]
    }


def meta():
    return parse_schedule({"events": [event()]})[0]


def summary():
    sides = []
    for team in ("1", "2"):
        entries = []
        for j in range(1, 6):
            entries.append({
                "athlete": {"id": f"{team}{j}", "displayName": f"Player {team}{j}"},
                "starter": True, "stats": ["28", "4-10", "1-2", "2-3"]
            })
        sides.append({"team": {"id": team}, "statistics": [{
            "names": ["MIN", "FG", "3PT", "FT"], "athletes": entries
        }]})
    return {
        "boxscore": {"players": sides},
        "plays": [
            {"id": "1", "sequenceNumber": 1, "period": {"number": 1},
             "text": "Player 11 makes free throw", "type": {"text": "Free Throw"},
             "scoringPlay": True, "scoreValue": 1, "team": {"id": "1"},
             "participants": [{"athlete": {"id": "11"}}]},
            {"id": "2", "sequenceNumber": 2, "period": {"number": 1},
             "text": "Player 21 misses 14-foot jumper", "type": {"text": "Jump Shot"},
             "shootingPlay": True, "scoringPlay": False, "scoreValue": 0,
             "team": {"id": "2"}, "participants": [{"athlete": {"id": "21"}}]},
            {"id": "3", "sequenceNumber": 3, "period": {"number": 1},
             "text": "Player 13 makes 3-point jumper", "type": {"text": "Jump Shot"},
             "shootingPlay": True, "scoringPlay": True, "scoreValue": 3,
             "team": {"id": "1"}, "participants": [{"athlete": {"id": "13"}}]},
            {"id": "4", "sequenceNumber": 4, "period": {"number": 1},
             "text": "Player 14 makes layup", "type": {"text": "Layup"},
             "shootingPlay": True, "scoringPlay": True, "scoreValue": 2,
             "team": {"id": "1"}, "participants": [{"athlete": {"id": "14"}}]},
        ]
    }


class ESPNAdapterTests(unittest.TestCase):
    def test_regular_season_and_started_status(self):
        a = event()
        b = event("401999998", "in")
        c = event("401999997")
        c["season"]["type"] = 3
        games = parse_schedule({"events": [a, b, c]})
        self.assertEqual([g["status"] for g in games], [3, 2])
        self.assertEqual({g["data_source"] for g in games}, {"espn_site_v2"})

    def test_monthly_calendar_is_complete(self):
        with patch("basketbot.espn.fetch_json", return_value={"events": [event()]}) as fetch, \
             patch("basketbot.espn.time.sleep"):
            games = get_schedule(datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(len(games), 1)  # Dedupe same event across months.
        self.assertEqual(fetch.call_count, 3)  # Oct, Nov, Dec.

    def test_first_basket_skips_free_throws_and_missed_shots(self):
        game = normalize_game(meta(), summary())
        self.assertEqual(game["first_fg"], {"player_id": "13", "team_id": "1"})
        self.assertEqual(game["first_shot"], {"2": "21", "1": "13"})
        self.assertEqual(game["early_shots"][0]["made"], False)
        players = parse_players(summary(), meta())
        self.assertEqual((players[0]["fgm"], players[0]["fga"]), (4, 10))
        self.assertEqual(sum(p["starter"] for p in players), 10)

    def test_missing_starters_and_actor_fail_closed(self):
        payload = summary()
        payload["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["starter"] = False
        with self.assertRaises(ValueError):
            normalize_game(meta(), payload)
        payload = summary()
        payload["plays"][2]["participants"] = []
        payload["plays"][3]["participants"] = []
        with self.assertRaises(ValueError):
            normalize_game(meta(), payload)

    def test_lineup_miss_is_not_hidden_from_full_scoring(self):
        prediction = {"predictions": [
            {"id": str(i), "probability": 0.1} for i in range(10)
        ]}
        s = score_predictions(prediction, "absent")
        self.assertFalse(s["graded"])
        self.assertTrue(s["lineup_miss"])
        self.assertEqual(s["top1"], 0)
        self.assertGreater(s["log_loss_all"], 18)
        self.assertAlmostEqual(s["brier_all"], 0.1)
        covered = score_predictions(prediction, "1")
        self.assertTrue(covered["graded"])
        self.assertAlmostEqual(covered["log_loss"], covered["uniform_log_loss"])


if __name__ == "__main__":
    unittest.main()
