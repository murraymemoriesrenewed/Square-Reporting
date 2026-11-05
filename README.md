# Square → Postgres → Metabase

A Python script copies your Square data into Postgres on a schedule. You build reports on top of it in Metabase, which runs on your own PC. Everything runs in Docker Desktop on Windows.

```
square-reporting/
├── docker-compose.yml     Postgres (+ optional Metabase, + the sync on demand)
├── .env.example           settings template → copy to .env
├── run_sync.bat           what Task Scheduler runs
├── db/init/               creates Metabase's own settings database on first start
└── sync/
    ├── square_sync.py     the sync script
    ├── schema.sql         tables (money in cents, full API object in `raw`)
    └── views.sql          report views (dollars, Chicago time); rebuilt every run
```

## 1. Get a Square access token

1. Go to https://developer.squareup.com and sign in with your Square account.
2. Create an app, for example "Reporting".
3. Switch the app's **Credentials** page to **Production** and copy the **Access token**.

This personal token has full access to your account, so keep it only in `.env`.

## 2. Configure

1. Install Docker Desktop.
2. Copy `.env.example` to `.env`.
3. Fill in the token and choose passwords. Use letters and numbers only.
4. Set `BACKFILL_START` to the date your history should start from.

## 3. Start Postgres and run the first sync

From this folder, in PowerShell:

```powershell
docker compose up -d                 # starts Postgres only
docker compose run --rm sync         # first sync (backfill), builds tables + views
```

To re-pull everything later, run `docker compose run --rm sync --full`.

If Postgres won't start because port 5432 is already in use (for example by another Postgres), set `PG_HOST_PORT=5433` in `.env` and run `docker compose up -d` again.

## 4. Connect Metabase

### Option A: You already run Metabase in Docker (recommended)

Postgres joins a Docker network named `square-reporting`, and your Metabase container needs to join it too. Find your Metabase container's name with `docker ps`, then run:

```powershell
docker network connect square-reporting <your-metabase-container>
```

This survives restarts. If you recreate the container (for example, when upgrading Metabase), you'll need to reconnect it. If your Metabase is started with `docker run`, add `--network square-reporting` to that command. If it's started with its own docker-compose file, add this:

```yaml
services:
  metabase:            # your existing service
    networks: [default, square-reporting]
networks:
  square-reporting:
    external: true
```

Then, in Metabase, go to **Admin settings → Databases → Add database**:

| Field | Value |
|---|---|
| Database type | PostgreSQL |
| Host | `square-db` |
| Port | `5432` (always 5432 here, even if you changed `PG_HOST_PORT`) |
| Database name | `square` |
| Username / Password | `METABASE_READER_USER` / `METABASE_READER_PASSWORD` from `.env` (a read-only login) |

**Fallback, with no network changes:** use Host `host.docker.internal` and Port `PG_HOST_PORT` (5432 unless you changed it). Docker Desktop routes this to your PC. It's simpler, but it depends on the published port.

### Option B: Start a new Metabase from this project

Run `docker compose --profile metabase up -d`, open http://localhost:3000, then add the database as above. Use Host `db` or `square-db`.

### Then

Browse **Our analytics → square**. The `v_…` views are the starting reports. When you add new views later, go to **Admin → Databases → square** and choose **Sync database schema now**, so Metabase picks them up right away.

## 5. Schedule it

In Task Scheduler, choose **Create Basic Task**:

- Trigger: Daily, or every few hours.
- Action: Start a program → `run_sync.bat`.
- Start in: this folder.

Output goes to `sync.log`. Each run re-reads the last `LOOKBACK_DAYS` (30 by default), which catches refunds and transfers whose status changed.

## Reports included (views)

| View | What it answers |
|---|---|
| `v_payout_summary` | One row per bank transfer: total, gross, fees, net, and how much came from invoices vs. other sales |
| `v_payout_details` | Every line in each transfer, with payment date, card, **invoice #, and customer** |
| `v_outstanding_invoices` | Unpaid and partially paid invoices: balance due, next due date, days overdue, aging bucket |
| `v_ar_aging` | Outstanding totals by bucket (Current / 1-30 / 31-60 / 61-90 / 90+) |
| `v_customer_balances` | Who owes what, largest balance first |
| `v_invoices` | All invoices with amounts and resolved customer name |
| `v_invoice_line_items` | What was on each invoice |
| `v_monthly_sales` | Sales, tips, refunds, fees, and net by month |
| `v_sales_by_item` | Items sold by month |

A good check: in `v_payout_summary`, `net` should equal `transfer_amount`. If they don't match, some of that transfer's entries are missing.

## How invoices link to transfers

Each Square transfer (payout) is made up of entries. A `CHARGE` entry carries a `payment_id`. That payment has an `order_id`, and an invoice shares the same `order_id`. The views follow that chain.

Entries without a payment, such as adjustments, reserves and some fees, show up with a blank invoice column.

## Developing your own reports

- **Simple reports:** Metabase's question builder works directly on the views.
- **SQL reports:** Write SQL in Metabase. Any Square field the script doesn't break out is still in the `raw` jsonb column, for example `raw->>'description'` or `raw->'accepted_payment_methods'`.
- **New views:** Add them to `sync/views.sql` and rebuild with `docker compose build sync`. They get created on the next run.
- **Running the script without Docker:** Run `pip install -r sync/requirements.txt`, then `python sync/square_sync.py`. It reads `DATABASE_URL` (localhost) from `.env`.
- **Testing:** To test against fake data, set `SQUARE_ENVIRONMENT=sandbox` and use a sandbox token.

## Notes

- Postgres and Metabase are bound to `127.0.0.1`, so only this PC can reach them.
- Back up the `pgdata` Docker volume if you'd like to keep your Metabase dashboards. The Square data itself can always be re-synced.
- Square API version is set in `.env` (`SQUARE_VERSION`).
