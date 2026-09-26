# Colab-mcp

An MCP server for bridging your local agent to a Colab session in the browser.

# Supported Clients
This MCP server requires client support for `notifications/tools/list_changed` and for the client to be running locally on your device. 

Popular clients that fit these criteria include:
- Gemini CLI
- Claude Code
- Windsurf


# Setup

- Install `uv` (`pip install uv`)
- Configure for usage (eg for mcp.json style services):

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uvx",
      "args": ["git+https://github.com/googlecolab/colab-mcp"],
      "timeout": 30000
    }
  }
...
```

(If you have a non-standard default package index (**Googlers**), you may also need to add `--index https://pypi.org/simple`)

# Reading cell outputs without re-running

The proxied Colab tools do not include a dedicated "get output" tool, and
`get_cells` hides outputs by default. This server injects an extra local tool,
`get_output_cell`, which reads the execution outputs (stdout, results, errors)
of an existing cell **without re-running it**:

```
Agent: get_output_cell(cellId="abc123")
> { "cellId": "abc123", "cell_type": "code", "outputs": [ ... ] }
```

Identify the cell either by `cellId` (preferred) or by 0-based `cellIndex`.
This is useful after `run_code_cell` returns, including for output produced by a
long-running cell whose tool response was cut off before the cell finished.

# Issues & Discussions

We are using GitHub [discussions](https://github.com/googlecolab/colab-mcp/discussions) as the
place for issue discussion and feature requests. As discussions mature into action items, we
will add those items as issues. This helps us ensure that issues in the issue tracker are
well-understood, deduplicated, and actionable. For these reasons, **please do <u>NOT</u> open
issues directly.** 

# Contributing 
We unfortunately don't have the bandwidth to support review of external contributions, and we 
don't want user PRs to languish, so we aren't accepting any external contributions right now.

If you have a great idea or pain point, we would love to hear about it on our 
[discussions](https://github.com/googlecolab/colab-mcp/discussions) page - the preferred place 
for issue discussion and feature requests.

# Internal - For Colab Developers

### Prerequisites

- `uv` is required (`pip install uv`)
- Configure git hooks to run repo presubmits

```shell
git config core.hooksPath .githooks
```

### Gemini CLI setup

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uv",
      "args": ["run", "colab-mcp"],
      "cwd": "/path/to/github/colab-mcp",
      "timeout": 30000
    }
  }
...
```
