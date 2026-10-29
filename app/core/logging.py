import logging


def setup_logging() -> None:
    """Configure process logging. Callers must not log AWS or Gemini secrets."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
