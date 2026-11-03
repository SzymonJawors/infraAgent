import logging
import time
from dataclasses import dataclass, field


logger = logging.getLogger("infraagent.timing")


@dataclass
class Timing:
    values: dict[str, float] = field(default_factory=dict)

    def measure(self, name: str):
        existing = [key for key in self.values if key == name or key.startswith(f"{name}_")]
        metric_name = name if not existing else f"{name}_{len(existing) + 1}"
        return _Measurement(self, metric_name)

    def report(self) -> dict[str, int]:
        result = {name: round(seconds * 1000) for name, seconds in self.values.items()}
        logger.info("agent_timing %s", " ".join(f"{name}_ms={value}" for name, value in result.items()))
        return result


class _Measurement:
    def __init__(self, timing: Timing, name: str) -> None:
        self.timing = timing
        self.name = name
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.timing.values[self.name] = time.perf_counter() - self.started
