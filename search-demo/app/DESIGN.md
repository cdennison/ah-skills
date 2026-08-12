# Search UI design

## Intent

A compact, single-screen utility for searching the repo's local agent-skill index.
The search field is always the first interactive control and results stay directly
below it so another query never requires navigation. A sidebar carries optional
filters so the main column stays uncluttered for the common free-text case.

## Visual system

- Use Streamlit's native layout and interactive dataframe for familiar behavior.
- Use a warm neutral page background, dark ink, blue actions, and restrained green,
  amber, and red status accents.
- Keep the content column wide enough for descriptions but cap its visual density.
- Use one radius scale (8-12px), subtle borders, and no decorative animation.

## Components and states

1. Header: product name, one-line explanation of local hybrid search, and a
   "How search works" expander explaining hybrid RRF, what Match/Stars/
   Discovered via/ranking filters mean, and that Match is a fused rank
   signal, not a percentage.
2. Sidebar filters: a "Reset filters" button, minimum-stars number input,
   "Discovered via" source multiselect, and an expander of dynamically
   discovered ranking-position filters (one per `search_rank_*` metric
   actually present in the index) — all point-and-click widgets, no typed
   query-language expressions. Every control carries `help=` tooltip text.
3. Search form: one text input (with a tooltip explaining hybrid search) and
   one primary submit button. The query can be left blank if at least one
   sidebar filter is set, which runs filter-only "browse" mode instead.
4. Results header: result count, submitted query (or "matching your
   filters" when browsing), and a sorting hint.
5. Results grid: rank, name, repository, match score (or "Browse" when
   there's no query to score against), mocked security scan, description,
   and source path. Column headers provide sorting and hover tooltips.
6. Empty state: prompt before the first search and a helpful message for
   zero hits (covers both an unmatched query and an over-restrictive filter
   combination).
7. Error state: a clear message if the local index cannot be opened or queried.

## Accessibility and interaction

- The input has a persistent label and keyboard submission works through the form.
- Status is always written as text, never communicated by color alone.
- Native focus states and table keyboard behavior remain intact.
- The last query and results persist during table interactions; submitting again
  replaces them with fresh results and newly mocked scan values.
- "Reset filters" clears sidebar widget state directly (not just the next
  render's derived filter object), so a subsequent free-text search behaves
  identically to a fresh page load with no filters ever touched.

## Filtering architecture

- `min_stars`, `sources`, and ranking-position filters are **all** pushed
  down to Qdrant as native payload filters (`FieldCondition`/`Range`/
  `MatchAny`), applied as part of the ANN search itself via
  `Prefetch(filter=...)`/`query_filter=...` (or `scroll_filter=...` in
  browse mode) — not as a client-side post-filter over an overfetched
  result set. See `docs/QUERY_INTERFACE.md`'s "Ranking metadata" section for
  the payload schema this depends on.
- Ranking-filter widgets are populated dynamically from whatever
  `search_rank_*` payload fields actually exist in the collection right now
  (`discover_rank_metrics()`), so a new (query, sort) combo shows up in the
  UI automatically instead of needing a hardcoded widget list.

## Accepted debt

- Security scan values are intentionally random placeholders, not real findings.
- This first version has one fixed result limit and no pagination.
