# Fact cards

Debaters' facts for the 5 Motions in `app/evidence/motions.json`, both sides. Print one card per side per Motion.
Every figure was checked against the linked page on Sat 7 Nov 2026.

> **Eval set:** `eval/verdict_gold.jsonl` must not share facts with these cards, otherwise the eval measures
> rehearsal and not the agent. The main session dedupes the eval set against this file.

**How to use a card**

- Say each fact as one sentence, with the number and the unit. One claim per sentence.
- **WRONG** facts are deliberately wrong. Use yours in your first turn, so the Moderator has time to correct it. The true figure is listed so we can check the agent got it right.
- **MISSING CONTEXT** facts are true but misleading. The ideal Verdict is `missing context`.
- Don't name private individuals. Keep it dry: the Moderator is the referee, not you.

---

## 1. This house would adopt a four-day working week

### Prop

1. "In the UK pilot, 60 per cent of employees found it easier to combine paid work with care responsibilities." [Autonomy report](https://autonomy.work/portfolio/uk4dwpilotresults/)
2. "In that pilot, 71 per cent of employees reported lower levels of burnout by the end." [Autonomy report](https://autonomy.work/portfolio/uk4dwpilotresults/)
3. "Staff leaving the pilot companies fell by 57 per cent compared with the same period the year before." [University of Cambridge, Sociology](https://www.sociology.cam.ac.uk/news/new-results-worlds-largest-trial-four-day-working-week)
4. "South Cambridgeshire council's four-day week trial kept 22 of its 24 performance measures the same or better." [University of Salford](https://www.salford.ac.uk/news/university-team-advises-council-on-four-day-working-week-benefits)
- **WRONG:** "Sick days in the UK pilot fell by 90 per cent."
  True: sick and personal days fell **65%**, from 2.0 to 0.7 per employee per month. [Autonomy report PDF, p.30](https://autonomy.work/wp-content/uploads/2023/02/The-results-are-in-The-UKs-four-day-week-pilot.pdf)
- **MISSING CONTEXT:** "The UK pilot was the biggest four-day week trial in the world."
  True when it reported in 2023, but it was 61 companies that chose to take part, about 2,900 workers, and two-thirds of the firms had 25 or fewer staff. [Autonomy report](https://autonomy.work/portfolio/uk4dwpilotresults/)

### Opp

1. "Two-thirds of the companies in the UK pilot had 25 or fewer employees." 66%. [Autonomy report PDF, p.18](https://autonomy.work/wp-content/uploads/2023/02/The-results-are-in-The-UKs-four-day-week-pilot.pdf)
2. "The pilot's own report says it can't show that the falls in sick days and resignations are statistically significant." [Autonomy report PDF, p.30](https://autonomy.work/wp-content/uploads/2023/02/The-results-are-in-The-UKs-four-day-week-pilot.pdf)
3. "The NHS in England had 100,165 vacancies at the end of December 2025, a vacancy rate of 6.7 per cent." [NHS England Digital](https://digital.nhs.uk/data-and-information/publications/statistical/nhs-vacancies-survey/april-2015---december-2025-experimental-statistics)
4. "In South Cambridgeshire's trial, rent collection and the time taken to re-let council homes both got worse." From the COVID-adjusted analysis. [Salford and Cambridge KPI evaluation](https://www.scambs.gov.uk/media/ncanup4n/report-final.pdf)
- **WRONG:** "The NHS in England has about a quarter of a million vacancies."
  True: **100,165** (6.7%) at 31 December 2025. [NHS England Digital](https://digital.nhs.uk/data-and-information/publications/statistical/nhs-vacancies-survey/april-2015---december-2025-experimental-statistics)
- **MISSING CONTEXT:** "The researchers who studied South Cambridgeshire's trial say they can't prove the four-day week caused any change."
  True, but the same evaluation found 22 of 24 measures stayed the same or improved. The caveat applies to the good results and the bad ones alike. [Salford and Cambridge KPI evaluation](https://www.scambs.gov.uk/media/ncanup4n/report-final.pdf)

---

## 2. This house would ban smartphones in UK schools

### Prop

1. "Across OECD countries, about 30 per cent of students say they get distracted by digital devices in most or every maths lesson." [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)
2. "Students distracted like that in most or every maths lesson scored 15 points lower in maths, after accounting for socio-economic background." [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)
3. "By the end of 2024, 79 education systems, 40 per cent of them, banned smartphones in school, up from 60 a year earlier." [UNESCO GEM Report](https://www.unesco.org/en/articles/smartphones-school-only-when-they-clearly-support-learning)
4. "Government guidance telling schools in England to prohibit phones throughout the school day became statutory in June 2026, for schools to follow from September." [GOV.UK, DfE, Mobile phones in schools](https://www.gov.uk/government/publications/mobile-phones-in-schools)
- **WRONG:** "Three-quarters of the world's countries already ban smartphones in schools."
  True: **79 education systems, or 40%**, at the end of 2024. [UNESCO GEM Report](https://www.unesco.org/en/articles/smartphones-school-only-when-they-clearly-support-learning)
- **MISSING CONTEXT:** "The OECD found students are less likely to be distracted in schools that ban phones."
  True, but the same report says more research is needed on whether bans work, and that bans are often not enforced. [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)

### Opp

1. "PISA found that students who used devices for leisure at school for up to an hour a day scored 20 points higher in maths than students who used none." 10 points after accounting for background. [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-12.html)
2. "Students who used devices for learning at school for up to an hour a day scored 25 points higher in maths than those who used none." 14 points after accounting for background. [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-12.html)
3. "In some countries, PISA found students at schools that ban phones were less likely to switch off notifications at bedtime." [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)
4. "In schools that ban phones, 21 per cent of students still said they used one at school every day or almost every day." OECD average. [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)
- **WRONG:** "PISA found being distracted by phones in class makes no measurable difference to maths scores."
  True: students distracted in most or every lesson scored **15 points lower**. [OECD, PISA 2022 Vol. II](https://www.oecd.org/en/publications/pisa-2022-results-volume-ii_a97db61c-en/full-report/component-14.html)
- **MISSING CONTEXT:** "Saudi Arabia has scrapped its school phone ban."
  True, but it was reversed after disability groups objected on medical grounds. In the same year, 19 more education systems brought bans in. [UNESCO GEM Report](https://www.unesco.org/en/articles/smartphones-school-only-when-they-clearly-support-learning)

---

## 3. This house would abolish the BBC licence fee

### Prop

1. "Only 45 per cent of 16 to 24-year-olds watched broadcast TV in an average week in 2024, down from 48 per cent." [Ofcom, Media Nations 2025](https://www.ofcom.org.uk/media-use-and-attitudes/media-habits-adults/tuning-into-youtube-uks-media-habits-revealed)
2. "Licences in force fell from 25.3 million in 2020-21 to 23.8 million in 2024-25." 25.28m to 23.79m. [TV Licence Fee Trust Statement 2024-25](https://assets.publishing.service.gov.uk/media/687679e2a8d0255f9fe28ef3/Final_Television_Licence_Fee_Trust_Statement_2024_25.pdf)
3. "In the year to June 2024, 73 per cent of TV licence evasion prosecutions in England and Wales were of women." [UK Parliament, written answer HL6009](https://questions-statements.parliament.uk/written-questions/detail/2025-03-20/HL6009/)
4. "In 2021, TV licence evasion made up 18 per cent of all prosecutions of women." [Explanatory memorandum, SI 2024/150](https://www.legislation.gov.uk/uksi/2024/150/pdfs/uksipes_20240150_en_001.pdf)
- **WRONG:** "Over 90 per cent of people prosecuted for licence evasion are women."
  True: about **73–75%**. [UK Parliament, HL6009](https://questions-statements.parliament.uk/written-questions/detail/2025-03-20/HL6009/)
- **MISSING CONTEXT:** "Nearly 30,000 people a year are still prosecuted for not having a TV licence."
  True (28,542 in the year to June 2024, England and Wales), but that is down from 81,788 four years earlier. [UK Parliament, HL6009](https://questions-statements.parliament.uk/written-questions/detail/2025-03-20/HL6009/)

### Opp

1. "94 per cent of UK adults used the BBC each month last year." [GOV.UK, DCMS](https://www.gov.uk/government/news/cost-of-tv-licence-fee-set-for-202627)
2. "The BBC reached a weekly audience of 453 million people worldwide in 2024-25." [BBC Annual Report 2024/25](https://www.bbc.co.uk/aboutthebbc/documents/bbc-annual-report-and-accounts-24-25.pdf)
3. "In 2024-25, 825,000 free licences went to over-75s." [TV Licence Fee Trust Statement 2024-25](https://assets.publishing.service.gov.uk/media/687679e2a8d0255f9fe28ef3/Final_Television_Licence_Fee_Trust_Statement_2024_25.pdf)
4. "Licence evasion prosecutions in England and Wales fell from 81,788 to 28,542 in four years." Years to June 2020 and June 2024. [UK Parliament, HL6009](https://questions-statements.parliament.uk/written-questions/detail/2025-03-20/HL6009/)
- **WRONG:** "More than two million over-75s get a free TV licence."
  True: **825,000** free over-75 licences in 2024-25. [TV Licence Fee Trust Statement 2024-25](https://assets.publishing.service.gov.uk/media/687679e2a8d0255f9fe28ef3/Final_Television_Licence_Fee_Trust_Statement_2024_25.pdf)
- **MISSING CONTEXT:** "Only 0.18 per cent of people who need a licence are ever prosecuted for evasion."
  True, but around three-quarters of those prosecuted are women. The BBC's own review puts the gap down to household make-up, who answers the door, and financial hardship. [BBC, Gender disparity review](https://www.bbc.co.uk/aboutthebbc/documents/gender-disparity-review.pdf)

---

## 4. This house would make public transport free

### Prop

1. "Cars and taxis produced 54 per cent of the UK's domestic transport emissions in 2023. Buses and coaches produced 2 per cent." 60.2 and 2.2 of 111.8 MtCO2e. [DfT, GHG emissions from transport 2023](https://www.gov.uk/government/statistics/transport-and-environment-statistics-2023--2/greenhouse-gas-emissions-from-transport-in-2023)
2. "Domestic transport was the UK's largest emitting sector in 2023, at 29 per cent of greenhouse gas emissions." [DESNZ, 2023 final figures](https://assets.publishing.service.gov.uk/media/67a30e4f7da1f1ac64e5feb1/2023-final-greenhouse-gas-emissions-statistical-release.pdf)
3. "22 per cent of households in England had no car in 2024, rising to about 40 per cent in the lowest income fifth." [DfT, National Travel Survey 2024](https://www.gov.uk/government/statistics/national-travel-survey-2024/nts-2024-household-car-availability-and-trends-in-car-trips)
4. "Local bus fares in England rose 7 per cent in the year to March 2025, while CPI inflation was 3 per cent." [DfT, Annual bus statistics](https://www.gov.uk/government/statistics/annual-bus-statistics-year-ending-march-2025/annual-bus-statistics-year-ending-march-2025)
- **WRONG:** "Transport produces half of the UK's greenhouse gas emissions."
  True: **29%** in 2023. [DESNZ](https://assets.publishing.service.gov.uk/media/67a30e4f7da1f1ac64e5feb1/2023-final-greenhouse-gas-emissions-statistical-release.pdf)
- **MISSING CONTEXT:** "The government already pays nearly half the cost of running the railway."
  True: £11.9bn, 46% of operating income in 2024-25. But fares paid almost as much (£11.5bn), so free rail would roughly double that bill. [ORR](https://www.orr.gov.uk/search-news/increasing-fares-revenue-contributes-reduction-government-subsidy-railways)

### Opp

1. "Rail passengers in Great Britain paid £11.5 billion in fares in the year to March 2025." [ORR, Rail industry finance](https://dataportal.orr.gov.uk/statistics/finance/rail-industry-finance)
2. "Germany set aside €2.5 billion of federal money to cover just three months of its €9-a-month ticket in 2022." [Regionalisierungsgesetz §8](https://www.gesetze-im-internet.de/regg/__8.html)
3. "Taxpayers put £21.6 billion into the railways in 2024-25, HS2 included." [ORR](https://www.orr.gov.uk/search-news/increasing-fares-revenue-contributes-reduction-government-subsidy-railways)
4. "Public transport was the main way of getting about on only 9 per cent of trips in England in 2024." [DfT, NTS 2024: public transport](https://www.gov.uk/government/statistics/national-travel-survey-2024/nts-2024-trends-in-public-transport-trips)
- **WRONG:** "Rail fares only bring in about £2 billion a year, so they barely matter."
  True: **£11.5 billion** in 2024-25. [ORR](https://dataportal.orr.gov.uk/statistics/finance/rail-industry-finance)
- **MISSING CONTEXT:** "Only 2 per cent of trips in England are made by rail."
  True, but rail covers 11% of the distance people travel, because its trips are long. [DfT, NTS 2024 factsheet](https://www.gov.uk/government/statistics/national-travel-survey-2024/nts-2024-factsheet)

---

## 5. This house would ban cars from city centres (holdout)

### Prop

1. "Five of the UK's 43 air quality zones broke the legal annual limit for nitrogen dioxide in 2024, including the West Midlands, Greater Manchester and Bristol urban areas." [Defra, Air pollution in the UK 2024](https://www.gov.uk/government/publications/air-pollution-in-the-uk-2024/air-pollution-in-the-uk-2024-compliance-assessment-summary)
2. "At the roadside, about two-thirds of nitrogen oxides, 67 per cent in 2024, came from road transport." [Defra, Emissions of air pollutants: NOx](https://www.gov.uk/government/statistics/emissions-of-air-pollutants/emissions-of-air-pollutants-in-the-uk-nitrogen-oxides-nox)
3. "The only monitoring station in England above the 2040 fine-particle target in 2024 was London Marylebone Road, a roadside site in central London." [Defra, Air pollution in the UK 2024](https://www.gov.uk/government/publications/air-pollution-in-the-uk-2024/air-pollution-in-the-uk-2024-compliance-assessment-summary)
4. "A quarter of trips in England are under a mile, and 70 per cent are under five miles." 2024. [DfT, National Travel Survey 2024](https://www.gov.uk/government/statistics/national-travel-survey-2024/nts-2024-mode-share-and-multi-modal-trips)
- **WRONG:** "More than half a million people were hurt on Britain's roads in 2024."
  True: **128,272** reported casualties of all severities in Great Britain. [DfT, Reported road casualties 2024](https://www.gov.uk/government/statistics/reported-road-casualties-great-britain-annual-report-2024/reported-road-casualties-great-britain-annual-report-2024)
- **MISSING CONTEXT:** "London's ULEZ cut nitrogen dioxide in central London by 46 per cent."
  It's 46% below a modelled no-ULEZ scenario, and the ULEZ charges older vehicles rather than banning cars. [GLA, Inner London ULEZ one-year report](https://www.london.gov.uk/programmes-strategies/environment-and-climate-change/environment-and-climate-change-publications/inner-london-ultra-low-emission-zone-expansion-one-year-report)

### Opp

1. "3.07 million Blue Badges for disabled parking were held in England in March 2025, up 8 per cent in a year." [DfT, Blue Badge statistics 2024-25](https://www.gov.uk/government/statistics/blue-badge-scheme-statistics-april-2024-to-march-2025/blue-badge-scheme-statistics-april-2024-to-march-2025)
2. "Cars carried people on 59 per cent of trips in England in 2024, and 76 per cent of the distance travelled." [DfT, National Travel Survey 2024](https://www.gov.uk/government/statistics/national-travel-survey-2024/nts-2024-household-car-availability-and-trends-in-car-trips)
3. "Nitrogen oxide emissions from cars and taxis have fallen 87 per cent since 1990." To 2023. [DfT, Transport and environment statistics 2023](https://www.gov.uk/government/statistics/transport-and-environment-statistics-2023--2/domestic-air-pollution-emissions-from-transport-in-2023)
4. "Road deaths in Great Britain fell to 1,602 in 2024, the lowest on record outside the pandemic years." [DfT, Reported road casualties 2024](https://www.gov.uk/government/statistics/reported-road-casualties-great-britain-annual-report-2024/reported-road-casualties-great-britain-annual-report-2024)
- **WRONG:** "One in five people in England holds a Blue Badge."
  True: **5.2%**, about one in twenty. [DfT, Blue Badge statistics](https://www.gov.uk/government/statistics/blue-badge-scheme-statistics-april-2024-to-march-2025/blue-badge-scheme-statistics-april-2024-to-march-2025)
- **MISSING CONTEXT:** "Road transport is only 30 per cent of the UK's nitrogen oxide emissions."
  True nationally (2024), but at the roadside, where city-centre exposure happens, road transport is about 67% of NOx. [Defra, NOx](https://www.gov.uk/government/statistics/emissions-of-air-pollutants/emissions-of-air-pollutants-in-the-uk-nitrogen-oxides-nox)
