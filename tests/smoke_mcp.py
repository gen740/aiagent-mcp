from __future__ import annotations

import anyio
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def run_smoke() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "smoke@example.invalid"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Smoke Test"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "combined_mcp", tmp],
            env=env,
        )

        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "fs-read",
                    "fs-write",
                    "fs-delete",
                    "fs-edit",
                    "fs-patch",
                    "git-remote",
                    "git-status",
                    "git-stash-list",
                    "git-stash-pop",
                    "git-stash-push",
                    "git-unstage-all",
                    "shell-exec",
                } <= names

                await session.call_tool(
                    "fs-write",
                    {"path": "hello.txt", "content": "hello\n"},
                )
                read_result = await session.call_tool("fs-read", {"path": "hello.txt"})
                assert read_result.structuredContent == {"content": "hello\n"}

                binary_result = await session.call_tool(
                    "fs-read-binary", {"path": "hello.txt"}
                )
                assert binary_result.structuredContent is not None
                assert binary_result.structuredContent["mimeType"] == "text/plain"

                edit_preview = await session.call_tool(
                    "fs-edit",
                    {
                        "path": "hello.txt",
                        "edits": [{"oldText": "hello\n", "newText": "hello edited\n"}],
                    },
                )
                assert edit_preview.structuredContent is not None
                assert edit_preview.structuredContent["applied"] is False
                assert "hello edited" in edit_preview.structuredContent["diff"]

                edit_result = await session.call_tool(
                    "fs-edit",
                    {
                        "path": "hello.txt",
                        "edits": [{"oldText": "hello\n", "newText": "hello\n"}],
                        "dryRun": False,
                    },
                )
                assert edit_result.structuredContent is not None
                assert edit_result.structuredContent["applied"] is True

                await session.call_tool(
                    "fs-write",
                    {"path": "delete-me.txt", "content": "delete me\n"},
                )
                delete_preview = await session.call_tool(
                    "fs-delete", {"path": "delete-me.txt"}
                )
                assert delete_preview.structuredContent is not None
                assert delete_preview.structuredContent["deleted"] is False
                delete_result = await session.call_tool(
                    "fs-delete", {"path": "delete-me.txt", "dryRun": False}
                )
                assert delete_result.structuredContent is not None
                assert delete_result.structuredContent["deleted"] is True

                search_result = await session.call_tool(
                    "fs-search", {"path": tmp, "pattern": "*.txt", "maxResults": 1}
                )
                assert search_result.structuredContent is not None
                assert len(search_result.structuredContent["matches"]) == 1

                tree_result = await session.call_tool(
                    "fs-tree", {"path": tmp, "maxResults": 1}
                )
                assert tree_result.structuredContent is not None
                assert len(tree_result.structuredContent["tree"]) == 1

                os.mkdir(Path(tmp) / "real-dir")
                (Path(tmp) / "real-dir" / "nested.txt").write_text(
                    "nested\n", encoding="utf-8"
                )
                os.symlink(Path(tmp) / "real-dir", Path(tmp) / "linked-dir")
                list_result = await session.call_tool("fs-list", {"path": tmp})
                assert list_result.structuredContent is not None
                assert "[DIR] linked-dir" in list_result.structuredContent["content"]
                tree_symlink_result = await session.call_tool(
                    "fs-tree", {"path": tmp, "maxDepth": 2}
                )
                assert tree_symlink_result.structuredContent is not None
                linked_dir = next(
                    item
                    for item in tree_symlink_result.structuredContent["tree"]
                    if item["name"] == "linked-dir"
                )
                assert linked_dir["type"] == "directory"
                assert "children" in linked_dir
                os.symlink(Path(tmp), Path(tmp) / "self-link")
                tree_cycle_result = await session.call_tool(
                    "fs-tree", {"path": tmp, "maxResults": 100}
                )
                assert tree_cycle_result.structuredContent is not None
                self_link = next(
                    item
                    for item in tree_cycle_result.structuredContent["tree"]
                    if item["name"] == "self-link"
                )
                assert self_link == {
                    "name": "self-link",
                    "type": "directory",
                    "children": [],
                }

                shell_result = await session.call_tool(
                    "shell-exec",
                    {"command": "printf ok", "cwd": tmp},
                )
                assert shell_result.structuredContent is not None
                assert shell_result.structuredContent["exitCode"] == 0
                assert shell_result.structuredContent["stdout"] == "ok"

                large_shell_result = await session.call_tool(
                    "shell-exec",
                    {"command": "yes x | head -c 3000000", "cwd": tmp},
                )
                assert large_shell_result.structuredContent is not None
                assert large_shell_result.structuredContent["stdoutTruncated"] is True

                timeout_marker = f"combined_mcp_timeout_{os.getpid()}"
                timeout_command = (
                    f"{shlex.quote(sys.executable)} -c "
                    f"{shlex.quote('import time; time.sleep(30)')} "
                    f"{timeout_marker}"
                )
                timeout_result = await session.call_tool(
                    "shell-exec",
                    {"command": timeout_command, "cwd": tmp, "timeoutSeconds": 1},
                )
                assert timeout_result.structuredContent is not None
                assert timeout_result.structuredContent["exitCode"] == 124
                await anyio.sleep(0.2)
                ps_result = subprocess.run(
                    ["ps", "ax", "-o", "pid=", "-o", "command="],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                leaked_pids = [
                    int(line.strip().split(maxsplit=1)[0])
                    for line in ps_result.stdout.splitlines()
                    if timeout_marker in line and "ps ax" not in line
                ]
                for pid in leaked_pids:
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass
                assert leaked_pids == []

                patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+hello patched
"""
                patch_result = await session.call_tool(
                    "fs-patch",
                    {"cwd": tmp, "patch": patch, "dryRun": False},
                )
                assert patch_result.structuredContent is not None
                assert patch_result.structuredContent["applied"] is True

                default_patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello patched
+hello default patched
"""
                default_patch_result = await session.call_tool(
                    "fs-patch",
                    {"cwd": tmp, "patch": default_patch},
                )
                assert default_patch_result.structuredContent is not None
                assert default_patch_result.structuredContent["applied"] is True

                status_result = await session.call_tool(
                    "git-status", {"repo_path": tmp}
                )
                assert status_result.structuredContent is not None
                assert "hello.txt" in status_result.structuredContent["content"]

                await session.call_tool("git-add", {"repo_path": tmp, "files": ["."]})
                await session.call_tool("git-unstage-all", {"repo_path": tmp})
                await session.call_tool("git-add", {"repo_path": tmp, "files": ["."]})
                commit_result = await session.call_tool(
                    "git-commit",
                    {"repo_path": tmp, "message": "smoke commit"},
                )
                assert commit_result.structuredContent is not None
                assert "smoke commit" in commit_result.structuredContent["content"]

                remote_result = await session.call_tool(
                    "git-remote", {"repo_path": tmp}
                )
                assert remote_result.structuredContent is not None

                await session.call_tool(
                    "fs-write",
                    {"path": "hello.txt", "content": "stash me\n"},
                )
                stash_push = await session.call_tool(
                    "git-stash-push",
                    {"repo_path": tmp, "message": "smoke stash"},
                )
                assert stash_push.structuredContent is not None
                assert (
                    "Saved working directory" in stash_push.structuredContent["content"]
                )

                stash_list = await session.call_tool(
                    "git-stash-list", {"repo_path": tmp}
                )
                assert stash_list.structuredContent is not None
                assert "smoke stash" in stash_list.structuredContent["content"]

                stash_pop = await session.call_tool("git-stash-pop", {"repo_path": tmp})
                assert stash_pop.structuredContent is not None
                assert "hello.txt" in stash_pop.structuredContent["content"]


def main() -> int:
    async def run_with_timeout() -> None:
        with anyio.fail_after(10):
            await run_smoke()

    anyio.run(run_with_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
