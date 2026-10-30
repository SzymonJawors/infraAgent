import logging


def setup_logging() -> None:
    """Configure process logging. Callers must not log AWS or OpenAI secrets."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
