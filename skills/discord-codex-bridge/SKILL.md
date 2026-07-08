---
name: "discord-codex-bridge"
description: "Install, configure, run, and troubleshoot the Discord Codex Bridge service that forwards Discord bot messages into a local Codex CLI session."
---

# Discord Codex Bridge

Use this skill when the user asks to set up or repair the Discord -> Codex bridge from this repository.

## Workflow

1. Inspect the repository root for `discord_codex_bridge.py`, `.env`, `.env.example`, and `requirements.txt`.
2. Verify Python and dependencies:
   ```powershell
   python --version
   python -m pip install -r requirements.txt
   ```
3. Verify Codex CLI:
   ```powershell
   codex --help
   python discord_codex_bridge.py --list-sessions
   ```
4. Check `.env` without printing secrets:
   ```powershell
   python discord_codex_bridge.py --status
   ```
5. If a Discord token or channel id is missing, ask the user to set it in `.env`. Never ask them to paste a real token into chat.
6. Start the bridge:
   ```powershell
   python discord_codex_bridge.py
   ```
7. For background Windows runs, redirect logs under `data/discord_codex_bridge/`.

## Safety

- Never commit `.env`, logs, or transcript data.
- Prefer a fixed `CODEX_TARGET_SESSION_ID` over `CODEX_USE_LAST=1` for predictable control.
- Keep `CODEX_ALLOW_DANGEROUS=0` unless the user explicitly understands the risk.
- If a real Discord token appears in chat or logs, recommend resetting it in the Discord Developer Portal.
