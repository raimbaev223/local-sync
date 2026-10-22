/**
 * local-sync — desktop half.
 *
 * A pane in the left sidebar: discovery switch, discovered peers, pairing with a
 * shared password (or a 6-digit code), dry-run plan and sync. All state lives in
 * the Python half (dashboard/plugin_api.py → the local-sync daemon); this file
 * only renders and calls ctx.rest.
 */

import { haptic } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'local-sync'

const rowStyle = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', flexWrap: 'wrap' }
const cardStyle = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: '8px', padding: '10px' }
const dimStyle = { color: 'var(--ui-text-tertiary)', fontSize: '0.72rem' }
const inputStyle = {
  width: '100%',
  background: 'transparent',
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '6px',
  padding: '4px 8px',
  color: 'var(--ui-text-primary)',
  font: 'inherit'
}
const btnStyle = (primary) => ({
  border: '1px solid ' + (primary ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'),
  background: primary ? 'var(--ui-accent)' : 'transparent',
  color: primary ? 'var(--ui-background)' : 'var(--ui-text-primary)',
  borderRadius: '6px',
  padding: '3px 9px',
  font: 'inherit',
  fontSize: '0.75rem',
  cursor: 'pointer'
})

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
    pair: (peer, password) => call('/pair', { peer, password }),
    accept: (peer, password, code) => call('/accept', { peer, password, code }),
    deny: (pairId) => call('/deny', { pair_id: pairId }),
    plan: (peer) => call('/plan', { peer }),
    sync: (peer) => call('/sync', { peer })
  }
}

function Section({ title, children }) {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: '8px' },
    children: [jsx('div', { style: { ...dimStyle, textTransform: 'uppercase', letterSpacing: '0.06em' }, children: title }), children]
  })
}

