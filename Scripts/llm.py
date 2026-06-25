import copy
import json
import os
import random
import re as _re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# Tool-call logging (enabled by MEMGRAPH_LLM_LOG_TOOL_CALLS env var)
# ---------------------------------------------------------------------------

_tool_log_path = None
_tool_log_results = False
_tool_log_lock = threading.Lock()


def configure_tool_logging(output_dir):
    global _tool_log_path, _tool_log_results
    if os.environ.get("MEMGRAPH_LLM_LOG_TOOL_CALLS"):
        _tool_log_path = os.path.join(output_dir, "TOOL-CALL.log")
        _tool_log_results = bool(os.environ.get("MEMGRAPH_LLM_LOG_TOOL_RESULTS"))


# ---------------------------------------------------------------------------
# Streaming JSONL event log
# ---------------------------------------------------------------------------

class EventLog:
    """Appends one JSON object per line to a file. Thread-safe.

    Each event has at minimum: {"ts": "...", "event": "...", ...}

    Usage:
        log = EventLog("/tmp/debate/fix_agent.jsonl")
        log.emit("prompt", prompt="Fix the leak...")
        log.emit("llm_response", turn=1, text="I'll check...", tool_calls=[...])
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        # Ensure parent dir exists
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def emit(self, event, **kwargs):
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        record.update(kwargs)
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")


def _log_tool_call(name, arguments, result, elapsed):
    if not _tool_log_path:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"[{ts}] {name} ({elapsed:.1f}s)",
        f"  args: {json.dumps(arguments)}",
    ]
    if _tool_log_results:
        lines.append(f"  result: {result[:500]}{'...' if len(result) > 500 else ''}")
    lines.append("")
    with _tool_log_lock:
        with open(_tool_log_path, "a") as f:
            f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Per-turn prompt-size logging (LLM_PROMPT_LOG env var)
#
# Tracks how many (estimated) tokens each tool result adds and the
# cumulative conversation size.  Enabled by setting LLM_PROMPT_LOG to a
# file path, or to "1"/"true" to write to /tmp/llm-prompt-size.log.
# ---------------------------------------------------------------------------

_prompt_log_lock = threading.Lock()


def _prompt_log_path():
    raw = os.environ.get("LLM_PROMPT_LOG", "")
    if not raw:
        return None
    if raw.lower() in ("1", "true", "yes"):
        return "/tmp/llm-prompt-size.log"
    return raw


def _estimate_chars(obj):
    """Recursively estimate the character count of a message or messages list."""
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, list):
        return sum(_estimate_chars(item) for item in obj)
    if isinstance(obj, dict):
        return sum(_estimate_chars(v) for v in obj.values())
    return len(str(obj)) if obj is not None else 0


def _estimate_tokens(chars):
    """Rough chars-to-tokens estimate (1 token ≈ 4 chars)."""
    return chars // 4


def _log_prompt_turn(log_path, turn, tool_calls, results, messages):
    """Append a per-turn prompt-size breakdown to the log file."""
    if not log_path:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    total_chars = _estimate_chars(messages)
    total_tok = _estimate_tokens(total_chars)

    lines = [f"[{ts}] Turn {turn}  |  conversation: ~{total_tok:,} tokens ({total_chars:,} chars)"]
    for tc, result in zip(tool_calls, results):
        r_chars = len(result) if isinstance(result, str) else len(json.dumps(result))
        r_tok = _estimate_tokens(r_chars)
        args_summary = json.dumps(tc["arguments"])
        flag = "  ⚠️" if r_tok > 50_000 else ""
        lines.append(f"  {tc['name']}({args_summary}) -> {r_chars:,} chars (~{r_tok:,} tokens){flag}")
    lines.append("")

    with _prompt_log_lock:
        with open(log_path, "a") as f:
            f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Tools — read_file, run_command, apply_edit, write_file
# ---------------------------------------------------------------------------

TOOLS = {
    "read_file": {
        "description": "Read the contents of a file at the given path.",
        "parameters": {
            "path": {"type": "string", "description": "Absolute or relative file path to read"},
        },
        "required": ["path"],
    },
    "run_command": {
        "description": "Run a shell command and return its stdout/stderr.",
        "parameters": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    },
    "apply_edit": {
        "description": (
            "Replace exactly one occurrence of old_string with new_string in an existing file. "
            "old_string must match exactly and appear EXACTLY ONCE in the file — otherwise the "
            "edit fails. Use this for targeted changes that preserve all surrounding content. "
            "PREFER this over write_file for editing existing source — write_file overwrites the "
            "whole file and can silently delete content."
        ),
        "parameters": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "old_string": {"type": "string", "description": "Exact text to find (must be unique in the file; include enough context to disambiguate)"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    "write_file": {
        "description": (
            "Write full content to a file, creating parent directories as needed. Replaces the "
            "file's content entirely if it exists. Use ONLY for brand-new files. To modify an "
            "existing file, use apply_edit (or read it first and pass the full preserved content)."
        ),
        "parameters": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "content": {"type": "string", "description": "Full content to write"},
        },
        "required": ["path", "content"],
    },
}


# Default tools enabled when no explicit list is passed. Read-only by
# default — write tools are opt-in per call (e.g. fix authors enable them
# via tools=[...]).
_DEFAULT_TOOLS = ("read_file", "run_command")


def _resolve_cwd_path(path, cwd):
    """Resolve a tool-supplied path to an absolute one, anchored to cwd.

    When cwd is set, every path — including absolute ones — must end up
    INSIDE cwd. Absolute paths that escape cwd are rejected. This stops a
    misbehaved agent from editing the main checkout when it should be
    editing a per-call worktree.

    Returns (absolute_path, error_message). If error_message is non-None,
    the caller should return it to the model verbatim.
    """
    if not cwd:
        return os.path.abspath(path), None
    cwd_abs = os.path.realpath(os.path.abspath(cwd))
    if os.path.isabs(path):
        candidate = os.path.realpath(path)
    else:
        candidate = os.path.realpath(os.path.join(cwd_abs, path))
    # Ensure candidate is within cwd_abs (or equal to it).
    rel = os.path.relpath(candidate, cwd_abs)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None, (
            f"Error: path {path!r} resolves to {candidate!r}, which is "
            f"OUTSIDE the working directory ({cwd_abs}). Use a path "
            f"relative to the worktree, NOT an absolute path to a "
            f"different checkout."
        )
    return candidate, None


def execute_tool(name, arguments, cwd=None, readonly=True):
    if name == "read_file":
        path = arguments["path"]
        resolved, err = _resolve_cwd_path(path, cwd)
        if err:
            return err
        try:
            with open(resolved) as f:
                return f.read()
        except Exception as e:
            return f"Error reading {path}: {e}"

    if name == "write_file":
        path = arguments["path"]
        resolved, err = _resolve_cwd_path(path, cwd)
        if err:
            return err
        try:
            parent = os.path.dirname(resolved)
            if parent:
                os.makedirs(parent, exist_ok=True)
            content = arguments.get("content", "")
            with open(resolved, "w") as f:
                f.write(content)
            return f"Wrote {len(content)} bytes to {resolved}"
        except Exception as e:
            return f"Error writing {path}: {e}"

    if name == "apply_edit":
        path = arguments["path"]
        resolved, err = _resolve_cwd_path(path, cwd)
        if err:
            return err
        old = arguments.get("old_string", "")
        new = arguments.get("new_string", "")
        if not old:
            return "Error: old_string is empty — apply_edit requires a non-empty match string"
        try:
            with open(resolved) as f:
                content = f.read()
            count = content.count(old)
            if count == 0:
                return (
                    f"Error: old_string not found in {resolved}. "
                    f"Read the file first to confirm exact whitespace and indentation."
                )
            if count > 1:
                return (
                    f"Error: old_string appears {count} times in {resolved}; "
                    f"include more surrounding context so the match is unique."
                )
            new_content = content.replace(old, new, 1)
            with open(resolved, "w") as f:
                f.write(new_content)
            return (
                f"Edited {resolved}: replaced {len(old)} bytes with {len(new)} bytes"
            )
        except FileNotFoundError:
            return f"Error: {resolved} does not exist"
        except Exception as e:
            return f"Error editing {resolved}: {e}"

    if name == "run_command":
        cmd_str = arguments["command"]
        rejection = _reject_pathological_command(cmd_str, readonly=readonly)
        if rejection:
            msg, hint = rejection
            return f"Error: refused — {msg}.\nSuggestion: {hint}"
        try:
            # Disable LFS filter processes — they connect to the LFS server
            # on every git status/show and wedge under concurrent load.
            # Preserve the rest of the user's git config (user.name, etc.).
            env = {
                **os.environ,
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_LFS_SKIP_PUSH": "1",
            }
            result = subprocess.run(
                cmd_str,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                env=env,
                timeout=120,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 120 seconds. Use a more targeted command."
        except Exception as e:
            return f"Error: {e}"

    return f"Unknown tool: {name}"


# Wrappers that prefix Claude-issued commands. Strip iteratively so the
# pathological-pattern checks below see the actual command shape Claude
# is running. Order matters: outer wrappers first (parens, env vars,
# `timeout N`), then `cd ... && ...` chains.
_WRAPPER_PATTERNS = [
    _re.compile(r"^\s*\(\s*"),                           # `( ...`
    _re.compile(r"^\s*nohup\s+"),                        # `nohup ...`
    _re.compile(r"^\s*GIT_PAGER=\S+\s+"),                # `GIT_PAGER=cat ...`
    _re.compile(r"^\s*GIT_OPTIONAL_LOCKS=\S+\s+"),       # `GIT_OPTIONAL_LOCKS=0 ...`
    _re.compile(r"^\s*timeout\s+[0-9]+(\.[0-9]+)?\s+"),  # `timeout 60 ...`
    _re.compile(r"^\s*cd\s+\S+\s*&&\s*"),                # `cd dir && ...`
    _re.compile(r"^\s*cd\s+\$\([^)]+\)\s*&&\s*"),        # `cd $(git rev-parse --show-toplevel) && ...`
]


def _strip_wrappers(cmd):
    """Peel off shell wrappers (timeout, cd-then, env vars, parens) so we
    can pattern-match on the inner command Claude actually wants to run."""
    prev = None
    while prev != cmd:
        prev = cmd
        for pat in _WRAPPER_PATTERNS:
            cmd = pat.sub("", cmd, count=1)
    return cmd


def _reject_pathological_command(raw_cmd, readonly=True):
    """Return (msg, hint) if the command is one of the known-slow shapes
    that Claude likes to run despite the prompt's warnings; None otherwise.

    Catches `git log --all`, unscoped pickaxe, `find .` without -maxdepth,
    `git grep` without path, `rg`/`grep -r` at repo scope, and pipe-into-
    xargs-grep patterns. On a large repo each of these reliably hits the
    30-second subprocess timeout and wastes a tool turn.

    When *readonly* is False (fix/write sessions), write-modifying commands
    (git commit, sed -i, etc.) are allowed through.
    """
    inner = _strip_wrappers(raw_cmd)

    # `git log --all` — full-history walk across every branch
    if _re.search(r"\bgit\s+log\b.*\s--all\b", inner):
        return (
            "git log --all walks every branch's full history",
            "drop --all and scope with a branch, commit range, or path",
        )

    # `git log -S` or `git log -G` (pickaxe) without any scope
    if _re.search(r"\bgit\s+log\b", inner) and _re.search(r"\s-(S|G)\b", inner):
        scoped = (
            _re.search(r"-n\s+\d+|--max-count\b|--since\b|--until\b", inner)
            or _re.search(r"\b[0-9a-f]{7,}\.\.", inner)
            or " -- " in inner
        )
        if not scoped:
            return (
                "git log pickaxe (-S/-G) without scope walks every diff in history",
                "add `-n 100`, `--since=2.years.ago`, a commit range `<sha>..HEAD`, or `-- path/`",
            )

    # `git log -L <range:file>` — line-history search. Even with `-n N`
    # it walks the full diff history. Reliably 30s+ on long-lived files.
    if _re.search(r"\bgit\s+log\b.*\s-L\s", inner):
        return (
            "git log -L (line history) walks every diff for the file, slow on big repos",
            "use `git log -n 30 --oneline -- <path>` and read the diffs you care about with `git show <sha> -- <path>`",
        )

    # `git log` with no scope at all
    if _re.match(r"\s*git\s+log\b", inner):
        scoped = (
            _re.search(r"-n\s+\d+|--max-count\b|--since\b|--until\b", inner)
            or _re.search(r"\b[0-9a-f]{7,}\.\.", inner)
            or " -- " in inner
            or _re.search(r"git\s+log\s+\S+:[^/\s]", inner)  # git log <ref>:<path>
        )
        if not scoped:
            return (
                "git log without scope walks the entire branch history",
                "add `-n 30`, `--since=...`, a commit range, or `-- path/`",
            )

    # `find` without -maxdepth — catches `find .`, `find /`, `find ~/`, etc.
    if _re.match(r"\s*find\s+", inner) and "-maxdepth" not in inner:
        return (
            "find without -maxdepth walks the entire tree",
            "use `git ls-files <pattern>` or `find . -maxdepth 3 -name ...`",
        )

    # `find /` — never allow a filesystem-root walk, even with -maxdepth
    if _re.match(r"\s*find\s+/", inner):
        return (
            "find / walks the entire filesystem",
            "search within the repo instead: `git ls-files <pattern>` or `find . -maxdepth 3 -name ...`",
        )

    # `git grep` without `-- <path>` scope
    if _re.match(r"\s*git\s+grep\b", inner) and " -- " not in inner:
        return (
            "git grep at repo scope is slow on large repos",
            "add `-- path/to/dir/` or `-- '*.kt'` to scope the search",
        )

    # `rg` without `-g` glob filter and without a positional path
    if _re.match(r"\s*rg\b", inner):
        # `rg PATTERN path/` — the path argument provides scope.
        # `rg -g '*.kt' PATTERN` — the glob provides scope.
        # `rg -t kotlin PATTERN` — the type filter provides scope.
        has_glob = _re.search(r"\s-g\s|\s--glob\s|\s-t\s|\s--type\s|\s--include\s", inner)
        # Heuristic for "has path arg": more than 2 non-flag tokens after `rg`
        # is hard to detect reliably; lean on the explicit scoping flags above.
        if not has_glob:
            return (
                "rg at repo scope is slow on large repos",
                "add `-g '*.ext'` or `-t <type>` to limit the file types",
            )

    # `grep -r` / `grep -rn` / `grep -rln` without --include
    if _re.search(r"\bgrep\s+-r[a-z]*\b", inner) and "--include" not in inner:
        return (
            "grep -r without --include walks the entire tree",
            "add `--include='*.ext'` or use `git grep -- path/` instead",
        )

    # `git ls-files | xargs grep` — repo-wide grep via pipe
    if _re.search(r"git\s+ls-files\b.*\|\s*xargs\b.*grep", inner):
        return (
            "git ls-files | xargs grep effectively greps every tracked file",
            "scope `git ls-files` to a path/glob first, e.g. `git ls-files '**/*.kt'`",
        )

    # ---------------------------------------------------------------------
    # Write-modifying commands. Read-only sessions (advocates, analysis)
    # exist to inspect code, not mutate it. Without a sandbox, any write
    # would land in the user's actual checkout. The prompt warns against
    # this but the same data we used to design these filters (~816 of
    # 461K calls) shows claude does occasionally try writes — almost
    # always staging output under /tmp, which we explicitly allow.
    #
    # Fix sessions (readonly=False) need git add/commit/push, sed -i,
    # file writes, etc. — skip these guards entirely for them.
    # ---------------------------------------------------------------------

    if not readonly:
        return None

    _SAFE_WRITE_PREFIXES = ("/tmp/", "/var/folders/", "/dev/null", "/dev/stdout", "/dev/stderr")

    # Mutating git subcommands. None of these have a place in verify.
    if _re.search(r"\bgit\s+(checkout|reset|clean|rm|add|commit|stash|merge|rebase|pull|push|fetch|init|branch\s+-[Dd]\b)\b", inner):
        return (
            "git mutation commands modify repository state",
            "verification is read-only — use `git show`, `git log`, `git grep`, `git ls-files` instead",
        )

    # `sed -i` — in-place edit
    if _re.search(r"\bsed\s+-i\b", inner):
        return (
            "sed -i modifies files in place",
            "drop -i and redirect output to /tmp/ if you need to capture the result",
        )

    # chmod / chown
    if _re.match(r"\s*(chmod|chown)\b", inner):
        return (
            "chmod/chown modifies file permissions/ownership",
            "verification doesn't require permission changes",
        )

    # File-write redirect (`>` and `>>`). Allow targets under /tmp, /var,
    # or /dev/null; reject everything else (would write into the repo).
    # The negative lookbehind on `>` prevents matching `>>` twice and
    # `2>&1`-style stream redirects.
    redirect_match = _re.search(r"(?<![&|>])>{1,2}\s*([^\s&|;<>]+)", inner)
    if redirect_match:
        target = redirect_match.group(1).strip("\"'")
        # Empty target (e.g. claude wrote `> ''`) is a malformed shell command
        # — let the shell error report it; not our concern to flag here.
        if target and not target.startswith(_SAFE_WRITE_PREFIXES) and target not in ("&1", "&2"):
            return (
                f"redirect target '{target[:80]}' is not under /tmp/",
                "redirect to /tmp/ instead, e.g. `> /tmp/out.txt`, or pipe through `| head`",
            )

    # `tee` — file write via pipe
    tee_match = _re.search(r"\|\s*tee\s+(?:-a\s+)?([^\s|;&]+)", inner)
    if tee_match:
        target = tee_match.group(1).strip("\"'")
        if not target.startswith(_SAFE_WRITE_PREFIXES):
            return (
                f"tee writes to '{target[:80]}'",
                "tee to /tmp/ instead, or drop the tee and pipe to `head`/`less`",
            )

    # rm / mv / cp / touch / mkdir / rmdir — paths must be under /tmp.
    fs_match = _re.match(r"\s*(rm|mv|cp|touch|mkdir|rmdir)\b\s+(.*)", inner)
    if fs_match:
        verb = fs_match.group(1)
        # Strip flags. Treat `-` followed by a word as a flag; everything
        # else is a path. Conservative: if ANY non-flag arg is outside
        # the safe prefixes, reject.
        tokens = [t for t in fs_match.group(2).split() if not t.startswith("-") and t not in ("--",)]
        if tokens:
            safe = all(t.startswith(_SAFE_WRITE_PREFIXES) or t.startswith("$") for t in tokens)
            if not safe:
                return (
                    f"{verb} against paths outside /tmp/ would mutate the working tree",
                    "verification is read-only — restrict to /tmp/ paths or skip the operation",
                )

    return None


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def _resolve_tool_names(tools_enabled):
    """Map the polymorphic tools_enabled argument to a tuple of tool names.

      False / None  → empty (no tools)
      True          → _DEFAULT_TOOLS (read-only)
      sequence      → that sequence verbatim (e.g. authors enable apply_edit/write_file)
    """
    if not tools_enabled:
        return ()
    if tools_enabled is True:
        return _DEFAULT_TOOLS
    return tuple(tools_enabled)


def _anthropic_tool_schemas(names=None):
    return [
        {
            "name": name,
            "description": tool["description"],
            "input_schema": {
                "type": "object",
                "properties": tool["parameters"],
                "required": tool["required"],
            },
        }
        for name, tool in TOOLS.items()
        if names is None or name in names
    ]


def _openai_responses_tools(names=None):
    """Tool schemas for the Responses API (/v1/responses).

    The Responses API expects a FLAT shape: name/description/parameters at
    the top level alongside type=function. The Chat Completions API uses a
    nested shape — see _openai_chatcompletions_tools.
    """
    return [
        {
            "type": "function",
            "name": name,
            "description": tool["description"],
            "parameters": {
                "type": "object",
                "properties": tool["parameters"],
                "required": tool["required"],
            },
        }
        for name, tool in TOOLS.items()
        if names is None or name in names
    ]


def _openai_chatcompletions_tools(names=None):
    """Tool schemas for the Chat Completions API (/v1/chat/completions).

    Chat Completions wraps the function definition in a `function` key.
    The Responses API rejects this shape — see _openai_responses_tools.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool["description"],
                "parameters": {
                    "type": "object",
                    "properties": tool["parameters"],
                    "required": tool["required"],
                },
            },
        }
        for name, tool in TOOLS.items()
        if names is None or name in names
    ]


