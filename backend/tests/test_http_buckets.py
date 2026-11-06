from __future__ import annotations

from prometheus_client import generate_latest

from scadbuddy.core.metrics import Metrics


def test_a_slow_request_lands_in_a_bucket_above_10_s() -> None:
    """#2293: list routes take 10 to 16 s and a save with its polling ~37 s. Past the
    top finite bucket a quantile reads as that bucket, so the buckets go on to 120 s."""
    metrics = Metrics()
    metrics.http_duration.labels("GET", "/api/v1/models").observe(12)
    metrics.http_duration.labels("GET", "/api/v1/models").observe(37)
    text = generate_latest(metrics.registry).decode()

    def bucket(le: str) -> float:
        line = next(
            line
            for line in text.splitlines()
            if line.startswith("scadbuddy_http_request_duration_seconds_bucket")
            and f'le="{le}"' in line
        )
        return float(line.rsplit(" ", 1)[1])

    assert bucket("10.0") == 0
    assert bucket("15.0") == 1
    assert bucket("30.0") == 1
    assert bucket("60.0") == 2
    assert bucket("120.0") == 2
