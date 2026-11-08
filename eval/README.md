# eval

- `eval_extract.py` and `gold_claims.jsonl`: score claim extraction. Owned by Brain, with gold labels from Harkaran.
- `eval_verdicts.py`: scores Evidence against `verdict_gold.jsonl`. Owned by Evidence. The design and gates are on issue #15, and the file's docstring has the commands.
- `eval_consistency.py` and `consistency_gold.jsonl`: score the consistency checker (U-turns, concessions, agreement)
  on the planted cases in `fixtures/consistency_debate.jsonl`. Free by default (the heuristic); `--llm` uses the model
  and costs tokens. Target: zero false U-turns.
- `verdict_gold.example.jsonl`: the row format. The real set is written by hand from primary sources.

Pass/fail checks belong next to each module instead.