def _http_post(url, headers, body, timeout=300):
    """POST JSON, returning parsed response.

    Retries transparently on 429 (rate limit) and 5xx (server error),
    honoring the `Retry-After` response header when present. Backs off
    exponentially to 300s, then retries every 5 minutes indefinitely.

    Non-retryable errors (4xx except 429) are raised immediately.
    """


    data = json.dumps(body).encode()

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or 500 <= e.code < 600):
                try:
                    body = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    body = ""
                raise RuntimeError(f"HTTP {e.code}: {body or e.reason}") from e

            err_code = e.code
            err_reason = e.reason
            err_headers = e.headers
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                err_body = ""

            wait = _parse_retry_after(err_headers) or min(5 * (2**attempt), 300.0)
            wait = wait * (0.5 + random.random())

            print(
                f"[llm] HTTP {err_code} from {url}; retrying in {wait:.1f}s (attempt {attempt})",
                file=sys.stderr,
            )
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            wait = min(5 * (2 ** attempt), 300.0) * (0.5 + random.random())
            print(
                f"[llm] Connection error: {e}; retrying in {wait:.1f}s (attempt {attempt})",
                file=sys.stderr,
            )
            time.sleep(wait)


def _format_retry_detail(code, body, wait, attempt, max_retries):
    parts = [f"HTTP {code}"]
    msg = _extract_error_message(body)
    if msg:
        parts.append(msg)
    parts.append(f"retry {attempt}/{max_retries} in {wait:.0f}s")
    return " — ".join(parts)


