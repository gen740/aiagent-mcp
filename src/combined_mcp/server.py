from __future__ import annotations

import base64
import difflib
import fnmatch
import mimetypes
import os
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


SERVER_NAME = "combined-filesystem-shell"
MAX_SHELL_TIMEOUT_SECONDS = 300
DEFAULT_GIT_CONTEXT_LINES = 3
MAX_READ_TEXT_BYTES = 2 * 1024 * 1024
MAX_READ_BINARY_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_RESULTS = 1000
mcp = FastMCP(SERVER_NAME)
_tools = None


class ToolError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PathGuard:
    def __init__(self, raw_directories: list[str]) -> None:
        directories: list[Path] = []
        for raw in raw_directories:
            if not raw:
                continue
            expanded = Path(os.path.expanduser(raw)).resolve(strict=False)
            try:
                resolved = expanded.resolve(strict=True)
            except FileNotFoundError:
                print(
                    f"Warning: allowed directory does not exist, skipping: {expanded}",
                    file=sys.stderr,
                )
                continue
            if not resolved.is_dir():
                print(
                    f"Warning: allowed path is not a directory, skipping: {resolved}",
                    file=sys.stderr,
                )
                continue
            directories.append(resolved)

        unique: list[Path] = []
        for directory in directories:
            if directory not in unique:
                unique.append(directory)

        if not unique:
            raise ToolError("No accessible allowed directories configured")
        self.allowed_directories = unique

    def validate(self, requested_path: str, *, parent_may_exist: bool = False) -> Path:
        if "\x00" in requested_path:
            raise ToolError("Path contains a null byte")

        expanded = Path(os.path.expanduser(requested_path))
        if not expanded.is_absolute():
            expanded = self.allowed_directories[0] / expanded
        absolute = expanded.resolve(strict=False)

        if absolute.exists():
            real = absolute.resolve(strict=True)
            if not self._is_allowed(real):
                raise ToolError(
                    f"Access denied - path outside allowed directories: {absolute}"
                )
            return real

        parent = absolute.parent
        if not parent.exists():
            if parent_may_exist:
                self.validate(str(parent), parent_may_exist=True)
                return absolute
            raise ToolError(f"Parent directory does not exist: {parent}")

        real_parent = parent.resolve(strict=True)
        if not self._is_allowed(real_parent):
            raise ToolError(
                f"Access denied - parent outside allowed directories: {real_parent}"
            )
        return absolute

    def _is_allowed(self, path: Path) -> bool:
        return any(
            path == allowed or allowed in path.parents
            for allowed in self.allowed_directories
        )

    def list_allowed(self) -> list[str]:
        return [str(path) for path in self.allowed_directories]


class CombinedTools:
    def __init__(self, guard: PathGuard) -> None:
        self.guard = guard

    def search(
        self, root: Path, pattern: str, excludes: list[str], max_results: int
    ) -> tuple[list[str], bool]:
        if max_results < 1:
            raise ToolError("maxResults must be at least 1")

        results: list[str] = []
        truncated = False
        for current_root, dir_names, file_names in os.walk(root):
            current = Path(current_root)
            rel_dir = current.relative_to(root).as_posix()
            if rel_dir == ".":
                rel_dir = ""

            dir_names[:] = [
                item
                for item in dir_names
                if not excluded(join_rel(rel_dir, item), excludes)
            ]
            for name_item in dir_names + file_names:
                rel = join_rel(rel_dir, name_item)
                if excluded(rel, excludes):
                    continue
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name_item, pattern):
                    candidate = current / name_item
                    try:
                        self.guard.validate(str(candidate))
                    except ToolError:
                        continue
                    results.append(str(candidate))
                    if len(results) >= max_results:
                        truncated = True
                        dir_names[:] = []
                        return results, truncated
        return results, truncated

    def shell_exec(
        self,
        command: str,
        cwd: str | None,
        timeout_seconds: int,
        extra_env: dict[str, str] | None,
    ) -> dict[str, Any]:
        working_directory = self.guard.validate(
            cwd if cwd else str(self.guard.allowed_directories[0])
        )
        if not working_directory.is_dir():
            raise ToolError(f"cwd is not a directory: {working_directory}")

        if timeout_seconds < 1 or timeout_seconds > MAX_SHELL_TIMEOUT_SECONDS:
            raise ToolError(
                f"timeoutSeconds must be between 1 and {MAX_SHELL_TIMEOUT_SECONDS}"
            )

        env = os.environ.copy()
        env.update(extra_env or {})
        result = run_shell_process(command, working_directory, env, timeout_seconds)
        result["cwd"] = str(working_directory)
        return result


