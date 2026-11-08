# Other ways to check claims, and what to change

Written Sun 8 Nov 2026, after a night of tuning the current pipeline (index, then web, then one verdict). The
question: are we tunnel-visioned, and what do other live fact-checking systems do instead?

## Recommendation

| Change | What it fixes | Live latency | Confidence |
|---|---|---|---|
| **1. Pre-verify the claims we expect, before the debate, and match live claims to them** | Speed, accuracy and consistency for the claims most likely to be made | Faster: a match is a lookup | Medium-high |
| 2. Ask the model where the fact is published, then fetch that and quote it | Primary sources our searches never find | Too slow for live; offline and deep checks only | Medium |
| 3. Split a claim into 2-3 questions and find evidence for each | Missing context, and claims with two parts | Slower | Medium |
| 4. Several sources per verdict, flagging when they disagree | Sources that conflict (transport-2) | About the same | Medium |
| 5. Grow the eval to 50+ claims | We can't tell a real gain from noise at 20 | None | High |
| Fine-tuned small models (as LiveFC) | Speed and cost | Faster | Not for a hackathon |

**The main finding:** every serious system we found does the heavy work before or outside the live moment, and
treats live work as detection, matching and a light check. We do everything live, inside about 8 seconds, so the
verdict model only ever sees what a fast search found in that window. The demo Motions are known in advance, so
most of the heavy work can move to before the debate, where time and cost don't matter.

## What our own measurements say

From the evals on 7 and 8 Nov (`eval/verdict_results.tsv`, `app/evidence/README.md`):

- **Misses are mostly about sources, not judgment.** Of Opus's misses, one was a judgment error. The rest were the
  right document never being found (bbc-4, all four holdout claims) or sources that disagree (transport-2).
- **Bigger indexes only help with better ranking.** About 600 pages per Motion scored 13/20 on BM25 and 18/20 with
  embeddings and 8 chunks.
- **A slower live second pass didn't help on Kimi** (fixed 0, broke 1). It's untested on Opus.
- **Model caution matters.** Kimi K2.6 rarely says unverifiable (10%) and labelled 3 of 5 unverifiable claims as
  contradicted or missing context. Opus says it 35-45% of the time. A referee that speaks false corrections is
  worse than a quiet one.
- **The eval is small.** With 20 claims, one or two either way is noise.

## What other systems do

