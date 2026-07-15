"""Framework-internal tools for agentic workers.

Two tool families, both ``langchain_core.tools.tool``-decorated functions that
the LLM drives via ``bind_tools`` (the framework owns the model→tool→model
loop; we never hand-roll a dispatcher — memory ``engines-use-frameworks-not-handrolled``):

1. ``tools_for_group(group_id)`` — the **group-workspace** tools (``read_file`` /
   ``write_file`` / ``edit_file`` / ``list_dir`` / ``run_command``), bound to
   ``DATA_DIR/workspaces/{group_id}/``. These back the resident execute path
   (PL-05). Path-safety via ``engine.workspace.safe_path``.

2. ``tools_for_skill(skill_id)`` — the **skill-sandbox** controlled tools
   (``file_read`` / ``file_write`` / ``bash_run``), bound to
   ``DATA_DIR/skills/{skill_id}/workspace/``. These back the skill run path
   (task38) and are the tools a skill's ``requires_tools`` frontmatter names.
   Bash is **denylist**-gated: dangerous ops (``rm -rf``, network clients,
   privilege/system) are rejected by default (sandbox-design memory: 危险操作
   默认禁，按需白名单开). Path-safety via ``skill_assets.safe_skill_path``.

Tool names are stable strings (``"file_read"`` / ``"file_write"`` / ``"bash_run"``)
so a skill's ``requires_tools: ["file_read","bash_run"]`` frontmatter can name them
without coupling to the closure factory.
"""
from __future__ import annotations

import asyncio
import logging
import os

from langchain_core.tools import tool

from engine.workspace import safe_path, workspace_path
from store import skill_assets

logger = logging.getLogger("multi-agent.tools")

_READ_LIMIT = 8 * 1024  # 8 KB
_LIST_LIMIT = 200
_CMD_TIMEOUT_DEFAULT = 30

# ── skill-sandbox bash denylist (task35 / task40 审计面) ──────────────
# 危险操作默认禁（sandbox-design 记忆：危险操作默认禁，按需白名单开）。
# 这里用 denylist：拒绝破坏性删除、包安装（越权装东西）、网络客户端（外联数据
# 渗出/拉取）、提权与系统控制命令。匹配大小写不敏感，对整个命令串做子串/词级
# 检测。denylist 是 MVP 取舍——真正的隔离靠容器/受限 shell（sandbox-design 长期债），
# task40 会全审锁死。
_DANGEROUS_PATTERNS = (
    "rm -rf", "rm -fr", "rmdir", "mkfs", "dd if=", "shred",
    # 包管理（apt/pip/npm install/yum/brew 等）——技能沙箱不应越权装系统级依赖
    "apt-get", "apt install", "aptitude", "dpkg -i",
    "pip install", "pip3 install", "pipx install",
    "npm install", "npm i ", "yarn add", "pnpm add", "pnpm install",
    "yum install", "dnf install", "brew install", "conda install",
    # 网络客户端（curl/wget/nc/ssh/scp/telnet）——外联数据渗出/拉取
    "curl ", "curl>", "wget ", "nc ", "ncat ", "netcat",
    "ssh ", "scp ", "sftp ", "telnet ", "ftp ",
    # 提权与系统控制
    "sudo ", "su ", "doas ", "chmod 777", "chown ", "chgrp ",
    "systemctl ", "service ", "shutdown", "reboot", "halt", "poweroff",
    "kill -9", "pkill", "killall",
    # 写设备/挂载（绕过 FS 层直接写盘）
    "> /dev/", " /dev/sd", "mount ", "umount ",
    # 进程脱离与后台持久化（脱离沙箱生命周期）
    "nohup ", "disown", "setsid",
    # 内省宿主 / 逃逸线索
    " /etc/", "/etc/passwd", "/etc/shadow",
    "crontab ", "at now",
)


def _is_dangerous(command: str) -> str | None:
    """Return the first matched dangerous pattern (lowercased) or ``None``.

    Case-insensitive substring match over the whole command. A hit means the
    command is rejected. Empty/whitespace commands are rejected up-front by
    the caller, not here.
    """
    low = command.lower()
    for pat in _DANGEROUS_PATTERNS:
        if pat in low:
            return pat
    return None


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
                # race: process already exited between timeout and kill —
                # nothing to kill, fall through to the timeout message below.
                # `pass` is correct (not a swallow): ProcessLookupError is the
                # expected benign outcome of a kill-after-exit race, and there
                # is nothing to log or recover (B31 错误处理重巡航——已注释说明
                # 这是有意吞没的良性竞态，非静默吞没).
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


# ── skill-sandbox controlled tools (task35 · stage-4 executability) ────
# These are the tools a skill's ``requires_tools`` frontmatter names. They are
# bound to the skill's sandbox workspace (``DATA_DIR/skills/{id}/workspace/``)
# and gated by a bash denylist + path-safety (task40 审计锁). Names are stable
# strings so ``requires_tools: ["file_read","bash_run"]`` resolves without
# coupling to the factory.

# stable tool-name → factory-key registry (task36 resolves requires_tools via this)
SKILL_TOOL_NAMES = ("file_read", "file_write", "bash_run")


