{% test currency_code(model, column_name) %}
-- Rows whose currency is not a three-letter upper-case ISO 4217 code.
select {{ column_name }}
from {{ model }}
where {{ column_name }} is not null
  and {{ column_name }} !~ '^[A-Z]{3}$'
{% endtest %}