def _extract_error_message(body):
    if not body:
        return ""
    try:
        obj = json.loads(body)
        err = obj.get("error", obj)
        if isinstance(err, dict):
            return err.get("message", "")
        return str(err)[:120]
    except (json.JSONDecodeError, AttributeError):
        return body.strip().splitlines()[0][:120] if body.strip() else ""


def _parse_retry_after(headers):
    """Read `Retry-After` as a float seconds, or None. Accepts integer/
    float seconds (Anthropic's default) and falls back to None on HTTP-
    date format (not worth the parsing dance for our use case)."""
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


def _anthropic_chat(messages, model, tools_enabled, max_tokens=32768, **_kwargs):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    # Deep-copy so we can attach cache_control markers without mutating the
    # caller's messages list (which chat_with_tools builds up turn-by-turn).
    msgs = _with_cache_control(messages)

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": msgs,
    }
    tool_names = _resolve_tool_names(tools_enabled)
    if tool_names:
        tools = _anthropic_tool_schemas(names=tool_names)
        if tools:
            # Cache the tool-definitions block. These never change between
            # calls, so every request after the first reads them from cache
            # (charged at ~10% of normal input tokens, and cache reads DON'T
            # count against the non-cache ITPM limit — which is typically
            # the rate limit that bites at scale).
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        body["tools"] = tools

    result = _http_post(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        body,
    )
    return result


