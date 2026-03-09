# sentry2cc

Poll Sentry for errors, pass them to a Claude Code agent, and do whatever you want with the result — automatically, on a schedule.

The tool is deliberately minimal: it handles the plumbing (Sentry API, prompt rendering, Claude Code SDK, poll loop) and leaves all decisions to you via two small Python functions you write yourself.

---

## How it works

```
┌─────────────────────────────────────────────────────────────────┐
│  Every N seconds                                                │
│                                                                 │
│  Sentry API ──► list_issues(query, limit, sort)                 │
│                      │                                         │
│               for each issue:                                   │
│                      │                                         │
│                      ▼                                         │
│            trigger_fn(issue, client, **kwargs)                  │
│                 └─ True → continue                              │
│                 └─ False → skip                                 │
│                      │                                         │
│                      ▼                                         │
│            get_latest_event(issue_id)                           │
│                      │                                         │
│                      ▼                                         │
│            render_prompt(issue, event, issue_markdown)          │
│                      │                                         │
│                      ▼                                         │
│            Claude Code Agent runs on your codebase             │
│                      │                                         │
│                      ▼                                         │
│            post_exec_fn(issue, event, result, **kwargs)         │
└─────────────────────────────────────────────────────────────────┘
```

Issues are processed **one at a time** — intentional, to avoid concurrent writes to the same codebase.

---

## Installation

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/).

```bash
# Install from source
git clone https://github.com/anomalyco/sentry2cc
cd sentry2cc
uv sync

# Or install as a tool
uv tool install sentry2cc
```

---

## Quick start

**1. Create a config file**

```yaml
# sentry2cc.yaml
sentry:
  auth_token: "${SENTRY_AUTH_TOKEN}"
  organization: "my-org"
  project: "my-project"
  poll_interval: 60
  query: "is:unresolved level:error"
  issue_limit: 5

trigger:
  module: "my_rules"
  function: "should_fix"

claude_code:
  cwd: "/path/to/my/codebase"
  max_turns: 30
  max_budget_usd: 2.0

post_execution:
  module: "my_rules"
  function: "after_fix"
```

**2. Write your trigger and post-execution functions**

```python
# my_rules.py  (must be in the same directory as sentry2cc.yaml)

_seen: set[str] = set()

async def should_fix(issue, sentry_client, **kwargs) -> bool:
    """Return True to dispatch the Claude Code agent for this issue."""
    if issue.short_id in _seen:
        return False
    if issue.level not in ("error", "fatal"):
        return False
    _seen.add(issue.short_id)
    return True

async def after_fix(issue, event, result, sentry_client, **kwargs) -> None:
    """Called after the agent finishes."""
    if result.success:
        print(f"Fixed {issue.short_id} in {result.num_turns} turns (${result.total_cost_usd:.2f})")
```

**3. Run**

```bash
export SENTRY_AUTH_TOKEN=sntryu_...
sentry2cc --config sentry2cc.yaml
```

---

## CLI reference

```
sentry2cc --config PATH [--once] [--log-level LEVEL]
```

| Flag | Default | Description |
|---|---|---|
| `--config` / `-c` | `sentry2cc.yaml` | Path to YAML config file |
| `--once` | off | Poll exactly once and exit (useful for cron / CI) |
| `--log-level` | `INFO` | `DEBUG` · `INFO` · `WARNING` · `ERROR` · `CRITICAL` |
| `--version` | — | Print version and exit |

---

## Configuration reference

All string values support `${ENV_VAR}` interpolation. The config file's directory is automatically added to `sys.path`, so you can reference modules in the same directory without any path setup.

### `sentry`

