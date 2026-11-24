-- Typed orders, one row per order_id.
-- A re-delivered order keeps the row from its latest business date.
with source as (
    select * from {{ source('raw', 'orders') }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by order_id
            order by _business_date desc, _record_number desc
        ) as _version
    from source
)

select
    order_id,
    customer_id,
    merchant,
    product,
    quantity::integer as quantity,
    unit_price::numeric(12, 2) as unit_price,
    amount::numeric(12, 2) as amount,
    upper(currency) as currency,
    lower(status) as status,
    ordered_at::timestamptz as ordered_at,
    (ordered_at::timestamptz at time zone 'UTC')::date as order_date,
    _business_date as business_date,
    _file_sha256 as source_file_sha256
from ranked
where _version = 1
