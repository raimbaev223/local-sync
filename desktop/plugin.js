/**
 * local-sync — desktop half: a radar, not a table.
 *
 * Center is this machine; paired machines sit on the inner ring (green), freshly
 * discovered ones on the outer ring (amber). The sweep runs only while discovery
 * is on. Data still comes from dashboard/plugin_api.py → the local-sync daemon;
 * this file only renders and calls ctx.rest.
 */

import { haptic } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'local-sync'

const CSS = `
.ls-root{display:grid;grid-template-columns:minmax(340px,1fr) 330px;height:100%;min-height:520px;font-size:13px}
@media (max-width:900px){.ls-root{grid-template-columns:1fr}}
.ls-scope{position:relative;display:flex;flex-direction:column;padding:18px 20px;overflow:hidden}
.ls-scope::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(circle at 50% 46%,color-mix(in srgb,var(--ui-accent) 9%,transparent),transparent 60%)}
.ls-head{display:flex;align-items:center;justify-content:space-between;gap:14px;z-index:2}
.ls-title h1{margin:0;font-size:16px;font-weight:600;letter-spacing:.02em;color:var(--ui-text-primary)}
.ls-title span{display:block;color:var(--ui-text-tertiary);font-size:11.5px;margin-top:2px}
.ls-search{display:inline-flex;align-items:center;gap:9px;border:1px solid var(--ui-stroke-secondary);
  background:color-mix(in srgb,var(--ui-accent) 8%,transparent);color:var(--ui-text-primary);
  border-radius:999px;padding:7px 14px;cursor:pointer;font:inherit;font-size:12.5px}
.ls-search i{width:7px;height:7px;border-radius:50%;background:var(--ui-text-tertiary);display:block}
.ls-search.on i{background:var(--ui-accent);animation:ls-pulse 1.8s infinite}
@keyframes ls-pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ui-accent) 45%,transparent)}
  70%{box-shadow:0 0 0 9px transparent}100%{box-shadow:0 0 0 0 transparent}}
.ls-stage{position:relative;flex:1;display:grid;place-items:center;min-height:340px;z-index:1}
.ls-radar{width:min(100%,520px);aspect-ratio:1;display:block}
.ls-ring{fill:none;stroke:var(--ui-stroke-secondary);stroke-width:1}
.ls-ring.dash{stroke-dasharray:2 6;opacity:.7}
.ls-cross{stroke:var(--ui-stroke-secondary);stroke-width:1;opacity:.45}
.ls-sweep{transform-origin:50% 50%;animation:ls-spin 4.2s linear infinite}
.ls-sweep.paused{animation-play-state:paused}
@keyframes ls-spin{to{transform:rotate(360deg)}}
.ls-blip{cursor:pointer}
.ls-blip:hover .ls-core{filter:brightness(1.4)}
.ls-label{fill:var(--ui-text-tertiary);font-size:10.5px}
.ls-self{fill:var(--ui-text-primary);font-size:11px;font-weight:600}
.ls-hint{position:absolute;bottom:8px;left:0;right:0;text-align:center;color:var(--ui-text-tertiary);font-size:11.5px}
.ls-side{border-left:1px solid var(--ui-stroke-secondary);display:flex;flex-direction:column;min-height:0}
.ls-side header{padding:14px 16px 10px;border-bottom:1px solid var(--ui-stroke-secondary)}
.ls-side header b{font-size:12.5px;color:var(--ui-text-primary)}
.ls-side header div{color:var(--ui-text-tertiary);font-size:11.5px;margin-top:2px}
.ls-scroll{overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.ls-peer{border:1px solid var(--ui-stroke-secondary);border-radius:12px;padding:11px;
  display:flex;flex-direction:column;gap:9px;background:color-mix(in srgb,var(--ui-accent) 3%,transparent)}
.ls-peer.sel{border-color:color-mix(in srgb,var(--ui-accent) 55%,transparent);
  box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-accent) 18%,transparent)}
.ls-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ls-nm{font-weight:600;font-size:12.5px;color:var(--ui-text-primary)}
.ls-meta{color:var(--ui-text-tertiary);font-size:11px}
.ls-pill{font-size:10.5px;padding:2px 8px;border-radius:999px;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-tertiary)}
.ls-pill.ok{color:var(--ui-green,#4ade80);border-color:color-mix(in srgb,var(--ui-green,#4ade80) 40%,transparent)}
.ls-pill.warn{color:var(--ui-orange,#fbbf24);border-color:color-mix(in srgb,var(--ui-orange,#fbbf24) 40%,transparent)}
.ls-actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.ls-act{border:1px solid var(--ui-stroke-secondary);background:transparent;color:var(--ui-text-primary);
  border-radius:8px;padding:4px 9px;font:inherit;font-size:11.5px;cursor:pointer}
.ls-act:hover{background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
.ls-act.primary{border-color:color-mix(in srgb,var(--ui-accent) 55%,transparent);
  background:color-mix(in srgb,var(--ui-accent) 16%,transparent);font-weight:600}
.ls-act:disabled{opacity:.45;cursor:default}
.ls-input{width:100%;background:transparent;border:1px solid var(--ui-stroke-secondary);border-radius:8px;
  color:var(--ui-text-primary);padding:5px 8px;font:inherit;font-size:12px;letter-spacing:.12em}
.ls-handoff{border:1px dashed color-mix(in srgb,var(--ui-accent) 45%,transparent);border-radius:10px;
  padding:9px;font-size:11.5px;color:var(--ui-text-secondary);
  background:color-mix(in srgb,var(--ui-accent) 7%,transparent)}
.ls-handoff b{display:block;font-size:20px;letter-spacing:.22em;color:var(--ui-text-primary);margin-top:3px}
.ls-log{margin:0;white-space:pre-wrap;color:var(--ui-text-tertiary);font-size:10.5px;max-height:150px;overflow:auto}
`