def _with_cache_control(messages):
    """Return a deep-copy of `messages` with cache_control: ephemeral
    attached to the last content block of the last message.

    Anthropic's prompt caching stores attention state for the prefix up
    to the marker. On subsequent calls where the prefix is byte-identical,
    the cached state is reused (charged at ~10% of normal input tokens).
    For our workload — multi-turn tool-use conversations where each turn
    replays the full growing context — this means every turn after the
    first gets caching benefit on all prior turns automatically.

    Cache reads don't count against the non-cache ITPM rate limit, so
    this also gives substantial rate-limit headroom.
    """
    msgs = copy.deepcopy(messages)
    if not msgs:
        return msgs
    last = msgs[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        # Already in block form — mark the last block. It may already
        # have other fields (tool_result, etc.); cache_control coexists.
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}
    return msgs


def _anthropic_extract_response(result):
    text_parts = []
    tool_calls = []
    for block in result.get("content", []):
        if block["type"] == "text":
            text_parts.append(block["text"])
        elif block["type"] == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": block["input"],
                }
            )
    usage = {
        "input_tokens": result.get("usage", {}).get("input_tokens"),
        "output_tokens": result.get("usage", {}).get("output_tokens"),
        "cache_read": result.get("usage", {}).get("cache_read_input_tokens"),
        "cache_creation": result.get("usage", {}).get("cache_creation_input_tokens"),
    }
    return "\n".join(text_parts), tool_calls, result.get("stop_reason"), usage


