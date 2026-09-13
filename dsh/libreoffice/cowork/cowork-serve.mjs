// cowork-serve — the loopback endpoint the sidebar talks to.
//
// WHY THIS EXISTS RATHER THAN A SEPARATE AGENT SERVICE
// ----------------------------------------------------
// The first design had a standalone `cowork-agent` process: the panel spoke a
// bespoke JSON protocol to it, and it drove the runtime over SDK JSON-RPC on
// stdio. That meant three processes and two protocols for one conversation, a
// second thing to install, a systemd unit, and a service that could be running
// twice and collide on its port — which is exactly what happened.
//
// None of it was necessary. The runtime already owns the agent loop, the tools,
// the skills and the model route; what was missing was only a way for the panel
// to reach it. So this row IS the endpoint: it serves the conversation on
// loopback from inside the runtime process, where the tool calls actually happen.
//
// One process. One port. One tool surface.

import { randomUUID } from 'node:crypto'
import { appendFileSync } from 'node:fs'
import { createServer } from 'node:http'
// The runtime's own message constructor: `agent.followup()` takes a durable
// user message, not a string. Discovered by reading how the SDK server queues
// prompts rather than guessing at `session.prompt(...)`, which does not exist.
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { join } from 'node:path'

/**
 * A plain file log, because this row's failures are otherwise invisible. A
 * console logger goes to a stdout the launcher may own, and a silent early
 * return made an earlier version of this plugin look like it simply was not
 * mounted. Startup, listening and every request are recorded here.
 */
const LOG_PATH = process.env.COWORK_SERVE_LOG ||
  join(process.env.HOME || '/tmp', '.cache', 'cowork-serve.log')

function note(message) {
  try {
    appendFileSync(LOG_PATH, `${new Date().toISOString()} ${message}\n`)
  } catch {
    /* logging must never break the server */
  }
}

export const name = 'cowork-serve'

/** `agents` is the registry this serves a session from. */
export const inject = ['agents']

const DEFAULT_PORT = 8765
const DEFAULT_CWD = process.env.COWORK_CWD || process.env.HOME || '/'
const PROVIDER = process.env.COWORK_PROVIDER || 'litellm'
const MODEL = process.env.COWORK_MODEL || 'deepseek-api/v4.1-flash'

const TOOL_LABELS = {
  document_list: 'Looking at what you have open…',
  document_read: 'Reading the document…',
  document_outline: 'Reading the document structure…',
  document_find: 'Searching the document…',
  document_append: 'Adding to the document…',
  document_replace: 'Editing the document…',
  document_check_layout: 'Checking the layout…',
  document_render: 'Rendering the page to look at it…',
  read_image: 'Looking at the rendered page…',
  document_end_turn: 'Finishing the turn…',
  document_undo: 'Undoing that…',
  sheet_read_range: 'Reading the spreadsheet…',
  sheet_write_range: 'Writing to the spreadsheet…',
  skill: 'Checking how best to do this…',
  bash: 'Running a command…',
  read: 'Reading a file…',
}

