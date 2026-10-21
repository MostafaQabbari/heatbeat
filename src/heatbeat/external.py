"""Clients for open data: DWD weather forecast (via Bright Sky) and day-ahead prices (Energy-Charts).

What this file does: fetches and PARSES external data. Parsing is a pure
function, tested against saved sample responses, so a format change shows up
as a failing test, not as wrong recommendations. Network problems raise
`SourceUnavailable`; the optimiser then makes NO recommendation (S19).

Sources and terms (re-check before commercial use):
- Bright Sky (https://brightsky.dev, MIT): DWD open data; credit "Deutscher Wetterdienst"
- Energy-Charts (Fraunhofer ISE, https://api.energy-charts.info): add attribution
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx2


class SourceUnavailable(Exception):
    """The external source did not answer, or answered with something we cannot use."""


@dataclass(frozen=True)
class HourlyTemperature:
    ts: datetime
    temperature: float


@dataclass(frozen=True)
class HourlyPrice:
    ts: datetime
    eur_per_mwh: float


def parse_brightsky(payload: dict[str, Any]) -> list[HourlyTemperature]:
    """Bright Sky /weather response -> hourly temperatures (rows without temperature are skipped)."""
    try:
        rows = payload["weather"]
    except (KeyError, TypeError) as exc:
        raise SourceUnavailable("no 'weather' list in response") from exc
    if not isinstance(rows, list):
        raise SourceUnavailable("'weather' is not a list")
    out = []
    for row in rows:
        temp = row.get("temperature")
        if temp is None:
            continue
        ts = datetime.fromisoformat(row["timestamp"]).astimezone(UTC)
        out.append(HourlyTemperature(ts, float(temp)))
    return out


def parse_energy_charts(payload: dict[str, Any]) -> list[HourlyPrice]:
    """Energy-Charts /price response (parallel arrays) -> prices."""
    try:
        seconds, prices = payload["unix_seconds"], payload["price"]
    except (KeyError, TypeError) as exc:
        raise SourceUnavailable("no 'unix_seconds'/'price' arrays in response") from exc
    if len(seconds) != len(prices):
        raise SourceUnavailable("price arrays have different lengths")
    return [
        HourlyPrice(datetime.fromtimestamp(s, UTC), float(p))
        for s, p in zip(seconds, prices, strict=True)
        if p is not None
    ]


class OpenDataClient:
    def __init__(self, weather_url: str, prices_url: str, client: httpx2.Client | None = None) -> None:
        self.weather_url = weather_url.rstrip("/")
        self.prices_url = prices_url.rstrip("/")
        self.http = client or httpx2.Client(timeout=15, headers={"User-Agent": "heatbeat/0.1"})

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.get(url, params=params)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
        except (httpx2.HTTPError, ValueError) as exc:
            raise SourceUnavailable(f"{url}: {exc}") from exc

    def forecast(self, lat: float, lon: float, first: date, last: date) -> list[HourlyTemperature]:
        payload = self._get(
            f"{self.weather_url}/weather",
            {"lat": lat, "lon": lon, "date": first.isoformat(), "last_date": last.isoformat(), "tz": "UTC"},
        )
        return parse_brightsky(payload)

    def day_ahead_prices(self, first: date, last: date, zone: str = "DE-LU") -> list[HourlyPrice]:
        payload = self._get(
            f"{self.prices_url}/price", {"bzn": zone, "start": first.isoformat(), "end": last.isoformat()}
        )
        return parse_energy_charts(payload)
