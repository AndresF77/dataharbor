-- An order total must equal quantity × unit price (both already rounded to cents).
select order_id, quantity, unit_price, amount
from {{ ref('stg_orders') }}
where amount <> quantity * unit_price
