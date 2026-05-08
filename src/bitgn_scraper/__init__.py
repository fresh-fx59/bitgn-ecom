"""BitGN PROD harness scraper.

Standalone tooling that walks BitGN's PROD playground API to populate
a local SQLite store with task workspaces, instructions, and grader
rules. Designed as a drop-in data source for the local gRPC harness
clone (built in a separate plan).
"""

__version__ = "0.1.0"
