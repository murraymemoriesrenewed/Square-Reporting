-- Reporting views for Metabase. Recreated on every sync run, so edit freely.
-- Amounts are in dollars. Dates are converted to the business time zone below.
-- (Change 'America/Chicago' everywhere if you move.)

-- cents -> dollars, rounded to 2 places
CREATE OR REPLACE FUNCTION usd(cents numeric) RETURNS numeric(14,2)
    LANGUAGE sql IMMUTABLE AS $$ SELECT round(cents / 100.0, 2) $$;

DROP VIEW IF EXISTS v_payout_details, v_payout_summary, v_invoices,
    v_outstanding_invoices, v_ar_aging, v_monthly_sales, v_sales_by_item,
    v_invoice_line_items, v_customer_balances CASCADE;

------------------------------------------------------------------------------
-- All invoices with amounts and the customer name resolved
------------------------------------------------------------------------------
CREATE VIEW v_invoices AS
SELECT
    i.id                                            AS invoice_id,
    i.invoice_number,
    i.title,
    i.status,
    COALESCE(NULLIF(i.recipient_company, ''), NULLIF(c.company_name, ''),
             NULLIF(i.recipient_name, ''),
             NULLIF(TRIM(CONCAT(c.given_name, ' ', c.family_name)), ''),
             '(no customer)')                        AS customer,
    COALESCE(i.recipient_email, c.email)            AS customer_email,
    i.customer_id,
    (i.created_at AT TIME ZONE 'America/Chicago')::date AS created_date,
    i.sale_or_service_date,
    i.next_due_date,
    usd(i.amount_cents)                          AS invoice_amount,
    usd(i.paid_cents)                            AS amount_paid,
    usd(i.amount_cents - i.paid_cents)         AS balance_due,
    i.order_id,
    i.public_url
FROM invoices i
LEFT JOIN customers c ON c.id = i.customer_id;

------------------------------------------------------------------------------
-- Outstanding invoices (sent, not fully paid) with aging
------------------------------------------------------------------------------
CREATE VIEW v_outstanding_invoices AS
SELECT
    v.*,
    GREATEST(CURRENT_DATE - v.next_due_date, 0)     AS days_overdue,
    CASE
        WHEN v.next_due_date IS NULL OR v.next_due_date >= CURRENT_DATE THEN '0 Current'
        WHEN CURRENT_DATE - v.next_due_date <= 30 THEN '1 1-30 days'
        WHEN CURRENT_DATE - v.next_due_date <= 60 THEN '2 31-60 days'
        WHEN CURRENT_DATE - v.next_due_date <= 90 THEN '3 61-90 days'
        ELSE '4 90+ days'
    END                                             AS aging_bucket
FROM v_invoices v
WHERE v.status IN ('UNPAID', 'PARTIALLY_PAID')
  AND v.balance_due > 0;

CREATE VIEW v_ar_aging AS
SELECT aging_bucket,
       COUNT(*)         AS invoices,
       SUM(balance_due) AS balance_due
FROM v_outstanding_invoices
GROUP BY aging_bucket
ORDER BY aging_bucket;

CREATE VIEW v_customer_balances AS
SELECT customer,
       customer_email,
       COUNT(*)            AS open_invoices,
       SUM(balance_due)    AS balance_due,
       MIN(next_due_date)  AS oldest_due_date,
       MAX(days_overdue)   AS max_days_overdue
FROM v_outstanding_invoices
GROUP BY customer, customer_email
ORDER BY balance_due DESC;