def _anthropic_tool_result_message(tool_calls, results):
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result,
            }
            for tc, result in zip(tool_calls, results)
        ],
    }


def _anthropic_assistant_message(result):
    return {"role": "assistant", "content": result.get("content", [])}


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter (Cerebras, OpenAI, etc.)
# ---------------------------------------------------------------------------


def _openai_chat(messages, model, tools_enabled, api_base=None, api_key_env="OPENAI_API_KEY", **_kwargs):
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{api_key_env} not set")

    base = (api_base or "https://api.openai.com/v1").rstrip("/")

    # Convert messages to a STRUCTURED Responses API `input` array. The
    # prior implementation flattened everything to a single string, which
    # defeated server-side prefix caching: each agentic turn appended new
    # content to the same string, so the cache key changed every call and
    # we re-paid full input rate on the entire conversation. With the
    # array form the prefix is byte-identical across turns and OpenAI's
    # automatic prompt cache (~50% off on cached tokens) actually engages.
    input_items = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            if isinstance(content, str):
                if content:
                    input_items.append(
                        {"type": "message", "role": "user", "content": content}
                    )
            elif isinstance(content, list):
                # Anthropic-style tool_result blocks → Responses API
                # function_call_output items. Plain text blocks → user
                # messages.
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out_text = block.get("content", "")
                        if isinstance(out_text, list):
                            # Multi-block tool result — flatten.
                            out_text = "".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in out_text
                            )
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": out_text,
                        })
                    elif isinstance(block, dict) and block.get("type") == "text":
                        input_items.append({
                            "type": "message", "role": "user",
                            "content": block.get("text", ""),
                        })
                    elif isinstance(block, str):
                        input_items.append({
                            "type": "message", "role": "user", "content": block,
                        })
        elif role == "_openai_assistant":
            # Marker message stashed by _openai_assistant_message — replay
            # the prior turn's output items so the conversation is
            # faithfully reconstructed (and prefix-cacheable).
            #
            # Pass through reasoning items, function_call items, etc.
            # verbatim. GPT-5 reasoning models pair each function_call
            # with a `reasoning` item by ID — dropping the reasoning
            # item triggers a 400 "function_call provided without its
            # required reasoning item" on the next turn.
            #
            # Only `message` items need flattening: the output uses
            # `output_text` content blocks, but input messages expect
            # plain strings (or input_text blocks). Flatten to a string.
            for item in msg.get("output_items") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "message":
                    blocks = item.get("content") or []
                    text_parts = []
                    if isinstance(blocks, list):
                        for b in blocks:
                            if isinstance(b, dict):
                                txt = b.get("text", "")
                                if txt:
                                    text_parts.append(txt)
                            elif isinstance(b, str):
                                text_parts.append(b)
                    elif isinstance(blocks, str):
                        text_parts.append(blocks)
                    if text_parts:
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": "\n".join(text_parts),
                        })
                else:
                    # reasoning, function_call, web_search_call, etc. —
                    # pass through unchanged.
                    input_items.append(item)
        elif role == "assistant":
            # Anthropic-style assistant content arrives as a list of
            # blocks; translate any tool_use blocks into function_call
            # items so the Responses API knows what was called.
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        input_items.append({
                            "type": "function_call",
                            "call_id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        })
                    elif block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            input_items.append({
                                "type": "message", "role": "assistant",
                                "content": text,
                            })
            elif isinstance(content, str) and content:
                input_items.append({
                    "type": "message", "role": "assistant", "content": content,
                })
        elif role == "tool":
            # Chat-Completions-style tool message
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else json.dumps(content),
            })

    body = {
        "model": model,
        "input": input_items,
    }
    tool_names = _resolve_tool_names(tools_enabled)
    if tool_names:
        body["tools"] = _openai_responses_tools(names=tool_names)

    result = _http_post(
        f"{base}/responses",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body,
    )
    return result