def read_lines(path: Path, head: int | None, tail: int | None) -> str:
    if head is not None and tail is not None:
        raise ToolError("Cannot specify both head and tail")
    if head is not None and head < 0:
        raise ToolError("head must be non-negative")
    if tail is not None and tail < 0:
        raise ToolError("tail must be non-negative")

    if head is not None:
        return read_head(path, head)
    if tail is not None:
        return read_tail(path, tail)

    size = path.stat().st_size
    if size > MAX_READ_TEXT_BYTES:
        raise ToolError(
            f"File is too large for fs-read without head/tail: {size} bytes > {MAX_READ_TEXT_BYTES} bytes"
        )
    return path.read_text(encoding="utf-8", errors="replace")


def read_head(path: Path, line_count: int) -> str:
    if line_count == 0:
        return ""

    data = bytearray()
    lines_seen = 0
    with path.open("rb") as file:
        while lines_seen < line_count and len(data) <= MAX_READ_TEXT_BYTES:
            chunk = file.readline()
            if not chunk:
                break
            data.extend(chunk)
            lines_seen += 1

    if len(data) > MAX_READ_TEXT_BYTES:
        raise ToolError(f"fs-read head output exceeds {MAX_READ_TEXT_BYTES} bytes")
    return bytes(data).decode("utf-8", errors="replace").rstrip("\n")


def read_tail(path: Path, line_count: int) -> str:
    if line_count == 0:
        return ""

    size = path.stat().st_size
    start = max(0, size - MAX_READ_TEXT_BYTES)
    with path.open("rb") as file:
        file.seek(start)
        data = file.read(MAX_READ_TEXT_BYTES + 1)

    if len(data) > MAX_READ_TEXT_BYTES:
        raise ToolError(
            f"fs-read tail window exceeds {MAX_READ_TEXT_BYTES} bytes; use fewer lines or a smaller file"
        )
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-line_count:])


def format_stat(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {
        "path": str(path),
        "size": info.st_size,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_ctime)),
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime)),
        "accessed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_atime)),
        "isDirectory": path.is_dir(),
        "isFile": path.is_file(),
        "permissions": stat.filemode(info.st_mode),
    }


def excluded(relative_path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative_path, pattern)
        or fnmatch.fnmatch(Path(relative_path).name, pattern)
        for pattern in patterns
    )


def join_rel(parent: str, child: str) -> str:
    return child if not parent else f"{parent}/{child}"


def build_tree(
    root: Path,
    current: Path,
    guard: PathGuard,
    excludes: list[str],
    max_depth: int | None,
    depth: int,
    max_entries: int,
    counter: dict[str, int],
    visited_directories: set[Path],
) -> list[dict[str, Any]]:
    if max_entries < 1:
        raise ToolError("maxResults must be at least 1")
    if max_depth is not None and depth > max_depth:
        return []

    entries: list[dict[str, Any]] = []
    for child in sorted(current.iterdir(), key=lambda item: item.name.lower()):
        if counter["count"] >= max_entries:
            counter["truncated"] = 1
            break

        rel = child.relative_to(root).as_posix()
        if excluded(rel, excludes):
            continue
        try:
            guard.validate(str(child))
        except ToolError:
            continue

        is_directory = child.is_dir()
        item: dict[str, Any] = {
            "name": child.name,
            "type": "directory" if is_directory else "file",
        }
        counter["count"] += 1
        if is_directory:
            real_directory = child.resolve(strict=True)
            if real_directory in visited_directories:
                item["children"] = []
            else:
                item["children"] = build_tree(
                    root,
                    child,
                    guard,
                    excludes,
                    max_depth,
                    depth + 1,
                    max_entries,
                    counter,
                    visited_directories | {real_directory},
                )
        entries.append(item)
    return entries


