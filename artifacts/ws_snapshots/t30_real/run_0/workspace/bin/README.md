# Tools

Runtime tools live here. Run any tool with `--help` to read its usage.

`/bin/discount` applies the requested basket discount mechanically. It does not enforce `/docs/discounts.md`.

`/bin/payments` applies payment workflow actions mechanically. It does not enforce `/docs/payments/3ds.md`.

Useful entry query:

```sql
select sku, name, path from products limit 5;
```

Schema discovery:

```sql
select name, sql from sqlite_schema where sql is not null order by type, name;
```