def _openai_extract_response(result):
    text_parts = []
    tool_calls = []
    for item in result.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text_parts.append(content.get("text", ""))
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                {
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": args,
                }
            )
    usage_raw = result.get("usage", {})
    cached = (usage_raw.get("input_tokens_details") or {}).get("cached_tokens") or 0
    input_total = usage_raw.get("input_tokens") or 0
    # Normalize to Anthropic's convention: input_tokens excludes the
    # cached portion. OpenAI reports input_tokens GROSS (including cache
    # hits) and breaks out cached_tokens separately. compute_cost charges
    # input_tokens at the full rate AND cache_read at the cache rate, so
    # if we left input_tokens gross the cached portion would be billed
    # twice (once at full + once at half). Subtracting here keeps the
    # cost formula provider-agnostic.
    usage = {
        "input_tokens": max(input_total - cached, 0),
        "output_tokens": usage_raw.get("output_tokens"),
        "cache_read": cached,
    }
    return "\n".join(text_parts), tool_calls, result.get("status"), usage


def _openai_tool_result_message(tool_calls, results):
    # Use Anthropic-shaped tool_result blocks under the user role; the
    # _openai_chat input-item builder translates them to Responses API
    # function_call_output items. This keeps the messages list provider-
    # neutral so the same chat_with_tools loop drives both adapters.
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result,
            }
            for tc, result in zip(tool_calls, results)
        ],
    }


def _openai_assistant_message(result):
    # Stash the prior turn's full output array as a synthetic message so
    # _openai_chat can replay the items verbatim on the next turn. This
    # lets the Responses API see byte-identical prefixes across turns and
    # lets server-side prefix caching engage. Was previously an empty
    # placeholder, which (combined with concat-as-string input) defeated
    # caching entirely and made gpt-5.5 verifier calls cost 10x opus's.
    return {
        "role": "_openai_assistant",
        "output_items": result.get("output", []),
    }


# ---------------------------------------------------------------------------
# Chat Completions adapter (Cerebras, etc.)
# ---------------------------------------------------------------------------


def _chatcompletions_chat(messages, model, tools_enabled, api_base=None, api_key_env="OPENAI_API_KEY", **_kwargs):
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{api_key_env} not set")

    base = (api_base or "https://api.openai.com/v1").rstrip("/")

    cc_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "tool":
            cc_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
            )
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    cc_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        }
                    )
                elif isinstance(block, str):
                    parts.append(block)
            if parts:
                cc_messages.append({"role": role, "content": "\n".join(parts)})
        else:
            out = {"role": role, "content": content}
            if role == "assistant" and "tool_calls" in msg:
                out["tool_calls"] = msg["tool_calls"]
            cc_messages.append(out)

    body = {"model": model, "messages": cc_messages}
    tool_names = _resolve_tool_names(tools_enabled)
    if tool_names:
        body["tools"] = _openai_chatcompletions_tools(names=tool_names)

    result = _http_post(
        f"{base}/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body,
    )
    return result


def _chatcompletions_extract_response(result):
    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})
    text = message.get("content") or ""
    tool_calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(
            {
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": args,
            }
        )
    usage_raw = result.get("usage", {})
    usage = {
        "input_tokens": usage_raw.get("prompt_tokens"),
        "output_tokens": usage_raw.get("completion_tokens"),
    }
    return text, tool_calls, choice.get("finish_reason"), usage


def _chatcompletions_tool_result_message(tool_calls, results):
    return [
        {
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": result,
        }
        for tc, result in zip(tool_calls, results)
    ]


def _chatcompletions_assistant_message(result):
    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})
    out = {"role": "assistant", "content": message.get("content", "")}
    if "tool_calls" in message:
        out["tool_calls"] = message["tool_calls"]
    return out


# ---------------------------------------------------------------------------
# Provider config and registry
# ---------------------------------------------------------------------------

PROVIDER_CONFIG = {
    "anthropic": {"adapter": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"adapter": "openai", "api_key_env": "OPENAI_API_KEY"},
    "cerebras": {
        "adapter": "chatcompletions",
        "api_key_env": "CEREBRAS_API_KEY",
        "api_base": "https://api.cerebras.ai/v1",
    },
}

PROVIDERS = {
    "anthropic": {
        "chat": _anthropic_chat,
        "extract": _anthropic_extract_response,
        "tool_result_message": _anthropic_tool_result_message,
        "assistant_message": _anthropic_assistant_message,
    },
    "openai": {
        "chat": _openai_chat,
        "extract": _openai_extract_response,
        "tool_result_message": _openai_tool_result_message,
        "assistant_message": _openai_assistant_message,
    },
    "chatcompletions": {
        "chat": _chatcompletions_chat,
        "extract": _chatcompletions_extract_response,
        "tool_result_message": _chatcompletions_tool_result_message,
        "assistant_message": _chatcompletions_assistant_message,
    },
}


