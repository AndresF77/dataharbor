-- A day cannot have more paid orders than completed ones.
select revenue_date, currency, completed_orders, paid_orders
from {{ ref('daily_revenue') }}
where paid_orders > completed_orders
