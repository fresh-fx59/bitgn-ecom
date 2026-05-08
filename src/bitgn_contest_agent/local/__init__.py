"""Local harness — filesystem-backed mocks for offline replay and
unit tests. The PAC1 lineage shipped a similar mock for vault
snapshots; the ECOM port mirrors the public ECOM RPC surface
(read/write/delete/list/tree/find/search/stat/exec/context/answer)
against an on-disk workspace plus an optional SQLite catalogue.
"""
