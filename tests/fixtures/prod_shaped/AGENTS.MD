readme.md files take role of agents.md. Trust them, follow the chain of command.

This is Agentic E-Commerce OS system. All paths are / rooted (pass as such to tools and OS ops).

Key locations:
- Catalogue is in proc/catalog, stores are in proc/stores
- Tools are in `/bin`. especially see sql tool that takes SQLite query as stdin. It operates with the state of the entire catalogue at the start of the session. Run `/bin/sql .schema` to see the schema.
- /docs has the lane rulebook excerpts.

If you change catalogue state, do it through tooling, not by editing files.
