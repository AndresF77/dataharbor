{% test unique_combination(model, columns) %}
-- Combinations of `columns` that occur more than once.
select {{ columns | join(', ') }}, count(*) as occurrences
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
