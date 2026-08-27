#!/usr/bin/env node
/**
 * Regression tests for the prompt_*.js wrappers in skill-scan-eval/.
 *
 * These exist because of a real bug: the old JSON+`{{var}}` prompt format
 * (prompt_*.json + defaultTest.vars) silently sent literal, un-substituted
 * template text like "{{system_prompt_semantic_security}}" to the model for
 * one of the four prompts. The eval still reported 0 errors -- promptfoo has
 * no built-in check that a template var actually resolved to something other
 * than itself -- so the bug only surfaced when a human read the results in
 * `promptfoo view` and noticed the raw placeholder.
 *
 * Run: node tests/test_prompts.js
 * (plain Node + assert, no new npm dependency)
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');

// Every active prompt wrapper, paired with the source markdown it must load.
// Keeping this mapping explicit (not auto-discovered) means a prompt wired
// to the WRONG source file -- e.g. cisco_code_alignment accidentally loading
// skill_threat_analysis_prompt.md -- fails loudly instead of just producing
// plausible-looking but wrong results.
const PROMPTS = [
  {
    file: 'prompt_threat_analysis.js',
    sourceMd: '../prompts/skill_threat_analysis_prompt.md',
  },
  {
    file: 'prompt_code_alignment.js',
    sourceMd: '../prompts/code_alignment_threat_analysis_prompt.md',
  },
  {
    file: 'prompt_semantic_security_discovery.js',
    sourceMd: '../prompts/semantic_security_discovery_prompt.md',
  },
  {
    file: 'prompt_semantic_quality_policy.js',
    sourceMd: '../prompts/semantic_quality_policy_prompt.md',
  },
];

const FAKE_VARS = {
  skill_name: '__TEST_SKILL_NAME__',
  skill_payload: '__TEST_SKILL_PAYLOAD__',
};

let failures = 0;
let passed = 0;

// `fn` may be sync or async -- always await so a rejected promise is caught
// here instead of becoming an unhandled rejection the runner never sees.
async function check(description, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`ok - ${description}`);
  } catch (err) {
    failures += 1;
    console.error(`NOT OK - ${description}`);
    console.error(`  ${err.message}`);
  }
}

async function main() {
  for (const { file, sourceMd } of PROMPTS) {
    const modulePath = path.join(ROOT, file);
    const expectedContent = fs.readFileSync(path.join(ROOT, sourceMd), 'utf8');

    await check(`${file}: module loads without throwing (source file exists/readable)`, () => {
      delete require.cache[require.resolve(modulePath)];
      require(modulePath);
    });

    let messages;
    await check(`${file}: exports an async function returning an array of 2 messages`, async () => {
      delete require.cache[require.resolve(modulePath)];
      const fn = require(modulePath);
      assert.strictEqual(typeof fn, 'function', 'default export must be a function');
      messages = await fn({ vars: FAKE_VARS });
      assert.ok(Array.isArray(messages), 'must return an array');
      assert.strictEqual(messages.length, 2, 'must return exactly [system, user]');
    });

    if (!messages) continue; // above check already recorded the failure

    const [systemMsg, userMsg] = messages;

    await check(`${file}: system message has role "system" and non-trivial content`, () => {
      assert.strictEqual(systemMsg.role, 'system');
      assert.ok(
        typeof systemMsg.content === 'string' && systemMsg.content.length > 50,
        'system content must be a real prompt, not empty/tiny',
      );
    });

    await check(`${file}: system content matches ${sourceMd} exactly (correct file wired up)`, () => {
      assert.strictEqual(systemMsg.content, expectedContent);
    });

    await check(`${file}: system content has no unsubstituted {{...}} template markers`, () => {
      const match = systemMsg.content.match(/\{\{[^}]*\}\}/);
      assert.strictEqual(
        match,
        null,
        `found literal template placeholder in system content: ${match && match[0]}`,
      );
    });

    await check(`${file}: user message role is "user" and interpolates skill_name + skill_payload`, () => {
      assert.strictEqual(userMsg.role, 'user');
      assert.ok(
        userMsg.content.includes(FAKE_VARS.skill_name),
        'user content must contain the actual skill_name value',
      );
      assert.ok(
        userMsg.content.includes(FAKE_VARS.skill_payload),
        'user content must contain the actual skill_payload value',
      );
      const match = userMsg.content.match(/\{\{[^}]*\}\}/);
      assert.strictEqual(
        match,
        null,
        `found literal template placeholder in user content: ${match && match[0]}`,
      );
    });
  }

  console.log(`\n${passed} passed, ${failures} failed`);
  process.exit(failures > 0 ? 1 : 0);
}

main();
