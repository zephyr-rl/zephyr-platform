"""Running the platform without Docker (Windows Server until the Linux VPS exists).

Layout of the install folder (``--root``, for example ``C:\\ZephyrPlatform``)::

    app\\            backend and built panel (replaced by every update)
    venv\\           Python environment (uv)
    python\\  cache\\  tools\\   uv's Python, cache, and uv.exe itself
    caddy\\          caddy.exe and the generated Caddyfile (HTTPS)
    backups\\        nightly database dumps
    logs\\           one log file per process
    run\\            process ids, and the "stop" file used by updates
    legacy\\         read-only snapshots of the old stores (for imports and comparisons)
    .env            the keys and the database password: keep a copy outside the server
"""