def skill_tool_by_name(name: str, skill_id: str):
    """Return the controlled tool instance for ``name`` bound to ``skill_id``.

    Used by task36 to resolve a skill's ``requires_tools`` list into concrete
    ``BaseTool`` objects. Returns ``None`` for unknown names so the caller can
    emit a mount-time validation warning (task36) rather than crash.
    """
    tools = {t.name: t for t in tools_for_skill(skill_id)}
    return tools.get(name)


def resolve_skill_tools(manifest: list[dict]) -> tuple[list, list[str]]:
    """Resolve ``requires_tools`` across a skill manifest to concrete tools.

    Each skill's required tools bind to THAT skill's own sandbox workspace
    (``DATA_DIR/skills/{skill_id}/workspace/``). Returns ``(tools, warnings)``:
      - ``tools``: deduped ``BaseTool`` list (first skill wins on name clash);
      - ``warnings``: human-readable strings for unknown tool refs and for
        multi-skill same-name collisions.

    Multi-skill name collision note: ``file_read``/``file_write``/``bash_run``
    are shared tool names — two skills each requiring ``file_read`` would bind
    two different sandboxes under the same name, which LangGraph rejects. We
    dedupe (first skill's sandbox wins) and warn. The clean single-skill case
    is the skill **run endpoint** (task38); the group execute path (task36
    wiring in ``agent_executor``) tolerates multi-skill by deduping, with the
    understanding that a mounted skill's sandbox tools are additive capability
    (the agent already has group-workspace ``read_file``/``run_command``).
    """
    if not manifest:
        return [], []
    tools: list = []
    seen: set[str] = set()
    warnings: list[str] = []
    for m in manifest:
        sid = m.get("id", "")
        sname = m.get("name", sid)
        for tool_name in (m.get("requires_tools") or []):
            if tool_name not in SKILL_TOOL_NAMES:
                warnings.append(
                    f"技能 {sname!r} 引用了未知工具 {tool_name!r}"
                    f"（可用：{', '.join(SKILL_TOOL_NAMES)}）"
                )
                continue
            if tool_name in seen:
                warnings.append(
                    f"工具 {tool_name!r} 已由首个技能绑定，"
                    f"技能 {sname!r} 的同名工具被跳过（多技能共享工具名仅首个沙箱生效）"
                )
                continue
            t = skill_tool_by_name(tool_name, sid)
            if t is not None:
                tools.append(t)
                seen.add(tool_name)
    return tools, warnings


def tools_for_skill(skill_id: str) -> list:
    """Return the controlled @tool set bound to a skill's sandbox workspace.

    Tools (stable names, referenced by ``requires_tools``):
    - ``file_read(path)``: read a UTF-8 file in the sandbox (truncated 8KB)
    - ``file_write(path, content)``: create/overwrite a file in the sandbox
    - ``bash_run(command, timeout=30)``: run a denylist-gated shell command
      with the sandbox as cwd

    All paths resolve inside ``DATA_DIR/skills/{skill_id}/workspace/`` via
    ``skill_assets.safe_skill_path`` (no ``../`` escape). Bash is gated by
    ``_is_dangerous`` (rm/network/system/package install denied by default).
    """
    if not skill_id:
        raise ValueError("skill_id 不可为空")

    @tool
    def file_read(path: str) -> str:
        """Read a text file from the skill's workspace. Returns file content
        (UTF-8, truncated to 8KB). Use to inspect existing files.
        """
        try:
            p = skill_assets.safe_skill_path(skill_id, path)
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
    def file_write(path: str, content: str) -> str:
        """Create or overwrite a file in the skill's workspace with the given
        content. Parent directories are created automatically. Products should
        go under ``output/``.
        """
        try:
            p = skill_assets.safe_skill_path(skill_id, path)
        except ValueError as exc:
            return f"Error: {exc}"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
        except Exception as exc:
            return f"Error writing file: {exc}"
        return f"OK: wrote {len(content)} chars to {path}"

    @tool
    async def bash_run(command: str, timeout: int = _CMD_TIMEOUT_DEFAULT) -> str:
        """Run a shell command inside the skill's workspace. Returns combined
        stdout/stderr + exit code. Dangerous ops (rm -rf, network clients,
        sudo, package install) are blocked. Products land under ``output/``.
        """
        if not command or not command.strip():
            return "Error: empty command"
        danger = _is_dangerous(command)
        if danger:
            return (
                f"Error: command blocked by sandbox denylist (matched: {danger!r}). "
                "危险操作（删除/网络/提权/装包）默认禁用。"
            )
        ws = skill_assets.skill_workspace_path(skill_id)
        shell = "/bin/bash" if os.name != "nt" else (os.environ.get("COMSPEC", "cmd.exe"))
        shell_args = ["-c", command] if os.name != "nt" else ["/c", command]
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
                pass  # benign kill-after-exit race (see run_command note)
            return f"[timeout after {timeout}s] command: {command}"
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        result = ""
        if stdout:
            result += stdout
        if stderr:
            result += ("\n[stderr]\n" if result else "") + stderr
        result += f"\n[exit_code={proc.returncode}]"
        if len(result) > 8000:
            result = result[:8000] + "\n...[truncated]"
        return result.strip()

    return [file_read, file_write, bash_run]
