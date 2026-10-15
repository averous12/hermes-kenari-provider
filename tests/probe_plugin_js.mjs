/**
 * Runtime probe for kenari-helper/desktop/plugin.js.
 *
 * Loads the real plugin file under stub `react`, `react/jsx-runtime` and
 * `@hermes/plugin-sdk` modules so we can catch what static checks miss:
 * undefined SDK exports, bad import specifiers, and throw-on-render. Then it
 * registers the plugin and renders the chip against a realistic plan payload,
 * printing the label and tooltip the app would show.
 *
 * Run: node tests/probe_plugin_js.mjs
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))
const pluginPath = path.resolve(here, '..', 'kenari-helper', 'desktop', 'plugin.js')

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kenari-probe-'))

// ── stub modules (real ESM, so the plugin's imports actually execute) ────
fs.writeFileSync(
  path.join(tmp, 'stub-react.mjs'),
  `
export function useEffect(fn) { const d = fn(); (globalThis.__disposers ||= []).push(d); return d }
export function useState(i) { return [i, () => {}] }
export function useReducer(r, i) { return [i, () => {}] }
export function useCallback(fn) { return fn }
export function useRef(v) { return { current: v } }
export function useMemo(fn) { return fn() }
`
)

fs.writeFileSync(
  path.join(tmp, 'stub-jsx.mjs'),
  `
export const Fragment = 'Fragment'
export function jsx(type, props) { return { __jsx: true, type, props } }
export function jsxs(type, props) { return { __jsx: true, type, props } }
`
)

fs.writeFileSync(
  path.join(tmp, 'stub-sdk.mjs'),
  `
export const atoms = []
export function atom(init) {
  const a = { __value: init, get: () => a.__value, set: v => { a.__value = v }, listen: () => () => {}, subscribe: () => () => {} }
  atoms.push(a)
  return a
}
export function computed(fn) { return { get: fn } }
export function useValue(a) { return a.get() }

export const __state = { queryData: null, strings: {}, intervals: [], lastQuery: null }

export function useQuery(config) {
  __state.lastQuery = config
  return {
    data: __state.queryData,
    isLoading: __state.queryData === null,
    isError: false,
    refetch: () => {},
    isFetching: false
  }
}
export function useQueryClient() { return { invalidateQueries: () => {} } }
export function usePluginI18n() {
  return (key, ...args) => {
    const entry = __state.strings[key]
    return typeof entry === 'function' ? entry(...args) : entry ?? key
  }
}
export function useI18n() { return k => k }
export function cn(...p) { return p.filter(Boolean).join(' ') }
export function haptic() {}
export const host = { state: { gateway: { get: () => 'open' } }, notify: () => {} }
export const Tip = 'Tip'
export const icons = {}
`
)

// ── rewrite the plugin's bare specifiers to the stubs ───────────────────
const source = fs.readFileSync(pluginPath, 'utf8')
const rewritten = source
  .replace("from 'react'", "from './stub-react.mjs'")
  .replace("from 'react/jsx-runtime'", "from './stub-jsx.mjs'")
  .replace("from '@hermes/plugin-sdk'", "from './stub-sdk.mjs'")

const pluginTmp = path.join(tmp, 'plugin-under-test.mjs')
fs.writeFileSync(pluginTmp, rewritten)

// ── load + register ─────────────────────────────────────────────────────
// The plugin injects a <style> tag; Node has no DOM, so provide the two
// methods it touches (getElementById / createElement+appendChild).
const headChildren = []
globalThis.document = {
  head: { appendChild: el => headChildren.push(el) },
  getElementById: () => null,
  createElement: () => ({ id: '', textContent: '', remove: () => {} })
}

const sdk = await import(pathToFileURL(path.join(tmp, 'stub-sdk.mjs')).href)
const plugin = (await import(pathToFileURL(pluginTmp).href)).default

console.log('plugin id:', plugin.id, '| name:', plugin.name)

const registered = []
const ctx = {
  i18n: {
    register: bundles => {
      sdk.__state.strings = bundles.en
      sdk.__state.bundles = bundles
    }
  },
  register: c => registered.push(c),
  setInterval: (fn, ms) => {
    sdk.__state.intervals.push({ fn, ms })
    return () => {}
  },
  setTimeout: () => () => {},
  onDispose: () => {},
  rest: async () => ({}),
  socket: () => () => {},
  storage: { get: () => undefined, set: () => {}, remove: () => {} },
  os: {},
  onEvent: () => () => {},
  addEventListener: () => () => {}
}

plugin.register(ctx)
console.log('contributions:', registered.map(c => `${c.id}@${c.area}`).join(', '))
console.log('i18n en keys:', Object.keys(sdk.__state.strings).length)

const chip = registered.find(c => c.id === 'usage-chip')
if (!chip) throw new Error('usage-chip contribution missing')

// ── realistic payload: Indie plan, week resets in ~4d 7h ────────────────
const HOUR = 3600 * 1000
const DAY = 24 * HOUR
sdk.__state.queryData = {
  ok: true,
  plan: {
    name: 'Indie',
    week_remaining_percent: 84,
    week_used_percent: 16,
    week_resets_at: new Date(Date.now() + 4 * DAY + 7 * HOUR).toISOString(),
    week_detail: 'Rp63.000 left of Rp75.000',
    month_remaining_percent: 96,
    month_used_percent: 4,
    month_resets_at: new Date(Date.now() + 21 * DAY + 3 * HOUR).toISOString(),
    month_detail: 'Rp288.000 left of Rp300.000'
  },
  free_cache: {
    plan_id: 'plan-ringan-v3',
    models: ['deepseek-v4-flash', 'deepseek-v4-1-flash', 'mimo-v2-6-flash'],
    count: 3
  },
  web_search: {
    used: 8,
    limit: 50,
    remaining: 42,
    resets_at: new Date(Date.now() + 12 * HOUR).toISOString()
  },
  windows: [],
  lines: ['Balance: Rp19.589 available (Rp0 reserved)', 'Last 7d: 565 requests • Rp0 spent']
}

function labelOf(tree) {
  // <Tip><button><span><span(🐦)/><span(label)/></span></button></Tip>
  return tree?.props?.children?.props?.children?.props?.children?.[1]?.props?.children
}

/** The chip's leading icon span (the canary mark). */
function iconOf(tree) {
  return tree?.props?.children?.props?.children?.props?.children?.[0]
}