------------------------------------------------------------------------------
-- Transfers (payouts): one row per entry, linked to payment and invoice
------------------------------------------------------------------------------
CREATE VIEW v_payout_details AS
SELECT
    po.id                                           AS payout_id,
    po.arrival_date                                 AS transfer_date,
    po.status                                       AS transfer_status,
    usd(po.amount_cents)                         AS transfer_amount,
    pe.id                                           AS entry_id,
    pe.type                                         AS entry_type,
    (pe.effective_at AT TIME ZONE 'America/Chicago')::date AS effective_date,
    usd(pe.gross_cents)                          AS gross,
    usd(pe.fee_cents)                            AS fee,
    usd(pe.net_cents)                            AS net,
    pe.payment_id,
    (p.created_at AT TIME ZONE 'America/Chicago')::date AS payment_date,
    p.source_type                                   AS payment_method,
    p.card_brand,
    p.last_4,
    i.invoice_id,
    i.invoice_number,
    i.title                                         AS invoice_title,
    i.status                                        AS invoice_status,
    i.invoice_amount,
    COALESCE(i.customer,
             NULLIF(c.company_name, ''),
             NULLIF(TRIM(CONCAT(c.given_name, ' ', c.family_name)), ''))
                                                    AS customer
FROM payout_entries pe
JOIN payouts po        ON po.id = pe.payout_id
LEFT JOIN payments p   ON p.id = pe.payment_id
LEFT JOIN v_invoices i ON i.order_id = p.order_id
LEFT JOIN customers c  ON c.id = p.customer_id;

CREATE VIEW v_payout_summary AS
SELECT
    payout_id,
    transfer_date,
    transfer_status,
    transfer_amount,
    COUNT(*)                                        AS entries,
    COUNT(DISTINCT invoice_id)                      AS invoices_paid,
    SUM(gross)                                      AS gross,
    SUM(fee)                                        AS fees,
    SUM(net)                                        AS net,
    SUM(net) FILTER (WHERE invoice_id IS NOT NULL)  AS net_from_invoices,
    SUM(net) FILTER (WHERE invoice_id IS NULL)      AS net_other
FROM v_payout_details
GROUP BY payout_id, transfer_date, transfer_status, transfer_amount
ORDER BY transfer_date DESC;

------------------------------------------------------------------------------
-- Miscellaneous
------------------------------------------------------------------------------
CREATE VIEW v_monthly_sales AS
SELECT
    date_trunc('month', p.created_at AT TIME ZONE 'America/Chicago')::date AS month,
    COUNT(*)                                        AS payments,
    usd(SUM(p.amount_cents))                     AS sales,
    usd(SUM(COALESCE(p.tip_cents, 0)))           AS tips,
    usd(SUM(COALESCE(p.refunded_cents, 0)))      AS refunds,
    usd(SUM(COALESCE(p.processing_fee_cents, 0))) AS processing_fees,
    usd(SUM(p.total_cents - COALESCE(p.refunded_cents, 0)
        - COALESCE(p.processing_fee_cents, 0))) AS net,
    COUNT(*) FILTER (WHERE i.id IS NOT NULL)        AS invoice_payments
FROM payments p
LEFT JOIN invoices i ON i.order_id = p.order_id
WHERE p.status = 'COMPLETED'
GROUP BY 1
ORDER BY 1 DESC;

CREATE VIEW v_sales_by_item AS
SELECT
    date_trunc('month', o.closed_at AT TIME ZONE 'America/Chicago')::date AS month,
    li.name                                         AS item,
    li.variation_name                               AS variation,
    SUM(li.quantity)                                AS quantity,
    usd(SUM(li.gross_sales_cents))               AS gross_sales,
    usd(SUM(li.discount_cents))                  AS discounts,
    usd(SUM(li.total_cents))                     AS total
FROM order_line_items li
JOIN orders o ON o.id = li.order_id
WHERE o.state = 'COMPLETED'
GROUP BY 1, 2, 3;

CREATE VIEW v_invoice_line_items AS
SELECT
    i.invoice_number,
    i.customer,
    i.status,
    i.created_date,
    li.name                                         AS item,
    li.variation_name                               AS variation,
    li.quantity,
    usd(li.base_price_cents)                     AS unit_price,
    usd(li.discount_cents)                       AS discount,
    usd(li.tax_cents)                            AS tax,
    usd(li.total_cents)                          AS line_total
FROM v_invoices i
JOIN order_line_items li ON li.order_id = i.order_id;
