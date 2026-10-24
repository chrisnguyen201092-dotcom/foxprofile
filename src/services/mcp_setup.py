"""Ready-to-paste MCP client configurations, shared by the desktop app and the web panel."""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import asdict, dataclass

TOKEN_PLACEHOLDER = "<FOXPROFILE_API_TOKEN>"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
MCP_SCRIPT = PROJECT_ROOT / "foxprofile_mcp.py"


def _default_python() -> str:
    """Prefer the repository's .venv Python interpreter when present."""
    candidates = (
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "bin" / "python",
    )
    for c in candidates:
        if c.is_file():
            return str(c)
    return sys.executable


@dataclass
class ClientConfig:
    id: str
    name: str
    where: str  # where the snippet goes: a terminal or a config file path
    language: str  # "bash" or "json", for display
    code: str


def _json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_configs(
    mcp_url: str,
    token: str | None,
    *,
    local: bool,
    python: str | None = None,
    api_url: str | None = None,
) -> list[ClientConfig]:
    """Configs for common MCP clients (Antigravity, Codex, Hermes, Claude, Cursor, VS Code).

    `token` is the real token, a placeholder, or None when the API has none.
    `local` adds stdio configurations that start FoxProfile's stdio MCP
    server on this machine; otherwise clients use HTTP or mcp-remote.
    """
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    header_flag = f' --header "Authorization: Bearer {token}"' if token else ""
    py_cmd = python or _default_python()

    desktop_where = (
        "%APPDATA%\\Claude\\claude_desktop_config.json"
        if sys.platform == "win32"
        else "~/Library/Application Support/Claude/claude_desktop_config.json"
    )
    antigravity_where = (
        "%USERPROFILE%\\.gemini\\antigravity\\mcp_config.json"
        if sys.platform == "win32"
        else "~/.gemini/antigravity/mcp_config.json"
    )

    if local:
        env = {"FOXPROFILE_URL": api_url or mcp_url.rsplit("/mcp", 1)[0]}
        if token:
            env["FOXPROFILE_API_TOKEN"] = token
        stdio_server: dict = {
            "command": py_cmd,
            "args": [str(MCP_SCRIPT)],
            "env": env,
        }
        antigravity_server: dict = stdio_server
        codex_env_flag = f' --env "FOXPROFILE_API_TOKEN={token}"' if token else ""
        codex_cmd = f'codex mcp add foxprofile{codex_env_flag} -- "{py_cmd}" "{MCP_SCRIPT}"'
    else:
        # Remote HTTP bridge
        args = ["-y", "mcp-remote", mcp_url]
        stdio_server = {"command": "npx", "args": args}
        if token:
            args += ["--header", "Authorization:${FOXPROFILE_AUTH}"]
            stdio_server["env"] = {"FOXPROFILE_AUTH": f"Bearer {token}"}
        antigravity_server = {"serverUrl": mcp_url, **({"headers": auth} if auth else {})}
        codex_cmd = f"codex mcp add --url {mcp_url}{header_flag} foxprofile"

    configs = [
        ClientConfig(
            id="antigravity",
            name="Antigravity",
            where=antigravity_where,
            language="json",
            code=_json({"mcpServers": {"foxprofile": antigravity_server}}),
        ),
        ClientConfig(
            id="codex",
            name="Codex CLI",
            where="Terminal",
            language="bash",
            code=codex_cmd,
        ),
        ClientConfig(
            id="hermes",
            name="Hermes Agent",
            where="mcp_config.json / mcpServers",
            language="json",
            code=_json(
                {
                    "mcpServers": {
                        "foxprofile": (
                            stdio_server
                            if local
                            else {"url": mcp_url, **({"headers": auth} if auth else {})}
                        )
                    }
                },
            ),
        ),
        ClientConfig(
            id="claude-code",
            name="Claude Code",
            where="Terminal",
            language="bash",
            code=f"claude mcp add --transport http foxprofile {mcp_url}{header_flag}",
        ),
        ClientConfig(
            id="cursor",
            name="Cursor",
            where="~/.cursor/mcp.json",
            language="json",
            code=_json(
                {
                    "mcpServers": {
                        "foxprofile": {"url": mcp_url, **({"headers": auth} if auth else {})}
                    }
                },
            ),
        ),
        ClientConfig(
            id="vscode",
            name="VS Code",
            where=".vscode/mcp.json",
            language="json",
            code=_json(
                {
                    "servers": {
                        "foxprofile": {
                            "type": "http",
                            "url": mcp_url,
                            **({"headers": auth} if auth else {}),
                        },
                    },
                },
            ),
        ),
        ClientConfig(
            id="claude-desktop",
            name="Claude Desktop",
            where=desktop_where,
            language="json",
            code=_json({"mcpServers": {"foxprofile": stdio_server}}),
        ),
    ]
    return configs


def as_dicts(configs: list[ClientConfig]) -> list[dict]:
    return [asdict(c) for c in configs]
