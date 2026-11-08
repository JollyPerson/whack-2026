# fixtures

The shared test debate: a roughly 3-minute recording and its timestamped transcript.
Every module's mocks replay this, so nobody waits on anyone else. Recorded at kickoff.

`consistency_debate.jsonl`: a 90-second "ban cars from city centres" debate with planted cases for the consistency
checker: a U-turn per side (Prop on the scheme's cost, Opp on how shoppers travel), two Opp concessions (short trips,
NHS savings), one shared point (public transport investment) and one rhetorical "even if" that must NOT count as a
concession. Labels: `eval/consistency_gold.jsonl`. Used by `./run.sh test-haiku` and `python -m eval.eval_consistency`.