def allowed_directories_from_args(argv: list[str]) -> list[str]:
    if argv:
        return argv
    env_value = os.environ.get("MCP_ALLOWED_DIRS", "")
    if env_value:
        return env_value.split(os.pathsep)
    if Path("/projects").is_dir():
        return ["/projects"]
    return [os.getcwd()]


def read_limited_output(file: Any, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    file.seek(0)
    data = file.read(limit + 1)
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def run_process(
    args: list[str], cwd: Path, timeout_seconds: int = 60
) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
            stderr.write(
                f"\nCommand timed out after {timeout_seconds} seconds\n".encode()
            )

        stdout_text, stdout_truncated = read_limited_output(stdout)
        stderr_text, stderr_truncated = read_limited_output(stderr)
        return {
            "exitCode": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdoutTruncated": stdout_truncated,
            "stderrTruncated": stderr_truncated,
            "maxOutputBytes": MAX_OUTPUT_BYTES,
        }


def run_shell_process(
    command: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                shell=True,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            process.wait(timeout=timeout_seconds)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            stderr.write(
                f"\nCommand timed out after {timeout_seconds} seconds\n".encode()
            )

        stdout_text, stdout_truncated = read_limited_output(stdout)
        stderr_text, stderr_truncated = read_limited_output(stderr)
        return {
            "exitCode": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdoutTruncated": stdout_truncated,
            "stderrTruncated": stderr_truncated,
            "maxOutputBytes": MAX_OUTPUT_BYTES,
        }


def ensure_not_option(value: str, label: str) -> None:
    if value.startswith("-"):
        raise ToolError(f"{label} cannot start with '-': {value}")


def validate_repo_path(repo_path: str) -> Path:
    repo = get_tools().guard.validate(repo_path)
    if not repo.is_dir():
        raise ToolError(f"repo_path is not a directory: {repo}")

    result = run_process(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], repo)
    if result["exitCode"] != 0:
        raise ToolError(f"Not a Git repository: {repo}\n{result['stderr']}")

    top_level = get_tools().guard.validate(result["stdout"].strip())
    if not top_level.is_dir():
        raise ToolError(f"Git top-level is not a directory: {top_level}")
    return top_level


def git(repo_path: str, *args: str, timeout_seconds: int = 60) -> dict[str, Any]:
    repo = validate_repo_path(repo_path)
    return run_process(["git", "-C", str(repo), *args], repo, timeout_seconds)


def git_text(repo_path: str, *args: str, timeout_seconds: int = 60) -> dict[str, Any]:
    result = git(repo_path, *args, timeout_seconds=timeout_seconds)
    if result["exitCode"] != 0:
        raise ToolError(result["stderr"] or result["stdout"] or "git command failed")
    return result


def validate_repo_files(repo: Path, files: list[str]) -> None:
    if files == ["."]:
        return
    for file_path in files:
        if "\x00" in file_path:
            raise ToolError("file path contains a null byte")
        candidate = (repo / file_path).resolve(strict=False)
        try:
            candidate.relative_to(repo)
        except ValueError:
            raise ToolError(f"Path is outside repository: {file_path}") from None


def patch_path_from_header(value: str) -> str | None:
    path = value.split("\t", 1)[0].strip()
    if not path or path == "/dev/null":
        return None
    if (path.startswith("a/") or path.startswith("b/")) and len(path) > 2:
        return path[2:]
    return path


def extract_patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            for item in parts[2:4]:
                path = patch_path_from_header(item)
                if path is not None:
                    paths.add(path)
        elif line.startswith(("--- ", "+++ ")):
            path = patch_path_from_header(line[4:])
            if path is not None:
                paths.add(path)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            path = patch_path_from_header(line.split(" ", 2)[2])
            if path is not None:
                paths.add(path)
    return paths


def validate_patch_paths(cwd: Path, patch: str) -> list[str]:
    paths = sorted(extract_patch_paths(patch))
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolError(f"Patch path is not allowed: {path}")
        get_tools().guard.validate(str(cwd / candidate), parent_may_exist=True)
    return paths


def remove_path(path: Path, recursive: bool) -> None:
    if path.is_dir():
        if not recursive:
            raise ToolError(
                f"Refusing to delete directory without recursive=true: {path}"
            )
        shutil.rmtree(path)
    else:
        path.unlink()


def apply_text_edits(content: str, edits: list[dict[str, str]]) -> str:
    updated = content.replace("\r\n", "\n")
    for edit in edits:
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolError("Each edit must contain string oldText and newText")
        normalized_old = old_text.replace("\r\n", "\n")
        normalized_new = new_text.replace("\r\n", "\n")
        if normalized_old not in updated:
            raise ToolError(f"Could not find exact match for edit:\n{old_text}")
        updated = updated.replace(normalized_old, normalized_new, 1)
    return updated


def unified_diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def ensure_stash_ref(value: str | None, label: str) -> None:
    if value is None:
        return
    ensure_not_option(value, label)


def get_tools() -> CombinedTools:
    if _tools is None:
        raise ToolError("Server tools have not been initialized")
    return _tools


@mcp.tool(name="fs-read", annotations=ToolAnnotations(readOnlyHint=True))
def fs_read(
    path: str, head: int | None = None, tail: int | None = None
) -> dict[str, str]:
    """Read a UTF-8 text file inside allowed directories."""
    tools = get_tools()
    valid_path = tools.guard.validate(path)
    return {"content": read_lines(valid_path, head, tail)}


@mcp.tool(name="fs-read-binary", annotations=ToolAnnotations(readOnlyHint=True))
def fs_read_binary(path: str) -> dict[str, str]:
    """Read a file as base64 with MIME type inside allowed directories."""
    valid_path = get_tools().guard.validate(path)
    size = valid_path.stat().st_size
    if size > MAX_READ_BINARY_BYTES:
        raise ToolError(
            f"File is too large for fs-read-binary: {size} bytes > {MAX_READ_BINARY_BYTES} bytes"
        )
    return {
        "path": str(valid_path),
        "mimeType": mimetypes.guess_type(valid_path.name)[0]
        or "application/octet-stream",
        "data": base64.b64encode(valid_path.read_bytes()).decode("ascii"),
    }


@mcp.tool(
    name="fs-write",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=True
    ),
)
def fs_write(path: str, content: str, createParents: bool = False) -> dict[str, str]:
    """Create or overwrite a UTF-8 text file inside allowed directories."""
    tools = get_tools()
    valid_path = tools.guard.validate(path, parent_may_exist=createParents)
    if createParents:
        valid_path.parent.mkdir(parents=True, exist_ok=True)
        valid_path = tools.guard.validate(path)
    temp_path = valid_path.with_name(f".{valid_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(valid_path)
    return {"content": f"Successfully wrote to {valid_path}"}


@mcp.tool(name="fs-list", annotations=ToolAnnotations(readOnlyHint=True))
def fs_list(path: str, withSizes: bool = False) -> dict[str, str]:
    """List a directory inside allowed directories."""
    valid_path = get_tools().guard.validate(path)
    if not valid_path.is_dir():
        raise ToolError(f"Not a directory: {valid_path}")
    lines = []
    for child in sorted(valid_path.iterdir(), key=lambda item: item.name.lower()):
        prefix = "[DIR]" if child.is_dir() else "[FILE]"
        suffix = (
            f" {child.stat().st_size} bytes" if withSizes and child.is_file() else ""
        )
        lines.append(f"{prefix} {child.name}{suffix}")
    return {"content": "\n".join(lines)}


@mcp.tool(
    name="fs-mkdir",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=False
    ),
)
def fs_mkdir(path: str) -> dict[str, str]:
    """Create a directory and any missing parents inside allowed directories."""
    valid_path = get_tools().guard.validate(path, parent_may_exist=True)
    valid_path.mkdir(parents=True, exist_ok=True)
    return {"content": f"Successfully created directory {valid_path}"}


