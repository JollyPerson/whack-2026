"""ArgMap: statuses, notices, non-responsive turns, duplicates, context, scoreboard, snapshot. No network."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.brain.argmap import ArgMap
from app.schemas import (ArgMapSnapshot, Claim, Evidence, Link, Pause, SessionStart, SpeakerInfo, Utterance,
                         Verdict, parse_event)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_debate.jsonl"
SESSION = SessionStart(motion="This house would ban cars from city centres",
                       speakers={"A": SpeakerInfo(name="Priya", side="prop"), "B": SpeakerInfo(name="Tom", side="opp")})
TEXT = "We should weigh this point very carefully today."


# --------------------------------------------------------------------------- tiny DSL: ops -> ArgMap
def S(spk, dur=5.0, text=TEXT):                          # speaker says one final utterance
    return ("say", spk, dur, text)


def C(cid, spk, imp=1.0, links=(), text=None, **kw):    # claim from that speaker's latest utterance
    return ("claim", cid, spk, imp, links, text, kw)


def P(spk):
    return ("pause", spk)


def V(cid, label, conf=0.9):
    return ("verdict", cid, label, conf)


class Driver:
    def __init__(self, session=SESSION, **kw):
        self.m = ArgMap(**kw)
        self.m.reset(session)
        self.t, self.n, self.uid, self.notices = 100.0, 0, {}, []   # t=0 means "unknown" in the contract

    def run(self, ops):
        for op in ops:
            getattr(self, "_" + op[0])(*op[1:])
            self.notices += self.m.drain_notices()
        return self

    def _say(self, spk, dur, text):
        self.n += 1
        self.uid[spk] = uid = f"u-{spk}-{self.n:04d}"
        self.m.on_utterance(Utterance(id=uid, speaker=spk, text=text, t_start=self.t, t_end=self.t + dur,
                                      ts=self.t + dur + 0.5))
        self.t += dur

    def _claim(self, cid, spk, imp, links, text, kw):
        self.m.add_claim(Claim(id=cid, speaker=spk, utterance_id=self.uid.get(spk, ""), importance=imp,
                               text=text or f"Point {cid} about the motion", ts=self.t,
                               links=[Link(claim_id=d, relation=r) for r, d in links], **kw))

    def _pause(self, spk):
        self.m.on_pause(Pause(speaker=spk, t=self.t + 0.3, ts=self.t + 0.3))

    def _verdict(self, cid, label, conf):
        self.m.on_verdict(Verdict(claim_id=cid, label=label, confidence=conf,
                                  evidence=[Evidence(url="https://example.org", quote="q")]))

    def status(self, cid):
        return self.m.nodes[cid].status

    def kinds(self, kind):
        return [n for n in self.notices if n.kind == kind]


# --------------------------------------------------------------------------- statuses
STATUS_CASES = [
    ("answered by attack", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("attacks", "c1")])],
     {"c1": "answered", "c2": "open"}),
    ("conceded by support", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("supports", "c1")])],
     {"c1": "conceded"}),
    ("attack beats support", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("supports", "c1")]),
                                 C("c3", "B", links=[("attacks", "c1")])], {"c1": "answered"}),
    ("minor never unanswered", 1, [S("A"), C("c1", "A", imp=0.33), S("B"), C("c2", "B"), S("A")], {"c1": "minor"}),
    ("0.5 is eligible", 1, [S("A"), C("c1", "A", imp=0.5), S("B"), C("c2", "B"), S("A")], {"c1": "unanswered"}),
    ("LLM-scale 2 normalised", 1, [S("A"), C("c1", "A", imp=2), S("B"), C("c2", "B"), S("A")], {"c1": "unanswered"}),
    ("open while reply turn still open", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B")], {"c1": "open"}),
    ("unanswered after reply turn closes", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A")],
     {"c1": "unanswered", "c2": "open"}),
    ("short claimless turn not substantive", 1, [S("A"), C("c1", "A"), S("B", dur=3), S("A")], {"c1": "open"}),
    ("long claimless turn substantive", 1, [S("A"), C("c1", "A"), S("B", dur=9), S("A")], {"c1": "unanswered"}),
    ("same-side support is no answer", 1, [S("A"), C("c1", "A"), C("c2", "A", links=[("supports", "c1")]),
                                           S("B"), C("c3", "B"), S("A")], {"c1": "unanswered"}),
    ("k=2 not yet", 2, [S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A")], {"c1": "open"}),
    ("k=2 after two turns", 2, [S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A"), S("B"), C("c3", "B"), S("A")],
     {"c1": "unanswered"}),
    ("link to unknown id dropped", 1, [S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("attacks", "c99")]),
                                       S("A")], {"c1": "unanswered"}),
]


@pytest.mark.parametrize("name,k,ops,expect", STATUS_CASES, ids=[c[0] for c in STATUS_CASES])
def test_statuses(name, k, ops, expect):
    d = Driver(k_turns=k).run(ops)
    assert {cid: d.status(cid) for cid in expect} == expect


def test_unanswered_notice_exactly_once():
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A"), P("A"), S("B"), C("c3", "B"), S("A")])
    un = [n for n in d.kinds("unanswered") if n.claim_id == "c1"]
    assert len(un) == 1
    assert un[0].speaker == "B" and un[0].detail["side"] == "prop" and un[0].t > 0


@pytest.mark.parametrize("rel,status", [("attacks", "answered"), ("supports", "conceded")])
def test_late_reply_answers_unanswered(rel, status):
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A")])
    assert d.status("c1") == "unanswered"
    d.run([S("B"), C("c3", "B", links=[(rel, "c1")]), S("A"), S("B")])
    assert d.status("c1") == status
    ans = d.kinds("answered")
    assert [(n.claim_id, n.speaker, n.detail["status"], n.detail["by"]) for n in ans] == [("c1", "B", status, ["c3"])]
    assert len([n for n in d.kinds("unanswered") if n.claim_id == "c1"]) == 1


def test_reply_before_deadline_sends_no_notice():
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("attacks", "c1")]), S("A")])
    assert d.kinds("unanswered") == [] and d.kinds("answered") == []


# --------------------------------------------------------------------------- non-responsive turns
TWO_A = [S("A"), C("c1", "A"), C("c2", "A")]
NR_CASES = [
    ("flagged at close", TWO_A + [S("B"), C("c3", "B"), C("c4", "B"), S("A"), S("B"), S("A")],
     [(1, True, ["c1", "c2"])], 1),
    ("links a target", TWO_A + [S("B"), C("c3", "B", links=[("attacks", "c1")]), C("c4", "B"), S("A")], [], 0),
    ("supports count as engagement", TWO_A + [S("B"), C("c3", "B"), C("c4", "B", links=[("supports", "c2")]),
                                              S("A")], [], 0),
    ("one claim is too little", TWO_A + [S("B"), C("c3", "B"), S("A")], [], 0),
    ("minor targets only", [S("A"), C("c1", "A", imp=0.3), S("B"), C("c3", "B"), C("c4", "B"), S("A")], [], 0),
    ("provisional at pause, final at close", [S("A"), C("c1", "A"), S("B", dur=16), C("c3", "B"), C("c4", "B"),
                                              P("B")], [(1, False, ["c1"])], 0),
    ("short turn at pause not judged", [S("A"), C("c1", "A"), S("B"), C("c3", "B"), C("c4", "B"), P("B")], [], 0),
    ("provisional cleared by a later link", [S("A"), C("c1", "A"), S("B", dur=16), C("c3", "B"), C("c4", "B"), P("B"),
                                             C("c5", "B", links=[("attacks", "c1")]), S("A")], [], 0),
    ("already replied target not re-demanded", [S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("attacks", "c1")]),
                                                S("A", dur=10), S("B"), C("c3", "B"), C("c4", "B"), S("A")], [], 0),
    ("late linking claim removes entry", TWO_A + [S("B"), C("c3", "B"), C("c4", "B"), S("A"),
                                                  C("c5", "B", links=[("attacks", "c2")])], [], 1),
]


@pytest.mark.parametrize("name,ops,entries,n_notices", NR_CASES, ids=[c[0] for c in NR_CASES])
def test_nonresponsive(name, ops, entries, n_notices):
    d = Driver().run(ops)
    assert [(e["turn"], e["final"], e["ignored"]) for e in d.m.snapshot().nonresponsive] == entries
    nr = d.kinds("nonresponsive")
    assert len(nr) == n_notices
    if nr:
        assert nr[0].speaker == "B" and nr[0].detail["turn"] == 1 and nr[0].claim_id in nr[0].detail["ignored"]


def test_provisional_becomes_final_once():
    d = Driver().run([S("A"), C("c1", "A"), S("B", dur=16), C("c3", "B"), C("c4", "B"), P("B")])
    assert d.kinds("nonresponsive") == []
    d.run([S("A"), P("A"), S("B"), S("A")])
    assert [(n.speaker, n.detail) for n in d.kinds("nonresponsive")] == \
        [("B", {"ignored": ["c1"], "turn": 1, "side": "opp"})]
    assert d.m.snapshot().nonresponsive[0]["final"] is True


# --------------------------------------------------------------------------- duplicates + repeats
DUP_SETUP = [S("A"), C("c1", "A", text="Air pollution causes around 40,000 early deaths a year in the UK"),
             C("c2", "A", text="Road traffic is the biggest single source of pollution in our cities"),
             C("c3", "A", text="Nearly 70% of shoppers arrive by car"),
             C("c4", "A", text="Footfall went up in Ghent")]
DUP_CASES = [
    ("A", "Air pollution causes about 40000 early deaths every year in the UK", "c1"),
    ("A", "Air pollution causes around 50,000 early deaths a year in the UK", None),     # correction
    ("B", "Air pollution causes around 40,000 early deaths a year in the UK", None),     # other speaker
    ("A", "Road traffic is the single biggest source of pollution in cities", "c2"),
    ("A", "Nearly 70 percent of shoppers arrive by car", "c3"),
    ("A", "Footfall did not go up in Ghent", None),                                      # negation kept
    ("A", "Buses are unreliable outside London", None),
]


@pytest.mark.parametrize("spk,text,expect", DUP_CASES)
def test_find_duplicate(spk, text, expect):
    assert Driver().run(DUP_SETUP).m.find_duplicate(spk, text) == expect


def test_merge_repeat_counts_and_merges_links():
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B")])
    v = d.m.version
    d.m.merge_repeat("c2", [Link(claim_id="c1", relation="attacks"), {"claim_id": "c404", "rel": "attacks"}])
    assert d.m.nodes["c2"].repeats == 1 and d.status("c1") == "answered" and d.m.version > v
    assert [(e.src, e.dst, e.relation) for e in d.m.snapshot().edges] == [("c2", "c1", "attacks")]
    v = d.m.version
    d.m.merge_repeat("c404", [])
    assert d.m.version == v


# --------------------------------------------------------------------------- context + turn text
def _context_driver():
    ops = [S("A"), C("c1", "A"), C("c2", "A"), C("c3", "A"),
           S("B"), C("c4", "B", links=[("attacks", "c1")]), C("c5", "B"), S("A")]
    ops += [C(f"c{i}", "A", imp=0.33) for i in range(6, 14)]
    return Driver().run(ops)


def test_context_for_contents_order_and_cap():
    m = _context_driver().m
    ids = lambda cs: [c.id for c in cs]
    # B sees: A's unanswered c2,c3 (c1 answered), all of A's latest turn c6..c13, last 8 overall
    assert ids(m.context_for("B")) == ["c2", "c3"] + [f"c{i}" for i in range(6, 14)]
    # capped: priority pending > opponent's latest turn (newest first) > recent, then re-sorted oldest first
    assert ids(m.context_for("B", limit=5)) == ["c2", "c3", "c11", "c12", "c13"]
    # A sees B's open c4,c5 (B's turn still counts as the opponent's latest), plus recent
    assert ids(m.context_for("A")) == ["c4", "c5"] + [f"c{i}" for i in range(6, 14)]
    assert ids(m.context_for("opp")) == ids(m.context_for("B"))           # a side name works too
    assert all(c.side == "prop" for c in m.context_for("B") if c.speaker == "A")
    assert len(m.context_for("B", limit=15)) <= 15 and m.context_for("B", limit=0) == []


def test_turn_text():
    d = Driver().run([S("A", text="one"), S("A", text="two"), S("A", text="three")])
    m, (u1, u2, u3) = d.m, (f"u-A-000{i}" for i in (1, 2, 3))
    assert m.turn_text("A") == ["two", "three"]
    assert m.turn_text("A", exclude_utterance_id=u3) == ["one", "two"]
    assert m.turn_text("A", exclude_utterance_id=u2) == ["one"]
    d.run([S("B", text="Now it is my turn to speak.")])
    assert m.turn_text("A", exclude_utterance_id=u3) == ["one", "two"]     # late extraction of A's turn
    assert m.turn_text("B") == ["Now it is my turn to speak."] and m.turn_text("nobody") == []


# --------------------------------------------------------------------------- scoreboard
def _score(prop, opp):
    ops = [S("A")] + [C(f"p{i}", "A") for i in range(len(prop))] + [S("B")] + [C(f"o{i}", "B") for i in range(len(opp))]
    ops += [V(f"p{i}", *v) for i, v in enumerate(prop)] + [V(f"o{i}", *v) for i, v in enumerate(opp)]
    return Driver().run(ops).m.snapshot()


S9, C9, M9 = ("supported", 0.9), ("contradicted", 0.9), ("missing_context", 0.9)
SCORE_CASES = [
    ("no verdicts", [], [], "Not enough checked claims yet", 0.5, 0.5),
    ("one side under 2 rated", [S9, S9, S9], [S9], "Not enough checked claims yet", 0.8, 0.667),
    ("tie", [S9, S9], [S9, S9], "Too close to call on the evidence", 0.75, 0.75),
    ("missing context half credit", [M9, M9], [S9, C9], "Too close to call on the evidence", 0.5, 0.5),
    ("prop ahead", [S9, S9, S9, M9], [C9, C9, S9], "Proposition ahead on the evidence (7 checked claims)", 0.75, 0.4),
    ("opp ahead", [C9, C9], [S9, S9], "Opposition ahead on the evidence (4 checked claims)", 0.25, 0.75),
    ("low confidence is unverifiable", [S9, ("supported", 0.5), ("contradicted", 0.59)], [C9, C9],
     "Not enough checked claims yet", 0.667, 0.25),
]


@pytest.mark.parametrize("name,prop,opp,call,sp,so", SCORE_CASES, ids=[c[0] for c in SCORE_CASES])
def test_scoreboard(name, prop, opp, call, sp, so):
    snap = _score(prop, opp)
    assert snap.call == call
    assert (snap.score["prop"].score, snap.score["opp"].score) == (sp, so)


def test_scoreboard_counts_and_latest_verdict_wins():
    snap = _score([S9, ("supported", 0.5), ("contradicted", 0.59)], [C9, C9])
    p = snap.score["prop"]
    assert (p.supported, p.contradicted, p.unverifiable) == (1, 0, 2)
    d = Driver().run([S("A"), C("c1", "A"), V("c1", "contradicted"), V("c1", "supported")])
    assert d.m.nodes["c1"].verdict == "supported"
    d.run([V("c9", "contradicted")])                                     # verdict before its claim: held
    d.run([C("c9", "A")])
    assert d.m.nodes["c9"].verdict == "contradicted"


def test_engagement_counts():
    d = Driver().run([S("A"), C("c1", "A"), C("c2", "A"), S("B"), C("c3", "B", links=[("supports", "c2")]), S("A")])
    s = d.m.snapshot().score["prop"]
    assert (s.unanswered, s.conceded) == (1, 1)


# --------------------------------------------------------------------------- speakers + sides
def test_unknown_speakers_without_session():
    d = Driver(session=None).run([S("spk1"), C("c1", "spk1"), S("spk2"), C("c2", "spk2"), S("spk1")])
    m = d.m
    assert [m.side_of(s) for s in ("spk1", "spk2", "spk3", "spk4")] == ["prop", "opp", "prop", "opp"]
    assert d.status("c1") == "unanswered"
    assert [(n.claim_id, n.speaker) for n in d.kinds("unanswered")] == [("c1", "spk2")]
    assert m.snapshot().speakers == {"spk1": "prop", "spk2": "opp", "spk3": "prop", "spk4": "opp"}


def test_sides_come_from_session_or_claim_hint():
    m = ArgMap()
    m.reset(SessionStart(speakers={"B": SpeakerInfo(side="Proposition"), "A": SpeakerInfo(side="opposition")}))
    assert (m.side_of("A"), m.side_of("B")) == ("opp", "prop")
    m.reset(SessionStart(speakers={"Gov": SpeakerInfo(side="opp")}))
    assert m.side_of("X") == "prop"                                       # fills the empty side
    m.reset()
    m.add_claim(Claim(id="c1", speaker="X", text="t", side="opp"))
    assert (m.side_of("X"), m.side_of("Y")) == ("opp", "prop")


# --------------------------------------------------------------------------- turns
def test_backchannel_does_not_split_turn():
    d = Driver().run([S("A"), S("B", text="No!"), S("A")])
    assert [(t.speaker, len(t.utts)) for t in d.m.turns] == [("A", 2)]
    d.run([S("B", text="Yes."), S("B")])
    assert [(t.speaker, [x for _, x in t.utts]) for t in d.m.turns] == [("A", [TEXT, TEXT]), ("B", ["Yes.", TEXT])]
    d2 = Driver(backchannel_words=0).run([S("A"), S("B", text="No!"), S("A")])
    assert [t.speaker for t in d2.m.turns] == ["A", "B", "A"]


def test_claims_only_replay_segments_turns():
    d = Driver().run([C("c1", "A"), C("c2", "B"), C("c3", "A")])
    assert [(t.speaker, t.claims, t.closed) for t in d.m.turns] == [("A", ["c1"], True), ("B", ["c2"], True),
                                                                     ("A", ["c3"], False)]
    assert d.status("c1") == "unanswered"


def test_late_claim_lands_in_its_own_turn():
    d = Driver().run([S("A"), S("B")])
    d.m.add_claim(Claim(id="c1", speaker="A", utterance_id="u-A-0001", text="late"))
    d.m.add_claim(Claim(id="c2", speaker="A", utterance_id="unit-A-7", text="late, synthetic unit id"))
    assert d.m.turns[0].claims == ["c1", "c2"] and len(d.m.turns) == 2


# --------------------------------------------------------------------------- version, reset, upsert, snapshot
def test_version_bumps_on_every_change_only():
    m = ArgMap()
    seen = [m.version]

    def bumped(fn, expect=True):
        before = m.version
        fn()
        assert (m.version > before) is expect
        seen.append(m.version)

    bumped(lambda: m.reset(SESSION))
    bumped(lambda: m.on_utterance(Utterance(id="u1", speaker="A", text=TEXT, final=False)), False)
    bumped(lambda: m.on_utterance(Utterance(id="u1", speaker="A", text="   ")), False)
    bumped(lambda: m.on_utterance(Utterance(id="u1", speaker="A", text=TEXT)))
    bumped(lambda: m.add_claim(Claim(id="c1", speaker="A", utterance_id="u1", text="x")))
    bumped(lambda: m.add_claim(Claim(id="c1", speaker="A", utterance_id="u1", text="x")), False)   # exact dup
    bumped(lambda: m.add_claim(Claim(id="c1", speaker="A", utterance_id="u1", text="x edited")))  # upsert
    bumped(lambda: m.on_verdict(Verdict(claim_id="c1", label="unverifiable")))
    bumped(lambda: m.merge_repeat("c1", []))
    bumped(lambda: m.on_pause(Pause(speaker="B")), False)
    assert seen == sorted(seen)


def test_upsert_replaces_fields_and_links():
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B", links=[("attacks", "c1")])])
    assert d.status("c1") == "answered"
    d.run([C("c2", "B", links=[("supports", "c1")], text="Fair point, granted")])
    assert d.status("c1") == "conceded" and d.m.nodes["c2"].claim.text == "Fair point, granted"
    assert len(d.m.nodes) == 2 and d.m.turns[1].claims == ["c2"]


def test_reset_clears_state():
    d = Driver().run([S("A"), C("c1", "A"), S("B"), C("c2", "B"), S("A"), V("c1", "supported")])
    v = d.m.version
    d.m.reset(SessionStart(motion="New motion", speakers={"X": SpeakerInfo(side="prop")}))
    m = d.m
    assert (m.motion, m.nodes, m.turns, m.drain_notices(), m.find_duplicate("A", "Point c1 about the motion")) == \
        ("New motion", {}, [], [], None)
    assert m.version > v and m.context_for("X") == [] and m.side_of("A") == "opp"
    snap = m.snapshot()
    assert snap.nodes == [] and snap.turns == [] and snap.call == "Not enough checked claims yet"


def test_snapshot_validates_and_round_trips():
    d = Driver().run(TWO_A + [S("B"), C("c3", "B", type="statistic", topic="shoppers by car"), C("c4", "B"),
                              S("A"), C("c5", "A", links=[("attacks", "c3")]), V("c1", "supported"),
                              V("c3", "contradicted", 0.4)])
    snap = d.m.snapshot()
    assert snap.version == d.m.version and snap.motion == SESSION.motion
    assert ArgMapSnapshot.model_validate(snap.model_dump()).model_dump() == snap.model_dump()
    back = parse_event(snap.model_dump_json())
    assert isinstance(back, ArgMapSnapshot) and back.model_dump() == snap.model_dump()
    node = {n.claim_id: n for n in back.nodes}
    c3 = node["c3"]
    assert (c3.status, c3.verdict, c3.turn, c3.side) == ("answered", "contradicted", 1, "opp")
    assert node["c3"].topic == "shoppers by car"
    assert back.turns[0] == {"idx": 0, "speaker": "A", "side": "prop", "claims": ["c1", "c2"], "closed": True,
                             "t_start": 100.0, "t_end": 105.0, "substantive": True}
    assert back.nonresponsive[0]["ignored"] == ["c1", "c2"] and back.nonresponsive[0]["final"] is True
    assert back.score["opp"].unverifiable == 1


# --------------------------------------------------------------------------- fixture debate
FIXTURE_CLAIMS = {   # utterance id -> [(claim id, text, type, importance, links)]
    "u-A-0001": [("c1", "Air pollution causes around 40,000 early deaths a year in the UK", "statistic", 1.0, []),
                 ("c2", "Road traffic is the biggest source of air pollution in UK cities", "fact", 0.67, [])],
    "u-A-0002": [("c3", "Footfall in Ghent rose after it restricted cars in 2017", "statistic", 1.0, [])],
    "u-B-0001": [("c4", "The 40,000 figure is a 2016 modelled estimate, not a death count", "fact", 0.67,
                  [("attacks", "c1")])],
    "u-B-0002": [("c5", "Banning cars would devastate small businesses", "prediction", 0.67, [])],
    "u-B-0003": [("c6", "Nearly 70 percent of high-street shoppers arrive by car", "statistic", 1.0,
                  [("supports", "c5")])],
    "u-A-0003": [("c7", "Pedestrians spend more per month in local shops than drivers", "comparison", 0.67,
                  [("attacks", "c5")])],
    "u-A-0004": [("c8", "London's ULEZ cut roadside NO2 by nearly half", "statistic", 1.0, [])],
    "u-B-0004": [("c9", "People should be free to choose how they travel", "value", 0.33, [])],
    "u-B-0005": [("c10", "Public transport in most UK cities is too unreliable to replace the car", "fact", 0.67, [])],
    "u-A-0005": [("c11", "Bus use outside London has fallen by a third since 2010", "statistic", 1.0, [])],
    "u-A-0006": [("c12", "The answer is to invest in buses", "value", 0.67, [("attacks", "c10")])],
    "u-B-0006": [("c13", "Electric cars will make the pollution argument irrelevant within ten years", "prediction",
                  0.67, [("attacks", "c8")])],
}


def test_fixture_debate_70_percent_point_goes_unanswered():
    m, notices_at = ArgMap(k_turns=1), {}
    for line in FIXTURE.read_text().splitlines():
        ev = parse_event(line)
        if ev.event == "SessionStart":
            m.reset(ev)
        elif ev.event == "Pause":
            m.on_pause(ev)
        elif ev.event == "Utterance":
            if ev.id == "u-B-0004":
                assert m.nodes["c6"].status == "open"                     # A's reply turn not closed yet
            m.on_utterance(ev)
            for cid, text, typ, imp, links in FIXTURE_CLAIMS.get(ev.id, []):
                m.add_claim(Claim(id=cid, utterance_id=ev.id, speaker=ev.speaker, text=text, type=typ, importance=imp,
                                  links=[Link(claim_id=d, relation=r) for r, d in links], t_end=ev.t_end))
            notices_at[ev.id] = m.drain_notices()
    closing = [n for n in notices_at["u-B-0004"] if n.kind == "unanswered"]
    assert [(n.claim_id, n.speaker) for n in closing] == [("c4", "A"), ("c6", "A")]   # A ignored both of B's points
    assert sum(n.claim_id == "c6" for ns in notices_at.values() for n in ns) == 1
    assert m.nodes["c6"].status == "unanswered" and m.nodes["c5"].status == "answered"
    opened = {(n.claim_id, n.speaker) for n in notices_at["u-A-0003"] if n.kind == "unanswered"}
    assert opened == {("c2", "B"), ("c3", "B")}
    assert m.nodes["c1"].status == "answered" and m.nodes["c9"].status == "minor"
    assert not m.nodes["c9"].claim.checkable                              # value claims are never checkable
    nr = [n for ns in notices_at.values() for n in ns if n.kind == "nonresponsive"]
    assert [(n.speaker, n.detail["turn"], n.detail["ignored"]) for n in nr] == [("B", 3, ["c7", "c8"])]
    snap = parse_event(m.snapshot().model_dump_json())
    assert isinstance(snap, ArgMapSnapshot) and len(snap.nodes) == 13 and len(snap.turns) == 6
    assert (snap.score["opp"].unanswered, snap.score["prop"].unanswered) == (2, 3)   # c4,c6 | c2,c3,c7


def test_session_names_without_sides_get_opposite_sides():
    from app.schemas import SessionStart
    m = ArgMap()
    m.reset(SessionStart(motion="m", speakers={"A": {"name": "Ali"}, "B": {"name": "Bo"}}))
    assert {m.side_of("A"), m.side_of("B")} == {"prop", "opp"}
    m.reset(SessionStart(motion="m", speakers={"X": {"name": "no side"}, "Y": {"side": "Proposition"}}))
    assert m.side_of("Y") == "prop" and m.side_of("X") == "opp"      # declared side wins, the other gets the free one
