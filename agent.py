#!/usr/bin/env python3
"""
DevOps Support Agent

An interactive troubleshooting agent built on the Anthropic API (tool use).
Behavior is defined in SKILL.md; this file supplies the tools and the safety gate.

Usage:
    python agent.py                      # interactive chat
    python agent.py --once "why is checkout down?"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import anthropic

# --------------------------------------------------------------------------- #
# Configuration (override with environment variables, see .env.example)
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "15"))  # tool-loop cap per question
ALLOW_WRITES = os.getenv("AGENT_ALLOW_WRITES", "false").lower() == "true"
LOG_DIRS = [Path(p).resolve() for p in os.getenv("AGENT_LOG_DIRS", "/var/log").split(":") if p]
RUNBOOK_DIR = Path(os.getenv("AGENT_RUNBOOK_DIR", BASE_DIR / "runbooks"))
AUDIT_LOG = Path(os.getenv("AGENT_AUDIT_LOG", BASE_DIR / "audit.log"))
COMMAND_TIMEOUT = int(os.getenv("AGENT_COMMAND_TIMEOUT", "30"))
MAX_OUTPUT_CHARS = 8000

# --------------------------------------------------------------------------- #
# Command policy: read-only commands run automatically, state-changing ones need
# human approval (and AGENT_ALLOW_WRITES=true), everything else is blocked.
# --------------------------------------------------------------------------- #
SHELL_META = re.compile(r"[;&|<>`\n]|\$\(")
FOLLOW_FLAGS = {"-f", "--follow", "-w", "--watch"}  # would block until timeout

SIMPLE_READ = {
    "df", "free", "uptime", "ps", "ss", "netstat", "ping", "nslookup", "dig",
    "hostname", "date", "uname", "nproc", "lsblk", "whoami",
}
KUBECTL_READ = {"get", "describe", "logs", "top", "events", "version", "cluster-info"}
KUBECTL_WRITE = {"delete", "scale", "cordon", "uncordon"}
KUBECTL_VALUE_FLAGS = {
    "-n", "--namespace", "--context", "-l", "--selector", "-c", "--container",
    "-o", "--output", "--tail", "--since",
}
DOCKER_READ = {"ps", "logs", "stats", "images", "top"}
DOCKER_WRITE = {"restart", "start", "stop", "inspect"}  # inspect can leak env vars
SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units"}
SYSTEMCTL_WRITE = {"restart", "start", "stop", "reload"}

SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[a-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
]


def redact(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n... [truncated {len(text) - limit} chars] ...\n" + text[-limit // 2 :]


def kubectl_positionals(args: list[str]) -> list[str]:
    """Return non-flag tokens, skipping values of flags like -n or --context."""
    out, skip = [], False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if tok in KUBECTL_VALUE_FLAGS:
                skip = True
            continue
        out.append(tok)
    return out


def ensure_flag(argv: list[str], flag: str, value: str | None, after: int) -> list[str]:
    """Insert a safety flag (e.g. --tail 200) right after position `after` if absent."""
    if any(a == flag or a.startswith(flag + "=") for a in argv):
        return argv
    extra = [flag] + ([value] if value else [])
    return argv[:after] + extra + argv[after:]


def classify(argv: list[str]) -> tuple[str, str, list[str]]:
    """Return (decision, reason, argv). decision in {auto, approval, blocked}."""
    if not argv:
        return "blocked", "empty command", argv
    cmd, rest = argv[0], argv[1:]

    if any(a in FOLLOW_FLAGS for a in rest):
        return "blocked", "follow/watch flags would hang; use --tail or a bounded query", argv

    if cmd in SIMPLE_READ:
        if cmd == "ping":
            argv = ensure_flag(argv, "-c", "4", 1)
        return "auto", "", argv

    if cmd == "kubectl":
        pos = kubectl_positionals(rest)
        verb = pos[0] if pos else ""
        if verb in KUBECTL_READ:
            if any("secret" in a.lower() for a in rest):
                return "blocked", "reading Kubernetes secrets is not allowed", argv
            if verb == "logs":
                argv = ensure_flag(argv, "--tail", "200", 2)
            return "auto", "", argv
        if verb == "rollout":
            sub = pos[1] if len(pos) > 1 else ""
            if sub in {"status", "history"}:
                return "auto", "", argv
            if sub in {"restart", "undo"}:
                return "approval", "rollout change", argv
        if verb in KUBECTL_WRITE:
            return "approval", f"kubectl {verb} changes cluster state", argv
        return "blocked", f"kubectl '{verb}' is not in the allowlist", argv

    if cmd == "docker":
        verb = rest[0] if rest else ""
        if verb in DOCKER_READ:
            if verb == "logs":
                argv = ensure_flag(argv, "--tail", "200", 2)
            if verb == "stats":
                argv = ensure_flag(argv, "--no-stream", None, 2)
            return "auto", "", argv
        if verb in DOCKER_WRITE:
            return "approval", f"docker {verb}", argv
        return "blocked", f"docker '{verb}' is not in the allowlist", argv

    if cmd == "systemctl":
        verb = rest[0] if rest else ""
        if verb in SYSTEMCTL_READ:
            return "auto", "", argv
        if verb in SYSTEMCTL_WRITE:
            return "approval", f"systemctl {verb}", argv
        return "blocked", f"systemctl '{verb}' is not in the allowlist", argv

    if cmd == "journalctl":
        argv = ensure_flag(argv, "--no-pager", None, 1)
        if not any(a in {"-n", "--lines"} or a.startswith("--lines=") for a in argv):
            argv = ensure_flag(argv, "-n", "200", 1)
        return "auto", "", argv

    return "blocked", f"'{cmd}' is not in the allowlist", argv


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def audit(event: str, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    try:
        with AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # auditing must never crash the agent


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def tool_run_command(command: str, reason: str = "") -> str:
    if SHELL_META.search(command):
        audit("blocked", command=command, why="shell metacharacters")
        return "BLOCKED: pipes, redirects, ';', '&', backticks and $() are not allowed. Run one simple command."
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"BLOCKED: could not parse command ({exc})"

    decision, why, argv = classify(argv)
    shown = " ".join(shlex.quote(a) for a in argv)

    if decision == "blocked":
        audit("blocked", command=shown, why=why)
        return f"BLOCKED: {why}."

    if decision == "approval":
        if not ALLOW_WRITES:
            audit("write_disabled", command=shown, why=why)
            return (
                f"REFUSED: '{shown}' changes state ({why}) and write actions are disabled "
                "(AGENT_ALLOW_WRITES=false). Give the user the command to run themselves."
            )
        print(f"\n  Approval needed: {shown}\n  Reason: {reason or 'n/a'}  ({why})")
        answer = input("  Run it? [y/N] ").strip().lower()
        if answer != "y":
            audit("denied", command=shown, reason=reason)
            return "DENIED by the human operator. Do not retry; propose an alternative or ask what they prefer."
        audit("approved", command=shown, reason=reason)

    print(f"  $ {shown}")
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, shell=False
        )
    except FileNotFoundError:
        return f"ERROR: '{argv[0]}' is not installed on this machine."
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {COMMAND_TIMEOUT}s."

    audit("ran", command=shown, exit_code=proc.returncode, reason=reason)
    out = (proc.stdout or "") + (f"\n[stderr]\n{proc.stderr}" if proc.stderr else "")
    return truncate(redact(out.strip() or "(no output)")) + f"\n[exit code {proc.returncode}]"


def tool_read_log(path: str, lines: int = 100, grep: str | None = None) -> str:
    try:
        target = Path(path).resolve()
    except OSError as exc:
        return f"ERROR: {exc}"
    if not any(target == d or d in target.parents for d in LOG_DIRS):
        allowed = ", ".join(str(d) for d in LOG_DIRS)
        return f"BLOCKED: only files under {allowed} can be read."
    if not target.is_file():
        return f"ERROR: {target} is not a file."

    lines = max(1, min(int(lines), 500))
    try:
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
    except re.error as exc:
        return f"ERROR: invalid regex ({exc})"

    buf: deque[str] = deque(maxlen=lines)
    try:
        with target.open("r", errors="replace") as fh:
            for line in fh:
                if pattern is None or pattern.search(line):
                    buf.append(line.rstrip("\n"))
    except OSError as exc:
        return f"ERROR: {exc}"

    audit("read_log", path=str(target), lines=lines, grep=grep)
    return truncate(redact("\n".join(buf))) or "(no matching lines)"


def tool_http_check(url: str, timeout: int = 10) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "ERROR: only http(s) URLs are supported."
    if parsed.hostname in {"169.254.169.254", "metadata.google.internal"}:
        return "BLOCKED: cloud metadata endpoints are not allowed."

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "devops-support-agent"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=min(int(timeout), 30)) as resp:
            elapsed = (time.monotonic() - start) * 1000
            body = resp.read(500).decode("utf-8", errors="replace")
            result = f"HTTP {resp.status} in {elapsed:.0f} ms\nServer: {resp.headers.get('Server', '?')}\nBody preview: {body[:300]!r}"
    except urllib.error.HTTPError as exc:
        elapsed = (time.monotonic() - start) * 1000
        result = f"HTTP {exc.code} {exc.reason} in {elapsed:.0f} ms"
    except Exception as exc:  # noqa: BLE001 - report any network failure to the model
        result = f"FAILED: {type(exc).__name__}: {exc}"
    audit("http_check", url=url)
    return result


def tool_search_runbooks(query: str) -> str:
    if not RUNBOOK_DIR.is_dir():
        return "No runbooks directory found."
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    if not terms:
        return "Provide at least one keyword."
    scored = []
    for path in RUNBOOK_DIR.glob("**/*.md"):
        text = path.read_text(errors="replace")
        score = sum(text.lower().count(t) for t in terms)
        if score:
            scored.append((score, path, text))
    if not scored:
        return "No matching runbooks."
    scored.sort(key=lambda s: s[0], reverse=True)
    parts = [f"### {p.name}\n{truncate(t, 1500)}" for _, p, t in scored[:3]]
    return "\n\n".join(parts)


TOOL_IMPLS = {
    "run_command": lambda a: tool_run_command(a["command"], a.get("reason", "")),
    "read_log": lambda a: tool_read_log(a["path"], a.get("lines", 100), a.get("grep")),
    "http_check": lambda a: tool_http_check(a["url"], a.get("timeout", 10)),
    "search_runbooks": lambda a: tool_search_runbooks(a["query"]),
}

TOOLS = [
    {
        "name": "run_command",
        "description": (
            "Run one simple diagnostic command (kubectl, docker, systemctl, journalctl, df, free, "
            "ps, ss, dig, ping, uptime, ...). No pipes or redirects. Read-only commands run "
            "immediately; state-changing ones require human approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line, e.g. 'kubectl get pods -n prod'"},
                "reason": {"type": "string", "description": "Short reason, shown to the human approver"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_log",
        "description": "Tail a log file, optionally keeping only lines matching a regex. Limited to allowed log directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "lines": {"type": "integer", "description": "Max lines to return (default 100, max 500)"},
                "grep": {"type": "string", "description": "Optional case-insensitive regex filter"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "http_check",
        "description": "GET a URL and report status code, latency and a short body preview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds (default 10, max 30)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_runbooks",
        "description": "Keyword-search the team's runbooks for known procedures matching a symptom.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #
def load_system_prompt() -> str:
    """Use SKILL.md (minus YAML frontmatter) as the system prompt."""
    text = (BASE_DIR / "SKILL.md").read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    runtime = (
        f"\n\n## Runtime context\n"
        f"- Current UTC time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}\n"
        f"- Write actions enabled: {ALLOW_WRITES}\n"
        f"- Readable log directories: {', '.join(str(d) for d in LOG_DIRS)}\n"
    )
    return text.strip() + runtime


def run_turn(client: anthropic.Anthropic, system: str, messages: list[dict]) -> None:
    """Run the model until it stops requesting tools (or the turn cap is hit)."""
    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL, max_tokens=2048, system=system, tools=TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\n{block.text}\n")

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            impl = TOOL_IMPLS.get(block.name)
            try:
                output = impl(block.input) if impl else f"ERROR: unknown tool {block.name}"
            except Exception as exc:  # noqa: BLE001 - never crash the loop on a tool error
                output = f"ERROR: {type(exc).__name__}: {exc}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

    print(f"\n[stopped after {MAX_TURNS} tool rounds; ask a follow-up to continue]\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="DevOps Support Agent")
    parser.add_argument("--once", metavar="QUESTION", help="answer one question and exit")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first (see .env.example).", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    system = load_system_prompt()
    messages: list[dict] = []

    if args.once:
        messages.append({"role": "user", "content": args.once})
        run_turn(client, system, messages)
        return 0

    print(f"DevOps Support Agent  (model: {MODEL}, writes {'ON' if ALLOW_WRITES else 'OFF'})")
    print("Describe the problem. Type 'exit' to quit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if user.lower() in {"exit", "quit"}:
            return 0
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        run_turn(client, system, messages)


if __name__ == "__main__":
    sys.exit(main())
