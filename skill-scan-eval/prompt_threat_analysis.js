const fs = require('fs');
const path = require('path');

// Reads the system prompt directly from disk instead of threading it through
// a test `var` -- keeps large prompt text out of the promptfoo results table
// (vars become columns in `promptfoo view`; this isn't a variable, it's fixed
// per-prompt content).
const SYSTEM_PROMPT = fs.readFileSync(
  path.join(__dirname, '..', 'prompts', 'skill_threat_analysis_prompt.md'),
  'utf8',
);

module.exports = async ({ vars }) => [
  { role: 'system', content: SYSTEM_PROMPT },
  {
    role: 'user',
    content: `Analyze the following Agent Skill package (directory: ${vars.skill_name}):\n\n${vars.skill_payload}`,
  },
];
