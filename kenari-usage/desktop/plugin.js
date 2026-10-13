/**
 * Kenari usage statusbar chip — a live ticker: quota left per window plus
 * how long until the weekly/monthly window resets.
 *
 * Install: `<hermes home>/plugins/kenari-usage/` (the app copies the
 * desktop half to `desktop-plugins/kenari-usage/` on launch), then enable
 * it under Capabilities → Plugins. Data comes from the companion backend
 * (`dashboard/plugin_api.py` → `GET /api/plugins/kenari-usage/usage-summary`),
 * which reuses the kenari provider's own usage snapshot — the same figures
 * `hermes usage --provider kenari` prints.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { useEffect } from 'react'
import {
  atom,
  cn,
  haptic,
  host,
  Tip,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'kenari-usage'

// Quota figures move slowly — this poll only refreshes the numbers.
const POLL_MS = 2 * 60 * 1000
// The countdown ticks locally off cached data (pure clock math, no network).
const TICK_MS = 1000
// How long each window holds the chip before rotating to the other one.
const ROTATE_MS = 8000

// ── shared reactive atoms ────────────────────────────────────────────────

const $now = atom(Date.now())
const $slot = atom(0)

// Free-cache model ids for the ACTIVE plan, as reported by the backend's
// `/usage-summary` (`free_cache.models`). The statusbar chip fills this in on
// every poll; the composer pill reads it to decide whether to badge. An empty
// list means "unknown / no plan", which renders the plain label — never a
// false badge.
const $freeCache = atom([])

/** Strip a `:free` route suffix when matching, so `x` and `x:free` agree. */
function baseId(model) {
  const s = String(model || '').trim()
  const colon = s.indexOf(':')
  return colon > 0 ? s.slice(0, colon) : s
}

/** Publish the free-cache list from a usage payload (no-op when absent). */
function publishFreeCache(summary) {
  const models = summary && summary.free_cache && summary.free_cache.models
  const next = Array.isArray(models) ? models.map(m => baseId(m)).filter(Boolean) : []
  const prev = $freeCache.get()
  // Atom writes are value-only; compare first so an identical poll doesn't
  // wake the pill's subscriber on a 2-minute timer.
  if (next.length === prev.length && next.every((m, i) => m === prev[i])) return
  $freeCache.set(next)
}