function makeApi(rest) {
  const call = async (path, body) => {
    const answer = await rest(path, body === undefined ? {} : { method: 'POST', body })
    if (answer && answer.ok === false) throw new Error(answer.error || 'ошибка')
    return answer && Object.prototype.hasOwnProperty.call(answer, 'result') ? answer.result : answer
  }
  return {
    status: () => call('/status'),
    peers: () => call('/peers'),
    search: (enabled = true) => call('/search', { enabled }),
    pair: (peer) => call('/pair', { peer }),
    accept: (peer, code) => call('/accept', { peer, password: code, code }),
    deny: (pairId) => call('/deny', { pair_id: pairId }),
    plan: (peer) => call('/plan', { peer }),
    sync: (peer) => call('/sync', { peer }),
    sessions: (peer) => call('/sessions', { peer }),
    profiles: (peer) => call('/peer-profiles', { peer }),
    importProfile: (peer, profile) => call('/import-profile', { peer, profile, overwrite: false })
  }
}

function angleFor(name) {
  let hash = 0
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) % 360
  return hash
}

function Radar({ peers, search, onSelect }) {
  const C = 260
  const R = 200
  const rings = [0.42, 0.68, 1].map((k, index) =>
    jsx('circle', { cx: C, cy: C, r: R * k, className: 'ls-ring' + (index === 0 ? ' dash' : '') }, `ring-${k}`)
  )
  const wedge = `M ${C} ${C} L ${C + R} ${C} A ${R} ${R} 0 0 1 ${C + R * Math.cos(-0.9)} ${C + R * Math.sin(-0.9)} Z`
  const blips = peers.map((peer) => {
    const angle = (angleFor(peer.name) * Math.PI) / 180
    const radius = peer.paired ? 92 : 145
    const x = C + radius * Math.cos(angle)
    const y = C + radius * Math.sin(angle)
    const color = peer.paired ? 'var(--ui-green,#4ade80)' : 'var(--ui-orange,#fbbf24)'
    const label = peer.name.length > 17 ? peer.name.slice(0, 16) + '…' : peer.name
    return jsxs(
      'g',
      {
        className: 'ls-blip',
        onClick: () => {
          haptic('tap')
          onSelect(peer.name)
        },
        children: [
          jsx('circle', { cx: x, cy: y, r: 12, fill: color, opacity: 0.1 }),
          jsx('circle', { cx: x, cy: y, r: 4.5, fill: color, className: 'ls-core' }),
          jsx('text', {
            x: x + (x > C ? 12 : -12),
            y: y + 3,
            textAnchor: x > C ? 'start' : 'end',
            className: 'ls-label',
            children: label
          })
        ]
      },
      peer.id || peer.name
    )
  })

  return jsx('svg', {
    viewBox: '0 0 520 520',
    className: 'ls-radar',
    children: jsxs('g', {
      children: [
        ...rings,
        jsx('line', { x1: C - R, y1: C, x2: C + R, y2: C, className: 'ls-cross' }),
        jsx('line', { x1: C, y1: C - R, x2: C, y2: C + R, className: 'ls-cross' }),
        jsx('g', {
          className: 'ls-sweep' + (search ? '' : ' paused'),
          children: jsx('path', { d: wedge, fill: 'var(--ui-accent)', opacity: 0.16 })
        }),
        jsx('circle', { cx: C, cy: C, r: 24, fill: 'var(--ui-accent)', opacity: 0.1 }),
        jsx('circle', { cx: C, cy: C, r: 5, fill: 'var(--ui-accent)' }),
        jsx('text', { x: C, y: C + 42, textAnchor: 'middle', className: 'ls-self', children: 'этот компьютер' }),
        ...blips
      ]
    })
  })
}

