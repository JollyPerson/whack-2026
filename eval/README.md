# eval

- `eval_extract.py` and `gold_claims.jsonl`: score claim extraction. Owned by Brain, with gold labels from Harkaran.
- `eval_verdicts.py`: scores Evidence against `verdict_gold.jsonl`. Owned by Evidence. The design and gates are on issue #15, and the file's docstring has the commands.
- `verdict_gold.example.jsonl`: the row format. The real set is written by hand from primary sources.

Pass/fail checks belong next to each module instead.
