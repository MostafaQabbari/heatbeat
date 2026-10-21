"""Tests for the open-data clients (src/heatbeat/external.py), scenario S19.

The sample responses follow the documented formats of Bright Sky and Energy-Charts
(shortened). If a provider changes its format, these tests show it.
"""

from datetime import UTC, date, datetime

import httpx2
import pytest

from heatbeat.external import OpenDataClient, SourceUnavailable, parse_brightsky, parse_energy_charts

BRIGHTSKY_SAMPLE = {
    "weather": [
        {"timestamp": "2026-10-21T00:00:00+00:00", "source_id": 1, "temperature": 7.3, "precipitation": 0.0},
        {"timestamp": "2026-10-21T01:00:00+00:00", "source_id": 1, "temperature": 6.9, "precipitation": 0.1},
        {"timestamp": "2026-10-21T02:00:00+02:00", "source_id": 1, "temperature": None},
    ],
    "sources": [{"id": 1, "dwd_station_id": "00096", "observation_type": "forecast"}],
}

ENERGY_CHARTS_SAMPLE = {
    "license_info": "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de",
    "unix_seconds": [1792540800, 1792544400, 1792548000],
    "price": [92.5, 88.1, None],
    "unit": "EUR / MWh",
    "deprecated": False,
}


def test_brightsky_rows_become_utc_hourly_temperatures():
    hours = parse_brightsky(BRIGHTSKY_SAMPLE)

    assert [h.temperature for h in hours] == [7.3, 6.9]  # the row without temperature is skipped
    assert hours[0].ts == datetime(2026, 10, 21, 0, 0, tzinfo=UTC)


def test_energy_charts_parallel_arrays_become_prices():
    prices = parse_energy_charts(ENERGY_CHARTS_SAMPLE)

    assert [p.eur_per_mwh for p in prices] == [92.5, 88.1]
    assert prices[0].ts.tzinfo is UTC


@pytest.mark.parametrize("payload", [{}, {"weather": None}, None])
def test_s19_unexpected_weather_format_is_a_clear_error(payload):
    with pytest.raises(SourceUnavailable):
        parse_brightsky(payload)


def test_price_arrays_of_different_length_are_rejected():
    with pytest.raises(SourceUnavailable):
        parse_energy_charts({"unix_seconds": [1, 2], "price": [1.0]})


def client_returning(status: int, body: object = None) -> OpenDataClient:
    transport = httpx2.MockTransport(lambda _req: httpx2.Response(status, json=body))
    return OpenDataClient("https://weather.test", "https://prices.test", httpx2.Client(transport=transport))


def test_client_sends_the_documented_query():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=BRIGHTSKY_SAMPLE)

    client = OpenDataClient(
        "https://weather.test", "https://p.test", httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    client.forecast(50.09, 9.06, date(2026, 10, 21), date(2026, 10, 24))

    assert seen[0].url.path == "/weather"
    assert seen[0].url.params["last_date"] == "2026-10-24"


def test_s19_source_down_raises_source_unavailable():
    with pytest.raises(SourceUnavailable):
        client_returning(503).forecast(50.0, 9.0, date(2026, 10, 21), date(2026, 10, 24))