def parse_model_spec(spec):
    if ":" not in spec:
        raise ValueError(f"Invalid model spec {spec!r}: expected 'provider:model'")
    provider, model = spec.split(":", 1)
    if provider not in PROVIDER_CONFIG:
        raise ValueError(f"Unknown provider {provider!r}. Known providers: {', '.join(sorted(PROVIDER_CONFIG))}")
    return provider, model


def model_slug_from_spec(spec):
    """Derive a filesystem-safe slug from a provider:model spec.

    E.g. "openai:gpt-5.5" -> "openai-gpt-5.5"
         "cerebras:llama-4-scout" -> "cerebras-llama-4-scout"
    """
    return _re.sub(r"[^a-zA-Z0-9._-]", "-", spec)


# ---------------------------------------------------------------------------
# Generic tool-call loop
# ---------------------------------------------------------------------------


FINALIZE_NUDGE = (
    "Investigation budget exhausted. Do not call any more tools.\n"
    "\n"
    "If — AND ONLY IF — you can commit to an answer based on what you've "
    "already seen without guessing, emit it now in exactly the format "
    "the original prompt required (e.g. an <<<ANSWER>>>...<<<END>>> "
    "envelope or the specified JSON shape), and nothing else.\n"
    "\n"
    "If you cannot commit without guessing — you were mid-trace, you "
    "needed a file you never got to read, the code is genuinely ambiguous "
    "beyond what your budget allowed — emit NOTHING. An empty response is "
    "the correct signal; the system will retry this item on a later pass "
    "with a fresh budget. Do NOT fall back to a placeholder or a hedged "
    "answer just to avoid emitting nothing."
)


def chat_with_tools(
    prompt,
    provider="anthropic",
    model="claude-opus-4-7",
    tools_enabled=True,
    cwd=None,
    max_turns=40,
    finalize_nudge=FINALIZE_NUDGE,
    tools=None,
    event_log=None,
    **provider_kwargs,
):
    # `tools` overrides `tools_enabled` when set — fix authors pass an
    # explicit list including apply_edit / write_file. Read-only callers
    # (audit, contract, verifiers) keep tools_enabled=True and get the
    # default read-only set.
    if tools is not None:
        tools_enabled = tools
    _resolved_tools = _resolve_tool_names(tools_enabled)
    _readonly = not any(t in _resolved_tools for t in ("apply_edit", "write_file"))
    config = PROVIDER_CONFIG.get(provider, {})
    adapter_name = config.get("adapter", provider)
    adapter = PROVIDERS[adapter_name]

    # Merge config kwargs (api_base, api_key_env) under explicit overrides
    merged_kwargs = {k: v for k, v in config.items() if k not in ("adapter",)}
    merged_kwargs.update(provider_kwargs)
    chat_fn = adapter["chat"]
    extract_fn = adapter["extract"]
    tool_result_fn = adapter["tool_result_message"]
    assistant_msg_fn = adapter["assistant_message"]

    messages = [{"role": "user", "content": prompt}]
    all_text = []
    # Track cache_read + cache_creation alongside the raw input/output —
    # without these keys in the accumulator, per-turn cache info from
    # the providers gets silently dropped and cost tracking shows 0
    # cache hits even when the API is caching properly.
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
    }

    if event_log:
        event_log.emit("prompt", prompt=prompt[:5000], model=model, provider=provider)

    p_log = _prompt_log_path()

    hit_ceiling_with_pending = False
    turn = 0
    while max_turns is None or turn < max_turns:
        turn += 1
        result = chat_fn(messages, model, tools_enabled, **merged_kwargs)
        text, tool_calls, stop_reason, usage = extract_fn(result)

        if text:
            all_text.append(text)
        for k in total_usage:
            if usage.get(k):
                total_usage[k] += usage[k]

        if event_log:
            event_log.emit("llm_response", turn=turn, text=text[:3000] if text else "",
                           tool_calls=[{"name": tc["name"], "arguments": tc["arguments"]} for tc in tool_calls],
                           stop_reason=stop_reason, usage=usage)

        if not tool_calls:
            break

        # Execute tools
        results = []
        for tc in tool_calls:
            t0 = time.monotonic()
            r = execute_tool(tc["name"], tc["arguments"], cwd=cwd, readonly=_readonly)
            elapsed = time.monotonic() - t0
            _log_tool_call(tc["name"], tc["arguments"], r, elapsed)
            if event_log:
                result_len = len(r) if isinstance(r, str) else len(str(r))
                event_log.emit("tool_result", turn=turn, name=tc["name"],
                               arguments=tc["arguments"],
                               result=r[:2000] if isinstance(r, str) else str(r)[:2000],
                               result_len=result_len,
                               elapsed=round(elapsed, 1))
            results.append(r)

        # Append assistant message + tool results to conversation
        messages.append(assistant_msg_fn(result))
        tool_msg = tool_result_fn(tool_calls, results)
        if isinstance(tool_msg, list):
            messages.extend(tool_msg)
        else:
            messages.append(tool_msg)

        _log_prompt_turn(p_log, turn, tool_calls, results, messages)

        # Detect the ceiling-with-pending case: we just executed tools on
        # what will be the last iteration. Without a finalize turn, the
        # model never gets to speak after the tool results — that's the
        # "response not valid JSON" soft-fail class. Claude Code's loop
        # runs until end_turn; we approximate by giving the model one
        # tools-disabled turn to emit the envelope with what it has.
        if max_turns is not None and turn >= max_turns:
            hit_ceiling_with_pending = True

    if hit_ceiling_with_pending and finalize_nudge:
        if event_log:
            event_log.emit("finalize_nudge", turn=turn + 1)
        messages.append({"role": "user", "content": finalize_nudge})
        try:
            result = chat_fn(messages, model, False, **merged_kwargs)
            text, _tool_calls, _stop, usage = extract_fn(result)
            if text:
                all_text.append(text)
            for k in total_usage:
                if usage.get(k):
                    total_usage[k] += usage[k]
            if event_log:
                event_log.emit("finalize_response", turn=turn + 1, text=text[:3000] if text else "", usage=usage)
        except Exception as e:
            print(f"[llm] finalize nudge failed: {e}", file=sys.stderr)
            if event_log:
                event_log.emit("finalize_error", error=str(e))

    if event_log:
        event_log.emit("done", total_turns=turn, total_usage=total_usage,
                        final_text="\n".join(all_text)[:5000])

    return "\n".join(all_text), total_usage


