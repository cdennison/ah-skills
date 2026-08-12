# Good search terms

`query.py` runs **hybrid search**: a dense embedding (semantic similarity)
and a BM25 sparse vector (exact term matching) are both computed for every
query, then combined with Qdrant's RRF fusion into one ranked list. Understanding
what each half is good at — and where fusion helps or doesn't — is the key to
getting good results out of this platform. This doc walks through each
retrieval mode, when it wins, and example queries for each, against the
~5,400 `SKILL.md` corpus.

## Semantic (dense) search — matches on meaning, not words

**Model:** `all-MiniLM-L6-v2`. Encodes query and document into the same
vector space; ranks by cosine similarity. No shared words required.

**Best for:** describing a *task* or *intent* in your own words. Skill
descriptions are written as "use this skill when the user asks to X" — full
sentences, not tag lists — so a task-shaped query matches on meaning even
when the vocabulary is completely different.

```
turn a spreadsheet into a chart          → matches "generate visualizations from Excel data"
summarize a long PDF for a client        → matches skills about document synthesis
help me debug a flaky test               → matches skills about test reliability / CI
```

**Weakness:** dense embeddings are bad at exact identifiers. A tool name
like `kicad` or `n8n` doesn't sit near any meaningful semantic neighborhood
— MiniLM has to fall back on nearby words like "PCB" or "workflow" and can
easily rank the wrong skill above the one that literally names the tool.

## Keyword (sparse/BM25) search — matches on exact terms

**Model:** FastEmbed's `Qdrant/bm25` — classic TF-IDF-style term-frequency
scoring, the same family as Elasticsearch/Lucene. No neural model, no
notion of "meaning" — a query term either appears in the document or it
doesn't (with rarer terms weighted higher).

**Best for:** exact tool names, library names, acronyms, or jargon that
would get lost in a dense embedding.

```
kicad                    → exact match to aklofas/kicad-happy, nothing else competes
splade                   → exact match to skills mentioning SPLADE by name
JLCPCB                   → exact match, dense search alone would likely miss this entirely
```

**Weakness:** zero tolerance for paraphrase. `"pcb fabrication service"`
won't match a skill that only ever says "JLCPCB" — no shared terms means no
score, regardless of how related the concepts are.

## Hybrid (RRF fusion) — what `query.py` actually runs

Every query hits both retrievers in parallel (`prefetch`), and results are
merged by [Reciprocal Rank Fusion](https://qdrant.tech/documentation/concepts/hybrid-queries/#reciprocal-rank-fusion-rrf)
— each doc's final rank is based on how high it placed in *either* list, not
a weighted score blend. This is why hybrid is the default and the right
choice for nearly every query here:

```
JLCPCB assembly ordering workflow   → BM25 anchors "JLCPCB", dense ranks by task match
home assistant automation skill     → BM25 anchors "home assistant", dense captures "automation"
extract data from a PDF datasheet   → dense handles the task, BM25 catches "PDF"/"datasheet" precisely
```

Mixing a **named entity** with a **task phrase** is the highest-precision
query shape for this corpus: BM25 locks onto the entity, dense ranks by how
well the surrounding intent matches the skill's actual description.

**What hybrid doesn't fix:** it can't invent a match neither retriever
found — if a query is pure paraphrase of a rare term BM25 needed
(`"pcb fabrication service"` instead of `"JLCPCB"`) *and* too vague for
dense to disambiguate, both lists come back weak and RRF has nothing good
to fuse.

## Reranking — not implemented, and here's what it would add

`query.py` does not currently rerank the fused results — RRF's rank-based
merge is the final order. A reranking step (e.g. a cross-encoder like
`BAAI/bge-reranker-base`, or Qdrant's native `Qdrant/bm42` reranker) would
sit *after* fusion: take the top ~20 fused candidates and re-score each
query/document pair jointly, rather than independently, which fixes cases
neither dense nor sparse handles well alone — e.g. distinguishing between
several plausible skills that all mention the same tool name but differ in
what task they solve. Worth adding if result quality on ambiguous queries
becomes a real problem; not worth the extra model/latency at this corpus
size (~5k docs, mostly narrow single-purpose skills) today.

## What tends to work poorly, regardless of mode

- **Single generic words** (`"data"`, `"tool"`, `"agent"`) — too many
  matches, low signal on either side of the fusion; add a domain or verb.
- **Questions phrased as questions** (`"how do I do X?"`) — no worse than
  a statement, but stripping it to the task itself (`"do X"`) tends to
  match the frontmatter phrasing more directly.

## Try it

```bash
uv run python query.py "kicad pcb design"
uv run python query.py "extract structured data from a PDF" -n 10
```
