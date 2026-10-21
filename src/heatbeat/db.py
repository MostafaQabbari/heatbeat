"""Database writer for the ingest path.

What this file does: writes one batch of checked messages to PostgreSQL in ONE
transaction: registers new plants, inserts readings with ON CONFLICT DO NOTHING
(so a redelivered MQTT message is harmless), and stores rejections. If the
connection breaks, it reconnects on the next attempt.
"""

import logging
from datetime import UTC, datetime

import psycopg

from heatbeat.contract import TelemetryMessage
from heatbeat.ingest import PlausibleRange, Rejection, check_reading

log = logging.getLogger(__name__)

INSERT_MEASUREMENTS = """
    INSERT INTO measurement (ts, plant_id, datapoint_id, value, quality)
    SELECT * FROM unnest(
        %s::timestamptz[], %s::integer[], %s::smallint[],
        %s::double precision[], %s::smallint[])
    ON CONFLICT ON CONSTRAINT measurement_unique DO NOTHING
"""


class PostgresSink:
    """Stores batches. `write` is all-or-nothing: commit, or raise and change nothing."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._conn: psycopg.Connection | None = None
        self._datapoints: dict[str, tuple[int, PlausibleRange]] = {}

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._url)
            self._load_catalogue(self._conn)
        return self._conn

    def _load_catalogue(self, conn: psycopg.Connection) -> None:
        rows = conn.execute("SELECT key, id, min_plausible, max_plausible FROM datapoint").fetchall()
        conn.commit()
        self._datapoints = {key: (dp_id, PlausibleRange(lo, hi)) for key, dp_id, lo, hi in rows}

    def write(self, messages: list[tuple[str, TelemetryMessage]], rejections: list[Rejection]) -> int:
        """Store a batch; return the number of NEW measurement rows."""
        try:
            conn = self._connection()
            with conn.transaction():
                return self._write(conn, messages, list(rejections))
        except psycopg.OperationalError:
            # Connection is broken: drop it so the next attempt reconnects.
            self.close()
            raise

    def _write(
        self,
        conn: psycopg.Connection,
        messages: list[tuple[str, TelemetryMessage]],
        rejections: list[Rejection],
    ) -> int:
        now = datetime.now(UTC)
        catalogue = {key: limits for key, (_, limits) in self._datapoints.items()}
        plant_ids = self._plant_ids(conn, messages)

        ts, plants, dps, values, qualities = [], [], [], [], []
        for topic, msg in messages:
            plant_id, owner = plant_ids[msg.plant]
            if owner != msg.gateway:
                # A gateway may not write data for a plant that belongs to another one.
                rejections.append(
                    Rejection(topic, "plant belongs to another gateway", msg.model_dump_json(by_alias=True))
                )
                continue
            for reading in msg.readings:
                result = check_reading(reading, now, catalogue)
                if isinstance(result, str):
                    rejections.append(Rejection(topic, result, reading.model_dump_json()))
                    continue
                ts.append(reading.t)
                plants.append(plant_id)
                dps.append(self._datapoints[reading.dp][0])
                values.append(reading.v)
                qualities.append(result)

        inserted = 0
        if ts:
            cur = conn.execute(INSERT_MEASUREMENTS, (ts, plants, dps, values, qualities))
            inserted = cur.rowcount
        if rejections:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO rejected_message (topic, reason, payload) VALUES (%s, %s, %s)",
                    [(r.topic, r.reason, r.payload[:10_000]) for r in rejections],
                )
        return inserted

    def _plant_ids(
        self, conn: psycopg.Connection, messages: list[tuple[str, TelemetryMessage]]
    ) -> dict[str, tuple[int, str]]:
        """Find or register each plant; return key -> (id, owning gateway).

        A plant stays bound to the first gateway that reported it.
        """
        first_claim: dict[str, str] = {}
        for _, msg in messages:
            first_claim.setdefault(msg.plant, msg.gateway)
        if not first_claim:
            return {}
        conn.execute(
            """
            INSERT INTO plant (key, gateway, last_seen_at)
            SELECT key, gateway, now() FROM unnest(%s::text[], %s::text[]) AS t(key, gateway)
            ON CONFLICT (key) DO UPDATE SET last_seen_at = now()
            """,
            (list(first_claim), list(first_claim.values())),
        )
        rows = conn.execute(
            "SELECT key, id, gateway FROM plant WHERE key = ANY(%s)", (list(first_claim),)
        ).fetchall()
        return {key: (plant_id, owner) for key, plant_id, owner in rows}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                log.warning("error while closing connection", exc_info=True)
            self._conn = None
