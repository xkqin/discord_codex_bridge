from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "discord_codex_bridge"
TRANSCRIPT_FILE = DATA_DIR / "transcript.jsonl"
DEFAULT_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_id_set(name: str) -> set[int]:
    values: set[int] = set()
    for part in os.getenv(name, "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            raise ValueError(f"{name} contains a non-numeric Discord id: {part}") from None
    return values


def discover_codex_cli() -> str:
    explicit = os.getenv("CODEX_CLI_PATH", "").strip()
    if explicit:
        return explicit

    if DEFAULT_CONFIG.exists():
        try:
            config = tomllib.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            env = (
                config.get("mcp_servers", {})
                .get("node_repl", {})
                .get("env", {})
            )
            configured = str(env.get("CODEX_CLI_PATH", "")).strip()
            if configured:
                return configured
        except (OSError, tomllib.TOMLDecodeError):
            pass

    found = shutil.which("codex")
    if found:
        return found
    raise RuntimeError("Could not find Codex CLI. Set CODEX_CLI_PATH in .env.")


@dataclass
class BridgeSettings:
    discord_token: str
    command_prefix: str
    codex_cli_path: str
    target_session_id: str
    use_last_session: bool
    workdir: Path
    model: str
    timeout_seconds: int
    allowed_channel_ids: set[int]
    allowed_user_ids: set[int]
    accept_all_messages: bool
    allow_dangerous: bool
    max_discord_chars: int

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        target_session_id = (
            os.getenv("CODEX_TARGET_SESSION_ID", "").strip()
            or os.getenv("CODEX_SESSION_ID", "").strip()
        )
        use_last = env_bool("CODEX_USE_LAST", not bool(target_session_id))
        workdir = Path(os.getenv("CODEX_WORKDIR", str(ROOT))).expanduser()
        return cls(
            discord_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
            command_prefix=os.getenv("DISCORD_CODEX_PREFIX", "!codex").strip() or "!codex",
            codex_cli_path=discover_codex_cli(),
            target_session_id=target_session_id,
            use_last_session=use_last,
            workdir=workdir,
            model=os.getenv("CODEX_MODEL", "").strip(),
            timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 900),
            allowed_channel_ids=env_id_set("DISCORD_ALLOWED_CHANNEL_IDS"),
            allowed_user_ids=env_id_set("DISCORD_ALLOWED_USER_IDS"),
            accept_all_messages=env_bool("DISCORD_CODEX_ACCEPT_ALL", False),
            allow_dangerous=env_bool("CODEX_ALLOW_DANGEROUS", False),
            max_discord_chars=max(500, min(env_int("DISCORD_MAX_REPLY_CHARS", 1800), 1900)),
        )


def recent_sessions(limit: int = 12) -> list[dict[str, Any]]:
    index_path = Path(os.getenv("CODEX_SESSION_INDEX", str(DEFAULT_SESSION_INDEX))).expanduser()
    if not index_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(payload)
    rows.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    return rows[:limit]


def format_sessions(limit: int = 12) -> str:
    sessions = recent_sessions(limit)
    if not sessions:
        return "No Codex sessions found."
    lines = []
    for item in sessions:
        session_id = str(item.get("id", ""))
        updated_at = str(item.get("updated_at", ""))[:19].replace("T", " ")
        name = str(item.get("thread_name", "")).replace("\n", " ")
        lines.append(f"{updated_at}  {session_id}  {name}")
    return "\n".join(lines)


def build_prompt(message: str, *, author: str, channel: str, attachment_urls: list[str]) -> str:
    parts = [
        "Message forwarded from Discord to Codex.",
        f"Discord author: {author}",
        f"Discord channel: {channel}",
        "",
        message.strip(),
    ]
    if attachment_urls:
        parts.extend(["", "Discord attachment URLs:"])
        parts.extend(f"- {url}" for url in attachment_urls)
    return "\n".join(parts).strip()


def parse_agent_message(stdout: str) -> str:
    messages: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = str(item.get("text", "")).strip()
            if text:
                messages.append(text)
    return "\n\n".join(messages).strip()


def append_transcript(record: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_codex(settings: BridgeSettings, prompt: str) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / f"last-message-{uuid.uuid4().hex}.txt"

    command = [
        settings.codex_cli_path,
        "exec",
        "resume",
        "--skip-git-repo-check",
        "--json",
        "--output-last-message",
        str(output_path),
    ]
    if settings.model:
        command.extend(["--model", settings.model])
    if settings.allow_dangerous:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if settings.use_last_session and not settings.target_session_id:
        command.extend(["--last", "-"])
    else:
        command.extend([settings.target_session_id, "-"])

    started = time.time()
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(settings.workdir),
        capture_output=True,
        timeout=settings.timeout_seconds,
    )

    message = ""
    if output_path.exists():
        message = output_path.read_text(encoding="utf-8", errors="replace").strip()
    if not message:
        message = parse_agent_message(completed.stdout)

    append_transcript(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": settings.target_session_id or "--last",
            "returncode": completed.returncode,
            "elapsed_seconds": round(time.time() - started, 2),
            "prompt": prompt,
            "response": message,
            "stdout_tail": completed.stdout[-3000:],
            "stderr_tail": completed.stderr[-3000:],
        }
    )

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout)[-1800:].strip()
        raise RuntimeError(tail or f"Codex exited with code {completed.returncode}")
    if not message:
        raise RuntimeError("Codex completed but did not produce a final message.")
    return message


