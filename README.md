# hermes-provider-kenari

[Kenari.id](https://kenari.id) model-provider plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

It registers Kenari as the `kenari` provider over its OpenAI-compatible
Chat Completions endpoint (`https://kenari.id/v1`), filters the `/model`
picker to tool-capable chat routes the configured key may actually call,
maps reasoning effort onto Kenari's `reasoning` object, prices responses
in Rupiah from the live catalog, and surfaces wallet/plan/usage via
`hermes usage`. It ships with a companion desktop plugin
(`kenari-usage/`) that shows plan quota in the statusbar. It touches no
Hermes core files — discovery, credential resolution, `hermes doctor`,
and the `--provider` flag all auto-wire from the provider registry.

## What it does

- **Picker filtering.** The public `/v1/models` catalog carries
  `endpoints` + `tool_call` per entry; the plugin keeps only
  tool-capable `chat` routes, so the picker never offers an image/video
  model or a text-only route the agent can't drive.
- **Per-key model filtering.** Dashboard keys can carry a model scope.
  The plugin probes it once per key (a `$0` impossible-model probe —
  errored requests bill nothing) and intersects the picker with the
  key's allowed list, so a scoped key never offers a model the gateway
  would reject with `400 ... is not allowed by this key`.
- **Reasoning mapping.** Hermes effort → Kenari's `extra_body.reasoning`
  object (`{enabled, effort}`), clamped client-side onto each model's
  catalog-advertised `reasoning_options`. Unset effort defaults to
  `medium`, matching Kenari's own default.
- **Pricing in Rupiah.** `get_usage_cost` prices each response from the
  cached catalog rates (micro-IDR/1M → Rp, with an indicative USD
  figure); `:free` models report `Rp0`. Cache-write with no listed rate
  bills at the input rate, per the Kenari docs.
- **Usage snapshot.** `fetch_account_usage` powers `hermes usage`:
  wallet balance, plan weekly/monthly (/5h) windows with Rp left and
  reset times, 7d/30d spend, and free-daily quota left (public
  allowance vs today's usage). Scoped keys get the key-visible subset
  with a note instead of an error.
- **`prompt_cache_key` opt-in** (documented Kenari field) and vision
  enabled (51 chat routes take image input).
- **Model capabilities, no manual overrides.** `model_capabilities`
  declares all 100 catalog models (context, tools, vision, reasoning,
  vendor family) from the live catalog — Hermes routes Kenari ids
  correctly without models.dev entries or hand-written
  `model_overrides`. Refreshed in place on every catalog fetch.
- **Desktop statusbar chip** (`kenari-usage/` companion): 7d/30d usage
  in `statusBar.right`, 5-minute poll, click-to-refresh, full lines in
  the tooltip. See [Desktop chip](#desktop-chip-7d30d-statusbar) below.

## Requirements

- Hermes Agent — any build with the model-provider plugin system
  (`providers.register_provider` + `providers.base.ProviderProfile`).
- A Kenari API key — dashboard → API keys at
  <https://kenari.id>. Top up via QRIS (from Rp 1.000), or skip top-up
  and use `:free`-suffixed models.

## Install

This repo ships **two independent plugins**. Install either or both.

| Folder | What it is | Install into |
|---|---|---|
| `kenari/` | The model provider (`kind: model-provider`) — inference, picker, pricing, `hermes usage` | `~/.hermes/plugins/model-providers/kenari` |
| `kenari-usage/` | The desktop statusbar chip (dashboard backend + `desktop/plugin.js`) | `~/.hermes/plugins/kenari-usage` |

They are separate because Hermes loads them through different registries:
model providers load from `plugins/model-providers/` (or the flat
`plugins/<name>/` when the manifest declares `kind: model-provider`), while
the desktop-half copier and the dashboard backend only scan the **flat**
`plugins/<name>/` root. The chip's backend reuses the provider's usage
snapshot, so **the provider must be installed for the chip to have data** —
but the provider works fine on its own.

### Provider (`kenari/`)

```bash
# From git — the subdirectory goes in the identifier (there is no --subdir flag):
hermes plugins install averous12/hermes-kenari-provider/kenari
# equivalent forms:
hermes plugins install averous12/hermes-kenari-provider#kenari
hermes plugins install https://github.com/averous12/hermes-kenari-provider.git/kenari

# Or as a pip package (entry point `hermes_agent.plugins`):
pip install git+https://github.com/averous12/hermes-kenari-provider.git
```

### Desktop chip (`kenari-usage/`)

```bash
hermes plugins install averous12/hermes-kenari-provider/kenari-usage
# or:
hermes plugins install averous12/hermes-kenari-provider#kenari-usage
```

Then restart the desktop app (it copies `desktop/plugin.js` into
`desktop-plugins/` on launch) and enable the chip under
**Capabilities → Plugins → Kenari Usage**.

### Manual install (no git)

```bash
git clone https://github.com/averous12/hermes-kenari-provider /tmp/kenari-plugin

cp -r /tmp/kenari-plugin/kenari        ~/.hermes/plugins/model-providers/kenari
cp -r /tmp/kenari-plugin/kenari-usage  ~/.hermes/plugins/kenari-usage
```

Verify:

```bash
hermes plugins list          # `kenari` and `kenari-usage`, both enabled
hermes doctor                # provider health check
hermes usage --provider kenari
```

## Configure

Add your API key to `~/.hermes/.env`:

```
KENARI_API_KEY=kn-...
```

| Variable | Required | Purpose |
|---|---|---|
| `KENARI_API_KEY` | yes | Kenari API key (`kn-...`) |
| `KENARI_BASE_URL` | no | Override the default endpoint (proxy / self-hosted relay) |

## Use

```bash
hermes model                                   # interactive picker → kenari
hermes --provider kenari -m step-3-7-flash:free
hermes usage --provider kenari                 # wallet + plan + spend
```

Or set it as your default in `~/.hermes/config.yaml`:

```yaml
model:
  provider: kenari
  default: step-3-7-flash:free
```

### Recommended models

| Model | Notes |
|---|---|
| `step-3-7-flash:free` | Free, tool-calling — good default for zero-balance setups |
| `muse-spark-1-3-contributor:free` | Free, vision-capable, 1M context |
| `step-3-7-flash` | Paid, cheapest tool-calling route at last check |
| `glm-5-3-flash` | Paid, cheap, vision, 1M context |
| `claude-haiku-5-5` | Paid, frontier-cheap, 1M context |

Run `hermes model` to see the live list for *your* key — it's read fresh
from the Kenari catalog and intersected with your key's scope.

## Desktop chip (7d/30d statusbar)

The `kenari-usage/` folder is a companion plugin: a `statusBar.right`
chip showing `kenari 7d 321 req • 30d 321 req` (live figures), with the
full usage lines in the tooltip and click-to-refresh. Data comes from a
small Python backend that reuses the provider's own usage snapshot.

```bash
# 1. Install the companion (flat plugin dir — the desktop-half copier
#    only scans plugins/<name>/, not plugins/model-providers/):
cp -r kenari-usage ~/.hermes/plugins/kenari-usage
# or: hermes plugins install averous12/hermes-kenari-provider/kenari-usage

# 2. Restart the desktop app (it copies the desktop half on launch),
#    or run Rescan/Rebuild from the app if available.

# 3. Enable the chip: Capabilities → Plugins → Kenari Usage → on.
#    The backend needs the provider enabled (already done above).
```

The chip polls every 5 minutes (Kenari rate-limits usage routes) and
fails quiet — no key or a down gateway shows `kenari n/a`, never a
toast.

## Troubleshooting

| Symptom | Check |
|---|---|
| `unknown provider: kenari` | `hermes plugins list` — is the plugin enabled? New sessions pick it up after enable. |
| `Model 'x' not found` | Run `hermes model` — the picker shows only tool-capable chat routes your key's scope allows. |
| `is not allowed by this key` | Your key is model-scoped; pick from its allowed list (the picker already filters to it). |
| `insufficient_balance` (402) | Top up at <https://kenari.id/pay>, lower `max_tokens`, or switch to a `:free` model. |
| `free_quota_daily` (429) | Free-daily allowance spent — wait for UTC midnight, or use a paid model. |
| Balance/plan hidden in `hermes usage` | Scoped keys can't read account endpoints; usage shown is this key only. |
| Wrong context/vision for a Kenari model | Should not happen — capabilities are declared from the live catalog. If one is wrong, your explicit `model_overrides.<kenari>.<model>` in `config.yaml` still wins; report it as a plugin issue. |

Logs live under `~/.hermes/logs/` — `hermes logs --follow` is the quickest
way to watch a request go through.

## Hermes version

There is no version pin. Every contact point with core
(`_supported_kwargs`, `_inherited_fetch`, method-body-only Hermes
imports) is resolved by introspection, so newer profile fields are used
when present and skipped on older builds.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests run fully offline: `tests/conftest_stub.py` stubs the Hermes
`providers` runtime (including a legacy profile base), `agent`
usage/cost contracts, and `hermes_cli.urllib_security`, so both modern
and older Hermes builds are exercised.

## Links

- Kenari docs: <https://kenari.id/docs>
- Live models + IDR pricing: <https://kenari.id/v1/models>
- OpenAPI spec: <https://kenari.id/openapi.json>
- Kenari MCP server: `https://kenari.id/mcp`

## License

MIT — see [LICENSE](LICENSE).
