-- Typed payments, one row per payment_id, with payments for unknown orders
-- flagged (`has_known_order = false`) instead of dropped.
with source as (
    select * from {{ source('raw', 'payments') }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by payment_id
            order by _business_date desc, _record_number desc
        ) as _version
    from source
),

payments as (
    select
        payment_id,
        order_id,
        lower(method) as method,
        amount::numeric(12, 2) as amount,
        upper(currency) as currency,
        lower(status) as status,
        paid_at::timestamptz as paid_at,
        (paid_at::timestamptz at time zone 'UTC')::date as paid_date,
        _business_date as business_date,
        _file_sha256 as source_file_sha256
    from ranked
    where _version = 1
)

select
    payments.*,
    orders.order_id is not null as has_known_order
from payments
left join {{ ref('stg_orders') }} as orders
    on orders.order_id = payments.order_id