def split_discord_message(text: str, limit: int) -> list[str]:
    text = text.strip() or "(empty response)"
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def require_discord_token(settings: BridgeSettings) -> None:
    if not settings.discord_token:
        raise RuntimeError("Set DISCORD_BOT_TOKEN in .env before running the Discord bot.")


async def run_discord_bot(settings: BridgeSettings) -> None:
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError(
            "discord.py is not installed. Run: python -m pip install -r requirements-discord.txt"
        ) from exc

    require_discord_token(settings)
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    lock = asyncio.Lock()

    def authorized(message: Any) -> bool:
        if settings.allowed_channel_ids and message.channel.id not in settings.allowed_channel_ids:
            return False
        if settings.allowed_user_ids and message.author.id not in settings.allowed_user_ids:
            return False
        return True

    def extract_prompt(message: Any) -> tuple[str, bool]:
        raw = message.content.strip()
        mention_prefixes = [f"<@{client.user.id}>", f"<@!{client.user.id}>"] if client.user else []
        for prefix in mention_prefixes:
            if raw.startswith(prefix):
                return raw[len(prefix):].strip(), True
        if raw.startswith(settings.command_prefix):
            return raw[len(settings.command_prefix):].strip(), True
        if settings.accept_all_messages:
            return raw, True
        return "", False

    @client.event
    async def on_ready() -> None:
        print(f"Discord Codex bridge logged in as {client.user}", flush=True)
        print(f"Target session: {settings.target_session_id or '--last'}", flush=True)
        print(f"Workdir: {settings.workdir}", flush=True)

    @client.event
    async def on_message(message: Any) -> None:
        if message.author.bot:
            return
        if not authorized(message):
            return

        prompt_text, matched = extract_prompt(message)
        if not matched:
            return
        if not prompt_text:
            await message.channel.send(
                f"Send `{settings.command_prefix} <message>` or mention me with a prompt."
            )
            return

        lowered = prompt_text.lower()
        if lowered in {"status", "ping"}:
            target = settings.target_session_id or "--last"
            await message.channel.send(
                f"Codex bridge online. target={target}, workdir={settings.workdir}"
            )
            return
        if lowered.startswith("sessions"):
            sessions_text = format_sessions()
            await message.channel.send(f"```text\n{sessions_text[:1800]}\n```")
            return

        attachment_urls = [attachment.url for attachment in message.attachments]
        prompt = build_prompt(
            prompt_text,
            author=f"{message.author} ({message.author.id})",
            channel=f"{message.channel} ({message.channel.id})",
            attachment_urls=attachment_urls,
        )

        async with lock:
            async with message.channel.typing():
                loop = asyncio.get_running_loop()
                try:
                    response = await loop.run_in_executor(None, run_codex, settings, prompt)
                except subprocess.TimeoutExpired:
                    await message.channel.send("Codex timed out. Increase CODEX_TIMEOUT_SECONDS if needed.")
                    return
                except Exception as exc:
                    await message.channel.send(f"Codex bridge failed:\n```text\n{str(exc)[-1600:]}\n```")
                    return

        for chunk in split_discord_message(response, settings.max_discord_chars):
            await message.channel.send(chunk)

    await client.start(settings.discord_token)


def print_status(settings: BridgeSettings) -> None:
    payload = {
        "codex_cli_path": settings.codex_cli_path,
        "target_session_id": settings.target_session_id or None,
        "use_last_session": settings.use_last_session,
        "workdir": str(settings.workdir),
        "command_prefix": settings.command_prefix,
        "allowed_channel_ids": sorted(settings.allowed_channel_ids),
        "allowed_user_ids": sorted(settings.allowed_user_ids),
        "accept_all_messages": settings.accept_all_messages,
        "allow_dangerous": settings.allow_dangerous,
        "timeout_seconds": settings.timeout_seconds,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord to Codex session bridge.")
    parser.add_argument("--status", action="store_true", help="Print resolved bridge settings.")
    parser.add_argument("--list-sessions", action="store_true", help="List recent Codex sessions.")
    parser.add_argument("--once", help="Send one prompt to Codex and print the response.")
    parser.add_argument("--dry-run", help="Build the prompt and command shape without calling Codex.")
    args = parser.parse_args()

    settings = BridgeSettings.from_env()
    if args.status:
        print_status(settings)
        return
    if args.list_sessions:
        print(format_sessions())
        return
    if args.dry_run is not None:
        prompt = build_prompt(args.dry_run, author="dry-run", channel="local", attachment_urls=[])
        print(textwrap.dedent(
            f"""
            target={settings.target_session_id or '--last'}
            workdir={settings.workdir}
            codex_cli={settings.codex_cli_path}

            {prompt}
            """
        ).strip())
        return
    if args.once is not None:
        prompt = build_prompt(args.once, author="local", channel="local", attachment_urls=[])
        print(run_codex(settings, prompt))
        return

    asyncio.run(run_discord_bot(settings))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"discord_codex_bridge: {exc}", file=sys.stderr)
        raise SystemExit(1)