export function apply(ctx, config = {}) {
  const port = Number(config.port ?? process.env.COWORK_PORT ?? DEFAULT_PORT)
  note(`apply() entered, port=${port}`)
  const agents = ctx.get('agents')
  if (agents === undefined) {
    // Loud on purpose. Returning quietly here is what made this plugin look
    // unmounted when it had in fact been applied.
    note('FATAL: no agents service in this scope; the sidebar cannot connect')
    throw new Error('cowork-serve: the agents service is not available')
  }
  note('agents service resolved')

  // One session per document, minted once per process. The runtime persists
  // session logs and refuses a reused id whose log does not match, so the id
  // must never repeat across runtime restarts.
  const sessions = new Map()
  let active = false

  const json = (res, status, body) => {
    const payload = JSON.stringify(body)
    res.writeHead(status, {
      'content-type': 'application/json',
      'content-length': Buffer.byteLength(payload),
    })
    res.end(payload)
  }

  async function sessionFor(document) {
    const key = document || '(untitled)'
    let record = sessions.get(key)
    if (record !== undefined) return record
    const handle = await agents.create({
      sessionId: `cowork-${randomUUID()}`,
      meta: { cwd: DEFAULT_CWD },
      agentOptions: { provider: PROVIDER, model: MODEL },
    })
    record = { agent: handle.agent, sessionId: handle.agent?.session?.id }
    sessions.set(key, record)
    note(`created session ${record.sessionId} for ${key}`)
    return record
  }

  // The runtime is driven by events, not by a return value: `prompt()` resolves
  // when the message is QUEUED. Completion is an `agent/status` transition back
  // to idle after having been running, and the answer arrives as `session/event`.
  // Subscribe once, at apply time, and route to whichever turn is active.
  let current = null   // { sessionId, onEvent, onDone }

  ctx.on('session/event', (session, event) => {
    if (current === null) return
    if (session?.id !== undefined && session.id !== current.sessionId) return
    current.onEvent(event)
  })

  ctx.on('agent/status', (payload) => {
    if (current === null) return
    const status = typeof payload === 'string' ? payload : payload?.status
    current.onStatus(status)
  })

  async function ask(req, res) {
    let body = ''
    for await (const chunk of req) body += chunk
    let request
    try {
      request = JSON.parse(body || '{}')
    } catch {
      return json(res, 400, { ok: false, error: 'bad request body' })
    }

    const prompt = String(request.text ?? '').trim()
    if (prompt === '') return json(res, 400, { ok: false, error: 'nothing to send' })
    if (active) {
      return json(res, 429, { ok: false, error: 'the previous request is still running' })
    }
    active = true

    res.writeHead(200, { 'content-type': 'application/x-ndjson' })
    const send = (payload) => res.write(`${JSON.stringify(payload)}\n`)

    let finished = false
    const finish = (payload) => {
      if (finished) return
      finished = true
      active = false
      current = null
      if (payload) send(payload)
      res.end()
    }

    let sawRunning = false
    let final = null
    let streamed = ''

    try {
      const record = await sessionFor(String(request.doc ?? ''))
      send({ ok: true, event: 'started' })
      current = {
        sessionId: record.sessionId,
        onStatus: (status) => {
          if (status === 'running') sawRunning = true
          else if (status === 'idle' && sawRunning) {
            finish({ ok: true, event: 'done', text: final ?? streamed })
          }
        },
        onEvent: (event) => {
          const type = event?.type
          if (type === 'assistant/chunk') {
            const chunk = event.data?.chunk ?? {}
            if (chunk.type === 'text-delta' && chunk.text) {
              streamed += chunk.text
              send({ ok: true, event: 'chunk', text: chunk.text })
            }
          } else if (type === 'tool/call') {
            const name = event.data?.name ?? ''
            send({ ok: true, event: 'tool', name, label: TOOL_LABELS[name] ?? 'Working…' })
          } else if (type === 'assistant/message') {
            const blocks = event.data?.message?.content ?? []
            const text = blocks.filter((b) => b.type === 'text').map((b) => b.text).join('')
            if (text) final = text
          } else if (type === 'turn/end') {
            const reason = event.data?.reason ?? {}
            if (reason.kind === 'error') {
              finish({ ok: false, error: reason.error?.message ?? 'the turn failed' })
            }
          }
        },
      }

      res.on('close', () => {
        if (!finished) {
          finished = true
          active = false
          current = null
        }
      })

      // `followup` queues the message; it returns before the answer exists.
      // Completion is observed through the status transition instead.
      record.agent.followup(createUserMessage({
        content: [{ type: 'text', text: prompt }],
        source: { kind: 'user' },
      }))
    } catch (error) {
      finish({ ok: false, error: String(error?.message ?? error) })
    }
  }

  const server = createServer((req, res) => {
    note(`${req.method} ${req.url}`)
    if (req.method === 'GET' && req.url === '/ping') {
      return json(res, 200, { ok: true, runtime: true, port })
    }
    if (req.method === 'POST' && req.url === '/ask') {
      return ask(req, res).catch((error) => {
        ctx.logger?.error?.(`cowork-serve: ${error?.stack ?? error}`)
        if (!res.headersSent) json(res, 500, { ok: false, error: String(error?.message ?? error) })
        else res.end()
      })
    }
    return json(res, 404, { ok: false, error: 'not found' })
  })

  // Loopback only: this endpoint can edit the user's documents.
  server.on('error', (error) => {
    note(`LISTEN FAILED on ${port}: ${error.code ?? error.message}. ` +
         'Another Cowork runtime is probably already running.')
  })
  server.listen(port, '127.0.0.1', () => note(`listening on 127.0.0.1:${port}`))

  ctx.effect(() => () => {
    server.close()
    ctx.logger?.info?.('cowork-serve: stopped')
  }, 'cowork-serve.listener')

  note('apply() complete')
}
