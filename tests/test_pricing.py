import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.services.pricing import AwsPricing


class PricingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pricing = AwsPricing(Settings(aws_region="eu-central-1"))

    def test_estimate_uses_aws_pricing_output(self) -> None:
        output = """
        # aws_instance.web will be created
          instance_type = "t3.micro"
        # aws_instance.old will be destroyed
          instance_type = "m5.large"
        Plan: 1 to add, 0 to change, 0 to destroy.
        """
        with patch.object(self.pricing, "_lookup", return_value=0.0104):
            estimate = self.pricing.estimate(output)

        self.assertEqual(estimate.status, "complete")
        self.assertAlmostEqual(estimate.monthly_usd or 0, 7.592, places=3)
        self.assertIn("EC2 aws_instance.web (t3.micro)", estimate.items[0])
        self.assertEqual(len(estimate.items), 1)

    def test_unavailable_pricing_does_not_create_fake_amount(self) -> None:
        with patch.object(self.pricing, "_lookup", return_value=None):
            estimate = self.pricing.estimate(
                '# aws_db_instance.main will be created\n  instance_class = "db.t3.micro"'
            )

        self.assertIsNone(estimate.monthly_usd)
        self.assertEqual(estimate.status, "unavailable")

    def test_s3_and_kms_are_reported_as_unpriced_cost_components(self) -> None:
        output = """
        # aws_s3_bucket.private will be created
        # aws_kms_key.s3 will be created
        """
        with patch.object(self.pricing, "_lookup", return_value=None):
            estimate = self.pricing.estimate(output)

        self.assertEqual(estimate.status, "unavailable")
        self.assertIsNone(estimate.monthly_usd)
