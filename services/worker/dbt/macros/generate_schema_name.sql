{#
  Build models in the schema named in dbt_project.yml (`staging`, `mart`)
  instead of dbt's default `<target schema>_<custom schema>`, so the
  warehouse keeps the raw / staging / mart layout from ADR 0001.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
