const fs = require('fs');
const path = require('path');

const SYSTEM_PROMPT = fs.readFileSync(
  path.join(__dirname, '..', 'prompts', 'semantic_quality_policy_prompt.md'),
  'utf8',
);

module.exports = async ({ vars }) => [
  { role: 'system', content: SYSTEM_PROMPT },
  {
    role: 'user',
    content: `Analyze the following Agent Skill package (directory: ${vars.skill_name}):\n\n${vars.skill_payload}`,
  },
];
