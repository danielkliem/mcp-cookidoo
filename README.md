# Cookidoo MCP Server

An MCP (Model Context Protocol) server for reading Cookidoo recipes and creating
TM7-optimized custom recipes with automatic quality validation, built with `fastmcp`.

> **Disclaimer:** Unofficial project. Not affiliated with Cookidoo, Vorwerk, Thermomix
> or any of their subsidiaries.

## Features

- **Lazy auth** — tools auto-connect on first call; no manual `connect` step required
- **Read recipes** — fetch full details for any Cookidoo recipe by ID
- **TM7 quality gate** — recipes are scored 0–100 on Thermomix vocabulary (time /
  temperature / speed / accessories / parallelization) and the upload tool refuses
  recipes below the configured bar unless `force_upload=true` is passed
- **Guided creation** — MCP prompt `create_tm7_recipe(dish)` drives Claude through
  an autonomous TM7-optimized recipe creation workflow
- **Two transports** — HTTP (stateless, token-auth) for remote Claude.ai Connectors,
  stdio for local Claude Desktop

## MCP Tools

| Tool | Description |
|---|---|
| `connect_to_cookidoo` | Verify credentials (optional — other tools auto-connect) |
| `get_recipe_details(recipe_id)` | Fetch existing recipe by ID (e.g. `r59322`) |
| `generate_recipe_structure(...)` | Parse + validate recipe data into JSON |
| `validate_recipe_quality(recipe_json)` | Score recipe against TM7 criteria |
| `upload_custom_recipe(recipe_json, force_upload=false)` | Upload to user's account (quality-gated) |

## MCP Prompts

- `create_tm7_recipe(dish)` — autonomous workflow for TM7-optimized recipe creation

## Quality Scoring

Score out of 100, bar defaults to 70 (configurable via `COOKIDOO_QUALITY_BAR`):

| Criterion | Points |
|---|---|
| Time indication on steps (need ≥80% coverage) | 30 |
| Temperature on cooking steps (need ≥60%) | 25 |
| Speed/mode setting (need ≥70%) | 20 |
| Accessory mentions (Schmetterling, Varoma, Spatel, …) | 10 |
| TM7 parallelization (Varoma-Aufsatz, Gareinsatz, …) | 15 |

The validator also generates contextual suggestions: Schmetterling for cream/egg
whites, Teigknetstufe for dough, etc.

## Local Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Cookidoo credentials
```

### Run as HTTP server
```bash
COOKIDOO_MCP_MODE=http ./venv/bin/python server.py
```
Default port 8001. Set `COOKIDOO_API_TOKEN` to require `Authorization: Bearer` on
`/mcp`.

### Run as stdio (Claude Desktop)
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "cookidoo": {
      "command": "/absolute/path/to/mcp-cookidoo/venv/bin/python",
      "args": ["/absolute/path/to/mcp-cookidoo/server.py"],
      "env": {
        "COOKIDOO_MCP_MODE": "stdio",
        "COOKIDOO_EMAIL": "you@example.com",
        "COOKIDOO_PASSWORD": "…"
      }
    }
  }
}
```

## Remote Deployment (Ubuntu + Caddy)

This server is currently deployed alongside `intep-mcp` on the Hetzner VPS and
exposed via `mcp.nullklick.ch`.

- **Service:** `/etc/systemd/system/cookidoo-mcp.service` → `python server.py` on
  `localhost:8001`, env loaded from `/opt/cookidoo-mcp/.env`
- **Reverse proxy:** Caddy matches path `/cookidoo/<TOKEN>`, rewrites to `/mcp`,
  injects `Authorization: Bearer <TOKEN>`, proxies to `localhost:8001`
- **Health:** `https://mcp.nullklick.ch/cookidoo/health`

### Claude.ai Connector
1. Settings → Connectors → Add custom connector
2. URL: `https://mcp.nullklick.ch/cookidoo/<TOKEN>`
3. Server reveals 5 tools and the `create_tm7_recipe` prompt

## Acknowledgments

Built on top of [cookidoo-api](https://github.com/miaucl/cookidoo-api).

## License

MIT