// `chip.render()` returns `<KenariChip ctx={..} />` — a component element, not
// a rendered tree. Call it the way React would so the hooks (useQuery,
// useValue, usePluginI18n) actually execute.
const tree = chip.render().type({ ctx })
console.log('\nchip label (slot 0):', labelOf(tree))
const icon = iconOf(tree)
console.log('chip icon:', JSON.stringify(icon?.props?.children), '| class:', icon?.props?.className)
if (icon?.props?.children !== '\u{1F426}') throw new Error('chip icon is not the canary (U+1F426)')
if (icon?.props?.className !== 'kenari-canary') throw new Error('chip icon class mismatch: ' + icon?.props?.className)
// The icon span owns the mark; the label must NOT repeat it, or the chip
// shows the bird twice.
if (String(labelOf(tree)).includes('\u{1F426}')) throw new Error('label duplicates the canary mark')
console.log('tooltip:')
for (const line of String(tree?.props?.label).split('\n')) console.log('   ', line)

// Rotate: advance the slot atom the way the interval would, then re-render.
// Identify the slot atom by BEHAVIOUR (whichever atom the 8s interval bumps) —
// "last atom declared" stopped being the slot once $freeCache was added.
const atomsBefore = sdk.atoms.map(a => a.get())
sdk.__state.intervals.find(i => i.ms === 8000).fn()
const changed = sdk.atoms.filter((a, i) => a.get() !== atomsBefore[i])
if (changed.length !== 1) throw new Error(`expected exactly one atom bumped by rotation, got ${changed.length}`)
const slotAtom = changed[0]
console.log('slot atom identified by rotation:', typeof slotAtom.get() === 'number')
slotAtom.set(slotAtom.get() + 1)
console.log('\nchip label (slot 1):', labelOf(chip.render().type({ ctx })))

// ── daily web-search slot ───────────────────────────────────────────────
// windows = [7d, 30d, ws] when the plan caps the daily allowance, so slot 2
// is the web-search counter (used/limit, not a percentage).
console.log('\n--- daily web-search slot ---')
const wsLabels = []
for (let i = 0; i < 3; i++) {
  slotAtom.set(i)
  wsLabels.push(labelOf(chip.render().type({ ctx })))
}
wsLabels.forEach((l, i) => console.log(`  slot ${i}:`, l))
if (!wsLabels[2].includes('8/50')) throw new Error('web-search slot missing the 8/50 counter: ' + wsLabels[2])
if (!wsLabels[2].includes('Search')) throw new Error('web-search slot label should read "Search": ' + wsLabels[2])
console.log('  counter 8/50 present in slot 2:', true)

// The tooltip must carry the same figure with the remaining count, and say
// "search + fetch" because the one allowance covers both.
slotAtom.set(2)
const wsTip = String(chip.render().type({ ctx })?.props?.label)
const wsLine = wsTip.split('\n').find(l => l.includes('Web search'))
console.log('  tooltip line:', wsLine)
if (!wsLine || !wsLine.includes('8/50') || !wsLine.includes('42 left')) {
  throw new Error('web-search tooltip line wrong: ' + wsLine)
}
if (!wsLine.includes('+ fetch')) {
  throw new Error('tooltip should say the allowance covers search + fetch: ' + wsLine)
}

