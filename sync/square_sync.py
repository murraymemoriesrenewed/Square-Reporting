"""
Square -> Postgres sync for Metabase reporting.

Pulls locations, customers, invoices, payments, orders, payouts (transfers) and
payout entries from the Square API and upserts them into Postgres. Safe to run
repeatedly: each run re-reads a lookback window and overwrites changed rows.

Configuration comes from environment variables (see .env.example).

Usage:
    python square_sync.py              # incremental sync
    python square_sync.py --full       # re-pull everything since BACKFILL_START
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg
import requests
from psycopg import sql
from psycopg.types.json import Jsonb

log = logging.getLogger("square_sync")

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val or ""


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so the script also runs outside Docker."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# Square API client
# --------------------------------------------------------------------------- #
class Square:
    def __init__(self, token: str, environment: str, version: str):
        host = ("https://connect.squareupsandbox.com" if environment == "sandbox"
                else "https://connect.squareup.com")
        self.base = host + "/v2"
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Square-Version": version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(6):
            r = self.s.request(method, self.base + path, timeout=60, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                log.warning("%s %s -> %s, retrying in %ss", method, path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} failed ({r.status_code}): {r.text}")
            return r.json()
        raise RuntimeError(f"{method} {path} kept failing after retries")

    def paginate(self, path: str, key: str, params: dict | None = None) -> Iterator[dict]:
        """GET list endpoints that use a `cursor` for paging."""
        params = dict(params or {})
        while True:
            data = self.request("GET", path, params=params)
            yield from data.get(key, [])
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def batch_retrieve_orders(self, order_ids: list[str]) -> Iterator[dict]:
        for i in range(0, len(order_ids), 100):
            chunk = order_ids[i:i + 100]
            data = self.request("POST", "/orders/batch-retrieve", json={"order_ids": chunk})
            yield from data.get("orders", [])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def cents(obj: dict | None, key: str) -> int | None:
    money = (obj or {}).get(key) or {}
    return money.get("amount")


def currency(obj: dict | None, key: str) -> str | None:
    return ((obj or {}).get(key) or {}).get("currency")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert(cur: psycopg.Cursor, table: str, rows: list[dict], pk: Iterable[str]) -> int:
    if not rows:
        return 0
    pk = list(pk)
    cols = list(rows[0].keys())
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
    if "synced_at" not in cols and table not in ("invoice_payment_requests", "order_line_items"):
        updates += ", synced_at = now()"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
    )
    cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


# --------------------------------------------------------------------------- #
# Row mappers (API object -> table row)
# --------------------------------------------------------------------------- #
def map_location(o: dict) -> dict:
    return {
        "id": o["id"], "name": o.get("name"), "status": o.get("status"),
        "currency": o.get("currency"), "timezone": o.get("timezone"), "raw": Jsonb(o),
    }


def map_customer(o: dict) -> dict:
    return {
        "id": o["id"], "given_name": o.get("given_name"), "family_name": o.get("family_name"),
        "company_name": o.get("company_name"), "email": o.get("email_address"),
        "phone": o.get("phone_number"), "created_at": o.get("created_at"),
        "updated_at": o.get("updated_at"), "raw": Jsonb(o),
    }


def map_invoice(o: dict) -> tuple[dict, list[dict]]:
    rcpt = o.get("primary_recipient") or {}
    reqs = o.get("payment_requests") or []
    amount = sum(cents(r, "computed_amount_money") or 0 for r in reqs)
    paid = sum(cents(r, "total_completed_amount_money") or 0 for r in reqs)
    unpaid_due = sorted(
        r["due_date"] for r in reqs
        if r.get("due_date")
        and (cents(r, "total_completed_amount_money") or 0) < (cents(r, "computed_amount_money") or 0)
    )
    cur = next((currency(r, "computed_amount_money") for r in reqs
                if currency(r, "computed_amount_money")), None)
    name = " ".join(p for p in (rcpt.get("given_name"), rcpt.get("family_name")) if p) or None
    row = {
        "id": o["id"], "location_id": o.get("location_id"),
        "invoice_number": o.get("invoice_number"), "title": o.get("title"),
        "status": o.get("status"), "order_id": o.get("order_id"),
        "customer_id": rcpt.get("customer_id"), "recipient_name": name,
        "recipient_company": rcpt.get("company_name"),
        "recipient_email": rcpt.get("email_address"),
        "sale_or_service_date": o.get("sale_or_service_date"),
        "created_at": o.get("created_at"), "updated_at": o.get("updated_at"),
        "public_url": o.get("public_url"), "currency": cur,
        "amount_cents": amount, "paid_cents": paid,
        "next_due_date": unpaid_due[0] if unpaid_due else None,
        "raw": Jsonb(o),
    }
    req_rows = [{
        "invoice_id": o["id"], "uid": r.get("uid") or f"idx{n}",
        "request_type": r.get("request_type"), "due_date": r.get("due_date"),
        "computed_cents": cents(r, "computed_amount_money"),
        "completed_cents": cents(r, "total_completed_amount_money"),
    } for n, r in enumerate(reqs)]
    return row, req_rows


def map_payment(o: dict) -> dict:
    card = ((o.get("card_details") or {}).get("card")) or {}
    fees = sum((f.get("amount_money") or {}).get("amount", 0) for f in o.get("processing_fee") or [])
    return {
        "id": o["id"], "location_id": o.get("location_id"), "order_id": o.get("order_id"),
        "customer_id": o.get("customer_id"), "status": o.get("status"),
        "source_type": o.get("source_type"), "card_brand": card.get("card_brand"),
        "last_4": card.get("last_4"), "receipt_number": o.get("receipt_number"),
        "note": o.get("note"), "created_at": o.get("created_at"),
        "updated_at": o.get("updated_at"), "currency": currency(o, "amount_money"),
        "amount_cents": cents(o, "amount_money"), "tip_cents": cents(o, "tip_money"),
        "total_cents": cents(o, "total_money"), "refunded_cents": cents(o, "refunded_money"),
        "processing_fee_cents": fees, "raw": Jsonb(o),
    }


def map_order(o: dict) -> tuple[dict, list[dict]]:
    row = {
        "id": o["id"], "location_id": o.get("location_id"), "customer_id": o.get("customer_id"),
        "state": o.get("state"), "created_at": o.get("created_at"),
        "updated_at": o.get("updated_at"), "closed_at": o.get("closed_at"),
        "currency": currency(o, "total_money"), "total_cents": cents(o, "total_money"),
        "tax_cents": cents(o, "total_tax_money"), "discount_cents": cents(o, "total_discount_money"),
        "tip_cents": cents(o, "total_tip_money"),
        "service_charge_cents": cents(o, "total_service_charge_money"), "raw": Jsonb(o),
    }
    lines = [{
        "order_id": o["id"], "uid": li.get("uid") or f"idx{n}", "name": li.get("name"),
        "variation_name": li.get("variation_name"),
        "catalog_object_id": li.get("catalog_object_id"),
        "quantity": li.get("quantity"),
        "base_price_cents": cents(li, "base_price_money"),
        "gross_sales_cents": cents(li, "gross_sales_money"),
        "discount_cents": cents(li, "total_discount_money"),
        "tax_cents": cents(li, "total_tax_money"),
        "total_cents": cents(li, "total_money"),
    } for n, li in enumerate(o.get("line_items") or [])]
    return row, lines


def map_payout(o: dict) -> dict:
    dest = o.get("destination") or {}
    return {
        "id": o["id"], "location_id": o.get("location_id"), "status": o.get("status"),
        "type": o.get("type"), "arrival_date": o.get("arrival_date"),
        "created_at": o.get("created_at"), "updated_at": o.get("updated_at"),
        "currency": currency(o, "amount_money"), "amount_cents": cents(o, "amount_money"),
        "destination_type": dest.get("type"), "destination_id": dest.get("id"),
        "number_of_entries": o.get("number_of_entries"),
        "end_to_end_id": o.get("end_to_end_id"), "raw": Jsonb(o),
    }


def map_payout_entry(o: dict) -> dict:
    # Each entry type has its own details object, e.g. type_charge_details,
    # type_refund_details, type_fee_details. Grab IDs from whichever is present.
    details: dict[str, Any] = {}
    for k, v in o.items():
        if k.startswith("type_") and k.endswith("_details") and isinstance(v, dict):
            details.update(v)
    return {
        "id": o["id"], "payout_id": o.get("payout_id"), "type": o.get("type"),
        "effective_at": o.get("effective_at"), "currency": currency(o, "gross_amount_money"),
        "gross_cents": cents(o, "gross_amount_money"), "fee_cents": cents(o, "fee_amount_money"),
        "net_cents": cents(o, "net_amount_money"), "payment_id": details.get("payment_id"),
        "refund_id": details.get("refund_id"), "raw": Jsonb(o),
    }


# --------------------------------------------------------------------------- #
# Sync steps
# --------------------------------------------------------------------------- #
def get_since(cur, stream: str, lookback_days: int, backfill: datetime, full: bool) -> datetime:
    if full:
        return backfill
    cur.execute("SELECT last_run_at FROM sync_state WHERE stream = %s", (stream,))
    row = cur.fetchone()
    if not row:
        return backfill
    return max(backfill, row[0] - timedelta(days=lookback_days))


def mark_done(cur, stream: str, started: datetime) -> None:
    cur.execute(
        "INSERT INTO sync_state (stream, last_run_at) VALUES (%s, %s) "
        "ON CONFLICT (stream) DO UPDATE SET last_run_at = EXCLUDED.last_run_at",
        (stream, started),
    )


def sync(conn: psycopg.Connection, sq: Square, full: bool) -> None:
    started = datetime.now(timezone.utc)
    backfill = datetime.fromisoformat(env("BACKFILL_START", "2020-01-01")).replace(tzinfo=timezone.utc)
    lookback = int(env("LOOKBACK_DAYS", "30"))
    order_ids: set[str] = set()

    with conn.cursor() as cur:
        # Locations
        locs = sq.request("GET", "/locations").get("locations", [])
        upsert(cur, "locations", [map_location(l) for l in locs], ["id"])
        loc_ids = [l["id"] for l in locs]
        log.info("locations: %d", len(locs))

        # Customers (full refresh; cheap for small businesses)
        n = 0
        batch: list[dict] = []
        for c in sq.paginate("/customers", "customers", {"limit": 100}):
            batch.append(map_customer(c))
            if len(batch) >= 500:
                n += upsert(cur, "customers", batch, ["id"]); batch = []
        n += upsert(cur, "customers", batch, ["id"])
        log.info("customers: %d", n)
        conn.commit()

        # Invoices (full refresh: statuses change as invoices get paid)
        n = 0
        for loc in loc_ids:
            inv_rows, req_rows = [], []
            for inv in sq.paginate("/invoices", "invoices", {"location_id": loc, "limit": 200}):
                row, reqs = map_invoice(inv)
                inv_rows.append(row); req_rows.extend(reqs)
                if row["order_id"]:
                    order_ids.add(row["order_id"])
            n += upsert(cur, "invoices", inv_rows, ["id"])
            if inv_rows:
                cur.execute("DELETE FROM invoice_payment_requests WHERE invoice_id = ANY(%s)",
                            ([r["id"] for r in inv_rows],))
            upsert(cur, "invoice_payment_requests", req_rows, ["invoice_id", "uid"])
        log.info("invoices: %d", n)
        conn.commit()

        # Payments (incremental by created_at with lookback for refunds/updates)
        since = get_since(cur, "payments", lookback, backfill, full)
        n = 0
        batch = []
        for p in sq.paginate("/payments", "payments",
                             {"begin_time": iso(since), "end_time": iso(started), "limit": 100}):
            batch.append(map_payment(p))
            if p.get("order_id"):
                order_ids.add(p["order_id"])
            if len(batch) >= 500:
                n += upsert(cur, "payments", batch, ["id"]); batch = []
        n += upsert(cur, "payments", batch, ["id"])
        mark_done(cur, "payments", started)
        log.info("payments since %s: %d", since.date(), n)
        conn.commit()

        # Orders referenced by invoices and payments (gives line-item detail)
        if not full:
            # Skip orders already stored that are closed and older than the lookback.
            cur.execute(
                "SELECT id FROM orders WHERE id = ANY(%s) AND state IN ('COMPLETED','CANCELED') "
                "AND updated_at < %s",
                (list(order_ids), started - timedelta(days=lookback)),
            )
            order_ids -= {r[0] for r in cur.fetchall()}
        n = 0
        ids = sorted(order_ids)
        for i in range(0, len(ids), 500):
            o_rows, l_rows = [], []
            for o in sq.batch_retrieve_orders(ids[i:i + 500]):
                row, lines = map_order(o)
                o_rows.append(row); l_rows.extend(lines)
            n += upsert(cur, "orders", o_rows, ["id"])
            if o_rows:
                cur.execute("DELETE FROM order_line_items WHERE order_id = ANY(%s)",
                            ([r["id"] for r in o_rows],))
            upsert(cur, "order_line_items", l_rows, ["order_id", "uid"])
            conn.commit()
        log.info("orders: %d", n)

        # Payouts (transfers) and their entries
        since = get_since(cur, "payouts", lookback, backfill, full)
        n_p = n_e = 0
        for loc in loc_ids:
            for po in sq.paginate("/payouts", "payouts",
                                  {"location_id": loc, "begin_time": iso(since),
                                   "end_time": iso(started), "limit": 100}):
                upsert(cur, "payouts", [map_payout(po)], ["id"])
                entries = [map_payout_entry(e) for e in
                           sq.paginate(f"/payouts/{po['id']}/payout-entries",
                                       "payout_entries", {"limit": 100})]
                n_e += upsert(cur, "payout_entries", entries, ["id"])
                n_p += 1
                conn.commit()
        mark_done(cur, "payouts", started)
        log.info("payouts since %s: %d (%d entries)", since.date(), n_p, n_e)
        conn.commit()


def setup_db(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute((HERE / "schema.sql").read_text())
        cur.execute((HERE / "views.sql").read_text())

        # Read-only login for Metabase (created once; password from .env)
        ro_user = env("METABASE_READER_USER", "metabase_reader")
        ro_pass = env("METABASE_READER_PASSWORD")
        if ro_pass:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ro_user,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(ro_user), sql.Literal(ro_pass)))
            cur.execute(sql.SQL(
                "GRANT USAGE ON SCHEMA public TO {u}; "
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {u};"
            ).format(u=sql.Identifier(ro_user)))
    conn.commit()


def main() -> None:
    load_dotenv(HERE.parent / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--full", action="store_true", help="re-pull everything since BACKFILL_START")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sq = Square(
        token=env("SQUARE_ACCESS_TOKEN", required=True),
        environment=env("SQUARE_ENVIRONMENT", "production"),
        version=env("SQUARE_VERSION", "2026-08-19"),
    )
    with psycopg.connect(env("DATABASE_URL", required=True)) as conn:
        setup_db(conn)
        sync(conn, sq, args.full)
    log.info("done")


if __name__ == "__main__":
    main()
