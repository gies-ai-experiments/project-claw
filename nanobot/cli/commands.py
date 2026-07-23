"""CLI commands for nanobot."""

import asyncio
import os
import select
import signal
import sys
from collections.abc import Callable
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from loguru import logger

# Remove default handler and re-add with unified nanobot format
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
)

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__, __version__
from nanobot.agent.loop import AgentLoop


def _sanitize_surrogates(text: str) -> str:
    """Reconstruct surrogate pairs into real characters; replace lone surrogates.

    On Windows, console input may produce lone surrogate code points (e.g.
    ``\\ud83d\\udc08`` for U+1F408).  Round-tripping through UTF-16 reconstructs
    paired surrogates into their actual characters and replaces unpaired ones
    with U+FFFD.
    """
    return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


class SafeFileHistory(FileHistory):
    """FileHistory subclass that sanitizes surrogate characters on write.

    On Windows, special Unicode input (emoji, mixed-script) can produce
    surrogate characters that crash prompt_toolkit's file write.
    See issue #2846.
    """

    def store_string(self, string: str) -> None:
        super().store_string(_sanitize_surrogates(string))


from nanobot.cli.stream import StreamRenderer, ThinkingSpinner
from nanobot.config.paths import get_workspace_path, is_default_workspace
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates
from nanobot.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)

app = typer.Typer(
    name="nanobot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}
_REASONING_SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？")
_REASONING_FLUSH_CHARS = 60

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    with suppress(Exception):
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return

    with suppress(Exception):
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    with suppress(Exception):
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    with suppress(Exception):
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())

    from nanobot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=sys.stdout.isatty(),
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
    show_header: bool = True,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    if show_header:
        console.print()
        console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""

    def _write() -> None:
        ansi = _render_interactive_ansi(lambda c: c.print(f"  [dim]↳ {text}[/dim]"))
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""

    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} nanobot[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(
    text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None
) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = (
        renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    )
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"  [dim]↳ {text}[/dim]")


class _ReasoningBuffer:
    def __init__(self) -> None:
        self._text = ""

    def add(self, text: str) -> str | None:
        if not text:
            return None
        self._text += text
        if self._should_flush(text):
            return self.flush()
        return None

    def flush(self) -> str | None:
        text = self._text.strip()
        self._text = ""
        return text or None

    def clear(self) -> None:
        self._text = ""

    def _should_flush(self, text: str) -> bool:
        stripped = text.rstrip()
        return (
            "\n" in text
            or stripped.endswith(_REASONING_SENTENCE_ENDINGS)
            or len(self._text) >= _REASONING_FLUSH_CHARS
        )