/** Run `setup` with a plugin-scoped interval; auto-clears on unload/reload. */
function useInterval(ctx, setup, deps) {
  useEffect(() => {
    const dispose = setup((fn, ms) => ctx.setInterval(fn, ms))
    return () => {
      if (typeof dispose === 'function') dispose()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}

/** Keep the clock atom in sync from a 1s plugin-scoped interval. */
function useClock(ctx) {
  const now = useValue($now)
  useInterval(
    ctx,
    setInterval => {
      const tick = () => $now.set(Date.now())
      tick()
      return setInterval(tick, TICK_MS)
    },
    [ctx]
  )
  return now
}

// ── countdown ────────────────────────────────────────────────────────────

/**
 * Compact countdown from a duration. Days are the headline and hours the
 * refinement on the chip; minutes/seconds appear in the tooltip once no
 * day remains, so the chip never grows wider than a few characters.
 */
function countdown(msLeft) {
  const ms = Math.max(0, msLeft)
  const s = Math.floor(ms / 1000)
  return {
    d: Math.floor(s / 86400),
    h: Math.floor((s % 86400) / 3600),
    m: Math.floor((s % 3600) / 60),
    s: s % 60,
    done: ms <= 0
  }
}

function pct(value) {
  if (typeof value !== 'number' || !isFinite(value)) return null
  return Math.round(value)
}

// ── component ────────────────────────────────────────────────────────────

function KenariChip({ ctx }) {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const gateway = useValue(host.state.gateway)
  const now = useClock(ctx)
  const slot = useValue($slot)

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: [ID, 'usage-summary'],
    queryFn: () => ctx.rest('/usage-summary', { timeoutMs: 15000 }),
    refetchInterval: POLL_MS,
    // The gateway may be down at app start — retry quietly, never toast.
    retry: 2,
    retryDelay: 5000
  })

  const summary = data && data.ok ? data : null
  const plan = summary ? summary.plan : null

  // Feed the composer-pill badge with this poll's free-cache list.
  useEffect(() => {
    publishFreeCache(summary)
  }, [summary])

  const windows = []
  if (plan) {
    const week = pct(plan.week_remaining_percent)
    const month = pct(plan.month_remaining_percent)
    if (week !== null) {
      windows.push({
        key: 'week', label: t('slotWeek'), percent: week,
        resetsAt: plan.week_resets_at, detail: plan.week_detail
      })
    }
    if (month !== null) {
      windows.push({
        key: 'month', label: t('slotMonth'), percent: month,
        resetsAt: plan.month_resets_at, detail: plan.month_detail
      })
    }
  }

  // Rotate only when there is more than one window to show.
  useInterval(
    ctx,
    setInterval => {
      if (windows.length < 2) return () => {}
      // nanostores' atom.set takes a VALUE, not an updater fn — read then write.
      return setInterval(() => $slot.set($slot.get() + 1), ROTATE_MS)
    },
    [ctx, windows.length]
  )
  const active = windows.length ? windows[slot % windows.length] : null

  let label
  if (isLoading && !summary) {
    label = t('loading')
  } else if (active) {
    const cd = active.resetsAt ? countdown(Date.parse(active.resetsAt) - now) : null
    if (cd && !cd.done) label = t('ticker', active.label, active.percent, t('inDays', cd.d, cd.h))
    else if (cd) label = t('tickerDue', active.label, active.percent)
    else label = t('tickerNoReset', active.label, active.percent)
  } else if (summary) {
    // Reachable but no plan windows (a model-scoped key gets 403 from
    // /account/quota, or there is no active subscription).
    label = t('noPlan')
  } else if (isError) {
    label = t('unavailable')
  } else {
    label = t('loading')
  }

  const tipLines = []
  if (plan && plan.name) tipLines.push(t('planName', plan.name))
  for (const w of windows) {
    const cd = w.resetsAt ? countdown(Date.parse(w.resetsAt) - now) : null
    const remaining = cd && !cd.done ? t('inDhms', cd.d, cd.h, cd.m, cd.s) : t('dueNow')
    tipLines.push(
      t(w.key === 'week' ? 'weekLine' : 'monthLine', w.percent, w.detail || '',
        w.resetsAt ? remaining : t('noReset'))
    )
  }
  // Free-cache models: cache reads billed at $0 on the active plan (Kenari
  // /api/plans → free_cache_models, matched to /account/quota's plan name).
  const freeCache = summary && summary.free_cache
  if (freeCache && freeCache.models && freeCache.models.length) {
    tipLines.push(t('freeCacheLine', freeCache.models.join(', ')))
  }
  if (summary && summary.lines && summary.lines.length) {
    if (tipLines.length) tipLines.push('')
    tipLines.push(...summary.lines)
  }
  if (!tipLines.length) tipLines.push(t('tipEmpty', (data && data.reason) || gateway))

  const onClick = () => {
    haptic('tap')
    queryClient.invalidateQueries({ queryKey: [ID, 'usage-summary'] })
    refetch()
  }

  return jsx(Tip, {
    label: tipLines.join('\n'),
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
        isFetching ? 'opacity-70' : null
      ),
      type: 'button',
      onClick,
      children: jsxs('span', {
        className: 'inline-flex items-center gap-1',
        children: [
          // The pulse is the ticker's "live" cue; reduced-motion turns it off.
          jsx('span', { className: 'kenari-canary', children: '🐦' }),
          jsx('span', { className: 'tabular-nums', children: label })
        ]
      })
    })
  })
}

// ── styles ───────────────────────────────────────────────────────────────
// Injected once per plugin load, removed with the plugin.

const STYLE_ID = 'kenari-usage-style'

