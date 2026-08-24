# Deploying the skill-scanner eval to EC2 (public read-only viewing)

Goal: rerun the scanner's threat-analysis eval on EC2 and serve promptfoo's
built-in web UI so **anyone with the URL** can see the scan results — this is
a public-viewing deployment, not the locked-down "team only" setup this doc
originally described.

This runs the "normal path" scanner eval (`skill-scan-eval/promptfooconfig.yaml`)
through promptfoo. It is separate from `red-teaming/`, which runs adversarial
probes against a skill's instructions rather than reviewing normal scans.

> Updated from the original `skill-scanner-llm/DEPLOY_TO_AWS.md`. Changes verified
> against the currently installed `promptfoo@0.122.0` on 2026-08-24:
> - `promptfoo view` **no longer has a `--host` flag** — it binds to all
>   interfaces (`*:<port>`) by default now, so no flag is needed for remote
>   access.
> - `requirements.txt` pins `litellm==1.89.0`, which has no wheel for
>   Python 3.14. Use **Python 3.10–3.12** on the instance.
> - Repo layout changed: the eval now lives at `skill-scan-eval/` in this repo,
>   and it expects a sibling `prompts/` directory one level up
>   (`../prompts/*.md`) — both must be cloned/present together.

## 1. Provision the EC2 instance

- Any small instance works (`t3.small` is plenty — this just runs Node +
  Python, no GPU needed).
- Security group: since the goal is public viewing, open your serving port
  (default `15500`) to `0.0.0.0/0`.
  - **Tradeoff**: the local/OSS promptfoo UI has no authentication and no
    per-user identity (see §6). Public access means anyone with the URL can
    also click pass/fail and leave comments, not just view. If you only want
    read access to be public, put a reverse proxy (Caddy/nginx) in front that
    serves the UI over HTTPS but blocks POST/PATCH routes, or simply accept
    that ratings are best-effort/unauthenticated for this deployment.
- Requires Node.js (for `npx promptfoo`) and Python 3.10–3.12.

## 2. Set up the repo on the instance

```bash
git clone <your-repo-url> ah-skills && cd ah-skills/skill-scan-eval

# Amazon Linux 2023 / Ubuntu both ship python3.12 or python3.11 by default —
# check with `python3 --version`; if it's 3.13+ install 3.12 explicitly
# (litellm's pin doesn't support 3.13/3.14 yet):
#   Amazon Linux: sudo dnf install -y python3.12
#   Ubuntu:       sudo apt install -y python3.12 python3.12-venv
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pyyaml

# Node (for npx promptfoo) — install via nvm or your distro's package if not present
node --version || curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs
```

## 3. Generate test cases and run the eval

```bash
cd skill-scan-eval   # if not already there
python gen_promptfoo_tests.py       # rebuild test cases from skills/

export ANTHROPIC_API_KEY=sk-ant-api0...   # real API key (sk-ant-api...), NOT a Claude Code oauth token (sk-ant-oat...)
npx promptfoo@latest eval -c promptfooconfig.yaml
```

The current config runs **3 prompts × every skill under `skills/`**
(`skill_threat_analysis_prompt.md`, `skill_meta_analysis_prompt.md`,
`code_alignment_threat_analysis_prompt.md`, all loaded from `../prompts/`),
so make sure that sibling directory came along with the clone.

Re-run `gen_promptfoo_tests.py` any time you add/change a skill under
`skills/` (each needs a `SKILL.md`, and optionally `_expected.json`), then
re-run `promptfoo eval` to (re)populate results. Each eval run is kept in
history rather than overwritten — nothing to clean up between runs.

### Rerunning periodically

To "rerun all scans" on a schedule (e.g. nightly, or whenever skills are
added), add a cron entry or systemd timer that does steps above non-interactively:

```bash
# /opt/skill-scan-eval/rerun.sh
#!/usr/bin/env bash
set -euo pipefail
cd /opt/ah-skills/skill-scan-eval
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-api0...
python gen_promptfoo_tests.py
npx promptfoo@latest eval -c promptfooconfig.yaml
```

```cron
# crontab -e
0 3 * * * /opt/skill-scan-eval/rerun.sh >> /var/log/skill-scan-eval.log 2>&1
```

The running `promptfoo view` process (§4) picks up new eval runs
automatically — no restart needed.

## 4. Serve the review UI (persistently)

For a one-off foreground test:

```bash
npx promptfoo@latest view --port 15500 -n
```

For production, run it under systemd so it survives reboots/disconnects and
auto-restarts:

```ini
# /etc/systemd/system/skill-scan-eval-view.service
[Unit]
Description=promptfoo view server for skill-scan-eval
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/ah-skills/skill-scan-eval
ExecStart=/usr/bin/npx promptfoo@latest view --port 15500 -n
Restart=on-failure
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now skill-scan-eval-view
```

Share `http://<ec2-public-ip>:15500` — anyone with the URL can view results.
Everyone hits the same running process and local SQLite database
(`~/.promptfoo/promptfoo.db`), so it's multi-viewer for free — no accounts
needed (and no login wall, per the public-access goal above).

## 5. Reviewing

Each row is one skill scan, one column per prompt (threat / meta / code
alignment). Click a cell to see the full findings, then use promptfoo's
built-in pass/fail toggle and comment box.

**Attribution:** the local/OSS promptfoo UI does not track *who* clicked
pass/fail — it's single-tenant by design, and doubly so now that the
instance is public. **Every reviewer should prefix their comment with their
name or initials**, e.g.:

```
[jane] looks correct, matches expected severity
[amir] disagree — this should be HIGH not MEDIUM, script writes outside skill dir
```

This is a convention, not enforced by the tool.

## 6. Known limitations / future upgrades

- No per-user identity on ratings (see above) — comment prefixes are the
  workaround for now.
- No TLS/auth on the `view` server itself. Since this deployment intentionally
  serves the UI publicly for viewing, at minimum put it behind HTTPS (Caddy
  with automatic Let's Encrypt certs is the least-effort option) so the URL
  isn't plaintext HTTP; if you later decide ratings need to be protected from
  vandalism while keeping viewing open, split it into a read-only reverse
  proxy in front of the raw promptfoo port.
- If real per-user attribution becomes a hard requirement, options are:
  promptfoo's cloud/teams tier (adds accounts, no longer fully local), or
  switching this eval to the CLI-based `eval_runner.py` (already in the
  upstream `skill-scanner-llm` repo) with a `--reviewer` flag, which stamps a
  name into a feedback file per verdict — trades the browser UI for
  guaranteed attribution.
- `~/.promptfoo/promptfoo.db` accumulates **every** eval ever run on the box,
  including unrelated ones if this instance is reused for other promptfoo
  projects. Keep this EC2 instance dedicated to this eval, or periodically
  prune old eval IDs with `promptfoo delete <id>` / `sqlite3` if reused.
