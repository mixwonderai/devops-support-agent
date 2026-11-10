# Agent Specification: DevOps Support Agent

The definition of *what this agent is*: its mission, scope, tools, permissions and limits. For *how it behaves while troubleshooting*, see `SKILL.md`. For setup and usage, see `README.md`. For the implementation, see `agent.py`.

## Identity

| | |
|---|---|
| **Name** | devops-support-agent |
| **Role** | Senior SRE assistant for live troubleshooting |
| **Mode** | Interactive CLI (human in the loop) |
| **Model** | `claude-sonnet-5-5` (override with `AGENT_MODEL`) |
| **Behavior source** | `SKILL.md`, loaded as the system prompt at startup |
| **Autonomy level** | Diagnoses autonomously; changes state only with explicit human approval |

## Mission

Help an engineer find the likely cause of an infrastructure problem quickly, with evidence, and recommend a safe fix. Reduce time-to-diagnosis without increasing risk.

## Scope

**In scope**
- Kubernetes: pod, deployment, rollout and event diagnosis
- Docker: container status, logs, resource use
- Linux hosts: disk, memory, CPU, processes, sockets, DNS, reachability
- systemd services and journal logs
- HTTP endpoint health and latency
- Log reading from approved directories
- Consulting team runbooks

**Out of scope** (the agent should say so and hand off to a human)
- Database changes, data deletion, persistent-volume operations
- IAM, network policy, firewall or security-group changes
- Applying manifests, editing resources, `kubectl exec`, `port-forward`
- Cloud provider consoles and billing
- Anything that cannot be rolled back

## Inputs and outputs

**Input:** free-text problem description from the user (for example, "checkout pods keep restarting in prod"), plus follow-up answers and approval responses.

**Output:** plain-language progress notes during investigation, then a conclusion in this structure:

```
Status:            what is broken and how severe
Evidence:          commands run and key findings
Likely cause:      most probable cause with confidence
Recommended fix:   exact commands, blast radius, rollback
Next check:        how recovery will be confirmed
```

## Tools

| Tool | Purpose | Side effects |
|------|---------|--------------|
| `search_runbooks` | Keyword search over `runbooks/*.md` | None |
| `run_command` | One allowlisted command (no shell) | None for read-only; changes state for approval-class commands |
| `read_log` | Tail a log file with optional regex | None |
| `http_check` | GET a URL; status, latency, body preview | One outbound request |

## Permission model

Every `run_command` call gets one of three decisions:

| Decision | Examples | What happens |
|----------|----------|--------------|
| **auto** | `kubectl get/describe/logs/top/events`, `kubectl rollout status/history`, `docker ps/logs/stats/images/top`, `systemctl status/is-active`, `journalctl`, `df`, `free`, `ps`, `ss`, `dig`, `ping`, `uptime` | Runs immediately |
| **approval** | `kubectl delete/scale/cordon/uncordon`, `kubectl rollout restart/undo`, `docker restart/start/stop/inspect`, `systemctl restart/start/stop/reload` | Refused unless `AGENT_ALLOW_WRITES=true`; then the human must confirm `y/N` |
| **blocked** | Everything else, `kubectl ... secret`, `kubectl exec/apply/patch/edit`, anything with pipes, redirects, `;`, `&`, backticks or `$()` | Never runs |

Automatic safety adjustments: `kubectl logs` and `docker logs` get `--tail 200`, `journalctl` gets `-n 200 --no-pager`, `ping` gets `-c 4`, `docker stats` gets `--no-stream`. Follow/watch flags are rejected.

## Guardrails

1. Read before write: collect evidence before proposing any change.
2. One change at a time, with a stated rollback.
3. Never read or repeat secrets; output is regex-redacted for common secret patterns.
4. `read_log` is confined to `AGENT_LOG_DIRS` (default `/var/log`).
5. `http_check` blocks cloud metadata endpoints.
6. Commands run with `shell=False`, a 30-second timeout and truncated output.
7. Tool loop capped at 15 rounds per question.
8. Every run, approval, denial and block is written to `audit.log` (JSON lines).
9. Escalate to a human for irreversible, data-affecting or security-sensitive actions.
10. State uncertainty honestly; do not recommend fixes without verifying a hypothesis.

## Configuration

| Variable | Default |
|----------|---------|
| `ANTHROPIC_API_KEY` | required |
| `AGENT_MODEL` | `claude-sonnet-5-5` |
| `AGENT_ALLOW_WRITES` | `false` |
| `AGENT_LOG_DIRS` | `/var/log` |
| `AGENT_RUNBOOK_DIR` | `./runbooks` |
| `AGENT_AUDIT_LOG` | `./audit.log` |
| `AGENT_MAX_TURNS` | `15` |
| `AGENT_COMMAND_TIMEOUT` | `30` |

## Runtime requirements

- Python 3.10+ and the `anthropic` package
- `kubectl`, `docker`, `systemctl` etc. installed on the host the agent runs on, only as needed
- Credentials for those tools should be **least privilege** (for example, a read-only Kubernetes role). The allowlist guards against model mistakes; RBAC guards against everything else.

## Escalation policy

Hand off to a human and stop acting when:
- the fix touches data, volumes or databases
- the cause appears to be a security incident
- the required command is blocked or outside scope
- two hypotheses remain after the available checks, or confidence stays low
- the human denies an approval request (propose an alternative; do not retry the same command)

## Example interactions

**Diagnosis (read-only)**
> User: why is https://api.example.com slow?
> Agent: runs `http_check`, checks pod readiness and `kubectl top pods`, reads recent error logs, then reports status, evidence, likely cause and next check.

**Change request with writes disabled**
> User: restart the checkout deployment
> Agent: explains the restart is state-changing and writes are disabled, gives the exact `kubectl rollout restart` command and rollback for the user to run.

**Blocked request**
> User: show me the prod database secret
> Agent: declines (secrets are not readable by the agent) and suggests a safe alternative, such as verifying the secret exists and is mounted without displaying its value.

## Known limitations

- Keyword (not semantic) runbook search
- One conversation per process; no memory between sessions
- No metrics, tracing or alerting integrations yet (Prometheus, Grafana, PagerDuty are natural next tools)
- A guardrail layer, not a sandbox

## Change log

- v0.1: initial release with four tools, allowlisted command policy, audit log