def _print_cli_reasoning(
    text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None
) -> None:
    """Print reasoning/thinking content in a distinct style."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = (
        renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    )
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"[dim italic]✻ {text}[/dim italic]")


def _flush_cli_reasoning(
    reasoning_buffer: _ReasoningBuffer,
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    text = reasoning_buffer.flush()
    if text:
        _print_cli_reasoning(text, thinking, renderer)


async def _print_interactive_progress_line(
    text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None
) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    if renderer:
        with renderer.pause_spinner():
            renderer.ensure_header()
            renderer.console.print(f"  [dim]↳ {text}[/dim]")
    else:
        with thinking.pause() if thinking else nullcontext():
            await _print_interactive_line(text)


async def _maybe_print_interactive_progress(
    msg: Any,
    thinking: ThinkingSpinner | None,
    channels_config: Any,
    renderer: StreamRenderer | None = None,
    reasoning_buffer: _ReasoningBuffer | None = None,
) -> bool:
    metadata = msg.metadata or {}
    if metadata.get("_retry_wait"):
        await _print_interactive_progress_line(msg.content, thinking, renderer)
        return True

    if not metadata.get("_progress"):
        return False

    reasoning_buffer = reasoning_buffer or _ReasoningBuffer()

    if metadata.get("_reasoning_end"):
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
        else:
            _flush_cli_reasoning(reasoning_buffer, thinking, renderer)
        return True

    is_tool_hint = metadata.get("_tool_hint", False)
    is_reasoning = metadata.get("_reasoning", False) or metadata.get("_reasoning_delta", False)
    if is_reasoning:
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
            return True
        text = reasoning_buffer.add(msg.content)
        if text:
            _print_cli_reasoning(text, thinking, renderer)
        return True
    if channels_config and is_tool_hint and not channels_config.send_tool_hints:
        return True
    if channels_config and not is_tool_hint and not channels_config.send_progress:
        return True

    await _print_interactive_progress_line(msg.content, thinking, renderer)
    return True


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """Initialize nanobot configuration and workspace."""
    from nanobot.config.loader import get_config_path, load_config, save_config, set_config_path
    from nanobot.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print(
                "  [bold]y[/bold] = overwrite with defaults (existing values will be lost)"
            )
            console.print(
                "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
            )
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(
                    f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
                )
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from nanobot.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'nanobot onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'nanobot agent -m "Hello!"'
    gateway_cmd = "nanobot gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print(
        "\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]"
    )


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from nanobot.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _model_display(config: Config) -> tuple[str, str]:
    """Return (resolved_model_name, preset_tag) for display strings."""
    resolved = config.resolve_preset()
    name = config.agents.defaults.model_preset
    tag = f" (preset: {name})" if name else ""
    return resolved.model, tag


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from nanobot.config.loader import load_config, resolve_config_env_vars, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    try:
        loaded = resolve_config_env_vars(load_config(config_path))
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from nanobot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


def _migrate_cron_store(config: "Config") -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from nanobot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command(name="provision-channels")
def provision_channels_cmd(
    dry_run: bool = typer.Option(True, "--dry-run/--create", help="Preview without creating"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Create one Slack channel per project (dev-side); prints the config mapping."""
    import asyncio as _asyncio

    from slack_sdk.web.async_client import AsyncWebClient

    from nanobot.channels.slack import SlackConfig
    from nanobot.cli.provision import provision_channels
    from nanobot.config.loader import load_config, set_config_path

    if config:
        set_config_path(config)
    cfg = load_config()
    slack = SlackConfig.model_validate(cfg.channels.slack)
    web = AsyncWebClient(token=slack.bot_token)
    existing = {
        name: chan_id
        for chan_id, pc in slack.project_channels.items()
        for name in pc.allowed_projects
    }
    rows = _asyncio.run(provision_channels(web, list(slack.projects.values()), existing, dry_run))
    for r in rows:
        console.print(r)
    console.print("[yellow]Bake channel_id values into config projects/projectChannels.[/yellow]")


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API server port"),
    host: str | None = typer.Option(None, "--host", "-H", help="Bind address"),
    timeout: float | None = typer.Option(
        None, "--timeout", "-t", help="Per-request timeout (seconds)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show nanobot runtime logs"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the OpenAI-compatible API server (/v1/chat/completions)."""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print("[red]aiohttp is required. Install with: pip install 'nanobot-ai[api]'[/red]")
        raise typer.Exit(1)

    from loguru import logger

    from nanobot.api.server import create_app
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.image_generation import image_gen_provider_configs
    from nanobot.session.manager import SessionManager

    if verbose:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    runtime_config = _load_runtime_config(config, workspace)
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config,
            bus,
            session_manager=session_manager,
            image_generation_provider_configs=image_gen_provider_configs(runtime_config),
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    model_name, preset_tag = _model_display(runtime_config)
    console.print(f"{__logo__} Starting OpenAI-compatible API server")
    console.print(f"  [cyan]Endpoint[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]Model[/cyan]    : {model_name}{preset_tag}")
    console.print("  [cyan]Session[/cyan]  : api:default")
    console.print(f"  [cyan]Timeout[/cyan]  : {timeout}s")
    if host in {"0.0.0.0", "::"}:
        console.print(
            "[yellow]Warning:[/yellow] API is bound to all interfaces. "
            "Only do this behind a trusted network boundary, firewall, or reverse proxy."
        )
    console.print()

    api_app = create_app(agent_loop, model_name=model_name, request_timeout=timeout)

    async def on_startup(_app):
        await agent_loop._connect_mcp()

    async def on_cleanup(_app):
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    web.run_app(api_app, host=host, port=port, print=lambda msg: logger.info(msg))


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the nanobot gateway."""
    if verbose:
        logger.remove(_log_handler_id)
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <5}</level> | "
                "<cyan>{extra[channel]}</cyan> | "
                "<level>{message}</level>"
            ),
            level="DEBUG",
            colorize=None,
            filter=lambda record: record["extra"].setdefault("channel", "-") or True,
        )
    cfg = _load_runtime_config(config, workspace)
    _run_gateway(cfg, port=port)


async def _setup_agent_memory(agent: AgentLoop, config: Config, pool: Any) -> None:
    """Attach optional conversation memory to an already initialized database."""
    if not config.memory.active or pool is None:
        return
    try:
        from nanobot.channels.project_resolver import ProjectResolver
        from nanobot.store.message_store import MessageStore

        agent.attach_memory(MessageStore(pool), ProjectResolver(pool), config.memory.inject_limit)
        logger.info("project-context-db memory active (dsn configured)")

        if config.memory.distiller_active:
            _attach_distiller(agent, pool, config)

    except Exception:  # noqa: BLE001 - never block gateway startup
        logger.warning("memory setup failed; continuing without memory")


def _attach_distiller(agent: AgentLoop, pool: Any, config: Config) -> None:
    """Build the L2 Distiller and attach it to the agent loop.

    The distiller uses ``config.memory.distiller_model`` for fact extraction and
    the OpenAI provider's client for embeddings (which the hybrid search path
    needs). Any failure is logged and swallowed — the gateway still starts; the
    distiller cron job simply no-ops (its handler checks ``agent.distiller``).
    """
    try:
        from nanobot.providers.factory import make_provider
        from nanobot.store.distiller import Distiller
        from nanobot.store.embeddings import OpenAIEmbedder

        model = config.memory.distiller_model
        provider = make_provider(config, model=model)

        embedder: OpenAIEmbedder | None = None
        openai_cfg = getattr(config.providers, "openai", None)
        if openai_cfg and getattr(openai_cfg, "api_key", None):
            try:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(
                    api_key=openai_cfg.api_key,
                    base_url=openai_cfg.api_base or None,
                )
                embedder = OpenAIEmbedder(client)
            except Exception:
                logger.warning("distiller: embedder init failed; facts will land without vectors")

        agent.attach_distiller(
            Distiller(
                conn=pool,
                provider=provider,
                model=model,
                embedder=embedder,
                batch_messages=config.memory.distiller_batch_messages,
                max_threads_per_run=config.memory.distiller_max_threads_per_run,
            )
        )
        logger.info("distiller active (model={}, cron='{}')", model, config.memory.distiller_cron)
    except Exception:
        logger.exception("distiller setup failed; continuing without L2 distillation")


