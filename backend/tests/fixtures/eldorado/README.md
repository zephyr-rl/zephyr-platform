# Eldorado fixtures

Responses of the Eldorado seller API and the TalkJS chat API used by the connector contract
tests (`tests/test_eldorado_connector.py`). The tests never touch the network.

Their structure was checked on 2026-11-14 against a read-only, redacted recording of the live
API for the three stores (orders, offers of every kind, reviews, vault, chat). Identifiers and
texts here are invented; no real data is committed. Notable facts from that recording:

- order lists are paged with `nextPageCursor`; `GET api/orders/me/{id}` answers 403 for the
  seller API, so orders are found by searching the list
- reviews come as `reviews.results[].orderReview.review`
- TalkJS messages carry their text in a `content` array of blocks
- `api/orders/me/statesCount` answers 403 for the seller API

To record again (GET requests only, secrets redacted):

    uv run python -m app.cli eldorado-record --out tests/fixtures/eldorado/recorded

The recorder only sends GET requests and redacts passwords, tokens and buyer names.

Raw recordings go to `recorded/`, which is ignored by git.
