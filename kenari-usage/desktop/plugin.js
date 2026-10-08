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

function fmtRequests(total) {
  if (!total || typeof total.requests !== 'number') return null
  return `${total.requests} req`
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

  const totals = data && data.ok ? data : null
  const seven = totals && totals.usage_7d ? fmtRequests(totals.usage_7d) : null
  const thirty = totals && totals.usage_30d ? fmtRequests(totals.usage_30d) : null

  let label
  if (isLoading && !totals) {
    label = t('loading')
  } else if (seven && thirty) {
    label = t('chipLabel', seven, thirty)
  } else if (totals) {
    label = t('partial')
  } else if (isError || (data && !data.ok)) {
    label = t('unavailable')
  } else {
    label = t('loading')
  }

  const tipLines = totals && totals.lines && totals.lines.length
    ? totals.lines.join('\n')
    : t('tipEmpty', (data && data.reason) || gateway)

  const onClick = () => {
    haptic('tap')
    // Invalidate + refetch: click means "refresh now".
    queryClient.invalidateQueries({ queryKey: [ID, 'usage-summary'] })
    refetch()
  }

  return jsx(Tip, {
    label: tipLines,
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
        chipTip: 'Kenari 7d / 30d usage — click to refresh',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        partial: 'kenari ~',
        chipLabel: (seven, thirty) => `kenari 7d ${seven} • 30d ${thirty}`,
        tipEmpty: (why) => `Kenari usage unavailable (${why})`
      },
      id: {
        chipTip: 'Pemakaian Kenari 7d / 30d — klik untuk memuat ulang',
        loading: 'kenari …',
        unavailable: 'kenari n/a',
        partial: 'kenari ~',
        chipLabel: (seven, thirty) => `kenari 7d ${seven} • 30d ${thirty}`,
        tipEmpty: (why) => `Pemakaian Kenari tidak tersedia (${why})`
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
