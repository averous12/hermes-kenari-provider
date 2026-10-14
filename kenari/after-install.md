Kenari provider installed.

1. Add your key to `~/.hermes/.env`:

   ```
   KENARI_API_KEY=kn-...
   ```

   Get one at https://kenari.id — dashboard → API keys. Top up via QRIS
   (from Rp 1.000), or skip top-up and use `:free`-suffixed models.

2. Pick a model: run `hermes model` and choose `kenari`, or launch with
   `hermes --provider kenari -m step-3-7-flash:free`.

   `step-3-7-flash:free` costs nothing and is a good first check that
   the key and provider wiring work. The picker shows only models your
   key's scope allows.

   Check spend any time with `hermes usage --provider kenari` (wallet,
   plan weekly/monthly windows, 7d/30d totals, free-daily quota left).

3. Optional:

   - `KENARI_BASE_URL` — point at a proxy or self-hosted relay instead
     of the default `https://kenari.id/v1` endpoint.

   - `KENARI_PLAN_ONLY=1` — **show only plan-covered models.**

     The easiest way is the switch in the desktop: **Capabilities →
     Plugins → Kenari → Show only plan-covered models**. It saves to
     `plugins.entries.kenari.settings.plan_only` in `config.yaml`.

     For a headless/CLI install, set the env var instead:

     ```
     KENARI_PLAN_ONLY=1
     ```

     Resolution order: `KENARI_PLAN_ONLY` → the desktop switch →
     `kenari.plan_only` in `config.yaml` → off. An unparseable value
     counts as *unset* rather than a guess, so a typo can never silently
     hide models.

     Off by default: the picker lists everything your KEY can call, which
     may include models your plan does not cover.

     No active plan (or a model-scoped key that Kenari 403s on
     `/account/quota`) means **no filtering** — the picker shows
     everything the key allows. Hiding models with nothing to compare
     against would empty the picker.

   - The API key + endpoint live in the normal provider card
     (**Settings → Providers → Keys → Kenari**): a Get-key link plus a
     `KENARI_BASE_URL` override field, both declared by this plugin.