**LiveFC** (WSDM 2025), the closest to us: live fact-checking of the 2024 US presidential debate from audio
([paper](https://arxiv.org/html/2408.07448v2)). Its stages:

1. Whisper transcription and overlap-aware speaker diarisation.
2. Each sentence is rewritten into a self-contained claim, then a fine-tuned classifier decides if it's
   check-worthy.
3. Each claim is **split into questions**, and evidence is searched per question across several web search engines
   and an index of about 280,000 published fact-checks, then reranked with a cross-encoder.
4. A fine-tuned classifier labels each claim-evidence pair, and a majority vote gives the verdict.

Its fine-tuned small models beat GPT-4 on both check-worthiness (macro-F1 0.899 vs 0.695) and verdicts (0.708 vs
0.630). It found all 30 claims PolitiFact checked in that debate, plus false positives that needed filtering. It
only has supported and refuted, so partly true claims fall through. It gives no measured latency, only "within
seconds".

**AVeriTeC**, the shared task for real-world claims with web evidence, represents evidence as **questions and
answers** about the claim ([2024 overview](https://arxiv.org/html/2410.23850v1)). In 2024 the winner (TUDA_MAI)
scored 63% against an 11% baseline, mostly with large hosted models. In 2025, limited to open models on one GPU and
about a minute per claim, the winner (CTU AIC) scored 33%
([2025 overview](https://preview.aclanthology.org/setup/2025.fever-1.15)). Real-world claims are much harder than
our hand-picked 20, so our 18/20 shouldn't be read as real-world accuracy.

**FIRE** ([paper](https://arxiv.org/abs/2411.00784v2), NAACL 2025 Findings) lets the model use **what it already
knows first**, and searches only when it isn't confident, choosing each next query from what it has seen. Against
other multi-step frameworks it was slightly more accurate at 7.6 times lower LLM cost and 16.5 times fewer
searches. Our verdict prompt forbids outside knowledge entirely, which keeps quotes honest but throws away what the
model knows about where a fact is published.

**Full Fact** ([how they use AI](https://fullfact.org/blog/2025/feb/how-ai-can-help-fact-checkers/)) runs live
transcripts of PMQs and debates, detects and scores claims, and **matches them to claims already checked**: an LLM
embeds claims, entity recognition stops different people and places being confused, and a classifier decides if
it's a match. Their position is that no model can reliably decide if a claim is true, so humans make verdicts and
the AI alerts them.

**Claim decomposition** ([ProgramFC](https://arxiv.org/pdf/2305.12744), ACL 2023) splits a claim into sub-steps
and checks each. Its gain grows with complexity, 14.9% on four-step claims, but later work finds LLM splits can be
too coarse or ambiguous. Debate claims are mostly one or two steps, so expect a modest gain, mainly on claims like
"use went up 14% and car trips fell 10%".

**Google Fact Check Tools** returns published fact-checks (ClaimReview) for a query
([API](https://developers.google.com/fact-check/tools/api)). Our earlier research
([search-and-embeddings.md](search-and-embeddings.md)) already put it last in the cut order and out of the live
path: debate figures are rarely fact-checked word for word.

## The options for us

### 1. Pre-verify the claims we expect, then match live claims to them

Before the debate, for each Motion:

1. The model lists the claims each side is likely to make: figures, comparisons and case studies, both sides
   equally. A few hundred per Motion.
2. Each one is checked offline with no time limit: the big index, targeted searches (option 2), several sources
   (option 4), and the most careful model settings. A person can spot-check the ones the referee would correct.
3. Each verified fact is stored with its figure, unit, date, source and exact quote, and linked to the claims it
   settles. This is the graph from the earlier discussion: claims, facts and sources, built before the debate.

Live, a spoken claim is matched to the stored ones the way Full Fact does it: embeddings to find candidates,
entities and the subject to rule out the wrong person or place, then a light model call to confirm the match. The
figure is still compared in code (`checks.py`), so "sick days fell 90%" matched to the stored fact "fell 65%" gives
a correction with the stored quote. With no match, the current pipeline runs as now.

Why it suits us:

- **Latency:** a match is milliseconds plus one short model call, so corrections land while the speaker is still
  on the point.
- **Accuracy:** the hard part, finding the primary source and judging it, gets minutes and the best model, not
  seconds.
- **Consistency:** the same claim gets the same verdict every time, in every run. That was the original question.
- **Cost:** the expensive work happens once per Motion, not once per claim per debate.

Risks: a wrong match is a confident wrong correction, so the match needs its own threshold and eval. Coverage
depends on predicting what debaters say. For an honest measure, test coverage against `docs/FACT-CARDS.md` without
ever giving those cards to the system. That file already keeps the eval set separate from the cards.

### 2. Ask the model where the fact is published

In the spirit of FIRE: ask the model what it believes the true figure is and which document states it (for
example the BBC's Licence Fee Trust Statement 2024/25), then search for that document by name, fetch it, and keep
the verdict only if a verbatim quote from it passes the guard. The model's knowledge picks the source; the source,
not the model, settles the claim. That targets our most common miss, the primary document that search never
surfaces. It costs an extra call and a fetch, so use it offline (option 1) and in the deep check, not live.

### 3. Split claims into questions

As AVeriTeC and LiveFC do: two or three questions per claim ("What was the fall in car trips in Tallinn?", "Did car
traffic rise overall?"), with evidence found per question. This is mainly for missing context, which needs the
period, the basis or the other half of the picture. It fits option 1 better than the live path, where each
question adds time.

### 4. Several sources, and flag disagreement

As proposed before: up to three verified quotes from different sites. Speak a correction only when two independent
sources agree or one is official; when sources give different figures, say missing context or unverifiable, not
contradicted.

### 5. Fix the eval before trusting small gains

At least 50 claims across more Motions, a bigger holdout, and a separate eval for claim matching (right match, wrong
match, no match). Keep the eval apart from the fact cards.

### Not now

- **Fine-tuned small models** (LiveFC): faster, cheaper and, in their tests, better than GPT-4. But they need
  labelled data and training time, and only give supported or refuted.
- **Letting the model drive search itself** (an agent with a web search tool): the simplest design, but it's
  multi-step and slow, and gives less control over which sources are quoted. It could run inside option 1's
  offline checks.

## Sources

- [LiveFC: A System for Live Fact-Checking of Audio Streams](https://arxiv.org/html/2408.07448v2) (WSDM 2025)
- [The AVeriTeC shared task, 2024 overview](https://arxiv.org/html/2410.23850v1)
- [The second AVeriTeC shared task, FEVER 2025](https://preview.aclanthology.org/setup/2025.fever-1.15)
- [FIRE: Fact-checking with Iterative Retrieval and Verification](https://arxiv.org/abs/2411.00784v2)
- [Full Fact: How AI can help fact checkers](https://fullfact.org/blog/2025/feb/how-ai-can-help-fact-checkers/)
- [Full Fact AI](https://fullfact.org/ai/)
- [ProgramFC: Fact-Checking Complex Claims with Program-Guided Reasoning](https://arxiv.org/pdf/2305.12744)
- [Google Fact Check Tools API](https://developers.google.com/fact-check/tools/api)
