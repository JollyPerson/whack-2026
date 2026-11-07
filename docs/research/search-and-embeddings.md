# Search, embeddings and fact-check cache for pre-research and fallback

Research for issue #6. Written Sat 7 Nov 2026. Cost is ignored, as the team has credits.

## Recommendation

| Decision | Pick | Confidence |
|---|---|---|
| Pre-research search | Exa `type=auto` with `contents.text` | Medium-high |
| Live fallback | Exa `type=lite`, with Brave LLM Context fired as a hedge at about 1.5 s | Medium-high |
| Tavily | Do not use | Medium-high |
| Embedding model | Local `BAAI/bge-small-en-v1.5` through FastEmbed, for chunks and queries | Medium: **measure first** |
| Vector index | Exact flat search (a NumPy matrix); no HNSW or IVF | High |
| Google Fact Check | Keep it first in the cut order. If built, use it to seed pre-research and keep it out of the live path | Medium-high |

**The main finding:** the index is not what limits the 50 ms target. Flat search over 5,000 chunks takes under 2 ms (measured below). Embedding the query is the real cost, and hosted APIs typically take about 200-300 ms for it. Only a local model can plausibly reach about 50 ms.

## Evidence labels

- **[Vendor docs]**: the vendor's own documentation, authoritative for features and limits.
- **[Vendor claim]**: a performance or marketing claim the vendor makes about itself.
- **[Third-party]**: measured by someone else. Conflicts of interest are noted, and we did not reproduce the numbers.
- **[Measured here]**: run for this ticket.
- **[Inference]**: my reasoning from the above.

## 1. Tavily, Exa and Brave

### Features and limits [Vendor docs]

| | Exa | Brave | Tavily |
|---|---|---|---|
| Fastest mode | `lite` (`instant` and `fast` are now legacy) | LLM Context or Web | `ultra-fast`, `fast` |
| Page content in the search response | Yes: `contents.text`, `contents.highlights` | Yes: LLM Context returns extracted chunks | Yes: snippets of about 500-800 chars, or `include_raw_content` |
| Separate fetch step needed | No (`/contents` also exists) | No | No (Extract: up to 20 URLs per call) |
| UK targeting | `userLocation` | `country` (default `us`) | `country` boost, general topic only |
| Domain control | `includeDomains`, up to 1,200 | Goggles re-ranking | `include_domains`, up to 300 |
| Rate limit | `/search` 10 QPS, `/contents` 100 QPS | 50 QPS on the Search plan | 100 RPM dev key, 1,000 RPM production |
| Stated latency | Only `deep-lite`: "consistent 4-second latency" | Only the claim below | None |

All three can feed pre-research without a separate scraping step. Rate limits don't block any of them for 20 queries, though Exa's 10 QPS spreads the dispatch over about 2 s.

Exa `lite` limits, which matter for the fallback: it serves from Exa's index "without fetching pages at query time", and rejects freshness parameters such as `maxAgeHours`. It does accept `contents.text`, `contents.highlights`, `userLocation` and `includeDomains`. **Very recent news may be missing from `lite`**, which is one reason to hedge with Brave.

### Measured latency and quality [Third-party]

Latency is p50 unless marked. The quality metrics differ, so compare within a row only.

