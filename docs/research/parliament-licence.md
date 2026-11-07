# Parliamentary recording licence terms for the demo

Issue #8. Researched Sat 7 Nov 2026. This is a factual summary for a team decision, not legal advice. Only a licence holder, the Parliamentary Recording Unit (PRU) or a lawyer can confirm the items marked ambiguous.

## Answer

| Question | Answer | Confidence |
|---|---|---|
| Stream a PMQs clip with a fact-checking overlay in a public demo? | **Don't, without written PRU permission.** The published terms don't clearly allow it, and several clauses point against it. | High on what the terms say; the fit with our use is ambiguous |
| Are satire and comedy restricted? | **Yes.** The current clip terms ban satire, ridicule, denigration and entertainment use. | High |
| Reuse Hansard text for a transcript? | **Yes**, under the Open Parliament Licence (OPL), with attribution. | High, with the small gap noted below |
| Cite Full Fact checks as a benchmark? | **Yes, by naming and linking them and giving their conclusions in our own words.** Don't copy their articles, and don't imply they endorse us. | Medium: based on general copyright principles, as their terms don't address this |

**Recommendation:** drop the footage and use Hansard text for the PMQs segment. Either replay it as the `Utterance` stream, or have teammates read a passage into the mics in a neutral voice, labelled as read from Hansard. The second option also exercises speech-to-text. After the demo, link to the official parliamentlive.tv page for the sitting. Keep the footage only if the PRU says yes in writing.

## 1. Footage

**Two separate regimes apply.** Parliament's copyright page lists live and archive video and audio broadcasts as outside the OPL, so the OPL gives us no right to use the clip.

### Clips from parliamentlive.tv: Downloading and Sharing Terms

These are the terms we would accept when downloading a clip. Applied to our demo:

| Clause | What it says (paraphrased) | Effect on our demo |
|---|---|---|
| 1.1 | Material may be used only for a fair and accurate report of proceedings | **Ambiguous.** A fact-check might count as a report. A demo of our own product for a hackathon is harder to describe as one. |
| 1.2(a), (b) | No satire, ridicule or denigration; no entertainment content or productions | A dry referee is not satire, but a snarky persona would breach this. Whether a "personality" demo counts as entertainment is ambiguous. |
| 1.2(c), 1.5 | No promotion or publicity for commercial purposes or financial gain; no corporate, commercial or professional use without written consent | **Ambiguous.** A sponsored track with a prize and a product-style pitch could be read this way. |
| 1.4(c), 3.1(c) | Nothing that could bring Parliament or a Member into disrepute; no platform that lowers the dignity of either House or a Member | **Ambiguous, and a real risk.** An automated "contradicted" label on a named MP could be seen this way, especially if the verdict is wrong. |
| 2.1(a) | No editing, altering, adding to or manipulating, except selecting excerpts and correcting colour | **A burned-in overlay looks like a breach.** A dashboard beside an unaltered player is less clear; see the 2010 embedding terms below. |
| 2.1(b) | No reproducing image or sound out of context | A short clip streamed as if live needs care. Label it as recorded and don't cut mid-exchange. |
| 3.2 | Using a platform we don't control needs written confirmation that it will remove the material on request | Applies to a backup video or demo reel on YouTube or a submission site. |
| 5.2(d) | Nothing that suggests sponsorship | Avoid sponsor logos on the same screen as the clip. |
| General 3.1, 3.2 | Terms can change at any time; no transfer or sublicence without consent | Sending the audio to third-party speech-to-text or LLM providers is **ambiguous**. The terms don't address it directly. |
| General 2.1 | User indemnifies Parliament against third-party claims arising from breach | The risk sits with the team. |

The parliamentlive.tv help page also says recordings are for personal use only, which fits poorly with a public demo. It sits awkwardly with the clause-based terms and with Erskine May, which describes the public downloading and re-uploading clips. The PRU would have to resolve that.

### Other routes

- **PRU licence.** Parliament publishes separate broadcast and internet licences, applied for by emailing the PRU with an organisation name, registered address and details of intended use. The Commons internet licence copy I read took effect in 2011. It requires a watermark, bans use in light entertainment and political satire, and bans advertising or promotion. A secondary source says the current version dates from 2023, which I couldn't confirm. A student team may not fit this route, and I found no stated turnaround.
- **Embedding.** The help page says embedding is temporarily suspended, while another passage still describes embed codes. The 2010 embedding terms annexed to the licence forbid combining a recording with other material. They may be out of date, but they suggest a side-by-side layout isn't clearly safe.
- **Link only.** The help page says the YouTube stream can be linked to by any website. My reading is that sending people to the official page isn't a use of the material. I did not review YouTube's own terms.

### The satire question

The handoff was right to flag this. The current clip terms ban satire outright (1.2(a)), and the older licence annex bans political satire and light entertainment. Chortle reported in 2019 that the BBC had relaxed its position, quoting a BBC spokesman on changes in legislation. That concerns broadcasters' editorial guidelines, and I found nothing showing that the clip terms changed.

The legislation is probably CDPA s.30A, the fair dealing exception for caricature, parody or pastiche, which makes contract terms overriding it unenforceable. It doesn't help us, because we aren't making parody.

Other fair dealing exceptions exist in CDPA s.30: criticism or review, quotation, and reporting current events. Only the quotation exception has a contract override (s.30(4)). Whether any of them covers an old clip in a product demo needs legal advice, so we shouldn't plan around them.

**Keep the dry referee guardrail.** It also needs to apply to moderator interjections and to any demo script jokes about MPs.

## 2. Hansard

