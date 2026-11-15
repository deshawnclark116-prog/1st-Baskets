import unittest
from datetime import datetime, timezone, timedelta

from basketbot.source import schedule, normalize_game, made_fg, is_starter
from basketbot.model import predict_game, simulate, score_predictions, games_before


class EngineTests(unittest.TestCase):
    def make_game(self, day, home="H", away="A", winner="H1"):
        players = []
        for team in (home, away):
            for i in range(1, 6):
                players.append({"id": f"{team}{i}", "name": f"{team} player {i}", "team_id": team,
                                "starter": True, "fga": 10 if i == 1 else 5,
                                "fgm": 5 if i == 1 else 2, "minutes": "PT30M00S"})
        return {"game_id": f"002260{day:04d}",
                "tipoff_utc": (datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(days=day)).isoformat(),
                "home": {"id": home, "code": home}, "away": {"id": away, "code": away},
                "players": players, "early_shots": [{"player_id": f"{home}1", "team_id": home, "made": True}],
                "first_shot": {home: f"{home}1"}, "first_fg": {"player_id": winner, "team_id": home}, "tip_team_id": home}

    def test_schedule_regular_only(self):
        raw = {"leagueSchedule": {"gameDates": [{"games": [
            {"gameId": "0022600001", "gameDateTimeUTC": "2026-11-15T17:00:00Z", "gameStatus": 1,
             "homeTeam": {"teamId": 5, "teamTricode": "CHI"}, "awayTeam": {"teamId": 2, "teamTricode": "BOS"}},
            {"gameId": "0012600001", "gameDateTimeUTC": "2026-11-15T17:00:00Z", "gameStatus": 3,
             "homeTeam": {"teamId": 5}, "awayTeam": {"teamId": 2}}]}]}}
        self.assertEqual(len(schedule(raw)), 1)
        self.assertEqual(schedule(raw)[0]["home"]["id"], "5")

    def test_first_field_goal_not_free_throw(self):
        meta = self.make_game(1)
        box = {"game": {side + "Team": {"teamId": meta[side]["id"], "players": [
            {"personId": f"{meta[side]['id']}{i}", "name": "X", "starter": "1",
             "statistics": {"fieldGoalsAttempted": 8, "fieldGoalsMade": 4, "minutes": "PT30M00S"}}
            for i in range(1, 6)]} for side in ("home", "away")}}
        pbp = {"game": {"actions": [
            {"orderNumber": 1, "period": 1, "actionType": "freethrow", "personId": "H1", "teamId": "H", "scoreHome": "1", "scoreAway": "0"},
            {"orderNumber": 2, "period": 1, "actionType": "2pt", "isFieldGoal": 1, "shotResult": "Missed", "personId": "A1", "teamId": "A", "scoreHome": "1", "scoreAway": "0"},
            {"orderNumber": 3, "period": 1, "actionType": "2pt", "isFieldGoal": 1, "shotResult": "Made", "personId": "H2", "teamId": "H", "scoreHome": "3", "scoreAway": "0"}]}}
        rec = normalize_game(meta, box, pbp)
        self.assertEqual(rec["first_fg"]["player_id"], "H2")
        self.assertFalse(rec["early_shots"][0]["made"])

    def test_lookahead_exclusion(self):
        history = [self.make_game(i) for i in range(1, 8)]
        self.assertEqual(len(games_before(history, history[5]["tipoff_utc"])), 5)

    def test_probabilities_are_deterministic_and_sum_to_one(self):
        history = [self.make_game(i) for i in range(1, 16)]
        future = self.make_game(16)
        a = predict_game(future, history, n=2000)
        b = predict_game(future, history, n=2000)
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(p["probability"] for p in a["predictions"]), 1, places=4)
        self.assertEqual(len(a["predictions"]), 10)
        self.assertEqual(a["lineup_status"], "projected")
        self.assertTrue(a["predictions"][0]["probability"] >= a["predictions"][1]["probability"])
        score = score_predictions(a, a["predictions"][0]["id"])
        self.assertEqual(score["top1"], 1)

    def test_unseen_scorer_does_not_get_false_grade(self):
        history = [self.make_game(i) for i in range(1, 16)]
        p = predict_game(self.make_game(16), history, n=1000)
        self.assertFalse(score_predictions(p, "not_a_starter")["graded"])

    def test_missing_history_blocks_predictions(self):
        with self.assertRaises(ValueError):
            predict_game(self.make_game(1), [], n=1000)

    def test_boolean_starter(self):
        self.assertFalse(is_starter("0"))
        self.assertTrue(is_starter("1"))
        self.assertTrue(is_starter(True))


if __name__ == "__main__":
    unittest.main()
