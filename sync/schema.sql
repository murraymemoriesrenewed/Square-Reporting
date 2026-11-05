-- Tables populated by square_sync.py.
-- Money columns are stored in cents (Square's native unit); the views divide by 100.
-- Every table keeps the full API object in `raw` (jsonb) so you can report on any
-- field later without changing the script, e.g.  raw->>'description'.

CREATE TABLE IF NOT EXISTS sync_state (
    stream      text PRIMARY KEY,
    last_run_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id         text PRIMARY KEY,
    name       text,
    status     text,
    currency   text,
    timezone   text,
    raw        jsonb NOT NULL,
    synced_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customers (
    id            text PRIMARY KEY,
    given_name    text,
    family_name   text,
    company_name  text,
    email         text,
    phone         text,
    created_at    timestamptz,
    updated_at    timestamptz,
    raw           jsonb NOT NULL,
    synced_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invoices (
    id                   text PRIMARY KEY,
    location_id          text,
    invoice_number       text,
    title                text,
    status               text,
    order_id             text,
    customer_id          text,
    recipient_name       text,
    recipient_company    text,
    recipient_email      text,
    sale_or_service_date date,
    created_at           timestamptz,
    updated_at           timestamptz,
    public_url           text,
    currency             text,
    amount_cents         bigint,   -- sum of all payment requests
    paid_cents           bigint,   -- sum collected so far
    next_due_date        date,     -- earliest unpaid payment request
    raw                  jsonb NOT NULL,
    synced_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoices_order_id_idx ON invoices (order_id);

CREATE TABLE IF NOT EXISTS invoice_payment_requests (
    invoice_id      text NOT NULL,
    uid             text NOT NULL,
    request_type    text,
    due_date        date,
    computed_cents  bigint,
    completed_cents bigint,
    PRIMARY KEY (invoice_id, uid)
);

CREATE TABLE IF NOT EXISTS payments (
    id                   text PRIMARY KEY,
    location_id          text,
    order_id             text,
    customer_id          text,
    status               text,
    source_type          text,
    card_brand           text,
    last_4               text,
    receipt_number       text,
    note                 text,
    created_at           timestamptz,
    updated_at           timestamptz,
    currency             text,
    amount_cents         bigint,
    tip_cents            bigint,
    total_cents          bigint,
    refunded_cents       bigint,
    processing_fee_cents bigint,
    raw                  jsonb NOT NULL,
    synced_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_order_id_idx ON payments (order_id);
CREATE INDEX IF NOT EXISTS payments_created_at_idx ON payments (created_at);

CREATE TABLE IF NOT EXISTS orders (
    id                   text PRIMARY KEY,
    location_id          text,
    customer_id          text,
    state                text,
    created_at           timestamptz,
    updated_at           timestamptz,
    closed_at            timestamptz,
    currency             text,
    total_cents          bigint,
    tax_cents            bigint,
    discount_cents       bigint,
    tip_cents            bigint,
    service_charge_cents bigint,
    raw                  jsonb NOT NULL,
    synced_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_line_items (
    order_id             text NOT NULL,
    uid                  text NOT NULL,
    name                 text,
    variation_name       text,
    catalog_object_id    text,
    quantity             numeric,
    base_price_cents     bigint,
    gross_sales_cents    bigint,
    discount_cents       bigint,
    tax_cents            bigint,
    total_cents          bigint,
    PRIMARY KEY (order_id, uid)
);

CREATE TABLE IF NOT EXISTS payouts (
    id                text PRIMARY KEY,
    location_id       text,
    status            text,
    type              text,
    arrival_date      date,
    created_at        timestamptz,
    updated_at        timestamptz,
    currency          text,
    amount_cents      bigint,
    destination_type  text,
    destination_id    text,
    number_of_entries integer,
    end_to_end_id     text,
    raw               jsonb NOT NULL,
    synced_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payout_entries (
    id            text PRIMARY KEY,
    payout_id     text NOT NULL,
    type          text,
    effective_at  timestamptz,
    currency      text,
    gross_cents   bigint,
    fee_cents     bigint,   -- usually negative
    net_cents     bigint,
    payment_id    text,     -- pulled from whichever type_*_details object is present
    refund_id     text,
    raw           jsonb NOT NULL,
    synced_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payout_entries_payout_idx ON payout_entries (payout_id);
CREATE INDEX IF NOT EXISTS payout_entries_payment_idx ON payout_entries (payment_id);
