# Cookidoo MCP Server

An MCP (Model Context Protocol) server for reading Cookidoo recipes and creating
TM7-optimized custom recipes with automatic quality validation, built with `fastmcp`.

> **Disclaimer:** Unofficial project. Not affiliated with Cookidoo, Vorwerk, Thermomix
> or any of their subsidiaries.

## Features

- **Lazy auth with proactive token refresh** — tools auto-connect on first call;
  the OAuth access token is refreshed via the refresh-grant before it expires,
  and a 401 from the Cookidoo backend triggers a single refresh-and-retry. The
  long-running server process keeps working past the access token's lifetime
  without re-prompting the user for credentials.
- **Read recipes** — fetch full details for any Cookidoo recipe by ID
- **Guided-cooking annotations** — action steps in three modes (standard TTS,
  `MODE/STEAMING` for Varoma, `MODE/BROWNING` for Anbraten) are detected in the
  step text, emitted as proper backend annotations, and render on the TM7
  device with a Play-Button. Verb prefixes (`Mahlen`, `Zerkleinern`, `Anbraten`, …)
  are automatically stripped so steps stay pure-action (otherwise the TM7
  renders them with a "mark as done" checkbox instead).
- **TM7 quality gate** — recipes are scored 0–100 on Thermomix vocabulary and
  TM7-specific best practice; the upload tool refuses recipes below the
  configured bar unless `force_upload=true` is passed.
- **Upload rollback** — if the PATCH step fails (e.g. schema validation error)
  the partial recipe is automatically deleted, so failed attempts never leave
  zombie entries in the account.
- **Guided creation** — MCP prompt `create_tm7_recipe(dish)` drives Claude through
  an autonomous TM7-optimized recipe creation workflow.
- **Two transports** — HTTP (stateless, token-auth) for remote Claude.ai Connectors,
  stdio for local Claude Desktop.

## MCP Tools

| Tool | Description |
|---|---|
| `connect_to_cookidoo` | Verify credentials (optional — other tools auto-connect) |
| `get_recipe_details(recipe_id)` | Fetch existing recipe by ID (e.g. `r59322`) |
| `generate_recipe_structure(...)` | Parse + validate recipe data into JSON |
| `validate_recipe_quality(recipe_json)` | Score recipe against TM7 criteria |
| `upload_custom_recipe(recipe_json, force_upload=false)` | Upload to user's account (quality-gated, annotations + rollback on failure) |
| `list_my_custom_recipes` | List custom recipes in the account |
| `delete_custom_recipe(recipe_id)` | Delete a custom recipe (irreversible) |

## MCP Prompts

- `create_tm7_recipe(dish)` — autonomous workflow for TM7-optimized recipe creation

## Quality Scoring

Score out of 100, bar defaults to 70 (configurable via `COOKIDOO_QUALITY_BAR`).
The score is dominated by how many steps are parseable as TM7 *guided-cooking*
actions — only those render with a play button on the device.

| Criterion | Points | Notes |
|---|---|---|
| TTS/MODE-parseable action steps (play button) | 50 | Roughly half of steps should be pure actions like `5 Sek./Stufe 5` or `15 Min./Varoma/Stufe 2`. Score scales linearly with coverage. |
| Ingredient annotations resolved in step text | 20 | At least one step should reference an ingredient using the exact substring from the ingredient list (so the backend can link them). |
| Accessories mentioned (Schmetterling, Spatel, Varoma, Gareinsatz, …) | 10 | Helps the cook know what to attach. |
| TM7 parallelization (Varoma above, Gareinsatz inside, "gleichzeitig" / "während") | 20 | Encourages using the TM7's two cooking zones at once. |

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
`/mcp`. Health endpoint exposed at `GET /health`.

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

## Tests

Unit tests (no credentials needed — mock the Cookidoo library):

```bash
./venv/bin/python -m unittest test_auth_refresh -v
```

End-to-end integration test against the real Cookidoo API (creates a
uniquely-named test recipe, asserts the backend renders correct `<cr-tts>` and
`<cr-mode>` tags, then deletes the recipe — requires `COOKIDOO_EMAIL` and
`COOKIDOO_PASSWORD` in `.env`):

```bash
./venv/bin/python test_integration.py
```

## Remote Deployment

Run the server as a long-lived process and expose `/mcp` (+ optionally `/health`)
behind a reverse proxy. The OAuth access token is refreshed automatically — a
process restart is not required when the token expires.

**Example systemd unit** (`/etc/systemd/system/cookidoo-mcp.service`):

```ini
[Unit]
Description=Cookidoo MCP Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cookidoo-mcp
EnvironmentFile=/opt/cookidoo-mcp/.env
ExecStart=/opt/cookidoo-mcp/venv/bin/python server.py
Environment=COOKIDOO_MCP_MODE=http
Environment=COOKIDOO_API_TOKEN=<long-random-token>
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Example Caddy reverse proxy** (extracts the token from the URL path so the
client can pass it directly, no extra header config in Claude.ai needed):

```
mcp.example.com {
    handle_path /cookidoo/<TOKEN>* {
        rewrite * /mcp{path}
        request_header Authorization "Bearer <TOKEN>"
        reverse_proxy localhost:8001
    }
    handle_path /cookidoo/health {
        reverse_proxy localhost:8001
    }
}
```

### Claude.ai Connector

1. Settings → Connectors → Add custom connector
2. URL: `https://mcp.example.com/cookidoo/<TOKEN>`
3. Server reveals 7 tools and the `create_tm7_recipe` prompt

## Architecture Notes

- **Auth lifecycle** (`server._ensure_connected` + `CookidooService._authed_request`):
  the cached `Cookidoo` API client is reused across calls; before each call,
  `expires_in` is checked and the access token is refreshed via OAuth refresh-grant
  if it's within 60s of expiry. The raw-HTTP custom-recipe endpoints additionally
  retry once on a 401 (covering the race where the token expires mid-request).
  Full email+password re-login only happens if the refresh token itself dies.
- **TTS parsing** (`cookidoo_service.build_step_annotations`): regex-based parser
  for `<time> [Sek.|Min.] / [<temp>°C|Varoma] / [Linkslauf] / Stufe <speed>` plus
  the BROWNING variant `<time> Min./<140-160>°C/[Leicht|Intensiv]`. Overlap-free
  span matching, then ingredient-substring matching on the remainder.
- **Step normalization** (`normalize_action_step`): strips a single leading verb
  and trailing punctuation from pure-action steps so the backend stores them as
  play-button steps rather than checkbox steps.

## Acknowledgments

Built on top of [cookidoo-api](https://github.com/miaucl/cookidoo-api).

## License

MIT — see [LICENSE](LICENSE).