| Benchmark (date; independence) | Exa | Brave | Tavily |
|---|---|---|---|
| [Keenable NEEDLE](https://keenableai.github.io/needle/results/) (7 days to 7 Nov 2026; run by a competing vendor) | default 1.5 s, p95 3.4 s, score 70%; **lite 444 ms, p95 667 ms, score 70%** | web 570 ms, p95 782 ms, score 61%; LLM Context 1.2 s, p95 2.3 s, score 68% | 1.6 s, p95 3.0 s, score 44% |
| [cloro](https://github.com/cloro-dev/web-search-api-benchmark) (Nov 2026; run by a competing vendor, open data) | 0.74 s; SimpleQA 97.3%, FreshQA 83.0% | 1.21 s; SimpleQA 93.1%, FreshQA 84.9% | 2.00 s; SimpleQA 91.8%, FreshQA 81.8% |
| [OpenBenchmarks](https://openbenchmarks.com/web-search/fastest-search-api) (Oct 2026; [sells evals to vendors it ranks](https://openbenchmarks.com/about)) | instant 386 ms, 97.7%; fast 569 ms, 99.3% | LLM Context 590 ms, 94.0%; web 613 ms, 93.3% | basic 1.69 s, 87.7%; advanced 4.23 s, 93.0% |
| [Benchology](https://benchology.co/benchmark/search-apis/2026-09/) (Sept 2026; independence not checked) | 1,676 ms, p95 2,488 ms; 91.1% | 477 ms, p95 879 ms; 90.0% | 2,068 ms, p95 3,776 ms; 83.4% |
| [micr.dev blog](https://blog.micr.dev/blog/websearch-bench) (Aug 2026; individual; 96 queries) | 1,102 ms, p95 1,792 ms; nDCG 0.523 | 427 ms, p95 820 ms; nDCG 0.659 | 1,296 ms, p95 2,564 ms; nDCG 0.557 |
| [AIMultiple](https://aimultiple.com/agentic-search) (Dec 2025; mean latency; 100 queries) | ~1.2 s; 14.39 | 669 ms; 14.89 | 998 ms; 13.67 |
| [arlen/bench](https://arlenkumar.com/bench/leaderboards/web-search) (2026 Q3; no latency measured) | hit@5 80.9% | hit@5 58.9% | hit@5 49.4% |

What this supports [Inference]:

- **Tavily is out.** On quality it comes last of the three in every benchmark above except micr.dev, and at default depth it is usually the slowest too. Its `fast` mode is quick ([282 ms mean](https://github.com/openbenchmarks-labs/web-search-for-coding-agents)) but scored well below Exa `fast` there (47.3% vs 66.3%, on a coding-documentation task).
- **Exa has the quality edge, not a clean sweep.** It leads NEEDLE (default and `lite`), cloro SimpleQA, OpenBenchmarks and arlen/bench, and is level with Brave within error bars at Benchology. Brave leads at micr.dev, and its leads at AIMultiple and on FreshQA are not statistically clear.
- **Fast Exa and Brave both comfortably fit the 1-3 s fallback window.** Tavily default's p95 is 2.6-3.8 s across these runs, which would miss it.
- **Don't trust absolute numbers.** The same mode varies by up to about 2x between benchmarks (Exa default 0.74-1.68 s; Brave LLM Context 590 ms-1.2 s). Benchology also found that [concurrency changes the ranking](https://benchology.co/benchmark/search-apis/2026-09/).

No benchmark tests UK political claims, and none says it was run from London.

[Vendor claim] Brave says LLM Context "reduces latency to under 500ms at P90" and is "up to 20x faster" ([Brave](https://brave.com/search/api/)). The P90 claim is not corroborated: third-party p50s for that endpoint are already 590 ms-1.2 s. The 20x figure compares against the slowest product in the AIMultiple run, not against Exa or Tavily.

### Suggested settings [Inference, on vendor-documented parameters]

**Pre-research (Exa):** `type=auto`, `userLocation` set to GB, `contents.text` with `maxCharacters` of about 4,000 per result, 10 results per query. Run about half the queries with `includeDomains` on UK primary sources (such as ons.gov.uk, gov.uk, parliament.uk, fullfact.org) and the rest open. Stop at a hard deadline and index what has arrived.

**Live fallback:** Exa `type=lite` with `userLocation` GB and `contents.highlights`. If there is no answer at about 1.5 s, also call Brave LLM Context with `country=gb` and a low `maximum_number_of_tokens`. Take the first response. Brave is a separate index, so this covers both an Exa outage and a `lite` freshness miss.

## 2. Embedding model and index

### The index: flat search is enough

[Measured here] Exact cosine top-10, single-threaded C (`-O3 -march=native -ffast-math`), random unit vectors, 200 queries, on an Intel i7-9750H laptop:

| Chunks | 384 dims p50 / p95 | 1,536 dims p50 / p95 | 3,072 dims p50 / p95 |
|---|---|---|---|
| 1,000 | 0.04 / 0.04 ms | 0.21 / 0.40 ms | 0.55 / 0.79 ms |
| 5,000 | 0.29 / 0.47 ms | 1.60 / 1.88 ms | 3.47 / 3.83 ms |
| 20,000 | 1.69 / 2.08 ms | 7.55 / 10.24 ms | 15.63 / 23.40 ms |

This corpus will be small. 20 queries x 10 results x up to 6 chunks is about 1,200 chunks, which sits well under 1 ms at 384 dims. An approximate index would add dependencies and recall risk to save time we don't need. [FAISS's own guidance](https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index) is that for few searches "direct computation is the most efficient option", and that only flat indexes guarantee exact results.

NumPy (`matrix @ query`, then `argpartition`) was not measured here, as it isn't installed on this machine. I expect the same order of magnitude [Inference].

### The query embedding: where the time actually goes

| Option | Query embedding latency | Evidence |
|---|---|---|
| OpenAI `text-embedding-3-small` | p50 about 300 ms; p90 about 500 ms; p99 up to 5 s | [Third-party](https://nixiesearch.substack.com/p/benchmarking-api-latency-of-embedding) (one week of polling, 2025); [p50 292 ms in a 20-request test](https://news.ycombinator.com/item?id=44751712) |
| Google Gemini / Vertex | p50 about 270 ms (Gemini); lower median but volatile (Vertex) | Same two sources |
| Cohere | Lower median than OpenAI but volatile; sampled rows include 62-64 ms | [Third-party](https://nixiesearch.substack.com/p/benchmarking-api-latency-of-embedding), [raw data](https://huggingface.co/datasets/nixiesearch/embed-api-latency) |
| Voyage `voyage-4-lite` | p50 about 210 ms, p95 230-288 ms | [Third-party](https://yk-lab.net/archives/cloudflare-workers-ai-vs-voyage-ai-embedding-speed-benchmark/), from a Mac, likely in Japan |
| Local `bge-small-en-v1.5` (FastEmbed, ONNX, CPU) | About 38 ms per document, about 26 per second, on 2 vCPU | [Weak third-party](https://markaicode.com/vs/fastembed-vs-hugging-face/): method not described. **Unverified** |

None of the hosted options reliably hits 50 ms, and their tails are the bigger demo risk. Only a local model can plausibly do it, and it also keeps working if venue wifi struggles. FastEmbed lists `BAAI/bge-small-en-v1.5` at [384 dims, 0.067 GB, MIT licence](https://qdrant.github.io/fastembed/examples/Supported_Models/) [Vendor docs].

**The constraint this creates:** chunks and queries must use the same model, so local query speed means local indexing too. At 26 chunks/s, 1,200 chunks take about 46 s [Inference]. That fits in 60-90 s alongside the search step, but about 2,000 chunks is the ceiling on that weak figure. Cap the corpus and embed in priority order so the deadline cuts the least useful chunks. A faster laptop or batching should improve this, but that is untested.

**The quality trade-off is untested.** I found no comparison of `bge-small` with OpenAI on claim-to-evidence retrieval. OpenAI publishes [MTEB 62.3% for 3-small](https://developers.openai.com/api/docs/guides/embeddings) [Vendor claim], with no matching figure for `bge-small` from the same source.

### Decision rule for the Evidence owner (about 15 minutes)

1. On the demo laptop, time FastEmbed for a single query (warm) and for embedding 1,200 chunks.
2. Run the 20-claim eval set with both `bge-small` and `text-embedding-3-small`, and compare recall@5.
3. Keep local unless its recall is clearly worse or indexing overruns the budget. Otherwise switch to OpenAI with a 1 s timeout (limits: [2,048 inputs and 300,000 tokens per request](https://developers.openai.com/api/reference/resources/embeddings/methods/create)). That costs about 300 ms per claim, which still fits the 2-4 s end-to-end budget but misses the 50 ms target.

Whichever model wins, keep the raw chunk text next to each vector so the quote check runs against the retrieved source.

## 3. Google Fact Check Tools API and UK coverage

**Short answer:** coverage is good for UK political claims, thanks to Full Fact, and close to nil for light motions. It matches short keywords, not spoken sentences. First in the cut order is right.

### Coverage sample [Measured here]

Ten queries on the public [Fact Check Explorer](https://toolbox.google.com/factcheck/explorer), in English, on 7 Nov 2026. Google says the API queries [the same set of results](https://developers.google.com/fact-check/tools/api). This is a small hand-picked sample, not a coverage rate.

| Query | Result |
|---|---|
| `NHS waiting lists` | Many relevant Full Fact checks, up to Apr 2026 |
| `net migration UK` | Relevant Full Fact checks, including Jun 2026 |
| `university tuition fees UK` | 2 Full Fact checks (May 2026, Oct 2015) |
| `tax burden highest 70 years` | 2 Full Fact checks, May 2024 |
| `Britain pays more tax than it has in 70 years` | **None**, though the query above finds a match |
| `NHS waiting list has fallen this year under this government` | **None**, despite plenty of NHS coverage |
| `votes at 16` | 3 results, none about the UK (Malta, US) |
| `four day working week productivity` | 1 result, from Malta |
| `homework improves school results` | None |
| `cats better pets than dogs` | 1 irrelevant result |

Other things that affect the design:

- **Claim-shaped text misses.** Full sentences returned nothing where short keywords matched, so calling it directly with a transcribed claim will under-deliver [Inference from the sample].
- **`languageCode=en-GB` won't focus on the UK.** The docs say "we do not currently consider the region". Use [`reviewPublisherSiteFilter=fullfact.org`](https://developers.google.com/fact-check/tools/api/reference/rest/v1alpha1/claims/search) instead [Vendor docs].
- **Ratings are free text.** Full Fact's appear as sentences ("This is not correct. The latest data..."), not labels, so they can't map directly to `Verdict.label`. Treat them as evidence for the verdict step.
- **Old checks can mislead.** Results span 2015-2026 and figures like NHS waits go stale. Show the review date and use `maxAgeDays` where recency matters.
- **The service appears alive.** Google [phased out ClaimReview rich results in Search](https://developers.google.com/search/blog/2025/06/simplifying-search-results) from June 2025, but Explorer still shows checks from the last day, and [Full Fact said in Sept 2025](https://fullfact.org/technology/google-search-is-vandalising-the-internet-heres-how/) that it continues to use the markup. The API is still `v1alpha1`, with no stability promise that I could find.

### How to use it, if it survives the cut [Inference]

Keep it out of the live claim path. During pre-research, run 3-5 short keyword queries from the motion, filtered to Full Fact, and index the results as ordinary chunks. This helps most in the optional PMQs segment. It should add little for a light judge-picked motion, so the main demo shouldn't depend on it.

## What could not be verified

- **Latency from the venue.** No third-party figures are from London. Time each provider from the venue before relying on them.
- **FastEmbed speed on team hardware.** I found only one weak figure, and no vendor numbers. I did not install it or download the model for this ticket.
- **Retrieval quality on debate claims**, for any search provider or embedding model. The 20-claim eval set is the real test.
- **Exa with `contents.text` under load.** The benchmarks time search with their own settings, so full-text latency for `auto` and `lite` may be higher.
- **Google Fact Check API behaviour and latency.** No API key was available: an unauthenticated call returned HTTP 403. Coverage was sampled through Explorer instead, and API results may differ. No deprecation notice was found, but its absence isn't a guarantee.
- **Third-party benchmark integrity.** The numbers come from the published pages, mostly read as excerpts, and were not re-run. Several authors sell competing products or services to these vendors. Agreement across them is the useful signal.
