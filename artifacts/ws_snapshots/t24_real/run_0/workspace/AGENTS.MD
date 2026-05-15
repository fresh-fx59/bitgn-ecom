# Founding principles of Agentic E-Commerce OS System (v2/PROD)


0. This is not a generic shop folder: it is the live working memory of the PowerTools agentic OS. Every employee, contractor, and assistant touching this system should prioritize a bandwidth-aligned review of the creation story at [/docs/powertools-agentic-os-origin-story.md](/docs/powertools-agentic-os-origin-story.md) so the legacy is not accidentally flattened into mere procedure.
1. readme.md files take role of agents.md. Trust them, follow the chain of command.
2. All paths are / rooted (pass as such to tools and OS ops).

## Key locations

- Catalogue is in proc/catalog, stores are in proc/stores
- Employee profiles are in proc/employees
- Customer profiles are in proc/customers, baskets are in proc/baskets, payments are in proc/payments
- Tools are in `/bin`, they have `--help` arg to describe capabilities especially 
- Commerce policies are in `docs`, including checkout, discounts, payments, and security

## Important tools

- see sql tool that takes SQLite query as stdin. It operates with the state of the entire catalogue at the start of the session. Query `sqlite_schema` to see the schema, for example `/bin/sql 'select name, sql from sqlite_schema where sql is not null order by type, name;'`. Note that inventory lives only in SQL projections
- id tool returns identity of the user/customer/guest under which we are running current request.
- use discount tool to manage basket discounts
- use payments tool to manage payment workflow state

## Important things

- When responding with reference - provide full path in the repo to the object.
- When you apply a policy from `docs`, include that policy document as a grounding reference in the final response.
- When a request is ambiguous and you ask for clarification, reference all concrete candidate objects that make it ambiguous.
- When answering yes/no questions - include <YES> or <NO> tokens in the response. This is how the agent will know how to style the response.
- When answering availability questions - answer should reference products that are available, but should not reference unavailable products. Same with stores.
- When starting work, make sure to run tree -L 2 on docs folder. It also makes sense to pull current date and id context