function ensureStyles(ctx) {
  if (document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = `
    @keyframes kenari-canary-pulse {
      0%, 100% { opacity: 1; transform: translateY(0); }
      50%      { opacity: 0.72; transform: translateY(-1px); }
    }
    .kenari-canary { display: inline-block; animation: kenari-canary-pulse 2.4s ease-in-out infinite; }
    @media (prefers-reduced-motion: reduce) {
      .kenari-canary { animation: none; }
    }
  `
  document.head.appendChild(style)
  ctx.onDispose(() => {
    const el = document.getElementById(STYLE_ID)
    if (el) el.remove()
  })
}

/**
 * Composer-pill label provider (`composer.modelPill`).
 *
 * The pill shows the CURRENT model; a provider may rewrite only its label
 * text — chrome, menu, and the routed model id are untouched, so a badge
 * here can never break a request. We append a bolt marker when the active
 * model is on the plan's free-cache list, and decline (`null`) otherwise,
 * which leaves the core label in place.
 *
 * Reads the same `$freeCache` atom the statusbar chip publishes, so the two
 * surfaces can't disagree. `label()` runs inside the host's `useMemo`, NOT
 * during a component render — so it must be a plain atom read (`$freeCache.get()`),
 * never a hook. The host re-runs it when the atom's subscribers change.
 */
function kenariPillLabel() {
  return {
    label: ({ model, compact }) => {
      // The host skips providers in compact mode (chevron only); refuse
      // anyway so the contract holds regardless of host version.
      if (compact) return null
      const id = baseId(model)
      if (!id) return null
      const list = $freeCache.get()
      if (!list.length || !list.includes(id)) return null
      return pillText(id)
    }
  }
}

/** Fallback label text (used only if the i18n binder never runs). */
let pillText = id => `⚡ ${id} · cache-free`

/** Rebind `pillText` to the plugin's i18n resolver once ctx is available. */
function bindPillText(ctx) {
  try {
    const t = ctx && ctx.i18n && ctx.i18n.t
    if (typeof t === 'function') {
      pillText = id => t('pillFreeCache', id)
    }
  } catch {
    // Keep the built-in default; the badge is cosmetic.
  }
}

export default {
  id: ID, // must match the folder name (plugins/kenari-usage)
  name: 'Kenari Usage',
  register(ctx) {
    ensureStyles(ctx)

    ctx.i18n.register({
      en: {
        chipTip: 'Kenari plan quota — click to refresh',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        noPlan: 'kenari — no plan',
        slotWeek: '7d',
        slotMonth: '30d',
        ticker: (slot, percentLeft, until) => `kenari ${slot} ${percentLeft}% · ${until}`,
        tickerDue: (slot, percentLeft) => `kenari ${slot} ${percentLeft}% · resetting`,
        tickerNoReset: (slot, percentLeft) => `kenari ${slot} ${percentLeft}%`,
        inDays: (d, h) => (d > 0 ? `${d}d ${h}h left` : `${h}h left`),
        inDhms: (d, h, m, s) =>
          d > 0 ? `${d}d ${h}h ${m}m ${s}s` : h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`,
        dueNow: 'now',
        planName: plan => `Plan: ${plan}`,
        weekLine: (pctLeft, detail, remaining) =>
          `7d remaining: ${pctLeft}%${detail ? ` (${detail})` : ''} • resets in ${remaining}`,
        monthLine: (pctLeft, detail, remaining) =>
          `30d remaining: ${pctLeft}%${detail ? ` (${detail})` : ''} • resets in ${remaining}`,
        noReset: 'unknown',
        freeCacheLine: models => `Cache-free: ${models}`,
        pillFreeCache: id => `⚡ ${id} · cache-free`,
        tipEmpty: why => `Kenari usage unavailable (${why})`
      },
      id: {
        chipTip: 'Kuota paket Kenari — klik untuk memuat ulang',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        noPlan: 'kenari — tanpa paket',
        slotWeek: '7d',
        slotMonth: '30d',
        ticker: (slot, percentLeft, until) => `kenari ${slot} sisa ${percentLeft}% · ${until}`,
        tickerDue: (slot, percentLeft) => `kenari ${slot} sisa ${percentLeft}% · reset`,
        tickerNoReset: (slot, percentLeft) => `kenari ${slot} sisa ${percentLeft}%`,
        inDays: (d, h) => (d > 0 ? `${d}h ${h}j lagi` : `${h}j lagi`),
        inDhms: (d, h, m, s) =>
          d > 0 ? `${d}h ${h}j ${m}m ${s}s` : h > 0 ? `${h}j ${m}m ${s}s` : `${m}m ${s}s`,
        dueNow: 'sekarang',
        planName: plan => `Paket: ${plan}`,
        weekLine: (pctLeft, detail, remaining) =>
          `Sisa 7d: ${pctLeft}%${detail ? ` (${detail})` : ''} • reset dalam ${remaining}`,
        monthLine: (pctLeft, detail, remaining) =>
          `Sisa 30d: ${pctLeft}%${detail ? ` (${detail})` : ''} • reset dalam ${remaining}`,
        noReset: 'tidak diketahui',
        freeCacheLine: models => `Cache gratis: ${models}`,
        pillFreeCache: id => `⚡ ${id} · cache gratis`,
        tipEmpty: why => `Pemakaian Kenari tidak tersedia (${why})`
      }
    })

    bindPillText(ctx)

    ctx.register({
      id: 'usage-chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(KenariChip, { ctx })
    })

    // Composer-pill label: badges the ACTIVE model when its cache reads are
    // free on this plan. A `data` contribution — the host reads `.data` and
    // calls `.label(ctx)`. Declines (null) for every other model, so the
    // core label is untouched. Never affects routing.
    ctx.register({
      id: 'pill-label',
      area: 'composer.modelPill',
      data: kenariPillLabel()
    })
  }
}
