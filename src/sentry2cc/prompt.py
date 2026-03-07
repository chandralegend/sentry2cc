"""
Jinja2 prompt template loading and rendering.

Templates receive these context variables:

  issue          SentryIssue   — the Sentry issue model
  event          SentryEvent   — the latest event for the issue
  issue_markdown str           — pre-formatted Markdown from formatter.py
  config         Sentry2CCConfig — the full config object

Users can supply their own template via ``claude_code.prompt_template`` in
the YAML config. If not set, the built-in ``default_prompt.j2`` is used.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

if TYPE_CHECKING:
    from sentry2cc.config import Sentry2CCConfig
    from sentry2cc.models import SentryEvent, SentryIssue

logger = logging.getLogger(__name__)

# Path to the built-in templates directory (shipped with the package)
_TEMPLATES_PACKAGE = "sentry2cc.templates"
_DEFAULT_TEMPLATE_NAME = "default_prompt.j2"


def _load_builtin_template_source() -> str:
    """Load the default template source from the package."""
    try:
        # Python 3.9+ approach
        ref = resources.files(_TEMPLATES_PACKAGE).joinpath(_DEFAULT_TEMPLATE_NAME)
        return ref.read_text(encoding="utf-8")
    except Exception:
        # Fallback: resolve via __file__
        here = Path(__file__).parent
        template_path = here / "templates" / _DEFAULT_TEMPLATE_NAME
        return template_path.read_text(encoding="utf-8")


def _make_env_from_path(template_path: Path) -> tuple[Environment, str]:
    """Return a Jinja2 Environment and template name for a user-supplied path."""
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=select_autoescape([]),  # disable HTML escaping for prompt templates
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env, template_path.name


def render_prompt(
    issue: SentryIssue,
    event: SentryEvent,
    issue_markdown: str,
    config: Sentry2CCConfig,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """
    Render the prompt template with issue and event context.

    Parameters
    ----------
    issue:
        The Sentry issue (group).
    event:
        The latest event for this issue.
    issue_markdown:
        Pre-formatted Markdown string from ``formatter.format_issue()``.
    config:
        The full sentry2cc configuration.
    extra_context:
        Any additional variables to expose in the template.

    Returns
    -------
    str
        The rendered prompt string ready to pass to the Claude Code agent.
    """
    context: dict[str, Any] = {
        "issue": issue,
        "event": event,
        "issue_markdown": issue_markdown,
        "config": config,
        **(extra_context or {}),
    }

    user_template_path = config.claude_code.prompt_template

    if user_template_path:
        # User-supplied template
        tpl_path = Path(user_template_path)
        logger.debug("Using user-supplied prompt template: %s", tpl_path)
        env, tpl_name = _make_env_from_path(tpl_path)
        template = env.get_template(tpl_name)
    else:
        # Built-in default template
        logger.debug("Using built-in default prompt template")
        source = _load_builtin_template_source()
        env = Environment(
            undefined=StrictUndefined,
            autoescape=select_autoescape([]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.from_string(source)

    rendered = template.render(**context)
    logger.debug("Rendered prompt (%d chars)", len(rendered))
    return rendered
