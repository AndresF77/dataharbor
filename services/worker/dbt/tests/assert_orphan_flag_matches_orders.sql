-- `has_known_order` must agree with stg_orders in both directions: a flagged
-- orphan whose order exists, or a "known" payment whose order does not.
select p.payment_id, p.has_known_order
from {{ ref('stg_payments') }} as p
left join {{ ref('stg_orders') }} as o
    on o.order_id = p.order_id
where p.has_known_order <> (o.order_id is not null)
