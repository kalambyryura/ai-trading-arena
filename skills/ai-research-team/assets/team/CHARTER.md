# Charter

Owned by the lead; the coordinator commits it. Every change gets a line in the change log with the decision that made it. Owner decisions (G1, G2, …) change only with the owner's consent.

Version 0.1 · {{TODAY}} · created from the ai-research-team template. Replace every `<…>`.

## 1. Why we are here

<What the project is, what is already known, and what this stage must decide. Two or three paragraphs.>

What is known so far (label every line):

- [VERIFIED] <fact> — <evidence: path, commit, command>
- [ASSUMPTION] <assumption> — <how and when it will be checked>
- [OPEN] <question> — <who answers it>

## 2. Owner decisions

The lead interviews the owner before the team starts. Ask with concrete scenarios and a named example, recommended option first, two or three options, several questions batched in one message. Record the owner's words verbatim, with the place (which chat) and the clock time: the quote is the authority.

| # | Decision | Owner's words, place, time |
|---|---|---|
| G1 | Goal of this stage: <what must be found out; what counts as success and what as an honest "no"> | "<quote>" (<chat>, <YYYY-MM-DD HH:MM TZ>) |
| G2 | Scope: <domains, instruments, venues, data sources in scope; what is out> | |
| G3 | Money at risk: <total>; per run <…>; loss limit that stops everything <…> | |
| G4 | Paid-API budget: total <USD>; per author <USD>; provider-side credit limit set by the owner <USD> | |
| G5 | Time box and stop rule: <date or condition at which the owner decides to go on or stop> | |
| G6 | What the lead decides alone; what needs the owner | |
| G7 | Live or production policy: <minimum size, a "test" label, who installs, which runs count> | |
| G8 | Publication and sharing: <what may be published, by whom, after what check> | |
| G9 | Data partitions and cutoffs (section 4) | |
| G10 | Usage limits: <subscription, weekly limits, which roles run on which model> | |

Questions that must have an answer before day 1:

- Which actions with real-world effect exist in this project (orders, transfers, deployments, emails, publications), and which buttons or commands trigger them? They all go to the owner-only list in PROTOCOL.md and into the guard config.
- Which keys exist, with which scopes? Readers get read-only keys; executors the owner runs get trade-only keys, never withdrawal.
- Where is the development data cutoff, and what is held out? Fix it now, before anyone looks at recent data.
- What happens when the weekly usage limit runs out mid-run? Who supervises live runs then?

## 3. Statuses and gates

| Status | Meaning | Set by |
|---|---|---|
| DRAFT | idea under research | author |
| CANDIDATE | passed gate K on development data | author submits, coordinator reproduces |
| TEST-LIVE | candidate run by the owner at minimum size, labelled "test" | owner |
| PROVEN | passed gate D on data the author never saw | judge |
| CHAMPION | proven, and the cumulative live result is positive over <≥ 4> owner runs of <≥ 24 h> | coordinator, from live data |

Costs apply everywhere: <fees, slippage, spread, funding or carry; the equivalent costs in your domain>. Stress: <an extra cost per action, e.g. +5 bps per fill>. Benchmark: <cash, or the simplest baseline>.

**Gate K (candidate), on development data, with the official evaluator. Every check must pass:**

| # | Check | Threshold |
|---|---|---|
| K1 | Form | expressible in the shared format; passes validate and lint |
| K2 | Whole development set | net > 0 and net under stress > 0 |
| K3 | Size and worst period | ≥ <100> independent outcomes (e.g. closed trades); no calendar year below <−10 % of capital> |
| K4 | Modern epoch | net > 0 and stress > 0; ≥ <40> outcomes; ≥ <60 %> of active quarters positive |
| K5 | Recent tail, looked at once | ≥ <15> outcomes and net > 0 |
| K6 | Old epoch | under stress not below <−5 % of capital> |
| K7 | Not a few lucky outcomes | without the <10> best outcomes the result is ≥ 0 |
| K8 | Paper trail | a PREREG committed before the runs and a complete TRIALS log with the number of configurations tried |

