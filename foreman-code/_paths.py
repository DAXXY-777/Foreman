"""Lets this folder import graph_api and schema from the foreman-schema folder next to it."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "foreman-schema"), os.path.join(HERE, "schema_pkg")):
    cand = os.path.abspath(cand)
    if os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# Database settings (FALKORDB_HOST, FALKORDB_PORT, ...) may live in .env. They must be read before `schema` is
# imported, because schema reads them once at import time.
for _p in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", ".env")):
    if os.path.exists(_p):
        for _line in open(_p):
            _line = _line.strip()
            if _line.startswith("FALKORDB_") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ[_k.strip()] = _v.strip().strip('"').strip("'")
