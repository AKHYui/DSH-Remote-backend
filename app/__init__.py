"""DSH Remote Bridge — FastAPI relay for the DSH desktop plugin.

Package layout:
    app.protocol   wire contracts (shared with plugin/src/protocol.js)
    app.store      SQLite persistence (connectors, devices, pair codes, audit)
    app.auth       token hashing and rate limiting
    app.relay      DeviceLink / RelayHub: request correlation + event fan-out
    app.main       FastAPI application
    app.cli        administrator CLI
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
