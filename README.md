# combined-mcp

Docker-friendly MCP server that exposes filesystem and shell tools from one stdio server.

The filesystem behavior follows the same basic security model as `examples/servers/src/filesystem`: every filesystem path is normalized and restricted to configured allowed directories. `shell-exec` is also restricted to an allowed working directory, which is intended to be a Docker mount such as `/projects`.

## Tools

- `fs-read`: read a UTF-8 text file, with optional `head` or `tail`
- `fs-read-binary`: read a file as base64 with a MIME type, capped at 10 MiB
- `fs-write`: create or overwrite a UTF-8 text file
- `fs-list`: list a directory
- `fs-mkdir`: create a directory
- `fs-move`: move or rename a file or directory
- `fs-delete`: delete a file or directory, dry-run by default
- `fs-edit`: apply exact `oldText`/`newText` edits, dry-run by default
- `fs-stat`: file or directory metadata
- `fs-search`: recursive glob search with `maxResults`
- `fs-tree`: recursive JSON directory tree with `maxResults`
- `fs-patch`: validate and apply a unified diff patch
- `fs-allowed-directories`: show accessible roots
- `git-status`: show working tree status
- `git-diff-unstaged`: show unstaged changes
- `git-diff-staged`: show staged changes
- `git-diff`: compare current state with a target revision
- `git-add`: stage files
- `git-unstage-all`: unstage all staged files
- `git-commit`: commit staged changes
- `git-log`: show commit logs
- `git-create-branch`: create a branch
- `git-checkout`: switch branches
- `git-show`: show a revision with patch
- `git-branch`: list branches
- `git-remote`: list remotes
- `git-stash-list`: list stash entries
- `git-stash-push`: stash changes
- `git-stash-pop`: pop a stash entry
- `shell-exec`: run a shell command in an allowed `cwd`

## Run Locally

```bash
nix run . -- /path/to/allowed/project
```

If no paths are passed, the server uses `MCP_ALLOWED_DIRS` split by `:`. If that is unset, it uses `/projects` when present, otherwise the current working directory.

## Docker Image

Build the image tarball:

```bash
nix build .#docker
docker load < result
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "combined": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--mount",
        "type=bind,src=/Users/you/project,dst=/projects/workspace",
        "combined-mcp:latest",
        "/projects"
      ]
    }
  }
}
```

The container command defaults to `combined-mcp /projects`, so mounting workspaces under `/projects` keeps both filesystem and shell access contained.
