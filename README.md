# Cookidoo MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io/)

Let Claude read recipes from your Cookidoo account and create new TM7-optimized custom recipes with guided-cooking annotations, quality scoring, and automatic rollback on upload failure.

> **Disclaimer:** Unofficial project. Not affiliated with Cookidoo, Vorwerk, Thermomix or any of their subsidiaries. Cookidoo has no public API, so this server uses the [`cookidoo-api`](https://github.com/miaucl/cookidoo-api) scraping/auth library. Use at your own risk and review the Cookidoo Terms of Service.

## Quickstart (Claude Desktop)

1. Clone and install:
   ```bash
   git clone https://github.com/danielkliem/mcp-cookidoo.git
   cd mcp-cookidoo
   python3.12 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "cookidoo": {
         "command": "/absolute/path/to/mcp-cookidoo/venv/bin/python",
         "args": ["/absolute/path/to/mcp-cookidoo/server.py"],
         "env": {
           "COOKIDOO_MCP_MODE": "stdio",
           "COOKIDOO_EMAIL": "you@example.com",
           "COOKIDOO_PASSWORD": "your-password"
         }
       }
     }
   }
   ```

3. Restart Claude Desktop. Ask Claude: *"Create a TM7 recipe for Spaghetti Carbonara and upload it to my Cookidoo."*

> **Credentials warning:** Cookidoo has no OAuth, so the server logs in with your email + password. Run it locally (stdio mode) unless you control the host. The HTTP transport supports a Bearer token but does not encrypt credentials at rest beyond your `.env` file permissions.

## What it does

- **Read recipes.** Fetch any Cookidoo recipe by ID (e.g. `r59322`) including ingredients, steps, timings, and metadata.
- **Create TM7-optimized custom recipes.** Action steps are parsed and rewritten into the three guided-cooking annotation families:
  - **TTS** (standard): `30 Sek./Stufe 5`
  - **MODE/STEAMING** (Varoma): auto-detects the word "Varoma" and emits the correct accessory shape
  - **MODE/BROWNING** (Anbraten): `5 Min./150°C/Intensiv`
  
  These render as proper guided steps with a Play-Button on the TM7 device. Verb prefixes like *Mahlen*, *Zerkleinern*, *Anbraten* are auto-stripped, since pure-action steps render as guided steps while verb-prefixed prose renders as "mark as done" checkboxes.
- **Quality gate.** Recipes are scored 0–100 on Thermomix vocabulary coverage before upload. Below the configured bar, upload is refused unless `force_upload=true`.
- **Automatic rollback.** If the upload PATCH fails (e.g. schema validation error), the partial recipe is deleted so no zombies accumulate in your account.
- **Multilingual prompts.** Built-in workflow prompts in German (`prompt.md`) and French (`prompt_FR.md`).

## MCP Tools

| Tool | Description |
|------|-------------|
| `connect_to_cookidoo()` | Verify credentials. Optional, other tools auto-connect. |
| `get_recipe_details(recipe_id)` | Fetch an existing Cookidoo recipe by ID. |
| `generate_recipe_structure(...)` | Parse + validate recipe data into the upload JSON schema. |
| `validate_recipe_quality(recipe_json)` | Score recipe against TM7 criteria with suggestions. |
| `upload_custom_recipe(recipe_json, force_upload=false)` | Upload to the user's account. Quality-gated, with rollback on failure. |
| `list_my_custom_recipes()` | List custom recipes in the account. |
| `delete_custom_recipe(recipe_id)` | Delete a custom recipe. Irreversible. |

## MCP Prompts

- `create_tm7_recipe(dish)`: autonomous workflow that drives Claude through TM7-optimized recipe creation, from concept to validated upload.

## Quality Scoring

Score out of 100. Default upload threshold is 70, configurable via `COOKIDOO_QUALITY_BAR`.

| Criterion | Points | Coverage required |
|-----------|--------|-------------------|
| Time indication on steps | 30 | ≥80% of action steps |
| Temperature on cooking steps | 25 | ≥60% |
| Speed/mode setting | 20 | ≥70% |
| Accessory mentions (Schmetterling, Varoma, Spatel, …) | 10 | any |
| TM7 parallelization (Varoma-Aufsatz, Gareinsatz, …) | 15 | any |

The validator also produces contextual suggestions, e.g. Schmetterling for cream/egg whites, Teigknetstufe for dough.

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Default | Purpose |
|----------|---------|---------|
| `COOKIDOO_EMAIL` | required | Cookidoo account email |
| `COOKIDOO_PASSWORD` | required | Cookidoo account password |
| `COOKIDOO_MCP_MODE` | `stdio` | `stdio` for Claude Desktop, `http` for remote |
| `COOKIDOO_MCP_PORT` | `8001` | Local bind port (HTTP mode only) |
| `COOKIDOO_API_TOKEN` | empty | Bearer token required on `/mcp`. Empty = no middleware auth. |
| `COOKIDOO_QUALITY_BAR` | `70` | Minimum quality score required by `upload_custom_recipe` |

## HTTP Transport

For remote use (e.g. claude.ai connectors) run the server in HTTP mode:

```bash
COOKIDOO_MCP_MODE=http \
COOKIDOO_API_TOKEN=$(openssl rand -hex 32) \
./venv/bin/python server.py
```

Endpoints:
- `POST /mcp`: MCP protocol endpoint, requires `Authorization: Bearer <COOKIDOO_API_TOKEN>` if the token is set.
- `GET /health`: health check.

For production deployment behind a reverse proxy (Caddy/nginx/Traefik), terminate TLS at the proxy and forward to `localhost:8001`. A typical Caddy config injects the Bearer token server-side so the URL itself carries a path token, keeping credentials out of client config.

## Troubleshooting

- **401 after ~1 hour of uptime.** Fixed in [`5f85973`](https://github.com/danielkliem/mcp-cookidoo/commit/5f85973). The server now refreshes the OAuth token automatically before expiry. If you still see 401, update to latest.
- **Upload returns 404 on the response URL.** Make sure you're on a version that points at `/created-recipes/{locale}/{id}/edit` rather than the old `/recipes/custom-recipes/...` path.
- **Zombie empty recipes in your account.** Should not happen, since rollback runs automatically if the PATCH step fails. Delete leftovers with `delete_custom_recipe` or `list_my_custom_recipes`.
- **Step renders with "mark as done" checkbox instead of Play-Button.** The verb prefix wasn't stripped. Ensure the step text starts with a quantity (e.g. `30 Sek./Stufe 5`), not a verb (`Mahlen 30 Sek./Stufe 5`).

## Development

```bash
pip install -r requirements.txt
python -m pytest test_integration.py test_auth_refresh.py
```

`test_integration.py` creates a real recipe in the configured account, asserts backend rendering, and cleans up. Requires valid `.env` credentials.

## Acknowledgments

- [cookidoo-api](https://github.com/miaucl/cookidoo-api) by miaucl for the underlying Cookidoo client library.
- [FastMCP](https://github.com/jlowin/fastmcp) for the MCP server framework.

## License

MIT. See [LICENSE](LICENSE).
