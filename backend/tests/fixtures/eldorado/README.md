# Eldorado fixtures

Responses of the Eldorado seller API and the TalkJS chat API used by the connector contract
tests (`tests/test_eldorado_connector.py`). The tests never touch the network.

These files were written from the field names the old seller portal read from the live API
(`eldorado_client.py`, `item_delivery_manager.py`, `sales_monitor_service.py`,
`buyer_reminder_service.py`, `restock_manager.py`, `chat_monitor_service.py`). They are not a
live capture. Refresh them with a read-only recording once credentials are available:

    uv run python -m app.cli eldorado-record --out tests/fixtures/eldorado/recorded

The recorder only sends GET requests and redacts passwords, tokens and buyer names.
