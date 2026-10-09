/**
 * Runtime probe for kenari-usage/desktop/plugin.js.
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
const pluginPath = path.resolve(here, '..', 'kenari-usage', 'desktop', 'plugin.js')

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
  windows: [],
  lines: ['Balance: Rp19.589 available (Rp0 reserved)', 'Last 7d: 565 requests • Rp0 spent']
}

function labelOf(tree) {
  // <Tip><button><span><span(🪁)/><span(label)/></span></button></Tip>
  return tree?.props?.children?.props?.children?.props?.children?.[1]?.props?.children
}

// `chip.render()` returns `<KenariChip ctx={..} />` — a component element, not
// a rendered tree. Call it the way React would so the hooks (useQuery,
// useValue, usePluginI18n) actually execute.
const tree = chip.render().type({ ctx })
console.log('\nchip label (slot 0):', labelOf(tree))
console.log('tooltip:')
for (const line of String(tree?.props?.label).split('\n')) console.log('   ', line)

// Rotate: advance the slot atom the way the interval would, then re-render.
const slotAtom = sdk.atoms[sdk.atoms.length - 1]
slotAtom.set(slotAtom.get() + 1)
console.log('\nchip label (slot 1):', labelOf(chip.render().type({ ctx })))

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

// Exercise the interval callbacks so a throwing tick fails the probe.
for (const { fn } of sdk.__state.intervals) fn()
console.log('intervals ran without throwing')

fs.rmSync(tmp, { recursive: true, force: true })
