# Unfinished Tasks

- Test whether keyword search, rerank, etc. are actually working (verify the search pipeline stages produce correct/expected results, not just that they run).
- Add stats on what's already in the DB vs. net new (e.g. per pipeline run: count of skills already indexed vs. newly discovered/added), so ingestion runs are auditable.
- Zip file upload/download and recreating this repo fresh on a new computer (from `search-raw.zip` / `make_data_zip.sh`) hasn't been tested end-to-end.