# ---------------------------------------------------------------------------
# Simplified interface for memgraph pipeline
# ---------------------------------------------------------------------------

READONLY_TOOLS = ("read_file", "run_command")
ALL_TOOLS = ("read_file", "run_command", "apply_edit", "write_file")


def chat(prompt, provider="anthropic", model="claude-opus-4-7", tools=None, cwd=None, max_turns=80, event_log=None):
    """
    Convenience wrapper around chat_with_tools.

    Maps provider names: "claude" -> "anthropic", "openai" -> "openai".
    """
    provider_map = {"claude": "anthropic", "openai": "openai"}
    mapped = provider_map.get(provider, provider)
    return chat_with_tools(
        prompt,
        provider=mapped,
        model=model,
        tools=tools,
        cwd=cwd,
        max_turns=max_turns,
        finalize_nudge=None,
        event_log=event_log,
    )


class Session:
    """A persistent LLM conversation that can receive multiple prompts.

    Unlike chat(), which starts a fresh conversation each call, Session
    keeps the full message history so follow-up prompts have context from
    prior turns.

    Usage:
        s = Session(provider="claude", model="claude-opus-4-7", tools=ALL_TOOLS, cwd="/path")
        text, usage = s.send("Fix this memory leak...")
        # ... later, same conversation ...
        text, usage = s.send("CI failed. Fix it.")
        # ... later ...
        text, usage = s.send("Address these review comments: ...")
    """

    def __init__(self, provider="anthropic", model="claude-opus-4-7", tools=None, cwd=None, max_turns_per_send=80, event_log=None):
        provider_map = {"claude": "anthropic", "openai": "openai"}
        self._provider = provider_map.get(provider, provider)
        self._model = model
        self._tools = tuple(tools) if tools else ()
        self._readonly = not any(t in self._tools for t in ("apply_edit", "write_file"))
        self._cwd = cwd
        self._max_turns = max_turns_per_send
        self._messages = []
        self._total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
        self._event_log = event_log
        self._send_count = 0

        config = PROVIDER_CONFIG.get(self._provider, {})
        adapter_name = config.get("adapter", self._provider)
        self._adapter = PROVIDERS[adapter_name]
        self._kwargs = {k: v for k, v in config.items() if k not in ("adapter",)}

    def send(self, prompt):
        """Send a follow-up message and run tool calls until the model stops.

        Returns (text, usage) for this send only.
        """
        self._send_count += 1
        send_id = self._send_count
        self._messages.append({"role": "user", "content": prompt})

        if self._event_log:
            self._event_log.emit("send", send_id=send_id,
                                 prompt=prompt[:5000], model=self._model)

        chat_fn = self._adapter["chat"]
        extract_fn = self._adapter["extract"]
        tool_result_fn = self._adapter["tool_result_message"]
        assistant_msg_fn = self._adapter["assistant_message"]

        all_text = []
        send_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}

        for turn in range(self._max_turns):
            result = chat_fn(self._messages, self._model, self._tools, **self._kwargs)
            text, tool_calls, stop_reason, usage = extract_fn(result)

            if text:
                all_text.append(text)
            for k in send_usage:
                send_usage[k] += usage.get(k, 0)

            if self._event_log:
                self._event_log.emit("llm_response", send_id=send_id, turn=turn + 1,
                                     text=text[:3000] if text else "",
                                     tool_calls=[{"name": tc["name"], "arguments": tc["arguments"]} for tc in tool_calls],
                                     stop_reason=stop_reason, usage=usage)

            if not tool_calls:
                # Append assistant response to history
                self._messages.append(assistant_msg_fn(result))
                break

            # Execute tools
            results = []
            for tc in tool_calls:
                t0 = time.monotonic()
                r = execute_tool(tc["name"], tc["arguments"], cwd=self._cwd, readonly=self._readonly)
                elapsed = time.monotonic() - t0
                _log_tool_call(tc["name"], tc["arguments"], r, elapsed)
                if self._event_log:
                    result_len = len(r) if isinstance(r, str) else len(str(r))
                    self._event_log.emit("tool_result", send_id=send_id, turn=turn + 1,
                                         name=tc["name"], arguments=tc["arguments"],
                                         result=r[:2000] if isinstance(r, str) else str(r)[:2000],
                                         result_len=result_len,
                                         elapsed=round(elapsed, 1))
                results.append(r)

            self._messages.append(assistant_msg_fn(result))
            tool_msg = tool_result_fn(tool_calls, results)
            if isinstance(tool_msg, list):
                self._messages.extend(tool_msg)
            else:
                self._messages.append(tool_msg)

        for k in self._total_usage:
            self._total_usage[k] += send_usage[k]

        if self._event_log:
            self._event_log.emit("send_done", send_id=send_id, turns=turn + 1,
                                 usage=send_usage,
                                 final_text="\n".join(all_text)[:5000])

        return "\n".join(all_text), send_usage

    @property
    def total_usage(self):
        return dict(self._total_usage)
