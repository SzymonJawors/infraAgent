import unittest
from unittest.mock import patch

from app.core.config import get_settings


class SettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_settings_factory_is_cached_and_usable(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            first = get_settings()
            second = get_settings()

        self.assertIs(first, second)
        self.assertEqual(first.aws_region, "eu-central-1")


if __name__ == "__main__":
    unittest.main()
