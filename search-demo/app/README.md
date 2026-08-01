# Streamlit search app

This isolated app searches the existing `../qdrant_db/` collection without
modifying or rebuilding it. Results use the same dense + BM25 reciprocal-rank
fusion as the repository's `query.py` script.

From the repository root:

```bash
uv sync --project app
uv run --project app streamlit run app/streamlit_app.py
```

Open <http://localhost:8501>, enter a query, and press Enter or select **Search**.
Click any results-table header to sort. Submit another query to replace the table
with fresh matches and newly randomized placeholder security statuses.

Run the checks with:

```bash
cd app
uv run ruff check .
uv run basedpyright
uv run python -m pytest -q
```

The **Security Scan** column is mock data only; it does not represent a real scan.
