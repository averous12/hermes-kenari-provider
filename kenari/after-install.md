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
