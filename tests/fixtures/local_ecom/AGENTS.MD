# BitGN ECOM Local Fixture

This is a minimal ECOM-shaped workspace used by tests/test_local_ecom_client.py.

## Available tools

- `read`, `write`, `delete`, `list`, `tree`, `find`, `search`, `stat`, `context`
- `exec /bin/sql <<<'SELECT ...'` — runs SQL against the catalogue at
  `catalogue.db`. Tables: `orders`, `customers`.

## Catalogue schema

- `orders(id INTEGER, customer_id INTEGER, total REAL, status TEXT)`
- `customers(id INTEGER, name TEXT, country TEXT)`

## Conventions

- Money is stored in the smallest unit (cents).
- Dates are ISO-8601 UTC strings.
