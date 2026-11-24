{% test non_negative(model, column_name) %}
-- Rows where the column is below zero.
select {{ column_name }}
from {{ model }}
where {{ column_name }} < 0
{% endtest %}