- **Allowed.** OPL v3.0 grants a worldwide, royalty-free, perpetual licence to copy, publish, adapt and exploit information, commercially or not, including inside our own product. Parliament's copyright page says use of Parliamentary material is governed by the OPL.
- **Required attribution**, shown in the dashboard and README, with a link to the licence where possible: "Contains Parliamentary information licensed under the Open Parliament Licence v3.0."
- **Limits.** Don't suggest official status or endorsement. Don't use the Crowned Portcullis or the Royal Arms. The licence ends automatically if its conditions are breached.
- **Ambiguous.** The OPL excludes personal data in the information. Whether that affects Members' names and speech in Hansard isn't addressed, and normal practice appears to treat them as reusable. Parliament could confirm.
- **Accuracy caveat.** Hansard describes itself as substantially verbatim, with repetitions and obvious mistakes removed. It is a good source for claims but an imperfect reference for speech-to-text. Don't report a word error rate against it as if it were an exact transcript. The rolling version appears within about three hours and the full record next morning, so use a completed sitting.
- **Voices.** The OPL covers text, not a Member's voice or likeness. Don't clone or imitate MPs. This is my own caution rather than a licence term.

## 3. Full Fact

- **Terms.** Their site says content is copyright Full Fact, all rights reserved unless otherwise stated, and to contact them for licensing. Their FAQ says the same for republication. I found no Creative Commons licence.
- **Reasonable use.** Name Full Fact, link each check, and state the conclusion in our own words. Facts and conclusions aren't protected the way their wording is, and short acknowledged quotes can fall under CDPA s.30(1ZA). This is my reading, not Full Fact's stated position.
- **Avoid.** Don't republish articles, bulk-copy them into the repo or use their logo. Don't say "validated by Full Fact"; say "compared by us against N published Full Fact checks, labelled by our team".
- **Scraping.** Don't bulk-scrape. A third-party site, freenewsapi.ai, offers Full Fact articles as an API, but it isn't Full Fact and gives us no rights.
- **API licence.** A Full Fact API and Content Syndication Licence appeared in search results, but the page and `/api/` now return 404. Don't rely on it.
- **ClaimReview.** Full Fact says it publishes this markup. Google's docs say Search support is being phased out while Fact Check Explorer still supports it. I did not review the Fact Check Tools API terms, which the planned cache would use.

Two benchmark-quality points for the Evidence role:

- Full Fact says many issues aren't black and white, so mapping their conclusions to our four labels needs human judgement. Record that as our own mapping.
- Exclude fullfact.org and matching Fact Check results from retrieval during the benchmark. Otherwise the agent is marked against the answers it just looked up.

## Open items

1. **PRU (only if the team wants footage):** email `pru@parliament.uk` or call 020 7219 5511. It's Saturday and judging is at midday tomorrow, so assume no reply in time. I didn't contact them. Suggested question: "May student hackathon participants show a roughly 1-minute unaltered PMQs clip, beside an automated fact-checking dashboard, to judges at a sponsored event on 8 Nov 2026, and may the audio be processed by third-party speech-to-text?"
2. **Live page check:** parliament.uk returned 403 to direct requests, so I read its pages through a search provider's copy, with no capture date. Someone should open the live terms in a browser to confirm clauses 1 and 2. They take about two minutes to read.
3. **Not checked:** the current 2023 licence text, which Full Fact PMQs checks exist, the event's own rules on third-party content, YouTube's terms, and defamation risk from labelling named MPs' claims. For that last one, the "unverifiable by default" and "show the source" guardrails help, but they are not legal clearance.

## Sources

Parliament (read via search provider copy, as noted above):

- [Downloading and Sharing Terms and Conditions](https://www.parliament.uk/site-information/copyright-parliament/pru-licence-agreements/downloading--sharing-terms--conditions/) (also [an archived PDF copy](https://openaccess.city.ac.uk/id/eprint/22069/2/parliament.uk-Downloading%20and%20Sharing%20Terms%20and%20Conditions.pdf))
- [Parliamentary Copyright](https://www.parliament.uk/site-information/copyright-parliament/)
- [Open Parliament Licence v3.0](https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/)
- [PRU licence agreements](https://www.parliament.uk/site-information/copyright-parliament/pru-licence-agreements/)
- [Licence for internet use of Commons proceedings](https://www.parliament.uk/globalassets/documents/pru/commonsinternet.pdf), including the rules, signals and embedding annexes
- [Parliamentlive.tv help](https://www.parliament.uk/site-information/using-this-website/help/parliamentlivetv-help/)
- [About Hansard](https://hansard.parliament.uk/about)
- [Erskine May: online publication](https://erskinemay.parliament.uk/section/16871/online-publication)

Legislation:

- [CDPA 1988 s.30](https://www.legislation.gov.uk/ukpga/1988/48/section/30)
- [CDPA 1988 s.30A](https://www.legislation.gov.uk/ukpga/1988/48/section/30A)

Full Fact and Google:

- [Full Fact terms and conditions](https://fullfact.org/terms-and-conditions/)
- [Full Fact FAQ](https://fullfact.org/about/frequently-asked-questions/)
- [Full Fact AI](https://fullfact.org/ai/)
- [Google: Fact check structured data](https://developers.google.com/search/docs/appearance/structured-data/factcheck)

Secondary, used only as leads or where stated:

- [Chortle, Nov 2019](https://www.chortle.co.uk/news/2019/11/08/44723/commons_footage_can_now_be_used_in_satire)
- [Parliament Connect guide](https://parliamentconnect.com/blog/post-parliamentary-footage-social-media-rules), a commercial blog and the source of the 2023 licence date
