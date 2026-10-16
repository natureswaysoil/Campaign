import unittest
from optimized_launch_preview import allocate_launch_budgets, build_optimized_launch_preview

class LaunchBudgetTests(unittest.TestCase):
    def test_exact_total_across_budget_range(self):
        for cents in range(900, 5001, 17):
            total = cents / 100
            plan = allocate_launch_budgets(total)
            self.assertEqual(round(sum(plan), 2), total)
            self.assertTrue(all(x >= 3 for x in plan))
    def test_rejects_insufficient_and_invalid_budget(self):
        for value in (0, 3, 8.99, 50.01, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                allocate_launch_budgets(value)
    def test_preview_does_not_increase_budget(self):
        plan = build_optimized_launch_preview({"asin":"B000000000","sku":"SKU1","title":"Liquid Bone Meal"}, budget=10)
        self.assertEqual(sum(c["daily_budget"] for c in plan["campaigns"]), 10)
        self.assertEqual(len(plan["campaigns"]), 3)

if __name__ == "__main__":
    unittest.main()