def _run_gateway(
    config: Config,
    *,
    port: int | None = None,
    open_browser_url: str | None = None,
) -> None:
    """Shared gateway runtime; ``open_browser_url`` opens a tab once channels are up."""
    from nanobot.agent.tools.cron import CronTool
    from nanobot.agent.tools.message import MessageTool
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.channels.websocket import publish_runtime_model_update
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.providers.factory import build_provider_snapshot, load_provider_snapshot
    from nanobot.providers.image_generation import image_gen_provider_configs
    from nanobot.session.manager import SessionManager

    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting nanobot gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    try:
        provider_snapshot = build_provider_snapshot(config)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    session_manager = SessionManager(config.workspace_path)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop.from_config(
        config,
        bus,
        provider=provider_snapshot.provider,
        model=provider_snapshot.model,
        context_window_tokens=provider_snapshot.context_window_tokens,
        cron_service=cron,
        session_manager=session_manager,
        image_generation_provider_configs=image_gen_provider_configs(config),
        provider_snapshot_loader=load_provider_snapshot,
        runtime_model_publisher=lambda model, preset: publish_runtime_model_update(
            bus,
            model,
            preset,
        ),
        provider_signature=provider_snapshot.signature,
    )

    from nanobot.agent.loop import UNIFIED_SESSION_KEY
    from nanobot.bus.events import OutboundMessage

    def _channel_session_key(channel: str, chat_id: str) -> str:
        return (
            UNIFIED_SESSION_KEY
            if config.agents.defaults.unified_session
            else f"{channel}:{chat_id}"
        )

    async def _deliver_to_channel(
        msg: OutboundMessage,
        *,
        record: bool = False,
        session_key: str | None = None,
    ) -> None:
        """Publish a user-visible message and mirror it into that channel's session."""
        metadata = dict(msg.metadata or {})
        record = record or bool(metadata.pop("_record_channel_delivery", False))
        if metadata != (msg.metadata or {}):
            msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                reply_to=msg.reply_to,
                media=msg.media,
                metadata=metadata,
                buttons=msg.buttons,
            )
        if (
            record
            and msg.channel != "cli"
            and msg.content.strip()
            and hasattr(session_manager, "get_or_create")
            and hasattr(session_manager, "save")
        ):
            key = session_key or _channel_session_key(msg.channel, msg.chat_id)
            session = session_manager.get_or_create(key)
            extra: dict[str, Any] = {"_channel_delivery": True}
            if msg.media:
                extra["media"] = list(msg.media)
            session.add_message("assistant", msg.content, **extra)
            session_manager.save(session)
        await bus.publish_outbound(msg)

    message_tool = getattr(agent, "tools", {}).get("message")
    if isinstance(message_tool, MessageTool):
        message_tool.set_send_callback(_deliver_to_channel)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        # Dream is an internal job — run directly, not through the agent loop.
        if job.name == "dream":
            try:
                await agent.dream.run()
                logger.info("Dream cron job completed")
            except Exception:
                logger.exception("Dream cron job failed")
            return None

        # Distiller is an internal job — runs the L2 distiller once, no agent turn.
        if job.name == "distill":
            if getattr(agent, "distiller", None) is None:
                logger.debug("Distiller cron ticked but distiller is not attached; skipping")
                return None
            try:
                stats = await agent.distiller.run_once()
                logger.info("Distiller cron job completed: {}", stats)
            except Exception:
                logger.exception("Distiller cron job failed")
            return None

        # Daily digest is an internal job — runs the digest tick, no agent turn here.
        if job.name == "daily-digest":
            if daily_digest is None:
                return None
            try:
                await daily_digest.tick()
            except Exception:
                logger.exception("Daily-digest cron job failed")
            return None

        from nanobot.utils.evaluator import evaluate_response

        reminder_note = (
            "The scheduled time has arrived. Deliver this reminder to the user now, "
            "as a brief and natural message in their language. Speak directly to them — "
            "do not narrate progress, summarize, include user IDs, or add status reports "
            "like 'Done' or 'Reminded'.\n\n"
            f"Reminder: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)

        async def _silent(*_args, **_kwargs):
            pass

        message_record_token = None
        if isinstance(message_tool, MessageTool):
            message_record_token = message_tool.set_record_channel_delivery(True)

        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                on_progress=_silent,
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)
            if isinstance(message_tool, MessageTool) and message_record_token is not None:
                message_tool.reset_record_channel_delivery(message_record_token)

        response = resp.content if resp else ""

        if (
            job.payload.deliver
            and isinstance(message_tool, MessageTool)
            and message_tool._sent_in_turn
        ):
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response,
                reminder_note,
                agent.provider,
                agent.model,
            )
            if should_notify:
                await _deliver_to_channel(
                    OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response,
                        metadata=dict(job.payload.channel_meta),
                    ),
                    record=True,
                    session_key=job.payload.session_key,
                )
        return response

    cron.on_job = on_cron_job

    def _webui_runtime_model_name() -> str | None:
        model = getattr(agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    # Create channel manager (forwards SessionManager so the WebSocket channel
    # can serve the embedded webui's REST surface).
    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        webui_runtime_model_name=_webui_runtime_model_name,
    )

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    heartbeat_preamble = (
        "[Your response will be delivered directly to the user's messaging app. "
        "Output ONLY the final user-facing message. Never reference internal "
        "files (HEARTBEAT.md, AWARENESS.md, etc.), your instructions, or your "
        "decision process. If nothing needs reporting, respond with just "
        "'All clear.' and nothing else.]\n\n"
    )

    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        resp = await agent.process_direct(
            heartbeat_preamble + tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

        # Keep a small tail of heartbeat history so the loop stays bounded
        # without losing all short-term context between runs.
        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel.

        In addition to publishing the outbound message, this injects the
        delivered text as an assistant turn into the *target channel's*
        session.  Without this, a user reply on the channel (e.g. "Sure")
        lands in a session that has no context about the heartbeat message
        and the agent cannot follow through.
        """
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to

        await _deliver_to_channel(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response),
            record=True,
        )

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        llm_runtime=agent.llm_runtime,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )

    # Auto meeting-summary poller: watch Granola for new notes → summarize + assign.
    ms_cfg = config.gateway.meeting_summary
    meeting_summary = None
    if ms_cfg.enabled:
        import json as _json

        from nanobot.channels.slack import SlackConfig
        from nanobot.meeting_summary import MeetingSummaryService

        slack_raw = getattr(config.channels, "slack", None)
        slack_cfg = (
            slack_raw
            if isinstance(slack_raw, SlackConfig)
            else SlackConfig.model_validate(slack_raw)
            if slack_raw
            else None
        )
        ms_projects = list(slack_cfg.projects.values()) if slack_cfg else []

        async def on_new_meeting(project, note):
            ms = project.meeting_summary
            repo = ms.issue_repo or (
                project.github.repos[0] if project.github and len(project.github.repos) == 1 else ""
            )
            roster = _json.dumps([p.model_dump(by_alias=True) for p in project.people])

            # Ingest the note into L1 memory (distiller → L2 facts) when memory is on.
            store = getattr(agent, "_message_store", None)
            if store is not None:
                from nanobot.agent.tools.granola import _granola_get
                from nanobot.meeting_summary.ingest import ingest_note

                full = await _granola_get(config.tools.granola, f"/notes/{note.get('id')}")
                if isinstance(full, dict):
                    try:
                        await ingest_note(store, project, full, channel_id=ms.summary_channel)
                    except Exception:
                        logger.exception("daily-digest: note ingest failed for '%s'", project.name)

            trigger = (
                f"A new Granola meeting note landed for project '{project.name}'. "
                f"Run the meeting-summary skill.\n"
                f"note_id: {note.get('id')}\n"
                f"folder_id: {project.granola.folder_id}\n"
                f"issue_repo: {repo}\n"
                f"roster: {roster}"
            )

            async def _silent(*_a, **_k):
                pass

            resp = await agent.process_direct(
                trigger,
                session_key=f"meeting-summary:{project.name}",
                channel="slack",
                chat_id=ms.summary_channel,
                on_progress=_silent,
            )
            if resp and resp.content.strip():
                await _deliver_to_channel(
                    OutboundMessage(
                        channel="slack",
                        chat_id=ms.summary_channel,
                        content=resp.content,
                    ),
                    record=True,
                )

        meeting_summary = MeetingSummaryService(
            config.tools.granola,
            ms_projects,
            on_new_meeting,
            state_path=config.workspace_path / "meeting_summary_state.json",
            interval_s=ms_cfg.interval_s,
        )
        console.print(f"[green]✓[/green] Meeting-summary: every {ms_cfg.interval_s}s")

    # Daily digest: once/day per project, compare project memory vs live GitHub.
    dd_cfg = config.gateway.daily_digest
    daily_digest = None
    if dd_cfg.enabled:
        from nanobot.channels.slack import SlackConfig as _SlackConfig
        from nanobot.daily_digest import DailyDigestService
        from nanobot.daily_digest.service import _channel as _dd_channel

        _slack_raw = getattr(config.channels, "slack", None)
        _slack = (
            _slack_raw
            if isinstance(_slack_raw, _SlackConfig)
            else _SlackConfig.model_validate(_slack_raw)
            if _slack_raw
            else None
        )
        dd_projects = list(_slack.projects.values()) if _slack else []

        async def on_digest(project):
            from datetime import datetime, timedelta
            from datetime import timezone as _tzinfo

            channel = _dd_channel(project)
            repos = " ".join(project.github.repos) if project.github else ""
            since = (datetime.now(_tzinfo.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            trigger = (
                f"Daily digest for project '{project.name}'. Run the project-digest skill.\n"
                f"project: {project.name}\nrepos: {repos}\n"
                f"since: {since}\ndigest_channel: {channel}"
            )

            async def _silent(*_a, **_k):
                pass

            resp = await agent.process_direct(
                trigger,
                session_key=f"daily-digest:{project.name}",
                channel="slack",
                chat_id=channel,
                on_progress=_silent,
            )
            if resp and resp.content.strip():
                await _deliver_to_channel(
                    OutboundMessage(channel="slack", chat_id=channel, content=resp.content),
                    record=True,
                )

        daily_digest = DailyDigestService(
            dd_projects,
            on_digest,
            state_path=config.workspace_path / "daily_digest_state.json",
        )
        console.print(f"[green]✓[/green] Daily-digest: cron '{dd_cfg.cron}'")

    # GitHub poll: near-real-time per-project "what just landed on main" via commit
    # polling (no webhook/admin needed). Same plain-English style as the digest.
    gp_cfg = config.gateway.github_poll
    github_poll = None
    if gp_cfg.enabled:
        import os as _gp_os

        from nanobot.channels.slack import SlackConfig as _GpSlackConfig
        from nanobot.github_poll import GithubPollService, build_repo_channel_map

        _gp_raw = getattr(config.channels, "slack", None)
        _gp_slack = (
            _gp_raw
            if isinstance(_gp_raw, _GpSlackConfig)
            else _GpSlackConfig.model_validate(_gp_raw)
            if _gp_raw
            else None
        )
        gp_map = build_repo_channel_map(_gp_slack.projects) if _gp_slack else {}
        gp_token = _gp_os.environ.get("GH_TOKEN") or _gp_os.environ.get("GITHUB_TOKEN") or ""

        async def _gp_silent(*_a, **_k):
            pass

        async def gp_on_new(project: str, channel: str, repo: str, subjects: list[str]):
            commits = "\n".join(f"- {m}" for m in subjects[:20])
            trigger = (
                f"New commits just landed on the main branch of {repo}. Explain what "
                "changed in clear, plain, non-technical language for a project channel: "
                "one short intro line plus up to 3 bullets, under 60 words, no jargon, "
                "no commit hashes or PR numbers. If it is only trivial internal churn, "
                "reply with a single short line.\nCommits:\n" + commits
            )
            resp = await agent.process_direct(
                trigger,
                session_key=f"gh-poll:{repo}",
                channel="slack",
                chat_id=channel,
                on_progress=_gp_silent,
            )
            text = (resp.content if resp else "").strip()
            if text:
                await _deliver_to_channel(
                    OutboundMessage(channel="slack", chat_id=channel, content=text),
                    record=True,
                )

        github_poll = GithubPollService(
            gp_map,
            gp_token,
            gp_on_new,
            state_path=config.workspace_path / "github_poll_state.json",
            interval_s=gp_cfg.interval_s,
        )
        console.print(
            f"[green]✓[/green] GitHub-poll: every {gp_cfg.interval_s}s, {len(gp_map)} repos"
        )

    # Meeting classifier: one shared folder → classify per project → admin approval → fan-out.
    mc_cfg = config.gateway.meeting_classifier
    meeting_classifier = None
    provisioning_worker = None
    asana_client = None
    meeting_coordinator = None
    _slack_mc = None

    async def _mc_silent(*_a, **_k):
        pass

    if mc_cfg.enabled:
        from nanobot.channels.slack import SlackConfig as _SlackConfigMC

        _raw_mc = getattr(config.channels, "slack", None)
        _slack_mc = (
            _raw_mc
            if isinstance(_raw_mc, _SlackConfigMC)
            else _SlackConfigMC.model_validate(_raw_mc)
            if _raw_mc
            else None
        )

    if mc_cfg.enabled and not config.integrations.asana.enabled:
        import json as _json_mc

        from nanobot.meeting_classifier import ApprovalStore, MeetingClassifierService
        from nanobot.meeting_classifier import fanout as _mc_fo

        mc_projects = list(_slack_mc.projects.values()) if _slack_mc else []
        mc_known = {p.name for p in mc_projects}
        mc_channel_of = {p.name: p.channel for p in mc_projects}
        mc_store = ApprovalStore(config.workspace_path / "meeting_classifier_store.json")

        async def mc_on_new_note(note):
            note_id = str(note.get("id") or "")
            registry = _json_mc.dumps(
                [{"name": p.name, "description": p.description} for p in mc_projects]
            )
            title = note.get("title") or ""
            trigger = (
                "Classify this meeting note per project. Run the meeting-classify skill.\n"
                f"note_id: {note_id}\ntitle: {title}\nprojects: {registry}"
            )
            resp = await agent.process_direct(
                trigger,
                session_key=f"meeting-classify:{note_id}",
                channel="slack",
                chat_id=mc_cfg.admin_slack_id,
                on_progress=_mc_silent,
            )
            drafts = _mc_fo.parse_classification(resp.content if resp else "", mc_known)
            if not drafts:
                await _deliver_to_channel(
                    OutboundMessage(
                        channel="slack",
                        chat_id=mc_cfg.admin_slack_id,
                        content=f"Meeting '{title or note_id}': no project matched.",
                    ),
                    record=False,
                )
                return
            for d in drafts:
                d["title"] = title
                mc_store.add_draft(note_id, d["project"], d)
            text, buttons = _mc_fo.build_approval(title, note_id, drafts)
            await _deliver_to_channel(
                OutboundMessage(
                    channel="slack",
                    chat_id=mc_cfg.admin_slack_id,
                    content=text,
                    buttons=buttons,
                ),
                record=False,
            )

        async def mc_on_action(payload):
            sender_id = str(((payload.get("user") or {}).get("id")) or "")
            actions = payload.get("actions") or []
            value = str((actions[0] if actions else {}).get("value") or "")
            if sender_id != mc_cfg.admin_slack_id:
                return
            parsed = _mc_fo.parse_action(value)
            if not parsed:
                return
            decision, note_id, project = parsed
            entry = mc_store.get_draft(note_id, project)
            if not entry:
                return
            draft = entry["draft"]
            if decision == "skip":
                mc_store.mark(note_id, project, "skipped")
                return
            if not mc_store.mark(note_id, project, "approved"):
                return  # already decided — idempotent
            channel = mc_channel_of.get(project, "")
            if not channel:
                return
            await _deliver_to_channel(
                OutboundMessage(
                    channel="slack",
                    chat_id=channel,
                    content=_mc_fo.format_post(project, draft.get("title", ""), draft),
                ),
                record=True,
            )
            store = getattr(agent, "_message_store", None)
            if store is not None:
                from nanobot.meeting_summary.ingest import ingest_note

                proj_obj = next((p for p in mc_projects if p.name == project), None)
                if proj_obj is not None:
                    await ingest_note(
                        store,
                        proj_obj,
                        {
                            "id": f"{note_id}:{project}",
                            "title": f"{draft.get('title', 'Meeting')} ({project})",
                            "summary": draft.get("summary", ""),
                            "transcript": "\n".join(draft.get("actions") or []),
                        },
                        channel_id=channel,
                    )

        _sc_mc = channels.get_channel("slack")
        if _sc_mc is not None and hasattr(_sc_mc, "set_approval_callback"):
            _sc_mc.set_approval_callback(mc_on_action)

        meeting_classifier = MeetingClassifierService(
            config.tools.granola,
            mc_cfg.folder_id,
            mc_on_new_note,
            state_path=config.workspace_path / "meeting_classifier_state.json",
            interval_s=mc_cfg.interval_s,
        )
        console.print(f"[green]✓[/green] Meeting-classifier: folder {mc_cfg.folder_id}")

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def _health_server(host: str, health_port: int):
        """Lightweight HTTP health endpoint on the gateway port."""
        import json as _json

        async def handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
            except (asyncio.TimeoutError, ConnectionError):
                writer.close()
                return

            request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            method, path = "", ""
            parts = request_line.split(" ")
            if len(parts) >= 2:
                method, path = parts[0], parts[1]

            if method == "GET" and path == "/health":
                body = _json.dumps({"status": "ok"})
                resp = (
                    f"HTTP/1.0 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
            else:
                body = "Not Found"
                resp = (
                    f"HTTP/1.0 404 Not Found\r\n"
                    f"Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )

            writer.write(resp.encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, host, health_port)
        console.print(f"[green]✓[/green] Health endpoint: http://{host}:{health_port}/health")
        async with server:
            await server.serve_forever()

    # Register Dream system job (always-on, idempotent on restart)
    dream_cfg = config.agents.defaults.dream
    if dream_cfg.model_override:
        agent.dream.model = dream_cfg.model_override
    agent.dream.max_batch_size = dream_cfg.max_batch_size
    agent.dream.max_iterations = dream_cfg.max_iterations
    agent.dream.annotate_line_ages = dream_cfg.annotate_line_ages
    from nanobot.cron.types import CronJob, CronPayload

    cron.register_system_job(
        CronJob(
            id="dream",
            name="dream",
            schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
            payload=CronPayload(kind="system_event"),
        )
    )
    console.print(f"[green]✓[/green] Dream: {dream_cfg.describe_schedule()}")

    async def _open_browser_when_ready() -> None:
        """Wait for the gateway to bind, then point the user's browser at the webui."""
        if not open_browser_url:
            return
        import webbrowser

        # Channels start asynchronously; a short poll lets us avoid racing the bind.
        for _ in range(40):  # ~4s max
            try:
                reader, writer = await asyncio.open_connection(
                    config.gateway.host or "127.0.0.1", port
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            webbrowser.open(open_browser_url)
            console.print(f"[green]✓[/green] Opened browser at {open_browser_url}")
        except Exception as e:
            console.print(
                f"[yellow]Could not open browser ({e}); visit {open_browser_url}[/yellow]"
            )

    async def run():
        nonlocal asana_client, meeting_classifier, meeting_coordinator, provisioning_worker
        database_pool = None
        channels_task = None
        try:
            from nanobot.channels.slack import SlackConfig
            from nanobot.store.database import setup_database

            slack_channel = (
                channels.get_channel("slack") if hasattr(channels, "get_channel") else None
            )
            slack_runtime = getattr(slack_channel, "config", None)
            if slack_runtime is not None and not isinstance(slack_runtime, SlackConfig):
                slack_runtime = SlackConfig.model_validate(slack_runtime)
            database_pool = await setup_database(config, slack_runtime)
            await _setup_agent_memory(agent, config, database_pool)
            channels_task = asyncio.create_task(channels.start_all())

            if mc_cfg.enabled and config.integrations.asana.enabled:
                if database_pool is None:
                    raise RuntimeError("Asana meeting provisioning requires PostgreSQL.")
                if slack_channel is None or not hasattr(slack_channel, "wait_until_ready"):
                    raise RuntimeError("Asana meeting provisioning requires Slack Socket Mode.")
                await slack_channel.wait_until_ready(60)

                import json as _json_mc2
                from datetime import UTC, date, datetime

                from nanobot.integrations.asana import AsanaClient
                from nanobot.integrations.slack_workspace import SlackWorkspaceClient
                from nanobot.meeting_classifier import fanout as _mc_fo2
                from nanobot.meeting_classifier.coordinator import MeetingApprovalCoordinator
                from nanobot.meeting_classifier.identity import IdentityResolver
                from nanobot.meeting_classifier.provisioning import ProvisioningWorker
                from nanobot.meeting_classifier.repository import (
                    ApprovalRepository,
                    IdentityRepository,
                    ProvisioningRepository,
                )
                from nanobot.meeting_classifier.service import MeetingClassifierService
                from nanobot.store.runtime_registry import RuntimeProjectRegistry

                asana_client = AsanaClient(config.integrations.asana)
                await asana_client.validate_connection()
                slack_workspace = SlackWorkspaceClient(lambda: slack_channel.web_client)
                identity_repository = IdentityRepository(database_pool)
                provisioning_repository = ProvisioningRepository(database_pool)
                meeting_coordinator = MeetingApprovalCoordinator(
                    ApprovalRepository(database_pool),
                    provisioning_repository,
                    IdentityResolver(identity_repository, slack_workspace, asana_client),
                    slack_workspace,
                    admin_slack_id=mc_cfg.admin_slack_id,
                    known_projects=lambda: set(slack_channel.config.projects),
                )
                registry = RuntimeProjectRegistry(database_pool)
                provisioning_worker = ProvisioningWorker(
                    provisioning_repository,
                    asana_client,
                    slack_workspace,
                    identity_repository,
                    project_provider=lambda name: slack_channel.config.projects.get(name),
                    admin_slack_id=mc_cfg.admin_slack_id,
                    registry=registry,
                    slack_channel=slack_channel,
                )

                async def mc2_on_new_note(note):
                    note_id = str(note.get("id") or "")
                    title = str(note.get("title") or "")
                    live_projects = list(slack_channel.config.projects.values())
                    known_projects = {project.name for project in live_projects}
                    registry_json = _json_mc2.dumps(
                        [
                            {"name": project.name, "description": project.description}
                            for project in live_projects
                        ]
                    )
                    trigger = (
                        "Classify this meeting note into structured project task drafts. "
                        "Run the meeting-classify skill and return only its JSON array.\n"
                        f"note_id: {note_id}\ntitle: {title}\nprojects: {registry_json}"
                    )
                    resp = await agent.process_direct(
                        trigger,
                        session_key=f"meeting-classify:{note_id}",
                        channel="slack",
                        chat_id=mc_cfg.admin_slack_id,
                        on_progress=_mc_silent,
                    )
                    drafts = _mc_fo2.parse_structured_classification(
                        resp.content if resp else "", known_projects
                    )
                    if not drafts:
                        await _deliver_to_channel(
                            OutboundMessage(
                                channel="slack",
                                chat_id=mc_cfg.admin_slack_id,
                                content=f"Meeting '{title or note_id}': no project matched.",
                            ),
                            record=False,
                        )
                        return
                    raw_date = str(
                        note.get("meeting_date") or note.get("date") or note.get("created_at") or ""
                    )
                    try:
                        meeting_date = date.fromisoformat(raw_date[:10])
                    except ValueError:
                        meeting_date = datetime.now(UTC).date()
                    await meeting_coordinator.on_new_note(note_id, title, meeting_date, drafts)

                async def mc2_on_interaction(payload):
                    result = await meeting_coordinator.handle_interaction(payload)
                    if result.job_id is not None or result.kind == "retrying":
                        provisioning_worker.wake()
                    return result.response_action

                slack_channel.set_approval_callback(mc2_on_interaction)
                await provisioning_worker.start()
                meeting_classifier = MeetingClassifierService(
                    config.tools.granola,
                    mc_cfg.folder_id,
                    mc2_on_new_note,
                    state_path=config.workspace_path / "meeting_classifier_state.json",
                    interval_s=mc_cfg.interval_s,
                )
                console.print(
                    f"[green]✓[/green] Asana meeting provisioning: folder {mc_cfg.folder_id}"
                )
            if getattr(agent, "distiller", None) is not None:
                cron.register_system_job(
                    CronJob(
                        id="distill",
                        name="distill",
                        schedule=config.memory.distiller_schedule(config.agents.defaults.timezone),
                        payload=CronPayload(kind="system_event"),
                    )
                )
                console.print(
                    f"[green]✓[/green] Distiller: {config.memory.describe_distiller_schedule()}"
                )
            if daily_digest is not None:
                cron.register_system_job(
                    CronJob(
                        id="daily-digest",
                        name="daily-digest",
                        schedule=dd_cfg.digest_schedule(config.agents.defaults.timezone),
                        payload=CronPayload(kind="system_event"),
                    )
                )
            await cron.start()
            await heartbeat.start()
            if meeting_summary:
                await meeting_summary.start()
            if meeting_classifier:
                await meeting_classifier.start()
            if github_poll:
                await github_poll.start()
            tasks = [
                agent.run(),
                channels_task,
                _health_server(config.gateway.host, port),
            ]
            if open_browser_url:
                tasks.append(_open_browser_when_ready())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            if meeting_summary:
                meeting_summary.stop()
            if meeting_classifier:
                meeting_classifier.stop()
            if github_poll:
                github_poll.stop()
            if provisioning_worker is not None:
                with suppress(Exception):
                    await provisioning_worker.stop()
            if asana_client is not None:
                with suppress(Exception):
                    await asana_client.aclose()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            # Flush all cached sessions to durable storage before exit.
            # This prevents data loss on filesystems with write-back
            # caching (rclone VFS, NFS, FUSE mounts, etc.).
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Shutdown: flushed {} session(s) to disk", flushed)
            if database_pool is not None:
                with suppress(Exception):
                    from nanobot.store.pool import close_pool

                    await close_pool()

    asyncio.run(run())


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(
        True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
    ),
    logs: bool = typer.Option(
        False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"
    ),
):
    """Interact with the agent directly."""
    from loguru import logger

    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService
    from nanobot.providers.image_generation import image_gen_provider_configs

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    try:
        agent_loop = AgentLoop.from_config(
            config,
            bus,
            cron_service=cron,
            image_generation_provider_configs=image_gen_provider_configs(config),
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    restart_notice = consume_restart_notice_from_env()
    if restart_notice and should_show_cli_restart_notice(restart_notice, session_id):
        _print_agent_response(
            format_restart_completed_message(restart_notice.started_at_raw),
            render_markdown=False,
        )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    def _make_progress(renderer: StreamRenderer | None = None):
        reasoning_buffer = _ReasoningBuffer()

        async def _cli_progress(
            content: str, *, tool_hint: bool = False, reasoning: bool = False, **_kwargs: Any
        ) -> None:
            ch = agent_loop.channels_config

            if _kwargs.get("reasoning_end"):
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                else:
                    _flush_cli_reasoning(reasoning_buffer, _thinking, renderer)
                return

            if reasoning:
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                    return
                text = reasoning_buffer.add(content)
                if text:
                    _print_cli_reasoning(text, _thinking, renderer)
                return
            if ch and tool_hint and not ch.send_tool_hints:
                return
            if ch and not tool_hint and not ch.send_progress:
                return
            _print_cli_progress_line(content, _thinking, renderer)

        return _cli_progress

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(
                render_markdown=markdown,
                bot_name=config.agents.defaults.bot_name,
                bot_icon=config.agents.defaults.bot_icon,
            )
            response = await agent_loop.process_direct(
                message,
                session_id,
                on_progress=_make_progress(renderer),
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                print_kwargs: dict[str, Any] = {}
                if renderer.header_printed:
                    print_kwargs["show_header"] = False
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                    **print_kwargs,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nanobot.bus.events import InboundMessage

        _init_prompt_session()
        _model, _preset_tag = _model_display(config)
        console.print(
            f"{__logo__} Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} — type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
        )

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None
            reasoning_buffer = _ReasoningBuffer()

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if await _maybe_print_interactive_progress(
                            msg,
                            renderer,
                            agent_loop.channels_config,
                            renderer,
                            reasoning_buffer,
                        ):
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # Stop spinner before user input to avoid prompt_toolkit conflicts
                        if renderer:
                            renderer.stop_for_input()
                        user_input = _sanitize_surrogates(await _read_interactive_input_async())
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        reasoning_buffer.clear()
                        renderer = StreamRenderer(
                            render_markdown=markdown,
                            bot_name=config.agents.defaults.bot_name,
                            bot_icon=config.agents.defaults.bot_icon,
                        )

                        await bus.publish_inbound(
                            InboundMessage(
                                channel=cli_channel,
                                sender_id="user",
                                chat_id=cli_chat_id,
                                content=user_input,
                                metadata={"_wants_stream": True},
                            )
                        )

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                print_kwargs: dict[str, Any] = {}
                                if renderer and renderer.header_printed:
                                    print_kwargs["show_header"] = False
                                _print_agent_response(
                                    content,
                                    render_markdown=markdown,
                                    metadata=meta,
                                    **print_kwargs,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show channel status."""
    from nanobot.channels.registry import discover_all
    from nanobot.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force re-authentication even if already logged in"
    ),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from nanobot.channels.registry import discover_all
    from nanobot.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from nanobot.channels.registry import discover_all, discover_channel_names
    from nanobot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show nanobot status."""
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        _model, _preset_tag = _model_display(config)
        console.print(f"Model: {_model}{_preset_tag}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, Callable[[], None]] = {}
_LOGOUT_HANDLERS: dict[str, Callable[[], None]] = {}

_PROVIDER_DISPLAY: dict[str, str] = {
    "openai_codex": "OpenAI Codex",
    "github_copilot": "GitHub Copilot",
}


def _register_login(name: str):
    """Register an OAuth login handler."""

    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


def _register_logout(name: str):
    """Register an OAuth logout handler."""

    def decorator(fn):
        _LOGOUT_HANDLERS[name] = fn
        return fn

    return decorator


def _resolve_oauth_provider(provider: str):
    """Resolve and validate an OAuth provider configuration."""
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)
    return spec


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(
        ..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"
    ),
):
    """Authenticate with an OAuth provider."""
    spec = _resolve_oauth_provider(provider)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@provider_app.command("logout")
def provider_logout(
    provider: str = typer.Argument(
        ..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"
    ),
):
    """Log out from an OAuth provider."""
    spec = _resolve_oauth_provider(provider)

    handler = _LOGOUT_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Logout not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Logout - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        with suppress(Exception):
            token = get_token()
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(
            f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]"
        )
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_logout("openai_codex")
def _logout_openai_codex() -> None:
    """Clear local OAuth credentials for OpenAI Codex."""
    try:
        from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
        from oauth_cli_kit.storage import FileTokenStorage
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)

    storage = FileTokenStorage(token_filename=OPENAI_CODEX_PROVIDER.token_filename)
    _delete_oauth_files(storage.get_token_path(), _PROVIDER_DISPLAY["openai_codex"])


@_register_logout("github_copilot")
def _logout_github_copilot() -> None:
    """Clear local OAuth credentials for GitHub Copilot."""
    try:
        from nanobot.providers.github_copilot_provider import get_storage
    except ImportError:
        console.print(
            "[red]GitHub Copilot provider unavailable. Ensure oauth-cli-kit is installed.[/red]"
        )
        raise typer.Exit(1)

    storage = get_storage()
    _delete_oauth_files(storage.get_token_path(), _PROVIDER_DISPLAY["github_copilot"])


def _delete_oauth_files(token_path: Path, provider_label: str) -> None:
    """Delete OAuth token and lock files, reporting the result."""
    removed_paths: list[Path] = []
    skipped: list[tuple[Path, OSError]] = []
    for path in (token_path, token_path.with_suffix(".lock")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            skipped.append((path, exc))
            continue
        removed_paths.append(path)

    if not removed_paths and not skipped:
        console.print(f"[yellow]! No local OAuth credentials found for {provider_label}[/yellow]")
        return

    if removed_paths:
        console.print(f"[green]✓ Logged out from {provider_label}[/green]")
        for path in removed_paths:
            console.print(f"[dim]Removed: {path}[/dim]")
    for path, exc in skipped:
        console.print(f"[yellow]! Could not remove {path}: {exc}[/yellow]")


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    try:
        from nanobot.providers.github_copilot_provider import login_github_copilot

        console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")
        token = login_github_copilot(
            print_fn=lambda s: console.print(s),
            prompt_fn=lambda s: typer.prompt(s),
        )
        account = token.account_id or "GitHub"
        console.print(f"[green]✓ Authenticated with GitHub Copilot[/green]  [dim]{account}[/dim]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
