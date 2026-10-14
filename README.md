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
- **Plan-covered filter (opt-in).** `KENARI_PLAN_ONLY=1` (or
  `kenari.plan_only: true` in `config.yaml`) trims the picker to models
  the **active plan** covers (`scope_models`, matched to the plan name in
  `/v1/account/quota`). Off by default — a key can call models the plan
  excludes. With no readable plan the filter **fails open** (shows
  everything) rather than emptying the picker.
- **Desktop statusbar ticker** (`kenari-usage/` companion): plan quota
  % left for 7d/30d with a live reset countdown in `statusBar.right`,
  rotating windows, 2-minute poll, click-to-refresh, full lines in the
  tooltip. Plus a **composer-pill badge** for the active model when its
  cache reads are free on your plan. See
  [Desktop chip](#desktop-chip-7d30d-statusbar-ticker) below.

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
| repo root + `kenari/` | The model provider (`kind: model-provider`) — inference, picker, pricing, `hermes usage` | `~/.hermes/plugins/kenari` |
| `kenari-usage/` | The desktop statusbar chip (dashboard backend + `desktop/plugin.js`) | `~/.hermes/plugins/kenari-usage` |

They are separate because Hermes loads them through different registries:
model providers load from `plugins/model-providers/` (or the flat
`plugins/<name>/` when the manifest declares `kind: model-provider`), while
the desktop-half copier and the dashboard backend only scan the **flat**
`plugins/<name>/` root. The chip's backend reuses the provider's usage
snapshot, so **the provider must be installed for the chip to have data** —
but the provider works fine on its own.

### Provider — one command

```bash
hermes plugins install averous12/hermes-kenari-provider
```

That's it. (The repo root carries the provider manifest plus a small
`__init__.py` that loads the profile from `kenari/`.)

### Desktop chip — one command

```bash
hermes plugins install averous12/hermes-kenari-provider/kenari-usage
```

Then restart the desktop app (it copies `desktop/plugin.js` into
`desktop-plugins/` on launch) and enable the chip under
**Capabilities → Plugins → Kenari Usage**.

### Equivalent forms

```bash
# Provider, naming the subdirectory explicitly (keeps the plugin dir minimal):
hermes plugins install averous12/hermes-kenari-provider/kenari
hermes plugins install averous12/hermes-kenari-provider#kenari
hermes plugins install https://github.com/averous12/hermes-kenari-provider.git/kenari

# As a pip package (entry point `hermes_agent.plugins`):
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
| `KENARI_PLAN_ONLY` | no | `1` = list only models the active plan covers. Default off; see below. |

### `KENARI_PLAN_ONLY` — show only plan-covered models

Your key and your plan are different gates: a key may permit models the
plan excludes, and picking one of those fails or bills differently. This
toggle trims the picker to the **plan's** entitlement list.

```bash
KENARI_PLAN_ONLY=1                       # in ~/.hermes/.env
```

```yaml
# or persist it in ~/.hermes/config.yaml (env wins when both are set)
kenari:
  plan_only: true