@mcp.tool(
    name="fs-move",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def fs_move(source: str, destination: str) -> dict[str, str]:
    """Move or rename a file or directory inside allowed directories."""
    tools = get_tools()
    valid_source = tools.guard.validate(source)
    valid_destination = tools.guard.validate(destination)
    if valid_destination.exists():
        raise ToolError(f"Destination already exists: {valid_destination}")
    shutil.move(str(valid_source), str(valid_destination))
    return {"content": f"Successfully moved {valid_source} to {valid_destination}"}


@mcp.tool(
    name="fs-delete",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def fs_delete(
    path: str, recursive: bool = False, dryRun: bool = True
) -> dict[str, Any]:
    """Delete a file or directory inside allowed directories."""
    valid_path = get_tools().guard.validate(path)
    if dryRun:
        return {
            "deleted": False,
            "dryRun": True,
            "path": str(valid_path),
            "isDirectory": valid_path.is_dir(),
            "recursiveRequired": valid_path.is_dir(),
        }
    remove_path(valid_path, recursive)
    return {"deleted": True, "dryRun": False, "path": str(valid_path)}


@mcp.tool(
    name="fs-edit",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def fs_edit(
    path: str, edits: list[dict[str, str]], dryRun: bool = True
) -> dict[str, Any]:
    """Apply exact oldText/newText edits to a UTF-8 text file inside allowed directories."""
    valid_path = get_tools().guard.validate(path)
    if not valid_path.is_file():
        raise ToolError(f"Not a file: {valid_path}")
    before = valid_path.read_text(encoding="utf-8", errors="replace")
    after = apply_text_edits(before, edits)
    diff = unified_diff(valid_path, before, after)
    if dryRun:
        return {"applied": False, "dryRun": True, "path": str(valid_path), "diff": diff}
    temp_path = valid_path.with_name(f".{valid_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(after, encoding="utf-8")
    temp_path.replace(valid_path)
    return {"applied": True, "dryRun": False, "path": str(valid_path), "diff": diff}


@mcp.tool(name="fs-stat", annotations=ToolAnnotations(readOnlyHint=True))
def fs_stat(path: str) -> dict[str, Any]:
    """Get metadata for a file or directory inside allowed directories."""
    return format_stat(get_tools().guard.validate(path))


@mcp.tool(name="fs-search", annotations=ToolAnnotations(readOnlyHint=True))
def fs_search(
    path: str,
    pattern: str,
    excludePatterns: list[str] | None = None,
    maxResults: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Recursively search for paths matching a glob pattern inside allowed directories."""
    tools = get_tools()
    root = tools.guard.validate(path)
    if not root.is_dir():
        raise ToolError(f"Not a directory: {root}")
    matches, truncated = tools.search(root, pattern, excludePatterns or [], maxResults)
    return {"matches": matches, "truncated": truncated}


@mcp.tool(name="fs-tree", annotations=ToolAnnotations(readOnlyHint=True))
def fs_tree(
    path: str,
    excludePatterns: list[str] | None = None,
    maxDepth: int | None = None,
    maxResults: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Return a JSON directory tree for a path inside allowed directories."""
    tools = get_tools()
    root = tools.guard.validate(path)
    counter = {"count": 0, "truncated": 0}
    return {
        "tree": build_tree(
            root,
            root,
            tools.guard,
            excludePatterns or [],
            maxDepth,
            0,
            maxResults,
            counter,
            {root.resolve(strict=True)},
        ),
        "truncated": bool(counter["truncated"]),
    }


@mcp.tool(
    name="fs-patch",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def fs_patch(cwd: str, patch: str, dryRun: bool = False) -> dict[str, Any]:
    """Apply a unified diff patch inside an allowed directory."""
    working_directory = get_tools().guard.validate(cwd)
    if not working_directory.is_dir():
        raise ToolError(f"cwd is not a directory: {working_directory}")

    target_paths = validate_patch_paths(working_directory, patch)
    patch_file = working_directory / f".combined-mcp-{os.getpid()}.patch"
    get_tools().guard.validate(str(patch_file), parent_may_exist=True)
    patch_file.write_text(patch, encoding="utf-8")
    try:
        check = run_process(
            ["git", "apply", "--check", str(patch_file)], working_directory
        )
        if check["exitCode"] != 0:
            raise ToolError(check["stderr"] or check["stdout"] or "Patch check failed")
        if dryRun:
            return {
                "applied": False,
                "checked": True,
                "paths": target_paths,
                "stdout": check["stdout"],
                "stderr": check["stderr"],
            }
        applied = run_process(["git", "apply", str(patch_file)], working_directory)
        if applied["exitCode"] != 0:
            raise ToolError(
                applied["stderr"] or applied["stdout"] or "Patch apply failed"
            )
        return {
            "applied": True,
            "checked": True,
            "paths": target_paths,
            "stdout": applied["stdout"],
            "stderr": applied["stderr"],
        }
    finally:
        try:
            patch_file.unlink()
        except FileNotFoundError:
            pass


@mcp.tool(name="fs-allowed-directories", annotations=ToolAnnotations(readOnlyHint=True))
def fs_allowed_directories() -> dict[str, list[str]]:
    """List directories this MCP server can access."""
    return {"directories": get_tools().guard.list_allowed()}


@mcp.tool(name="git-status", annotations=ToolAnnotations(readOnlyHint=True))
def git_status(repo_path: str) -> dict[str, str]:
    """Show Git working tree status."""
    result = git_text(repo_path, "status")
    return {"content": result["stdout"]}


@mcp.tool(name="git-diff-unstaged", annotations=ToolAnnotations(readOnlyHint=True))
def git_diff_unstaged(
    repo_path: str, context_lines: int = DEFAULT_GIT_CONTEXT_LINES
) -> dict[str, str]:
    """Show unstaged Git changes."""
    result = git_text(repo_path, "diff", f"--unified={context_lines}")
    return {"content": result["stdout"]}


@mcp.tool(name="git-diff-staged", annotations=ToolAnnotations(readOnlyHint=True))
def git_diff_staged(
    repo_path: str, context_lines: int = DEFAULT_GIT_CONTEXT_LINES
) -> dict[str, str]:
    """Show staged Git changes."""
    result = git_text(repo_path, "diff", f"--unified={context_lines}", "--cached")
    return {"content": result["stdout"]}


@mcp.tool(name="git-diff", annotations=ToolAnnotations(readOnlyHint=True))
def git_diff(
    repo_path: str, target: str, context_lines: int = DEFAULT_GIT_CONTEXT_LINES
) -> dict[str, str]:
    """Show differences between current state and a Git target."""
    ensure_not_option(target, "target")
    git_text(repo_path, "rev-parse", "--verify", target)
    result = git_text(repo_path, "diff", f"--unified={context_lines}", target)
    return {"content": result["stdout"]}


@mcp.tool(
    name="git-add",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=False
    ),
)
def git_add(repo_path: str, files: list[str]) -> dict[str, str]:
    """Add file contents to the Git staging area."""
    repo = validate_repo_path(repo_path)
    validate_repo_files(repo, files)
    result = run_process(["git", "-C", str(repo), "add", "--", *files], repo)
    if result["exitCode"] != 0:
        raise ToolError(result["stderr"] or result["stdout"] or "git add failed")
    return {"content": "Files staged successfully"}


@mcp.tool(
    name="git-unstage-all",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=True
    ),
)
def git_unstage_all(repo_path: str) -> dict[str, str]:
    """Unstage all staged changes."""
    result = git_text(repo_path, "reset")
    return {"content": result["stdout"] or "All staged changes reset"}


@mcp.tool(
    name="git-commit",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=False
    ),
)
def git_commit(repo_path: str, message: str) -> dict[str, str]:
    """Commit staged Git changes."""
    result = git_text(repo_path, "commit", "-m", message)
    return {"content": result["stdout"]}


@mcp.tool(name="git-log", annotations=ToolAnnotations(readOnlyHint=True))
def git_log(
    repo_path: str,
    max_count: int = 10,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> dict[str, str]:
    """Show Git commit logs with optional date filtering."""
    args = ["log", f"--max-count={max_count}", "--format=%H%n%an%n%ad%n%s%n"]
    if start_timestamp:
        ensure_not_option(start_timestamp, "start_timestamp")
        args.extend(["--since", start_timestamp])
    if end_timestamp:
        ensure_not_option(end_timestamp, "end_timestamp")
        args.extend(["--until", end_timestamp])
    result = git_text(repo_path, *args)
    return {"content": result["stdout"]}


@mcp.tool(
    name="git-create-branch",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=False
    ),
)
def git_create_branch(
    repo_path: str, branch_name: str, base_branch: str | None = None
) -> dict[str, str]:
    """Create a new Git branch from an optional base branch."""
    ensure_not_option(branch_name, "branch_name")
    args = ["branch", branch_name]
    if base_branch:
        ensure_not_option(base_branch, "base_branch")
        args.append(base_branch)
    git_text(repo_path, *args)
    return {"content": f"Created branch '{branch_name}'"}


@mcp.tool(
    name="git-checkout",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def git_checkout(repo_path: str, branch_name: str) -> dict[str, str]:
    """Switch Git branches."""
    ensure_not_option(branch_name, "branch_name")
    result = git_text(repo_path, "checkout", branch_name)
    return {"content": result["stdout"] or result["stderr"]}


@mcp.tool(name="git-show", annotations=ToolAnnotations(readOnlyHint=True))
def git_show(repo_path: str, revision: str) -> dict[str, str]:
    """Show a Git revision with patch."""
    ensure_not_option(revision, "revision")
    result = git_text(repo_path, "show", "--format=fuller", "--patch", revision)
    return {"content": result["stdout"]}


@mcp.tool(name="git-branch", annotations=ToolAnnotations(readOnlyHint=True))
def git_branch(
    repo_path: str,
    branch_type: str = "local",
    contains: str | None = None,
    not_contains: str | None = None,
) -> dict[str, str]:
    """List Git branches."""
    args = ["branch"]
    match branch_type:
        case "local":
            pass
        case "remote":
            args.append("-r")
        case "all":
            args.append("-a")
        case _:
            raise ToolError(f"Invalid branch_type: {branch_type}")
    if contains:
        ensure_not_option(contains, "contains")
        args.extend(["--contains", contains])
    if not_contains:
        ensure_not_option(not_contains, "not_contains")
        args.extend(["--no-contains", not_contains])
    result = git_text(repo_path, *args)
    return {"content": result["stdout"]}


@mcp.tool(name="git-remote", annotations=ToolAnnotations(readOnlyHint=True))
def git_remote(repo_path: str) -> dict[str, str]:
    """List Git remotes with fetch and push URLs."""
    result = git_text(repo_path, "remote", "-v")
    return {"content": result["stdout"]}


@mcp.tool(name="git-stash-list", annotations=ToolAnnotations(readOnlyHint=True))
def git_stash_list(repo_path: str) -> dict[str, str]:
    """List Git stash entries."""
    result = git_text(repo_path, "stash", "list")
    return {"content": result["stdout"]}


@mcp.tool(
    name="git-stash-push",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def git_stash_push(
    repo_path: str,
    message: str | None = None,
    include_untracked: bool = False,
) -> dict[str, str]:
    """Stash current Git changes."""
    args = ["stash", "push"]
    if include_untracked:
        args.append("--include-untracked")
    if message:
        args.extend(["-m", message])
    result = git_text(repo_path, *args)
    return {"content": result["stdout"]}


@mcp.tool(
    name="git-stash-pop",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def git_stash_pop(repo_path: str, stash: str | None = None) -> dict[str, str]:
    """Pop a Git stash entry."""
    ensure_stash_ref(stash, "stash")
    args = ["stash", "pop"]
    if stash:
        args.append(stash)
    result = git_text(repo_path, *args)
    return {"content": result["stdout"] or result["stderr"]}


@mcp.tool(
    name="shell-exec",
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=True
    ),
)
def shell_exec(
    command: str,
    cwd: str | None = None,
    timeoutSeconds: int = 30,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a shell command in an allowed working directory."""
    return get_tools().shell_exec(command, cwd, timeoutSeconds, env)


def initialize_tools(argv: list[str]) -> None:
    global _tools
    guard = PathGuard(allowed_directories_from_args(argv))
    _tools = CombinedTools(guard)

    print(
        f"{SERVER_NAME} running on stdio; allowed directories: {', '.join(guard.list_allowed())}",
        file=sys.stderr,
    )


def main() -> int:
    try:
        initialize_tools(sys.argv[1:])
        mcp.run("stdio")
    except ToolError as error:
        print(error.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