// ── simulate the allowance running out ──────────────────────────────────
// used == limit: the chip must show a full counter, and the tooltip must say
// 0 left — never a negative remainder or a bare percentage.
for (const [used, limit, expect] of [[50, 50, '50/50'], [51, 50, '51/50']]) {
  sdk.__state.queryData.web_search = {
    used, limit, remaining: Math.max(0, limit - used),
    resets_at: new Date(Date.now() + 2 * HOUR).toISOString()
  }
  slotAtom.set(2)
  const tree = chip.render().type({ ctx })
  const label = labelOf(tree)
  const tip = String(tree?.props?.label)
  const line = tip.split('\n').find(l => l.includes('Web search'))
  console.log(`  used=${used} -> label=${JSON.stringify(label)} | tip=${JSON.stringify(line)}`)
  if (!label.includes(expect)) throw new Error(`expected ${expect} in label, got ${label}`)
  if (line.includes('-')) throw new Error('tooltip shows a negative remainder: ' + line)
}
console.log('  exhausted -> full counter, no negative remainder:', true)

// No cap on the plan (limit 0/absent) -> slot disappears, no empty counter.
sdk.__state.queryData.web_search = { used: 3, limit: 0, resets_at: null }
slotAtom.set(0)
const noCap = labelOf(chip.render().type({ ctx }))
console.log('  limit=0 -> chip falls back to a plan window:', JSON.stringify(noCap))
if (noCap.includes('/')) throw new Error('uncapped plan must not render a counter: ' + noCap)

// Countdown sanity: a window that resets in 90 minutes, forced onto slot 0.
slotAtom.set(0)
sdk.__state.queryData.plan.week_resets_at = new Date(Date.now() + 90 * 60 * 1000).toISOString()
console.log('chip label (week, <1d):', labelOf(chip.render().type({ ctx })))

// Rotation must be driven by a VALUE write, not an updater fn (nanostores
// atom.set takes a value only) — a function here would break the slot math.
slotAtom.set(0)
const before = slotAtom.get()
sdk.__state.intervals.find(i => i.ms === 8000).fn()
const after = slotAtom.get()
console.log('rotation: slot', before, '->', after, '| value-not-fn:', typeof after === 'number')

// A scoped key: no plan windows at all.
sdk.__state.queryData = { ok: true, plan: null, windows: [], lines: ['Last 7d: 565 requests • Rp0 spent'] }
console.log('chip label (no plan):', labelOf(chip.render().type({ ctx })))

console.log('\nintervals registered (ms):', sdk.__state.intervals.map(i => i.ms).join(', '))
console.log('query refetchInterval:', sdk.__state.lastQuery?.refetchInterval)

// ── composer pill label provider ────────────────────────────────────────
const pill = registered.find(c => c.id === 'pill-label')
if (!pill) throw new Error('pill-label contribution missing')
if (pill.area !== 'composer.modelPill') throw new Error('wrong pill area: ' + pill.area)
const provider = pill.data
if (!provider || typeof provider.label !== 'function') throw new Error('pill provider has no label()')

console.log('\n--- composer pill label ---')
// The chip publishes the free-cache list from its poll, so re-render it with a
// full plan payload first (the scoped-key case above cleared the list).
sdk.__state.queryData = {
  ok: true,
  plan: { name: 'Indie', week_remaining_percent: 24, month_remaining_percent: 81 },
  free_cache: { models: ['deepseek-v4-flash', 'deepseek-v4-1-flash', 'mimo-v2-6-flash'] },
  windows: [],
  lines: []
}
chip.render().type({ ctx })
for (const { fn } of sdk.__state.intervals) fn()

const cases = [
  ['deepseek-v4-1-flash', 'free-cache model -> badged'],
  ['deepseek-v4-flash', 'free-cache model -> badged'],
  ['mimo-v2-6-flash', 'free-cache model -> badged'],
  ['deepseek-v4-pro', 'paid model -> declines (core label wins)'],
  ['claude-sonnet-5', 'paid model -> declines'],
  ['', 'empty model -> declines'],
  ['mimo-v2-6-flash:free', ':free route suffix -> still matched']
]
for (const [model, why] of cases) {
  const out = provider.label({ model, reasoningEffort: '', compact: false })
  console.log(`  ${(model || '(empty)').padEnd(24)} -> ${out === null ? 'null' : JSON.stringify(out)}   (${why})`)
}

// compact mode: the host never consults providers, but a stray call must not throw.
const compactOut = provider.label({ model: 'deepseek-v4-1-flash', reasoningEffort: '', compact: true })
console.log('  compact:true             ->', compactOut === null ? 'null' : JSON.stringify(compactOut))

// A throwing/odd model value must not blank the pill (host catches, but be sure).
for (const bad of [undefined, null, 123, {}]) {
  try {
    provider.label({ model: bad, reasoningEffort: '', compact: false })
  } catch (e) {
    console.log('  THREW on', JSON.stringify(bad), '->', e.message)
  }
}
console.log('odd model values did not throw')

// Label must be a plain string (the host renders it straight into JSX).
const s = provider.label({ model: 'deepseek-v4-1-flash', reasoningEffort: '', compact: false })
console.log('label is a non-empty string:', typeof s === 'string' && s.trim() !== '')

for (const { fn } of sdk.__state.intervals) fn()
console.log('intervals ran without throwing (post-pill)')

fs.rmSync(tmp, { recursive: true, force: true })