```

| Value | Picker shows |
|---|---|
| unset / `0` (default) | every tool-capable chat model your **key** can call |
| `1` | those, narrowed to the **active plan's** `scope_models` |

Live example on an Indie plan: 70 key-callable models → 34 plan-covered;
the 36 dropped include `claude-opus-5`, `gpt-5-6-sol`, and several
`:free` routes the plan does not cover.

Behaviour when there is **no active plan** — a scoped key that 403s on
`/v1/account/quota`, no subscription, an unknown plan name, or an
unreachable plan catalog — is to **not filter**. Over-listing costs a
clear API error; hiding every model would leave an unusable picker. An
unparseable toggle value is likewise treated as unset, never guessed.

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

## Desktop chip (7d/30d statusbar ticker)

The `kenari-usage/` folder is a companion plugin with two desktop surfaces:

**Statusbar chip** (`statusBar.right`) — a live ticker showing **plan quota
remaining** (not request counts):

```
🐦 kenari 7d 24% · 1d 4h left      → rotates →      🐦 kenari 30d 81% · 21d 3h left
```

- Rotates between the 7-day and 30-day windows every 8s.
- Each shows percent left plus a live countdown to that window's reset
  (`4d 7h left` on the chip; `d h m s` in the tooltip).
- The 1s clock is local math over cached data — no extra network.
- The tooltip carries the full detail: plan name, both windows with Rp
  left, and the plan's free-cache model list.
- Click to refresh; polls every 2 minutes (well inside Kenari's
  60 req/min limit on usage routes).

**Composer pill** (`composer.modelPill`) — badges the *active* model when
its cache reads are free on your plan:

```
⚡ deepseek-v4-1-flash · cache-free
```

This is a plugin-provided **label** only: chrome, menu, and the routed
model id are untouched, so the badge can never affect a request. It
declines (`null`) for every other model, leaving the core label in place.

### Free-cache models are resolved dynamically

Kenari publishes `free_cache_models` per plan at `/api/plans`; the active
plan name comes from `/v1/account/quota`. The backend matches the two, so
**a plan change needs no code change** — the chip tooltip and the pill
badge both follow your current plan automatically.

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

The chip fails quiet — no key or a down gateway shows `kenari n/a`, never
a toast. A model-scoped key that Kenari 403s on `/account/quota` shows
`kenari — no plan` instead of an error.

## Model picker rows (optional, needs a core patch)

The in-chat picker's per-row price/badge chip is rendered by core code
from a **core-only** pricing registry (`_PRICING_FETCHERS`) with no plugin
hook, and picker rows have no plugin decoration area. A plugin therefore
cannot badge picker rows — the composer pill above is the plugin-reachable
equivalent.

If you want live prices and a `Cache-free` chip on picker rows, apply the
bundled patch:

```bash
python scripts/apply_kenari_pricing_patch.py            # idempotent
python scripts/apply_kenari_pricing_patch.py --revert    # byte-clean undo
python tests/verify_pricing_patch.py                     # apply/revert self-test
```

It registers a `kenari` entry in `_PRICING_FETCHERS` so `:free` routes
badge green and paid routes show `$in/$out` per Mtok (cache rate included).
**This edits Hermes core** (`hermes_cli/models_pricing.py`) and a `hermes`
update will revert it — re-run the script after upgrading. The script
keeps a `.kenari-orig` backup and refuses to double-apply.

### Why not suffix the model name instead?

The picker renders one string per model and uses it as **both** the label
and the routed id (`onSelect({ provider, model })`), so a suffix like
`deepseek-v4-1-flash · cache-free` would be sent to Kenari and rejected.
There is no separate display-name field, and the existing suffix
prettifier (`VARIANT_TAGS`: `-fast` / `-flash` / `-thinking` / `-preview`
/ `-latest`) is a fixed list that *strips* rather than appends. The
composer pill is the sanctioned place to change a model's display text.

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
python -m unittest discover -s tests -v      # provider: 50 offline tests
node tests/probe_plugin_js.mjs                # desktop half: loads the REAL
                                             # plugin under stubbed SDK/react
                                             # and asserts chip + pill output
python tests/verify_pricing_patch.py          # core patch apply/revert
```

The tests run fully offline: `tests/conftest_stub.py` stubs the Hermes
`providers` runtime (including a legacy profile base), `agent`
usage/cost contracts, and `hermes_cli.urllib_security`, so both modern
and older Hermes builds are exercised.

`tests/probe_plugin_js.mjs` is the only check that catches desktop-half
bugs static linting misses: it rewrites the plugin's bare import
specifiers to stubs, registers it, and inspects the rendered tree. It
asserts the chip's icon span glyph *and* class, so a wrong marker fails
loudly (that check exists because a wrong emoji shipped once).

### Gotchas worth knowing before you edit the desktop half

- **`atom.set()` takes a value, not an updater function.** `$slot.set(s => s + 1)`
  stores the *function*; use `$slot.set($slot.get() + 1)`.
- **The chip's mark is a separate `<span>`, not part of the label.** Editing
  the label strings alone leaves the icon unchanged — assert the icon span.
- **`composer.modelPill` labels run outside React render** (inside the host's
  `useMemo`), so a provider must read atoms directly (`$freeCache.get()`), never
  via a hook, and must return a plain string or `null`.
- **Emoji support is not uniform.** Prefer Unicode ≤6.0 (`🐦` U+1F426 is fine;
  `🪁` U+1FA81 and `🪶` U+1FAB6 are not). Older blocks render everywhere.
- **`ModelPricing` lives in a generated file**
  (`apps/shared/src/gateway-contract.generated.ts`) — never hand-edit it;
  extra payload keys type-check via its `[key: string]: unknown` index.

## Links

- Kenari docs: <https://kenari.id/docs>
- Live models + IDR pricing: <https://kenari.id/v1/models>
- OpenAPI spec: <https://kenari.id/openapi.json>
- Kenari MCP server: `https://kenari.id/mcp`

## License

MIT — see [LICENSE](LICENSE).
