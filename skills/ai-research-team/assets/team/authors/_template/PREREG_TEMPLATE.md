# PREREG <id> · cycle <cycle> · <direction in a few words>

Status: REGISTERED <YYYY-MM-DD HH:MM TZ, read from the clock in the command that writes this line>
Label: <"indicative, author's own engine" or "official evaluator">
Author: <name> · branch `team/<name>` · cycle opened by D<nnn>
Diversity cell: <family> × <information source> × <instrument> × <horizon>

<!-- Copy this file to team/authors/<name>/PREREG/<id>_<slug>.md. Then: append the TRIALS "prereg" line
(sha256 of this file, engine and code sha256, the config list, "seen before"), commit both in their own
commit with explicit paths, push your branch, tell the coordinator, and only then run.
Git history proves the order; self-written timestamps do not. Never edit this file after the runs start:
a change is a new PREREG with a new id. -->

## 1. Where the idea comes from, and what I have already seen

- Source: <own idea / another author's finding in exchange/<name>.md / literature>; `derived_from`: <none or candidate id>.
- Seen before: <every look at the data this idea touches, including descriptive looks; "tail not opened" or when it was opened>.

## 2. Mechanism

- Why it should work, and why it should survive a change of market regime: <…>
- Why it can beat the costs: expected gross move per outcome <…> against the round-trip cost <…> (rule of thumb: median gross move ≥ 2× round-trip cost).
- What refutes it: <the result that would close this line>.

## 3. Exact definitions

- Signal: <formula, inputs, closed bars only>
- Entry: <order type, timing, price>
- Exit: <stop, target, time limit, condition>
- Sizing: <…>
- Costs and stress: <fees, slippage, spread, funding; stress = +<x> per action>
- Data: <instrument, span, store and its manifest sha256>

## 4. Grid

| Config | Parameters |
|---|---|
| cfg-01 | <…> |

Count: <n> configurations (cycle budget: <≤ 24>). Every one gets a TRIALS line.

## 5. Stage 1: selection

- Selection span: <start → end, e.g. up to the day before the tail>
- Qualification criteria, fixed now: <…>
- Tie-break: <…>
- If nothing qualifies: the line closes with "no candidate".

## 6. Stage 2: one look

- One run of the selected config on the full development set and its tail (<start → end>); the code writes a look marker and refuses a second run.
- Pass rule, fixed now: <…>
- If the tail fails: the hypothesis closes. It is not retuned on the tail.

## 7. Controls

- Long and short separately.
- Random entries with the same sides and holding times, in the same regime.
- Placebo: the feature shifted in time by <…>.
- Neighbours: the share of adjacent configs that also qualify.
- Gross move per outcome against the round-trip cost.

Controls computed after the runs are `descriptive` lines and are reported as not affecting the selection.

## 8. Gates

How gate K applies (CHARTER section 3), and any adapted check proposed to the judge before the runs: <…>

## 9. Budget and resources

- Configurations: <n> of <budget>. Paid calls: <none, or spec sha256 and USD within the author cap>.
- Heavy runs: <how many, booked through the coordinator>. Memory and time estimate: <…>.
