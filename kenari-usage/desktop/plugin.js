/**
 * Kenari usage statusbar chip — 7d & 30d request/spend at a glance.
 *
 * Install: copy this folder (`kenari-usage/`) to `<hermes home>/plugins/`
 * so the desktop half lands at `plugins/kenari-usage/desktop/plugin.js`,
 * enable the Python backend (`hermes plugins enable kenari-usage` or the
 * `plugins.enabled` gate), then enable the chip in the desktop app under
 * Capabilities → Plugins. Data comes from the companion backend
 * (`dashboard/plugin_api.py` → `GET /api/plugins/kenari-usage/usage-summary`),
 * which reuses the kenari provider profile's own usage snapshot — the same
 * figures `hermes usage --provider kenari` prints.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { cn, haptic, host, Tip, usePluginI18n, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'kenari-usage'

// Usage routes are rate-limited (60/min/account across all three) and the
// figures move slowly — a 5-minute poll is plenty, with click-to-refresh.
const POLL_MS = 5 * 60 * 1000

function pct(value) {
  if (typeof value !== 'number' || !isFinite(value)) return null
  return `${Math.round(value)}%`
}

function KenariChip({ ctx }) {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const gateway = useValue(host.state.gateway)

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: [ID, 'usage-summary'],
    queryFn: () => ctx.rest('/usage-summary', { timeoutMs: 15000 }),
    refetchInterval: POLL_MS,
    // The gateway may be down when the app starts — retry quietly, never toast.
    retry: 2,
    retryDelay: 5000,
  })

  const summary = data && data.ok ? data : null
  const plan = summary ? summary.plan : null
  const weekLeft = plan ? pct(plan.week_remaining_percent) : null
  const monthLeft = plan ? pct(plan.month_remaining_percent) : null

  let label
  if (isLoading && !summary) {
    label = t('loading')
  } else if (weekLeft && monthLeft) {
    label = t('chipLabel', weekLeft, monthLeft)
  } else if (weekLeft || monthLeft) {
    label = t('chipPartial', weekLeft || '—', monthLeft || '—')
  } else if (summary) {
    // Reachable but no plan windows (e.g. a model-scoped key: Kenari
    // refuses /account/quota with 403, or no active subscription).
    label = t('noPlan')
  } else if (isError) {
    label = t('unavailable')
  } else {
    label = t('loading')
  }

  const tipLines = []
  if (plan) {
    if (plan.name) tipLines.push(t('planName', plan.name))
    if (weekLeft) {
      tipLines.push(
        t('weekLine', weekLeft, plan.week_detail || '', plan.week_resets_at || t('noReset'))
      )
    }
    if (monthLeft) {
      tipLines.push(
        t('monthLine', monthLeft, plan.month_detail || '', plan.month_resets_at || t('noReset'))
      )
    }
  }
  if (summary && summary.lines && summary.lines.length) {
    if (tipLines.length) tipLines.push('')
    tipLines.push(...summary.lines)
  }
  if (!tipLines.length) {
    tipLines.push(t('tipEmpty', (data && data.reason) || gateway))
  }

  const onClick = () => {
    haptic('tap')
    // Invalidate + refetch: click means "refresh now".
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
          jsx('span', { children: '🪁' }),
          jsx('span', { children: label }),
        ]
      })
    })
  })
}

export default {
  id: ID, // must match the folder name (plugins/kenari-usage)
  name: 'Kenari Usage',
  register(ctx) {
    ctx.i18n.register({
      en: {
        chipTip: 'Kenari plan quota left — 7d / 30d — click to refresh',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        noPlan: 'kenari — no plan',
        chipLabel: (week, month) => `kenari 7d ${week} left • 30d ${month} left`,
        chipPartial: (week, month) => `kenari 7d ${week} • 30d ${month}`,
        planName: plan => `Plan: ${plan}`,
        weekLine: (pctLeft, detail, reset) =>
          `7d remaining: ${pctLeft}${detail ? ` (${detail})` : ''} • resets ${reset}`,
        monthLine: (pctLeft, detail, reset) =>
          `30d remaining: ${pctLeft}${detail ? ` (${detail})` : ''} • resets ${reset}`,
        noReset: 'unknown',
        tipEmpty: why => `Kenari usage unavailable (${why})`
      },
      id: {
        chipTip: 'Sisa kuota paket Kenari — 7d / 30d — klik untuk memuat ulang',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        noPlan: 'kenari — tanpa paket',
        chipLabel: (week, month) => `kenari 7d sisa ${week} • 30d sisa ${month}`,
        chipPartial: (week, month) => `kenari 7d ${week} • 30d ${month}`,
        planName: plan => `Paket: ${plan}`,
        weekLine: (pctLeft, detail, reset) =>
          `Sisa 7d: ${pctLeft}${detail ? ` (${detail})` : ''} • reset ${reset}`,
        monthLine: (pctLeft, detail, reset) =>
          `Sisa 30d: ${pctLeft}${detail ? ` (${detail})` : ''} • reset ${reset}`,
        noReset: 'tidak diketahui',
        tipEmpty: why => `Pemakaian Kenari tidak tersedia (${why})`
      }
    })

    ctx.register({
      id: 'usage-chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(KenariChip, { ctx })
    })
  }
}
