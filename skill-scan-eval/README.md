# skill-scan-eval

Human-review eval harness for the Agent Skill security scanner prompts. Runs
each scanner prompt against a fixed set of real (and intentionally-malicious)
skill packages under `skills/`, across several LLMs via OpenRouter, and lets
reviewers compare verdicts side by side in promptfoo's web UI.

For how to provision a box and serve the results publicly, see
`docs/DEPLOY_TO_AWS.md` — this file only covers what the pieces are and how
to extend them.

## Current status (as of 2026-08-27)

- **4 active prompts** × **3 providers** × **11 skill fixtures** = 132
  (prompt, model, skill) combinations per full eval.
  - Prompts: `cisco_threat_analysis`, `cisco_code_alignment`,
    `nvidia_semantic_security_discovery`, `nvidia_semantic_quality_policy`
    (see "Prompt labels are prefixed by origin" below).
  - Providers, all via OpenRouter: `google/gemini-3.7-flash`,
    `openai/gpt-5.4-mini`, `deepseek/deepseek-v3.2`.
- Last full run: `eval-cSm-2026-08-27T01:42:15` — 132/132 passed, 0 errors.
  ("Passed" here means the API call completed without erroring — see
  the assertions caveat under Known limitations below, not that a verdict
  was judged correct.)
- `npx promptfoo view` is running and serving this data on port 15500. It is
  **not yet publicly reachable** — the security group on the box hosting it
  hasn't been opened for that port (see `docs/DEPLOY_TO_AWS.md` §1). Check
  with whoever's running it before assuming there's a shareable URL.
- All 34 checks in `tests/` (24 JS + 10 Python) pass against the current
  `promptfooconfig.yaml` and `prompt_*.js` wrappers.

## How the pieces fit together

```
skills/<name>/SKILL.md, scripts/...   <- the packages being scanned (test fixtures)
        |
        v  scanner.build_skill_payload()  (concatenates SKILL.md + scripts into one blob)
        |
gen_promptfoo_tests.py  --------->  promptfoo_tests.yaml   (one test case per skill)
                                     promptfoo_payloads/*.txt (one payload file per skill)
        |
promptfooconfig.yaml  ties together:
  - prompts:    prompt_<name>.js   (loads its system prompt from disk, see below)
  - providers:  openrouter:<vendor>/<model>
        |
        v  npx promptfoo eval -c promptfooconfig.yaml
        |
promptfoo-results/  +  ~/.promptfoo/promptfoo.db  (eval history, viewed via `promptfoo view`)
```

The actual scanner prompt text lives one level up, in the sibling `../prompts/`
directory (shared with anything else in this repo that wants to reuse a
prompt) — `skill-scan-eval/` itself only holds the promptfoo plumbing: the
`prompt_<name>.js` wrappers, the generated test cases/payloads, and the eval
config.

Each `prompt_<name>.js` is intentionally minimal — it reads its system prompt
straight from `../prompts/` at load time and builds the two-message array
directly in JS, no templating involved:

```js
const fs = require('fs');
const path = require('path');

const SYSTEM_PROMPT = fs.readFileSync(
  path.join(__dirname, '..', 'prompts', '<name>_prompt.md'),
  'utf8',
);

module.exports = async ({ vars }) => [
  { role: 'system', content: SYSTEM_PROMPT },
  {
    role: 'user',
    content: `Analyze the following Agent Skill package (directory: ${vars.skill_name}):\n\n${vars.skill_payload}`,
  },
];
```

**Why a `.js` function instead of a `prompt_<name>.json` + `{{var}}`
template** (which is how this used to work): a `defaultTest.vars` entry like
`system_prompt_x: file://../prompts/x_prompt.md` makes the whole prompt file
show up as a column in `promptfoo view`, and — worse — a promptfoo template
var that fails to substitute for any reason renders as the literal
`{{system_prompt_x}}` string sent straight to the model. That happened once
in practice: the eval still reported 0 errors (promptfoo has no way to know a
var was supposed to resolve to something else), and it only surfaced when a
human read the raw output in `promptfoo view`. Reading the file directly in
JS at module-load time removes the whole class of bug: if the file is
missing or misnamed, `require()` throws immediately instead of silently
degrading. `tests/test_prompts.js` (see Testing below) checks for this on
every prompt so this doesn't have to be caught by eye again.

`gen_promptfoo_tests.py` doesn't know or care how many prompts/providers
exist — it only builds `{skill_name, skill_payload}` test cases from
`skills/`. Every prompt × every provider gets run against every test case;
wiring which prompts/providers run is entirely promptfooconfig.yaml's job.

## Prompt labels are prefixed by origin

Every entry in `promptfooconfig.yaml`'s `prompts:` list has a `label` like
`cisco_threat_analysis` or `nvidia_semantic_security_discovery` — prefixed
with the project each prompt came from (Cisco's skill-scanner, NVIDIA's
SkillSpector) so results in `promptfoo view` are unambiguous about
provenance once there are prompts from more than one source. Keep this
prefix when adding a prompt from a new source; `tests/test_config.py` enforces
it's present.

## Adding a new scanner prompt