**Gate D (proven), run only by the judge, on sealed and forward data:**

| # | Check | Threshold |
|---|---|---|
| D1 | Submission | frozen recipe identified by sha256; at most <2> submissions per author in <14> days |
| D2 | Result | official evaluator, same costs and stress: net > 0 and stress > 0 |
| D3 | Sample | declared N ≥ <20>; the verdict uses the first N outcomes of the sample and is final for that sha256; INSUFFICIENT until then |
| D4 | Concentration and risk | largest outcome ≤ <30 %> of gross profit; max drawdown ≤ <10 %> of capital |
| D5 | Live | ≥ <2> owner runs of <≥ 24 h> without execution anomalies, actual costs no worse than the model by more than <3 bps> |
| D6 | Reporting | PASS / FAIL / INSUFFICIENT plus aggregates only |

Thresholds are the lead's initial values. The judge may tighten them before the first submission; after that they are fixed for the season. Only the judge changes gates; authors propose. If a participant's new version fails, its previous best version stays.

## 4. Data partitions

Fix the cutoffs on day 0, before any author looks at recent data.

| Set | Period | Who sees it | Used for |
|---|---|---|---|
| DEV | <start> to <cutoff> | everyone | research, gate K |
| SEALED | <cutoff> to <team start> | platform (loads it and records hashes only) and judge | gate D |
| FORWARD | after <team start> | judge; authors only through sanitized live packets | gate D, monitoring |
| LIVE | the owner's real runs | everyone, as sanitized packets | calibration and fixes |

Inside DEV: old epoch <…>, modern epoch <…>, tail <…> (looked at once per cycle).

## 5. Competition and sharing

- Authors compete, and the team wins when all of them are honest. Verified findings, negative results included, go to `exchange/<author>.md` for everyone.
- Nobody opens another author's drafts or branch before that recipe is a CANDIDATE.
- Each direction is tagged "family × information source × instrument × horizon". At most two authors per cell; the second explains how its mechanism differs.
- Clone rule: a new candidate is a clone if more than <50 %> of its entries coincide with an existing candidate's (within <±30 min>) or the daily result correlation is above <0.8>, computed both ways.
- Hybrids: allowed after one's own line failed, with `derived_from`, a new PREREG, at most <12> configurations, one decisive look and one's own information or mechanism.

## 6. Paid APIs, execution limits, participants

- Paid model calls go only through the research harness: a ledger per role, a hard cap of <USD> per author, unknown billing blocks every new call, prompts anonymised, answers recorded for replay. Paid mode is enabled only for request specs the coordinator approved after an independent review. The real limit is the provider-side credit limit the owner sets.
- Execution limits: <maximum position size, loss per run, leverage, instruments, isolated accounts>.
- Models never own risk: deterministic code checks every model output. An error, a timeout or an invalid answer means HOLD and no new action.

| Participant | Kind (AI session, human, external model) | Model as declared | First version |
|---|---|---|---|
| <name> | <kind> | <model> | <recipe or none> |

## 7. Lessons

Inherited from the reference team (the skill's references/lessons.md); keep the ones that apply and add your own with evidence:

1. Usage limits run out: put the coordinator on a mid-tier model, give routine work to helpers, end turns when nothing useful is left, keep state in files.
2. Supervision of live runs by AI sessions failed repeatedly: timers did not fire or died with session restarts. Use a plain program for alerts and a human who reacts.
3. Estimated timestamps forced corrections: read the clock in the same command.
4. A PREREG committed after the run proves nothing: commit first.
5. Too many roles too early produce private engines and idle sessions: start small.

## Change log

| Version | Date (clock) | Decision | What changed |
|---|---|---|---|
| 0.1 | {{TODAY}} | — | created from the template |
