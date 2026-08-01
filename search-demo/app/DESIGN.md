# Search UI design

## Intent

A compact, single-screen utility for searching the repo's local agent-skill index.
The search field is always the first interactive control and results stay directly
below it so another query never requires navigation.

## Visual system

- Use Streamlit's native layout and interactive dataframe for familiar behavior.
- Use a warm neutral page background, dark ink, blue actions, and restrained green,
  amber, and red status accents.
- Keep the content column wide enough for descriptions but cap its visual density.
- Use one radius scale (8-12px), subtle borders, and no decorative animation.

## Components and states

1. Header: product name and one-line explanation of local hybrid search.
2. Search form: one text input and one primary submit button.
3. Results header: result count, submitted query, and a sorting hint.
4. Results grid: rank, name, repository, match score, mocked security scan,
   description, and source path. Column headers provide sorting.
5. Empty state: prompt before the first search and a helpful message for zero hits.
6. Error state: a clear message if the local index cannot be opened or queried.

## Accessibility and interaction

- The input has a persistent label and keyboard submission works through the form.
- Status is always written as text, never communicated by color alone.
- Native focus states and table keyboard behavior remain intact.
- The last query and results persist during table interactions; submitting again
  replaces them with fresh results and newly mocked scan values.

## Accepted debt

- Security scan values are intentionally random placeholders, not real findings.
- This first version has one fixed result limit and no filters or pagination.
