import unittest
from basketbot.research import aggregate, grade_one, research, weights


class ResearchGateTests(unittest.TestCase):
    def setUp(self):
        self.players = [{"id": str(i), "team_id": "home" if i < 5 else "away",
                         "shot_share": 0.2, "fg_probability": 0.45,
                         "probability": 0.1} for i in range(10)]

    def test_methods_are_normalized_and_deterministic(self):
        board = {"predictions": self.players}
        for method in ("uniform", "shot_allocation", "shot_x_fg", "first_fg_empirical"):
            probs = weights(board, [], method)
            self.assertEqual(len(probs), 10)
            self.assertAlmostEqual(sum(probs), 1)
            self.assertEqual(probs, weights(board, [], method))

    def test_unmodeled_scorer_is_scored_not_ignored(self):
        result = grade_one([.1] * 10, [str(i) for i in range(10)], "missing")
        self.assertEqual(result["lineup_miss"], 1)
        self.assertEqual(result["top1"], 0)
        self.assertAlmostEqual(result["log_loss"], -__import__("math").log(.12))
        self.assertEqual(aggregate([result])["lineup_misses"], 1)

    def test_empty_research_does_not_promote(self):
        report = research([])
        self.assertFalse(report["promotion_earned"])
        self.assertEqual(report["status"], "insufficient_out_of_sample_data")


if __name__ == "__main__":
    unittest.main()