function View({ api }) {
  const [status, setStatus] = useState(null)
  const [table, setTable] = useState({ peers: [], pending: [] })
  const [selected, setSelected] = useState(null)
  const [handoff, setHandoff] = useState(null)
  const [profiles, setProfiles] = useState(null)
  const [draft, setDraft] = useState({})
  const [log, setLog] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [st, peers] = await Promise.all([api.status(), api.peers()])
      setStatus(st)
      setTable(peers)
    } catch (err) {
      setLog(String(err && err.message ? err.message : err))
    }
  }, [api])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 4000)
    return () => clearInterval(timer)
  }, [refresh])

  const act = async (fn, label) => {
    setBusy(true)
    try {
      const result = await fn()
      setLog(label + '\n' + JSON.stringify(result, null, 2))
      haptic('tap')
    } catch (err) {
      setLog(label + '\n' + String(err && err.message ? err.message : err))
    } finally {
      setBusy(false)
      refresh()
    }
  }

  const peers = table.peers || []
  const pending = table.pending || []
  const pairedCount = peers.filter((peer) => peer.paired).length
  const searchOn = !!(status && status.search)

  return jsxs('div', {
    className: 'ls-root',
    children: [
      jsx('style', { children: CSS }),
      jsxs('section', {
        className: 'ls-scope',
        children: [
          jsxs('div', {
            className: 'ls-head',
            children: [
              jsxs('div', {
                className: 'ls-title',
                children: [
                  jsx('h1', { children: 'Синхрофазатрон' }),
                  jsx('span', {
                    children: status
                      ? `${status.me.name} · профиль ${status.me.profile}`
                      : 'подключение к демону…'
                  })
                ]
              }),
              jsxs('button', {
                className: 'ls-search' + (searchOn ? ' on' : ''),
                type: 'button',
                onClick: () => act(() => api.search(!searchOn), searchOn ? 'поиск выключен' : 'поиск включён'),
                children: [jsx('i', {}), jsx('span', { children: searchOn ? 'поиск идёт' : 'поиск выкл' })]
              })
            ]
          }),
          jsxs('div', {
            className: 'ls-stage',
            children: [
              jsx(Radar, { peers, search: searchOn, onSelect: setSelected }),
              jsx('div', {
                className: 'ls-hint',
                children: peers.length
                  ? 'клик по метке — действия с машиной'
                  : 'никого не видно — включи поиск на обеих машинах'
              })
            ]
          })
        ]
      }),
      jsxs('aside', {
        className: 'ls-side',
        children: [
          jsxs('header', {
            children: [
              jsx('b', { children: pending.length ? `Запросы (${pending.length})` : 'На радаре' }),
              jsx('div', { children: `${peers.length} устройств · ${pairedCount} спарено` })
            ]
          }),
          jsxs('div', {
            className: 'ls-scroll',
            children: [
              pending.map((item) =>
                jsxs(
                  'div',
                  {
                    className: 'ls-peer',
                    children: [
                      jsxs('div', {
                        className: 'ls-row',
                        children: [
                          jsxs('div', {
                            children: [
                              jsx('div', { className: 'ls-nm', children: item.name }),
                              jsx('div', { className: 'ls-meta', children: `${item.profile} · просит подключиться` })
                            ]
                          }),
                          jsx('span', { className: 'ls-pill warn', children: item.kind === 'psk' ? 'код' : 'запрос' })
                        ]
                      }),
                      jsxs('div', {
                        className: 'ls-actions',
                        children: [
                          jsx('input', {
                            className: 'ls-input',
                            placeholder: 'код с другой машины',
                            value: draft[item.pair_id] || '',
                            onInput: (event) => setDraft({ ...draft, [item.pair_id]: event.target.value })
                          }),
                          jsx('button', {
                            className: 'ls-act primary',
                            disabled: busy,
                            onClick: () => act(() => api.accept(item.name, draft[item.pair_id] || item.code), `подтверждение ${item.name}`),
                            children: 'Подтвердить'
                          }),
                          jsx('button', {
                            className: 'ls-act',
                            disabled: busy,
                            onClick: () => act(() => api.deny(item.pair_id), 'отклонено'),
                            children: 'Отклонить'
                          })
                        ]
                      })
                    ]
                  },
                  item.pair_id
                )
              ),
              peers.map((peer) =>
                jsxs(
                  'div',
                  {
                    className: 'ls-peer' + (selected === peer.name ? ' sel' : ''),
                    children: [
                      jsxs('div', {
                        className: 'ls-row',
                        children: [
                          jsxs('div', {
                            children: [
                              jsx('div', { className: 'ls-nm', children: peer.name }),
                              jsx('div', { className: 'ls-meta', children: `${peer.profile} · ${peer.addr}` })
                            ]
                          }),
                          jsx('span', {
                            className: 'ls-pill ' + (peer.paired ? 'ok' : 'warn'),
                            children: peer.paired ? 'спарен' : 'найден'
                          })
                        ]
                      }),
                      peer.paired
                        ? jsxs('div', {
                            className: 'ls-actions',
                            children: [
                              jsx('button', { className: 'ls-act', disabled: busy, onClick: () => act(() => api.plan(peer.name), 'план'), children: 'План' }),
                              jsx('button', { className: 'ls-act primary', disabled: busy, onClick: () => act(() => api.sync(peer.name), 'синхронизация'), children: 'Синхронизировать' }),
                              jsx('button', { className: 'ls-act', disabled: busy, onClick: () => act(() => api.sessions(peer.name), 'диалоги'), children: 'Диалоги' }),
                              jsx('button', {
                                className: 'ls-act',
                                disabled: busy,
                                onClick: async () => {
                                  try {
                                    const answer = await api.profiles(peer.name)
                                    setProfiles({ peer: peer.name, items: answer.profiles || [] })
                                  } catch (err) {
                                    setLog(String(err && err.message ? err.message : err))
                                  }
                                },
                                children: 'Профили'
                              })
                            ]
                          })
                        : jsx('button', {
                            className: 'ls-act primary',
                            disabled: busy,
                            onClick: async () => {
                              try {
                                const result = await api.pair(peer.name)
                                setHandoff({ peer: peer.name, code: result.code })
                                setLog('код для подключения\n' + JSON.stringify(result, null, 2))
                              } catch (err) {
                                setLog(String(err && err.message ? err.message : err))
                              }
                            },
                            children:
                              handoff && handoff.peer === peer.name
                                ? `код ${handoff.code} — введи на другой машине`
                                : 'Подключить'
                          })
                    ]
                  },
                  peer.id || peer.name
                )
              ),
              profiles
                ? jsxs('div', {
                    className: 'ls-peer',
                    children: [
                      jsx('div', { className: 'ls-meta', children: `профили ${profiles.peer}` }),
                      ...profiles.items.map((item) =>
                        jsxs(
                          'div',
                          {
                            className: 'ls-row',
                            children: [
                              jsxs('div', {
                                children: [
                                  jsx('div', { className: 'ls-nm', children: item.name }),
                                  jsx('div', {
                                    className: 'ls-meta',
                                    children: `скиллов ${item.skills} · память ${Math.round((item.memory_bytes || 0) / 1024)} КБ`
                                  })
                                ]
                              }),
                              jsx('button', {
                                className: 'ls-act primary',
                                disabled: busy,
                                onClick: () => act(() => api.importProfile(profiles.peer, item.name), `импорт ${item.name}`),
                                children: 'Импортировать'
                              })
                            ]
                          },
                          item.name
                        )
                      )
                    ]
                  })
                : null,
              log ? jsx('pre', { className: 'ls-log', children: log }) : null
            ]
          })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'local-sync',
  register(ctx) {
    const api = makeApi((path, options) => ctx.rest(path, options))
    ctx.register({
      id: 'page',
      area: 'routes',
      title: 'Синхрофазатрон',
      data: { path: '/local-sync' },
      render: () => jsx(View, { api })
    })
    ctx.register({
      id: 'nav',
      area: 'sidebar.nav',
      data: { path: '/local-sync', label: 'Синхрофазатрон', codicon: 'radio-tower' }
    })
  }
}
