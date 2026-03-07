"""
YAML configuration loading and validation for sentry2cc.

Config file example (sentry2cc.yaml):

    sentry:
      auth_token: "${SENTRY_AUTH_TOKEN}"   # env var interpolation supported
      organization: "my-org"
      project: "my-project"
      base_url: "https://sentry.io"        # override for self-hosted
      poll_interval: 30                     # seconds between polls
      query: "is:unresolved"               # Sentry search query filter

    trigger:
      module: "my_rules.triggers"          # Python dotted module path
      function: "should_fix"              # function name within the module

    claude_code:
      cwd: "/path/to/codebase"
      allowed_tools:
        - Read
        - Edit
        - Glob
        - Grep
        - Bash
      permission_mode: "acceptEdits"
      system_prompt: null                  # null = use default
      max_turns: 50
      max_budget_usd: 5.0
      model: null                          # null = SDK default
      prompt_template: null               # null = use built-in default

    post_execution:
      module: "my_rules.hooks"
      function: "after_fix"
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: str) -> str:
    """Replace ${VAR_NAME} patterns with environment variable values."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            raise ValueError(
                f"Environment variable '{var_name}' referenced in config is not set"
            )
        return env_val

    return _ENV_PATTERN.sub(_replace, value)


def _interpolate_dict(obj: Any) -> Any:
    """Recursively interpolate env vars in all string values of a dict/list."""
    if isinstance(obj, str):
        return _interpolate_env(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_dict(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class SentryConfig(BaseModel):
    """Sentry connection and polling settings."""

    auth_token: str
    organization: str
    project: str
    base_url: str = "https://sentry.io"
    poll_interval: int = Field(30, ge=5, description="Seconds between polls")
    query: str = "is:unresolved"

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class FunctionRef(BaseModel):
    """Reference to a user-supplied Python function (module + name)."""

    module: str = Field(..., description="Dotted Python module path")
    function: str = Field(..., description="Function name within the module")


class ClaudeCodeConfig(BaseModel):
    """Claude Code Agent SDK configuration."""

    cwd: str = Field(..., description="Working directory for the agent (codebase root)")
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Edit", "Glob", "Grep", "Bash"]
    )
    permission_mode: str = "acceptEdits"
    system_prompt: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    model: str | None = None
    prompt_template: str | None = Field(
        None,
        description="Path to a Jinja2 template file. If null, the built-in default is used.",
    )

    @field_validator("cwd")
    @classmethod
    def resolve_cwd(cls, v: str) -> str:
        path = Path(v).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"cwd path does not exist: {path}")
        return str(path)

    @field_validator("permission_mode")
    @classmethod
    def validate_permission_mode(cls, v: str) -> str:
        valid = {"default", "acceptEdits", "plan", "bypassPermissions"}
        if v not in valid:
            raise ValueError(f"permission_mode must be one of {valid}, got '{v}'")
        return v

    @field_validator("prompt_template")
    @classmethod
    def resolve_template_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        path = Path(v).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"prompt_template path does not exist: {path}")
        return str(path)


class Sentry2CCConfig(BaseModel):
    """Root configuration model for sentry2cc."""

    sentry: SentryConfig
    trigger: FunctionRef
    claude_code: ClaudeCodeConfig = Field(alias="claude_code")
    post_execution: FunctionRef | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data: dict[str, Any]) -> dict[str, Any]:
        # Support both claude_code and claude-code in YAML keys
        if "claude-code" in data and "claude_code" not in data:
            data["claude_code"] = data.pop("claude-code")
        return data


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> Sentry2CCConfig:
    """
    Load and validate a sentry2cc YAML configuration file.

    Performs environment variable interpolation on all string values before
    passing to Pydantic for validation.

    Parameters
    ----------
    path:
        Path to the YAML config file.

    Returns
    -------
    Sentry2CCConfig
        Fully validated configuration object.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    ValueError
        If the config is invalid or a referenced env var is missing.
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config file must be a YAML mapping at the top level")

    # Interpolate environment variables
    interpolated = _interpolate_dict(raw)

    return Sentry2CCConfig.model_validate(interpolated)
