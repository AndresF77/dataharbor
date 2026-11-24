-- The mart must account for every completed order and every payment exactly
-- once: totals per currency in daily_revenue equal the staging totals.
with expected as (
    select
        currency,
        sum(amount) filter (where status = 'completed') as order_amount,
        count(*) filter (where status = 'completed') as completed_orders,
        0::numeric as orphan_amount,
        0 as orphan_payments
    from {{ ref('stg_orders') }}
    group by currency

    union all

    select
        currency,
        0,
        0,
        sum(amount) filter (where not has_known_order),
        count(*) filter (where not has_known_order)
    from {{ ref('stg_payments') }}
    group by currency
),

expected_totals as (
    select
        currency,
        coalesce(sum(order_amount), 0) as order_amount,
        sum(completed_orders) as completed_orders,
        coalesce(sum(orphan_amount), 0) as orphan_amount,
        sum(orphan_payments) as orphan_payments
    from expected
    group by currency
),

mart_totals as (
    select
        currency,
        sum(order_amount) as order_amount,
        sum(completed_orders) as completed_orders,
        sum(orphan_amount) as orphan_amount,
        sum(orphan_payments) as orphan_payments
    from {{ ref('daily_revenue') }}
    group by currency
)

select e.currency, e.order_amount, m.order_amount as mart_order_amount
from expected_totals as e
full outer join mart_totals as m
    on m.currency = e.currency
where (e.order_amount <> 0 or e.orphan_amount <> 0 or m.currency is not null)
  and (
      m.currency is null
      or e.currency is null
      or e.order_amount <> m.order_amount
      or e.completed_orders <> m.completed_orders
      or e.orphan_amount <> m.orphan_amount
      or e.orphan_payments <> m.orphan_payments
  )
