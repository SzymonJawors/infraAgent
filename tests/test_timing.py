import unittest

from app.services.timing import Timing


class TimingTests(unittest.TestCase):
    def test_records_repeated_measurements_without_overwriting(self) -> None:
        timing = Timing()
        with timing.measure("terraform_plan"):
            pass
        with timing.measure("terraform_plan"):
            pass

        self.assertIn("terraform_plan", timing.values)
        self.assertIn("terraform_plan_2", timing.values)
        self.assertGreaterEqual(timing.report()["terraform_plan"], 0)


if __name__ == "__main__":
    unittest.main()