function Pane({ api }) {
  const [status, setStatus] = useState(null)
  const [peers, setPeers] = useState({ peers: [], pending: [] })
  const [error, setError] = useState('')
  const [out, setOut] = useState('')
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState({}) // peer name -> password being typed

  const refresh = useCallback(async () => {
    try {
      const [st, table] = await Promise.all([api.status(), api.peers()])
      setStatus(st)
      setPeers(table)
      setError('')
    } catch (err) {
      setError(err && err.message ? err.message : String(err))
    }
  }, [api])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 3000)
    return () => clearInterval(timer)
  }, [refresh])

  const run = async (fn, label) => {
    setBusy(true)
    try {
      const result = await fn()
      setOut(label + '\n' + JSON.stringify(result, null, 2))
      haptic('tap')
    } catch (err) {
      setOut(label + '\n' + (err && err.message ? err.message : String(err)))
    } finally {
      setBusy(false)
      refresh()
    }
  }

  const header = status
    ? `${status.me.name} · ${status.me.profile}`
    : error
      ? 'local-sync недоступен'
      : 'загрузка…'

  const peerRows = (peers.peers || []).map((peer) => {
    const paired = peer.paired
    return jsxs(
      'div',
      {
        style: { ...cardStyle, display: 'flex', flexDirection: 'column', gap: '6px' },
        children: [
          jsxs('div', {
            style: rowStyle,
            children: [
              jsx('div', {
                children: jsxs('div', {
                  children: [
                    jsx('span', { style: { fontWeight: 600 }, children: peer.name }),
                    jsx('span', { style: { ...dimStyle, marginLeft: '6px' }, children: `${peer.profile} · ${peer.addr}:${peer.api_port}` })
                  ]
                })
              }),
              jsx('span', {
                style: { ...dimStyle, color: paired ? 'var(--ui-green, #5fb87a)' : 'var(--ui-yellow, #d8a657)' },
                children: paired ? 'спарен' : 'найден'
              })
            ]
          }),
          paired
            ? jsxs('div', {
                style: rowStyle,
                children: [
                  jsx('button', {
                    style: btnStyle(false),
                    onClick: () => run(() => api.plan(peer.name), `план ${peer.name}`),
                    children: 'План'
                  }),
                  jsx('button', {
                    style: btnStyle(true),
                    disabled: busy,
                    onClick: () => run(() => api.sync(peer.name), `синхронизация ${peer.name}`),
                    children: 'Синхронизировать'
                  })
                ]
              })
            : jsxs('div', {
                style: { display: 'flex', gap: '6px' },
                children: [
                  jsx('input', {
                    type: 'password',
                    placeholder: 'пароль (можно цифры)',
                    style: inputStyle,
                    value: draft[peer.name] || '',
                    onInput: (event) => setDraft({ ...draft, [peer.name]: event.target.value })
                  }),
                  jsx('button', {
                    style: btnStyle(true),
                    disabled: busy,
                    onClick: () => run(() => api.pair(peer.name, draft[peer.name] || ''), `подключение ${peer.name}`),
                    children: 'Подключить'
                  })
                ]
              })
        ]
      }
    )
  })

  const pendingRows = (peers.pending || []).map((item) => {
    const psk = item.kind === 'psk'
    const key = String(item.pair_id || item.id)
    return jsxs(
      'div',
      {
        style: { ...cardStyle, display: 'flex', flexDirection: 'column', gap: '6px' },
        children: [
          jsxs('div', {
            style: rowStyle,
            children: [
              jsxs('span', { children: [jsx('b', { children: item.name }), jsx('span', { style: { ...dimStyle, marginLeft: '6px' }, children: item.profile })] }),
              psk ? null : jsx('span', { style: dimStyle, children: `код ${item.code}` })
            ]
          }),
          jsxs('div', {
            style: { display: 'flex', gap: '6px' },
            children: [
              psk
                ? jsx('input', {
                    type: 'password',
                    placeholder: 'тот же пароль',
                    style: inputStyle,
                    value: draft[key] || '',
                    onInput: (event) => setDraft({ ...draft, [key]: event.target.value })
                  })
                : null,
              jsx('button', {
                style: btnStyle(true),
                disabled: busy,
                onClick: () => run(() => api.accept(item.name, psk ? draft[key] || '' : '', item.code), `подтверждение ${item.name}`),
                children: psk ? 'Подтвердить паролем' : 'Подтвердить'
              }),
              jsx('button', {
                style: btnStyle(false),
                disabled: busy,
                onClick: () => run(() => api.deny(item.pair_id), 'отклонено'),
                children: 'Отклонить'
              })
            ]
          })
        ]
      }
    )
  })

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm',
    children: [
      jsxs('div', {
        style: rowStyle,
        children: [
          jsxs('div', {
            children: [
              jsx('div', { style: { fontWeight: 600 }, children: 'local-sync' }),
              jsx('div', { style: dimStyle, children: header })
            ]
          }),
          jsx('span', { style: dimStyle, children: status && status.search ? 'поиск: вкл' : 'поиск: выкл' })
        ]
      }),
      jsxs('label', {
        style: { ...rowStyle, justifyContent: 'flex-start', gap: '8px', cursor: 'pointer' },
        children: [
          jsx('input', {
            type: 'checkbox',
            checked: !!(status && status.search),
            onChange: (event) => run(() => api.search(event.target.checked), 'поиск устройств')
          }),
          jsx('span', { children: 'Поиск устройств' })
        ]
      }),
      error ? jsx('div', { style: { color: 'var(--ui-red, #e06c75)', fontSize: '0.75rem' }, children: error }) : null,
      jsx(Section, { title: 'Устройства', children: peerRows.length ? peerRows : jsx('div', { style: dimStyle, children: 'никого не видно — включи поиск на обеих машинах' }) }),
      jsx(Section, { title: 'Запросы', children: pendingRows.length ? pendingRows : jsx('div', { style: dimStyle, children: 'нет' }) }),
      out
        ? jsx('pre', {
            style: {
              margin: 0,
              padding: '8px',
              borderRadius: '6px',
              border: '1px solid var(--ui-stroke-secondary)',
              color: 'var(--ui-text-tertiary)',
              fontSize: '0.7rem',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: '220px',
              overflow: 'auto'
            },
            children: out
          })
        : null
    ]
  })
}

export default {
  id: ID,
  name: 'local-sync',
  register(ctx) {
    const api = makeApi((path, options) => ctx.rest(path, options))

    // A full page in the workspace, reached from a row in the sidebar nav —
    // the same place Artifacts and Kanban live. Registry areas are plain
    // strings: 'routes' mounts the page, 'sidebar.nav' adds the row.
    ctx.register({
      id: 'page',
      area: 'routes',
      title: 'local-sync',
      data: { path: '/local-sync' },
      render: () => jsx(Pane, { api })
    })
    ctx.register({
      id: 'nav',
      area: 'sidebar.nav',
      data: { path: '/local-sync', label: 'local-sync', codicon: 'radio-tower' }
    })
  }
}
