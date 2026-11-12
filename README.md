# DevOps Support Agent

A command-line troubleshooting agent powered by Claude. Describe a problem ("checkout pods keep restarting"), and it investigates with read-only tools, shows its evidence, proposes a fix, and only changes anything after you approve.

## What's in the box

```
devops-support-agent/
├── SKILL.md          # The agent's playbook: workflow, safety rules, triage guides, output format
├── agent.py          # Tool-use loop + tools + safety gate
├── requirements.txt  # Python dependencies
├── .env.example      # Configuration template
├── runbooks/         # Your team's runbooks (markdown), searched by the agent
│   └── disk-full.md
└── README.md
```

`SKILL.md` is loaded as the system prompt, so you change the agent's behavior by editing that file, not the code.

## Quick start

```bash
cd devops-support-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit it
export $(grep -v '^#' .env | xargs)   # or use direnv / your shell of choice

python agent.py                                  # interactive
python agent.py --once "is https://example.com healthy?"
```

Requires Python 3.10+ and an Anthropic API key. The tools shell out to `kubectl`, `docker`, `systemctl`, etc., so run the agent on a machine where those are installed and authenticated (for example a bastion host or your laptop with the right kube-context).

## Tools the agent has

| Tool | What it does |
|------|--------------|
| `run_command` | Runs one allowlisted command (kubectl, docker, systemctl, journalctl, df, free, ps, ss, dig, ping, uptime, ...) |
| `read_log` | Tails a log file with an optional regex filter, limited to `AGENT_LOG_DIRS` |
| `http_check` | GET a URL, report status, latency, body preview |
| `search_runbooks` | Keyword search over `runbooks/*.md` |

## Safety model

The agent is designed so a wrong guess can't take down production.

1. **Allowlist, not blocklist.** Any command not explicitly listed is blocked.
2. **Three decisions per command:** `auto` (read-only, runs now), `approval` (state-changing, asks you `y/N`), `blocked`.
3. **Writes are off by default.** Even approval-class commands are refused unless `AGENT_ALLOW_WRITES=true`. In that mode the agent hands you the command to run yourself.
4. **No shell.** Commands run with `shell=False`; pipes, redirects, `;`, `&`, backticks and `$()` are rejected.
5. **Secrets.** `kubectl ... secret` is blocked, `docker inspect` needs approval (it can leak env vars), and output is regex-redacted for common secret patterns.
6. **Bounded output.** Logs default to `--tail 200`, follow/watch flags are rejected, commands time out after 30s, output is truncated.
7. **Path and URL guards.** `read_log` can't leave the allowed directories; `http_check` blocks cloud metadata endpoints.
8. **Audit trail.** Every run, approval, denial and block is appended to `audit.log` as JSON lines.

This is a guardrail layer, not a sandbox. For production use, also run the agent with a **least-privilege identity**: a read-only Kubernetes RBAC role and a service account that cannot touch what the agent shouldn't. The allowlist protects against model mistakes; RBAC protects against everything else.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (required) | API key |
| `AGENT_MODEL` | `claude-sonnet-5-5` | Model ID |
| `AGENT_ALLOW_WRITES` | `false` | Permit approval-gated state changes |
| `AGENT_LOG_DIRS` | `/var/log` | Colon-separated directories `read_log` may read |
| `AGENT_RUNBOOK_DIR` | `./runbooks` | Where runbooks live |
| `AGENT_AUDIT_LOG` | `./audit.log` | Audit file path |
| `AGENT_MAX_TURNS` | `15` | Max tool rounds per question |
| `AGENT_COMMAND_TIMEOUT` | `30` | Seconds before a command is killed |

## Example session

```
you> checkout pods in prod keep restarting

  $ kubectl get pods -n prod -l app=checkout
  $ kubectl describe pod checkout-7d9f-abc12 -n prod
  $ kubectl logs checkout-7d9f-abc12 -n prod --previous --tail 200

**Status:** checkout is crash-looping, 14 restarts in 40 min
**Evidence:** describe shows OOMKilled (exit 137); previous logs show heap growth after cache warmup
**Likely cause:** memory limit (512Mi) too low for current traffic, ~75% confidence
**Recommended fix:** raise the limit to 1Gi via your normal deploy flow; rollback: kubectl rollout undo deploy/checkout -n prod
**Next check:** watch restart count for 15 minutes
```

## Extending it

- **Add runbooks:** drop markdown files into `runbooks/`. Use clear titles and symptom keywords ("502", "OOMKilled", "disk full") because search is keyword-based.
- **Add a command:** extend the tables near the top of `agent.py` (`SIMPLE_READ`, `KUBECTL_READ`, ...) or add a branch in `classify()`. Default new commands to `approval`, then promote to `auto` once you trust them.
- **Add a tool:** write a `tool_xxx` function, add it to `TOOL_IMPLS`, and describe it in `TOOLS`. Ideas: Prometheus/Grafana queries, PagerDuty lookups, `terraform plan`, CI/CD status, cloud CLI read calls.
- **Change behavior or tone:** edit `SKILL.md`. Add playbooks for your stack in the "Quick triage playbooks" section.
- **Slack or web front end:** `run_turn(client, system, messages)` is the whole conversation engine. Wrap it in a Slack bolt handler or a FastAPI route, keep one `messages` list per thread, and replace the `input()` approval prompt with a button.

## Using SKILL.md as a Claude skill

`SKILL.md` follows the standard skill format (YAML frontmatter with `name` and `description`), so you can also zip the folder as a skill for Claude.ai or Claude Code. In that mode Claude uses its own tools instead of `agent.py`.

## Testing it safely

Start with `AGENT_ALLOW_WRITES=false` against a staging cluster and try:

1. `why is my pod crash looping?` (should read, not change)
2. `restart the checkout deployment` (should be refused, with the command given to you)
3. `show me the secrets in prod` (should be blocked)
4. `cat /etc/passwd | grep root` (should be rejected for the pipe)

Then check `audit.log` to confirm everything was recorded.

## Limitations

- Keyword runbook search, not semantic; swap in embeddings if your runbook set grows large.
- One conversation per process; no persistence between runs.
- The agent diagnoses and suggests. It is not an auto-remediation system, and it shouldn't become one without strong rollback and review.