| Field | Type | Default | Description |
|---|---|---|---|
| `auth_token` | string | **required** | Sentry user auth token (scopes: `event:read`, `project:read`) |
| `organization` | string | **required** | Organisation slug (from your Sentry URL) |
| `project` | string | **required** | Project slug |
| `base_url` | string | `https://sentry.io` | Override for self-hosted Sentry instances |
| `poll_interval` | int | `30` | Seconds between polls (minimum 5) |
| `query` | string | `is:unresolved` | [Sentry search query](https://docs.sentry.io/product/sentry-basics/search/) |
| `issue_limit` | int | `25` | Max issues fetched per poll (1–100) |
| `sort` | string | `new` | Sort order: `new` · `date` · `freq` · `priority` · `trends` · `user` |

### `trigger`

| Field | Type | Default | Description |
|---|---|---|---|
| `module` | string | **required** | Dotted Python module path (e.g. `my_rules`) |
| `function` | string | **required** | Function name in that module |
| `kwargs` | dict | `{}` | Static keyword arguments forwarded to the function on every call |

### `claude_code`

| Field | Type | Default | Description |
|---|---|---|---|
| `cwd` | string | **required** | Root directory of the codebase the agent operates on |
| `allowed_tools` | list | `[Read, Edit, Glob, Grep, Bash]` | Claude Code tools the agent may use |
| `permission_mode` | string | `acceptEdits` | `default` · `acceptEdits` · `plan` · `bypassPermissions` |
| `system_prompt` | string | `null` | Optional system prompt override |
| `max_turns` | int | `null` | Maximum agent turns (null = SDK default) |
| `max_budget_usd` | float | `null` | Spend cap in USD (null = no cap) |
| `model` | string | `null` | Model override (null = SDK default) |
| `prompt_template` | string | `null` | Path to a `.j2` Jinja2 template file (null = built-in default) |
| `add_dirs` | list | `[]` | Extra directories the agent may read/write outside `cwd` |

### `post_execution`

Same fields as `trigger` (`module`, `function`, `kwargs`). Optional — omit entirely if you don't need a post-execution hook.

---

## Writing trigger and post-execution functions

Both functions can be **sync or async** — sentry2cc detects this automatically and wraps sync functions with `asyncio.to_thread` so they don't block the event loop.

### Trigger function

```python
async def should_fix(
    issue: SentryIssue,
    sentry_client: SentryClient,
    **kwargs,           # receives config.trigger.kwargs
) -> bool:
    ...
```

Return `True` to dispatch the agent, `False` to skip.

**`SentryIssue` fields available in your trigger:**

| Attribute | Type | Description |
|---|---|---|
| `id` | `str` | Sentry internal issue ID |
| `short_id` | `str` | Human-readable ID (e.g. `MYAPP-1A2`) |
| `title` | `str` | Error title |
| `culprit` | `str \| None` | File/function where the error originated |
| `level` | `str` | `error` · `fatal` · `warning` · `info` |
| `status` | `str` | `unresolved` · `resolved` · `ignored` |
| `first_seen` | `datetime` | When the issue was first seen |
| `last_seen` | `datetime` | When it was most recently seen |
| `event_count` | `int` | Total occurrence count |
| `user_count` | `int` | Distinct affected users |
| `permalink` | `str` | URL to the issue in Sentry |
| `project.slug` | `str` | Project slug |

### Post-execution function

```python
async def after_fix(
    issue: SentryIssue,
    event: SentryEvent,
    result: AgentResult,
    sentry_client: SentryClient,
    **kwargs,           # receives config.post_execution.kwargs
) -> None:
    ...
```

**`AgentResult` fields:**

| Attribute | Type | Description |
|---|---|---|
| `success` | `bool` | `True` if the agent completed without error |
| `is_error` | `bool` | `True` if the agent hit an error or budget limit |
| `num_turns` | `int` | Number of agent turns used |
| `total_cost_usd` | `float \| None` | API cost in USD |
| `result` | `str \| None` | Agent's final text output |
| `stop_reason` | `str \| None` | Why the agent stopped |
| `session_id` | `str` | Claude Code session ID |

**`SentryClient` methods you can call from a hook:**

```python
await sentry_client.get_issue(issue_id)
await sentry_client.get_latest_event(issue_id)
await sentry_client.update_issue(issue_id, {"status": "resolved"})
```

### Passing config into your functions

Use `kwargs` in the config to pass any static values to your functions:

```yaml
trigger:
  module: my_rules
  function: should_fix
  kwargs:
    min_occurrences: 10
    findings_dir: "${FINDINGS_DIR}"

post_execution:
  module: my_rules
  function: after_fix
  kwargs:
    slack_webhook: "${SLACK_WEBHOOK_URL}"
    findings_dir: "${FINDINGS_DIR}"
```

```python
async def should_fix(issue, sentry_client, *, min_occurrences: int, findings_dir: str, **kwargs):
    if issue.event_count < min_occurrences:
        return False
    ...

async def after_fix(issue, event, result, sentry_client, *, slack_webhook: str, findings_dir: str, **kwargs):
    ...
```

---

## Prompt templates

By default, sentry2cc uses a built-in Jinja2 template that instructs Claude to investigate the stacktrace and fix the root cause. You can supply your own:

```yaml
claude_code:
  prompt_template: "/path/to/my_prompt.j2"
```

### Template context variables

| Variable | Type | Description |
|---|---|---|
| `issue` | `SentryIssue` | The Sentry issue |
| `event` | `SentryEvent` | The latest event for the issue |
| `issue_markdown` | `str` | Pre-formatted Markdown with stacktrace, tags, breadcrumbs, etc. |
| `config` | `Sentry2CCConfig` | The full config object |
| `...` | | All keys from `trigger.kwargs` are injected as top-level variables |

Any key defined in `trigger.kwargs` is available directly in the template. For example, if `kwargs` contains `findings_dir`, use `{{ findings_dir }}` in your template.

### Example: investigation-only template

```jinja2
You are investigating (not fixing) a Sentry error in the codebase.

{{ issue_markdown }}

Write a detailed analysis to `{{ findings_dir }}/{{ issue.short_id }}/analysis.md`.
Include:
- Root cause (specific file and line)
- Call chain from entry point to crash
- User impact
- Recommended fix (do not implement it)
```

---

## Deduplication

sentry2cc does **not** deduplicate internally — that is your responsibility in the trigger function. This gives you full control over what counts as "already processed".

Common patterns:

```python
# In-memory (resets on restart)
_seen: set[str] = set()

async def should_fix(issue, sentry_client, **kwargs) -> bool:
    if issue.short_id in _seen:
        return False
    _seen.add(issue.short_id)
    return True
```

```python
# Disk-based (survives restarts) — check for output folder existence
from pathlib import Path

async def should_fix(issue, sentry_client, *, findings_dir: str, **kwargs) -> bool:
    if Path(findings_dir, issue.short_id).exists():
        return False
    return True
```

```python
# Sentry status-based — mark resolved after the agent fixes it
async def after_fix(issue, event, result, sentry_client, **kwargs) -> None:
    if result.success:
        await sentry_client.update_issue(issue.id, {"status": "resolved"})
```

---

## Real-world example: investigation pipeline

A complete example that fetches the 3 newest errors every 5 minutes, investigates each one with Claude Code, and writes structured analysis documents.

**Directory layout:**

```
my-sentry2cc/
├── .env
├── sentry2cc.yaml
├── my_rules.py
└── my_prompt.j2
```

**`.env`:**

```bash
SENTRY_AUTH_TOKEN=sntryu_...
FINDINGS_DIR=/path/to/findings
CODEBASE_DIR=/path/to/my/codebase
```

**`sentry2cc.yaml`:**

```yaml
sentry:
  auth_token: "${SENTRY_AUTH_TOKEN}"
  organization: "my-org"
  project: "my-project"
  poll_interval: 300
  issue_limit: 3
  sort: "new"
  query: "is:unresolved level:error"

trigger:
  module: "my_rules"
  function: "should_investigate"
  kwargs:
    findings_dir: "${FINDINGS_DIR}"

claude_code:
  cwd: "${CODEBASE_DIR}"
  allowed_tools:
    - Read
    - Glob
    - Grep
    - Bash
    - Write
  permission_mode: "acceptEdits"
  max_turns: 60
  max_budget_usd: 3.0
  prompt_template: "my_prompt.j2"
  add_dirs:
    - "${FINDINGS_DIR}"

post_execution:
  module: "my_rules"
  function: "log_result"
  kwargs:
    findings_dir: "${FINDINGS_DIR}"
```

**`my_rules.py`:**

```python
from pathlib import Path
from loguru import logger

_seen_short_ids: set[str] = set()
_scanned_dir: str | None = None


def _scan_findings_dir(findings_dir: str) -> None:
    """Populate seen set from existing folders on disk (runs once per dir)."""
    global _scanned_dir
    if _scanned_dir == findings_dir:
        return
    p = Path(findings_dir)
    if p.is_dir():
        existing = {d.name for d in p.iterdir() if d.is_dir()}
        _seen_short_ids.update(existing)
        if existing:
            logger.info("Skipping {} already-analysed issues", len(existing))
    _scanned_dir = findings_dir


async def should_investigate(issue, sentry_client, *, findings_dir: str, **kwargs) -> bool:
    if issue.level not in ("error", "fatal"):
        return False
    if issue.status != "unresolved":
        return False
    _scan_findings_dir(findings_dir)
    if issue.short_id in _seen_short_ids:
        return False
    _seen_short_ids.add(issue.short_id)
    return True


async def log_result(issue, event, result, sentry_client, *, findings_dir: str, **kwargs) -> None:
    output = Path(findings_dir) / issue.short_id
    files = [f.name for f in output.rglob("*") if f.is_file()] if output.is_dir() else []
    status = "OK" if result.success else "ERROR"
    logger.info(
        "[{}] {} | turns={} cost=${:.4f} | files={}",
        status,
        issue.short_id,
        result.num_turns,
        result.total_cost_usd or 0.0,
        ", ".join(files) or "none",
    )
```

**`my_prompt.j2`:**

```jinja2
You are investigating a production Sentry error. Do not modify any source files.

{{ issue_markdown }}

Create the following files in `{{ findings_dir }}/{{ issue.short_id }}/`:

1. `analysis.md` — root cause, call chain, affected code, user impact
2. `fix-implementation.md` — recommended fix with actual code examples
3. `metadata.json` — machine-readable summary:
   {
     "short_id": "{{ issue.short_id }}",
     "title": {{ issue.title | tojson }},
     "occurrences": {{ issue.event_count }},
     "first_seen": "{{ issue.first_seen.isoformat() }}",
     "last_seen": "{{ issue.last_seen.isoformat() }}",
     "permalink": "{{ issue.permalink }}"
   }
```

**Run:**

```bash
set -a && source .env && set +a
sentry2cc --config sentry2cc.yaml --log-level INFO
```

---

## Running on a schedule

**cron** (every 5 minutes):

```cron
*/5 * * * * cd /path/to/my-sentry2cc && set -a && source .env && set +a && sentry2cc --config sentry2cc.yaml --once >> /var/log/sentry2cc.log 2>&1
```

**systemd** (`/etc/systemd/system/sentry2cc.service`):

```ini
[Unit]
Description=sentry2cc — Sentry to Claude Code
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/my-sentry2cc
EnvironmentFile=/path/to/my-sentry2cc/.env
ExecStart=/path/to/.venv/bin/sentry2cc --config sentry2cc.yaml --log-level INFO
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now sentry2cc
journalctl -fu sentry2cc
```

**nohup** (quick background run):

```bash
nohup sentry2cc --config sentry2cc.yaml > sentry2cc.log 2>&1 &
```

---

## Getting a Sentry auth token

1. Go to **Sentry → Settings → Account → API → Auth Tokens**
2. Create a new token with scopes: `event:read`, `project:read`
3. To also mark issues resolved after fixing: add `project:write`

The token format is `sntryu_...` for user tokens. Organisation tokens (`sntryo_...`) also work.

Your organisation slug is in your Sentry URL: `https://sentry.io/organizations/<slug>/`.

---

## Project structure

```
src/sentry2cc/
├── __init__.py         CLI entry point and logging setup
├── agent.py            Claude Code SDK wrapper
├── config.py           YAML config loading and validation
├── formatter.py        Sentry issue + event → Markdown
├── models.py           Pydantic models for Sentry API responses
├── prompt.py           Jinja2 template rendering
├── protocols.py        TriggerRule and PostExecution protocol definitions
├── runner.py           Poll loop and per-issue pipeline
├── sentry_client.py    Async Sentry REST API client
└── templates/
    └── default_prompt.j2   Built-in prompt template
```

---

## License

MIT