1. Drop the prompt markdown in `../prompts/<name>_prompt.md` (or point at
   wherever it already lives).
2. Add `prompt_<name>.js` in this directory, following the pattern above.
3. In `promptfooconfig.yaml`, add an entry under `prompts:` pointing at the
   new wrapper, with a label prefixed by its originating project (see above).
4. Add the new file to the `PROMPTS` list at the top of
   `tests/test_prompts.js` (source-of-truth mapping from wrapper -> expected
   markdown file), then run the tests (below) before running a real eval.
5. Re-run `python gen_promptfoo_tests.py` (only needed if `skills/` also
   changed) and `npx promptfoo eval -c promptfooconfig.yaml`.

No changes to `gen_promptfoo_tests.py` or `scanner.py` are needed just to add
a prompt — those only change if the *test fixtures* (`skills/`) or the raw
payload-building logic changes.

### A category of prompt that doesn't fit this eval yet

Some scanner prompts (e.g. `skill_meta_analysis_prompt.md` in `../prompts/`,
or SkillSpector's `meta_analyzer.md`) aren't independent detectors — they're
**triage layers that consume another analyzer's findings** (true/false
positive judgment, severity adjustment, correlation) rather than reading the
raw skill package themselves. Running one of these standalone against just
`{skill_name, skill_payload}`, with no findings to triage, only exercises a
fraction of what the prompt is designed to do and produces results that
aren't representative. They're deliberately left out of `promptfooconfig.yaml`
until there's a prior scan pass whose findings can be fed in as real input —
adding one back is the same 5 steps above, plus a findings payload sourced
from an actual scan run instead of a static file.

## Testing

Two independent checks, covering the config/wiring layer rather than model
output quality (that's what the eval itself, plus human review in
`promptfoo view`, is for):

```bash
# Prompt wrappers: each one loads, produces [system, user] messages, the
# system content matches its expected source file exactly, and neither
# message has a leftover {{...}} placeholder. Plain Node, no new npm dep.
node tests/test_prompts.js

# promptfooconfig.yaml structure: every prompt/tests file:// reference
# resolves, every provider goes through openrouter:, labels are unique and
# vendor-prefixed, and defaultTest.vars hasn't grown a system_prompt_* entry
# again. Also regenerates promptfoo_tests.yaml and checks it has one test
# case per skill with exactly {skill_name, skill_payload} vars and a real,
# non-empty payload file. Needs the .venv from docs/DEPLOY_TO_AWS.md (pyyaml
# + litellm, since gen_promptfoo_tests.py imports scanner.py).
source .venv/bin/activate
python -m unittest discover -s tests
```

Run both before trusting a new eval run, and especially before/after touching
`promptfooconfig.yaml` or any `prompt_*.js` file.

## Rerunning after adding a skill or a prompt

```bash
python gen_promptfoo_tests.py            # only if skills/ changed
npx promptfoo eval -c promptfooconfig.yaml
npx promptfoo view                        # picks up the new run automatically if already serving
```

Use `npx promptfoo eval -c promptfooconfig.yaml --filter-first-n 1` to
sanity-check a config/prompt change against just one skill (all prompts ×
all providers, one skill) before spending tokens on the full `skills/` set.

## Known limitations / possible next improvements

- **No ground-truth assertions.** `promptfoo_tests.yaml` test cases have no
  `assert:` block, so "132 passed" only means every API call returned
  without erroring — not that any verdict was judged correct. One skill
  (`skills/safe-skills/simple-math/_expected.json`) already has an expected-
  verdict file sitting unused; wiring assertions against files like that
  (or a rubric an LLM grader checks) would let promptfoo grade objectively
  instead of relying entirely on a human reading `promptfoo view`.
- **The two NVIDIA semantic prompts are adapted, not verbatim.** SkillSpector
  designed them for per-file scanning (`{file_label}`/`{numbered_content}`
  fed by its own chunking pipeline); `../prompts/semantic_*_prompt.md` here
  keep the analysis rules but run once against a whole-skill blob like the
  Cisco prompts do, dropping the per-file harness wiring. Worth validating
  this doesn't lose real detection fidelity versus true per-file scanning,
  or building an actual per-file mode if it does.
- **`skill_meta_analysis_prompt.md` and `meta_analyzer.md` are still
  unwired**, waiting on a prior scan pass whose findings they can triage
  (see "A category of prompt that doesn't fit this eval yet" above).
- **Model pins will drift.** The 3 provider IDs were verified live against
  `https://openrouter.ai/api/v1/models` on 2026-08-27; OpenRouter renames/
  bumps model slugs over time (this repo already moved past at least one
  Gemini Flash generation). Re-check the catalog periodically rather than
  assuming these IDs stay valid indefinitely.
- **Tests aren't wired into CI** — `node tests/test_prompts.js` and
  `python -m unittest discover -s tests` are both run by hand today.
- Two harmless leftover files, `prompt_semantic_security_discovery.json.bak`
  and `prompt_semantic_quality_policy.json.bak`, are the old (buggy)
  JSON-template wrappers kept around per request rather than deleted. Safe
  to remove once nobody needs to diff against them.
