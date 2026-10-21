"""Demo data: customers, users, sites, gateways, plants and default alarm rules.

What this file does: provisions everything the simulator needs (gateways must
be known before they may send data) and creates three users per customer
(admin, operator, viewer). Safe to run again: existing rows are kept or updated.
Plants are spread over the two customers so tenant isolation can be seen.

Run it with:  uv run heatbeat-seed --plants 9 --password <your-demo-password>
"""

import argparse
import random
import secrets

import psycopg

from heatbeat.alarm_rules import DEFAULT_RULES
from heatbeat.auth import hash_password
from heatbeat.config import Settings
from heatbeat.sim.plants import plant_type_for

TENANTS = [("demo-energie", "Demo Energie GmbH"), ("demo-wohnen", "Demo Wohnen eG")]
TOWNS = [  # (name, latitude, longitude): towns in the Rhein-Main / Lower Franconia area
    ("Alzenau", 50.088, 9.061),
    ("Aschaffenburg", 49.977, 9.152),
    ("Hanau", 50.133, 8.917),
    ("Offenbach", 50.096, 8.766),
    ("Frankfurt", 50.110, 8.682),
    ("Darmstadt", 49.872, 8.651),
    ("Wuerzburg", 49.791, 9.953),
    ("Gelnhausen", 50.201, 9.188),
]


def seed(conn: psycopg.Connection, plants: int, password: str, seed_value: int = 42) -> list[tuple[str, str]]:
    rng = random.Random(seed_value)
    users = []
    with conn.transaction():
        tenant_ids = []
        for key, name in TENANTS:
            tenant_ids.append(
                conn.execute(
                    "INSERT INTO tenant (key, name) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                    (key, name),
                ).fetchone()[0]  # type: ignore[index]
            )
            for role in ("admin", "operator", "viewer"):
                email = f"{role}@{key}.example"
                conn.execute(
                    """
                    INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash
                    """,
                    (tenant_ids[-1], email, hash_password(password), role),
                )
                users.append((email, role))

        for i in range(1, plants + 1):
            tenant_id = tenant_ids[(i - 1) % len(tenant_ids)]
            town, lat, lon = TOWNS[(i - 1) % len(TOWNS)]
            site_name = f"Liegenschaft {i:03d}"
            coords = (round(lat + rng.uniform(-0.02, 0.02), 5), round(lon + rng.uniform(-0.03, 0.03), 5))
            existing = conn.execute(
                "SELECT id FROM site WHERE tenant_id = %s AND name = %s", (tenant_id, site_name)
            ).fetchone()
            if existing:
                site_id = existing[0]
            else:
                site_id = conn.execute(
                    "INSERT INTO site (tenant_id, name, address, latitude, longitude) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (tenant_id, site_name, f"{town} (demo address)", *coords),
                ).fetchone()[0]  # type: ignore[index]
            gateway, plant = f"gw-{i:04d}", f"p-{i:04d}"
            conn.execute(
                "INSERT INTO gateway (key, tenant_id) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
                (gateway, tenant_id),
            )
            ptype = plant_type_for(i).value
            conn.execute(
                """
                INSERT INTO plant (key, gateway, tenant_id, site_id, plant_type, name) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, site_id = EXCLUDED.site_id,
                    plant_type = EXCLUDED.plant_type, name = EXCLUDED.name
                """,
                (plant, gateway, tenant_id, site_id, ptype, f"{town} {ptype.replace('_', ' ')} {i}"),
            )

        for tenant_id in tenant_ids:
            for rule in DEFAULT_RULES:
                conn.execute(
                    """
                    INSERT INTO alarm_rule (tenant_id, name, kind, datapoint_id, second_datapoint_id, plant_type,
                                            above, threshold, delay_s, hysteresis, window_s, urgency)
                    SELECT %s, %s, %s, (SELECT id FROM datapoint WHERE key = %s),
                           (SELECT id FROM datapoint WHERE key = %s), %s, %s, %s, %s, %s, %s, %s
                    ON CONFLICT (tenant_id, name, plant_type) DO NOTHING
                    """,
                    (
                        tenant_id,
                        rule.name,
                        rule.kind.value,
                        rule.datapoint,
                        rule.second_datapoint,
                        rule.plant_type,
                        rule.above,
                        rule.limit,
                        int(rule.delay.total_seconds()),
                        rule.hysteresis,
                        int(rule.window.total_seconds()) if rule.window else None,
                        rule.urgency.value,
                    ),
                )
    return users


def main() -> None:
    parser = argparse.ArgumentParser(description="Create demo customers, users, gateways and plants")
    parser.add_argument("--plants", type=int, default=9)
    parser.add_argument("--password", help="password for all demo users (default: a random one, printed once)")
    args = parser.parse_args()
    password = args.password or secrets.token_urlsafe(12)

    with psycopg.connect(Settings().database_url, autocommit=True) as conn:
        users = seed(conn, args.plants, password)
    print(f"Seeded {len(TENANTS)} customers and {args.plants} plants. Demo users (password: {password}):")
    for email, role in users:
        print(f"  {role:9} {email}")
