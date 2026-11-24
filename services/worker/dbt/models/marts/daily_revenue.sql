-- Revenue per order date and currency: completed orders, what was paid for
-- them and what is still outstanding. Only captured payments for known
-- orders count; orphan payments are reported separately by paid date.
with completed_orders as (
    select order_id, order_date, currency, amount
    from {{ ref('stg_orders') }}
    where status = 'completed'
),

captured as (
    select
        order_id,
        currency,
        sum(amount) as paid_amount
    from {{ ref('stg_payments') }}
    where status = 'captured' and has_known_order
    group by order_id, currency
),

orders_with_payments as (
    select
        o.order_date,
        o.currency,
        o.amount,
        coalesce(c.paid_amount, 0) as paid_amount,
        c.order_id is not null as is_paid
    from completed_orders as o
    left join captured as c
        on c.order_id = o.order_id and c.currency = o.currency
),

revenue as (
    select
        order_date as revenue_date,
        currency,
        count(*) as completed_orders,
        count(*) filter (where is_paid) as paid_orders,
        sum(amount) as order_amount,
        sum(paid_amount) as paid_amount
    from orders_with_payments
    group by order_date, currency
),

orphans as (
    select
        paid_date as revenue_date,
        currency,
        count(*) as orphan_payments,
        sum(amount) as orphan_amount
    from {{ ref('stg_payments') }}
    where not has_known_order
    group by paid_date, currency
)

select
    coalesce(r.revenue_date, o.revenue_date) as revenue_date,
    coalesce(r.currency, o.currency) as currency,
    coalesce(r.completed_orders, 0) as completed_orders,
    coalesce(r.paid_orders, 0) as paid_orders,
    coalesce(r.order_amount, 0)::numeric(14, 2) as order_amount,
    coalesce(r.paid_amount, 0)::numeric(14, 2) as paid_amount,
    (coalesce(r.order_amount, 0) - coalesce(r.paid_amount, 0))::numeric(14, 2)
        as outstanding_amount,
    coalesce(o.orphan_payments, 0) as orphan_payments,
    coalesce(o.orphan_amount, 0)::numeric(14, 2) as orphan_amount
from revenue as r
full outer join orphans as o
    on o.revenue_date = r.revenue_date and o.currency = r.currency
