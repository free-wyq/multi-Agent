"""Framework-internal tools for agentic workers.

Each tool is a ``langchain_core.tools.tool``-decorated function bound to a
specific group's workspace via a closure. The LLM (via ``bind_tools``) decides
which tool to call; the framework executes it directly (no external CLI).

Tools:
- ``read_file``: read a UTF-8 file (truncated to 8 KB)
- ``write_file``: write a UTF-8 file (mkdir parents)
- ``edit_file``: exact string replacement inside a file
- ``list_dir``: list directory entries (max 200)
- ``run_command``: run a shell command in the workspace (timeout, stdout+stderr)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from langchain_core.tools import tool

from engine.workspace import safe_path, workspace_path

logger = logging.getLogger("multi-agent.tools")

_READ_LIMIT = 8 * 1024  # 8 KB
_LIST_LIMIT = 200
_CMD_TIMEOUT_DEFAULT = 30


def tools_for_group(group_id: str) -> list:
    """Return a list of @tool functions bound to the given group's workspace.

    The closure captures ``group_id`` so each tool knows which workspace to
    operate in. The LLM receives these via ``ChatOpenAI.bind_tools(...)``.
    """

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the workspace. Returns the file content (UTF-8,
        truncated to 8KB). Use this to inspect existing files before editing.
        """
        try:
            p = safe_path(group_id, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not p.is_file():
            return f"Error: file not found: {path}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading file: {exc}"
        if len(text) > _READ_LIMIT:
            text = text[:_READ_LIMIT] + "\n...[truncated]"
        return text

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the workspace with the given content.
        Parent directories are created automatically. Use this to create new
        files or completely replace a file's content.
        """
        try:
            p = safe_path(group_id, path)
        except ValueError as exc:
            return f"Error: {exc}"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
        except Exception as exc:
            return f"Error writing file: {exc}"
        return f"OK: wrote {len(content)} chars to {path}"

    @tool
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """Replace the first occurrence of old_text with new_text in a file.
        All three arguments are strings. The old_text must match exactly
        (including whitespace). Fails if old_text is not found. Use this for
        precise, surgical edits rather than rewriting the whole file.
        """
        try:
            p = safe_path(group_id, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not p.is_file():
            return f"Error: file not found: {path}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading file: {exc}"
        if old_text not in text:
            return f"Error: old_text not found in {path}"
        new_text_full = text.replace(old_text, new_text, 1)
        try:
            p.write_text(new_text_full, encoding="utf-8")
        except Exception as exc:
            return f"Error writing file: {exc}"
        return f"OK: edited {path}"

    @tool
    def list_dir(path: str = ".") -> str:
        """List directory entries in the workspace. Returns a newline-separated
        list of names (files and directories). Defaults to the workspace root.
        """
        try:
            p = safe_path(group_id, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not p.is_dir():
            return f"Error: not a directory: {path}"
        try:
            entries = sorted(p.iterdir())
        except Exception as exc:
            return f"Error listing dir: {exc}"
        names = [e.name + ("/" if e.is_dir() else "") for e in entries]
        if len(names) > _LIST_LIMIT:
            names = names[:_LIST_LIMIT]
            names.append("...[truncated]")
        return "\n".join(names) if names else "(empty)"

    @tool
    async def run_command(command: str, timeout: int = _CMD_TIMEOUT_DEFAULT) -> str:
        """Run a shell command inside the workspace. Returns combined stdout and
        stderr with the exit code. Use for running tests, git, build tools, etc.
        The command runs with the workspace as cwd. Default timeout 30s.
        """
        ws = workspace_path(group_id)
        is_windows = os.name == "nt"
        shell = "/bin/bash" if not is_windows else (os.environ.get("COMSPEC", "cmd.exe"))
        shell_args = ["-c", command] if not is_windows else ["/c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                shell,
                *shell_args,
                cwd=str(ws),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return f"Error spawning command: {exc}"

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return f"[timeout after {timeout}s] command: {command}"

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        result = ""
        if stdout:
            result += stdout
        if stderr:
            result += ("\n[stderr]\n" if result else "") + stderr
        result += f"\n[exit_code={proc.returncode}]"
        # Truncate very long output
        if len(result) > 8000:
            result = result[:8000] + "\n...[truncated]"
        return result.strip()

    return [read_file, write_file, edit_file, list_dir, run_command]
