import unittest

from app.services.checkov_scan import _only_accepted_findings


class CheckovPolicyTests(unittest.TestCase):
    def test_s3_scope_findings_are_non_blocking(self) -> None:
        output = "CKV_AWS_18\nCKV2_AWS_61\nCKV_AWS_144\nCKV2_AWS_62"

        self.assertTrue(_only_accepted_findings(output))

    def test_security_findings_remain_blocking(self) -> None:
        output = "CKV_AWS_18\nCKV_AWS_145"

        self.assertFalse(_only_accepted_findings(output))


if __name__ == "__main__":
    unittest.main()
