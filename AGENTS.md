# Notes for coding agents

This repository is an archived research project: AI-designed crypto trading bots on OKX USDT perpetual futures, with the conclusion that no edge was found after costs. Read `README.md` and `docs/RESULTS.md` first.

## Safety rules

- Never place real orders. Do not run `one_trade.py --live`, the `*_LIVE*.cmd` launchers, the panel's Play or Stop button (`/api/play`, `/api/stop`), or the arena's Start, Stop or Resume (`/api/exchange/start`, `/stop`, `/resume`) against a real account. Real-money actions belong to the human who owns the account.
- Never read, print or commit exchange or API keys. They live only in local files that Git ignores (`LocalSettings/` and the local key files listed in `docs/QUICKSTART.md`, section Demo and Live).
- Test with fakes: the tests and `tools/preview_arena.py` use fake exchanges, synthetic markets and temporary folders.
- Paid LLM calls (Jev through OpenRouter) cost money and are capped by local ledgers in `runtime/`. Do not reset ledgers to get around a cap.

## Working here

- Python 3.12; `pip install -r requirements.txt` (numpy is needed by the evaluator).
- Tests: `py -3.12 -X utf8 -B -m unittest discover -s tests` (about 30 minutes on a laptop) and `node --test tests/*.cjs tests/v8_ui/*.test.js`.
- Market data is not in the repository. Download it with `tools/public_history.py` into `data/` (ignored by Git) and pass `--store` explicitly.
- Strategy recipes are JSON (`docs/RECIPE_SPEC.md`). Evaluate them with `tools/team_eval.py` and judge them with the gates in `docs/METHODOLOGY.md`. Register the hypothesis before the run and keep every attempt in a trial log.
- To run a multi-agent research team, use `skills/ai-research-team/`.
