# Current prompt/model selection

**Prompt:** [`skill_threat_analysis_prompt.md`](../prompts/skill_threat_analysis_prompt.md)
(the Cisco skill-scanner standard threat-analysis prompt — `cisco_threat_analysis`
in `promptfooconfig.yaml`).

**Model:** `deepseek/deepseek-v3.2` (via OpenRouter) — best quality/price tradeoff
of the 3 models currently in the eval.

This is the pairing `scanner.py`'s standalone CLI and the production scan path
should point at today. Revisit it whenever the eval below is rerun — a model
or prompt swap upstream (OpenRouter renames/deprecates model slugs regularly)
can change the answer.

## Where this comes from

Both picks come out of the promptfoo eval in this directory
(`promptfooconfig.yaml` + `promptfoo_tests.yaml`), not a one-off judgment call:

- **Prompt**: of the 4 prompts evaluated (`cisco_threat_analysis`,
  `cisco_code_alignment`, `nvidia_semantic_security_discovery`,
  `nvidia_semantic_quality_policy`), `cisco_threat_analysis` is the primary
  full-package verdict (findings + severity + overall assessment) the other
  three either narrow (code-alignment mismatches, semantic-intent attacks) or
  don't address safety at all (`nvidia_semantic_quality_policy` is a
  doc-quality linter, not a threat detector — see `README.md`).
- **Model**: of the 3 providers (`google/gemini-3.7-flash`,
  `openai/gpt-5.4-mini`, `deepseek/deepseek-v3.2`), the ground-truth-graded
  results in `promptfoo view` are the quality signal, and each provider's
  OpenRouter per-token price is the other half. As of the 2026-08-27 run:
  `gpt-5.4-mini` edged `deepseek-v3.2` slightly on the small graded set
  (10/12 vs 9/12 correct against `_expected.json`), but `deepseek-v3.2` is
  meaningfully cheaper per token on OpenRouter, which is why it's the current
  pick — re-check both numbers before trusting this, they move.
  `gemini-3.7-flash` is excluded from consideration right now: its raw
  output gets truncated mid-response on this eval a meaningful fraction of
  the time (see README "Known limitations"), which is a reliability problem
  independent of price.
- Cost isn't currently tracked automatically inside this repo's promptfoo
  runs (the `cost` field comes back `$0` for all 3 OpenRouter providers in
  local runs) — check OpenRouter's own pricing
  (`https://openrouter.ai/api/v1/models` or the OpenRouter dashboard) for
  current $/token rather than trusting a number baked into this file.

Only 4 of the 11 fixture skills have ground truth (`_expected.json`) wired up
today, so "quality" above is a small, partial signal — see README's "Known
limitations" for which skills still need real labels.

## How to update this selection

```bash
cd skill-scan-eval
source .venv/bin/activate          # see docs/DEPLOY_TO_AWS.md for first-time setup
python gen_promptfoo_tests.py      # only if skills/ changed
npx promptfoo@latest eval -c promptfooconfig.yaml
npx promptfoo@latest view          # inspect pass/fail per (prompt, model, skill) cell
```

Each labeled skill's cell shows PASS/FAIL against `_expected.json` (via
`assert_expected_verdict.js`) — click a cell for the `reason` string. Compare
pass rates across prompts and models, check current OpenRouter pricing for
the models involved, then update the **Prompt**/**Model** lines at the top of
this file to match. See `README.md` for the full harness layout and
`docs/DEPLOY_TO_AWS.md` for running this on a shared box others can view.
