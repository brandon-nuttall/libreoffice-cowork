// Cowork document tools — native Cordis rows on `ctx.tools`.
//
// This is the DSH half of LibreOffice Cowork. It spawns `cowork-uno`, a small
// Python helper that attaches to the LibreOffice the user already has open, and
// republishes that helper's operations as ordinary model-facing tools.
//
// Why a subprocess and a helper rather than an in-process bridge: LibreOffice
// speaks UNO, and its client bindings are Python (and C++/Java). Reimplementing
// the URP bridge in Node would be a large, fragile surface for no gain. The
// helper is stdlib-plus-uno only, and the interface between the two halves is
// newline-delimited JSON — small enough to be obviously correct.
//
// Deliberately NOT MCP: these are rows on the harness's own tool registry, so
// approvals, permission presets, plan mode, and tool presentation apply to them
// natively instead of across a protocol boundary.
//
// Undo discipline lives in the helper (see its TurnRegistry): every mutation
// carrying the same `turn_id` lands in one undo window, so a whole agent turn is
// a single Ctrl-Z in the user's document.

import { defineTool } from '@deepseek-ai/dsh-tools'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'cowork-office'

/**
 * `tools` is a hard dependency — without the registry there is nothing to
 * contribute. `subprocess` is not used: the helper is a plain child process we
 * own for the lifetime of this fiber, which keeps its undo-window state alive
 * across every call in a turn.
 */
export const inject = ['tools']

/**
 * Resolve the helper script and interpreter.
 *
 * The helper is installed beside this module, but the module can also be
 * referenced from the source tree during development, so a few candidates are
 * tried in order and the first that exists wins. `COWORK_HELPER` overrides all
 * of them for an unusual deployment.
 *
 * `COWORK_PYTHON` lets a deployment point at LibreOffice's own bundled
 * interpreter, which always has `uno`; otherwise system python3 is used and
 * `python3-uno` must be installed.
 */
function helperCandidates() {
  const here = dirname(fileURLToPath(import.meta.url))
  if (process.env.COWORK_HELPER) return [process.env.COWORK_HELPER]
  return [
    join(here, 'uno_bridge.py'),                       // installed beside us
    join(here, '..', 'cowork', 'uno_bridge.py'),       // run from the repo root
    join(here, '..', '..', 'cowork', 'uno_bridge.py'), // nested one level deeper
  ]
}

function helperCommand() {
  const candidates = helperCandidates()
  const script = candidates.find((candidate) => existsSync(candidate)) ?? candidates[0]
  const python = process.env.COWORK_PYTHON || 'python3'
  return { python, script }
}

/**
 * One long-lived helper process with request correlation.
 *
 * A fresh process per call would lose the helper's turn registry, and with it
 * the single-undo-per-turn guarantee. It is therefore started lazily on first
 * use and owned by this fiber, so stopping the plugin stops the child.
 */
class DocumentBridge {
  constructor(ctx) {
    this.ctx = ctx
    this.child = null
    this.pending = new Map()
    this.nextId = 1
    this.buffer = ''
    this.failures = []
  }

  _log(message) {
    this.ctx.logger?.info?.(`cowork-office: ${message}`)
  }

  _ensure() {
    if (this.child !== null && this.child.exitCode === null) return this.child
    const { python, script } = helperCommand()
    const child = spawn(python, [script], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: process.env,
    })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this._onData(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      const text = String(chunk).trim()
      if (text) this._log(text)
    })
    child.on('exit', (code) => {
      // Fail every in-flight request rather than leaving them pending forever.
      for (const [, slot] of this.pending) {
        slot.reject(new Error(
          `the document helper exited (code ${code}). ` +
          `Is LibreOffice running with the Cowork extension installed?`))
      }
      this.pending.clear()
      this.child = null
    })
    this.child = child
    return child
  }

  _onData(chunk) {
    this.buffer += chunk
    let index
    while ((index = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, index).trim()
      this.buffer = this.buffer.slice(index + 1)
      if (!line) continue
      let message
      try {
        message = JSON.parse(line)
      } catch {
        continue
      }
      const slot = this.pending.get(message.id)
      if (slot === undefined) continue
      this.pending.delete(message.id)
      if (message.ok === false) {
        slot.reject(new Error(message.error || 'the document operation failed'))
      } else {
        slot.resolve(message)
      }
    }
  }

  /**
   * Invoke one helper operation. Resolves to the helper's JSON payload.
   * Rejects with a message written for the model, not a stack trace.
   */
  call(op, args = {}, signal) {
    const child = this._ensure()
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        this.pending.delete(id)
        reject(new Error(`the "${op}" call was cancelled`))
      }
      if (signal?.aborted) return onAbort()
      signal?.addEventListener?.('abort', onAbort, { once: true })
      this.pending.set(id, {
        resolve: (value) => {
          signal?.removeEventListener?.('abort', onAbort)
          resolve(value)
        },
        reject: (error) => {
          signal?.removeEventListener?.('abort', onAbort)
          reject(error)
        },
      })
      child.stdin.write(JSON.stringify({ id, op, args }) + '\n')
    })
  }

  /** Stop the helper. Called by this fiber's disposer. */
  dispose() {
    if (this.child !== null) {
      try {
        this.child.stdin.end()
      } catch {
        /* already gone */
      }
      this.child.kill()
      this.child = null
    }
    this.pending.clear()
  }
}

const TARGET_NOTE =
  'Omit title/url when exactly one document is open; when several are open the ' +
  'call fails and names them rather than guessing.'

/** Standard arguments shared by every document tool. */
function targetParams() {
  return {
    title: {
      type: 'string',
      description: `Substring of the target document's title or path. ${TARGET_NOTE}`,
    },
    url: {
      type: 'string',
      description: 'Exact file URL of the target document.',
    },
  }
}

/**
 * Output schema for every cowork tool.
 *
 * Written in the harness's own schema DSL, where a property carries
 * `required: true` on itself. Raw JSON Schema's top-level `required: [...]`
 * array is rejected at registration with "schema.required must be an array of
 * strings", which fails the entire plugin mount rather than one tool.
 *
 * `additionalProperties: true` because the helper returns the document's own
 * data and we do not want its shape re-validated on every call.
 */
function looseOutput(render) {
  return {
    schema: { type: 'object', additionalProperties: true },
    render,
  }
}

function text(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]
}

/**
 * Enter the undo window that a mutation belongs to, then run the mutation.
 *
 * Turn identity is the agent session plus a monotonic counter, so two agents
 * editing two documents never merge their undo windows. The first mutation of a
 * turn opens the window; `document_end_turn` closes it.
 */
function makeTurnId(exec) {
  const session = exec?.agent?.session
  const key = session?.id ?? 'anonymous'
  return `${key}#${Date.now().toString(36)}`
}

export function apply(ctx, config = {}) {
  const bridge = new DocumentBridge(ctx)
  ctx.effect(() => () => bridge.dispose(), 'cowork-office.helper')

  // One turn id per (session, turn). Reusing the id across every call in a turn
  // is what makes the helper group them into a single undo action.
  const turnIds = new Map()

  function turnFor(exec, explicit) {
    if (typeof explicit === 'string' && explicit.length > 0) return explicit
    const key = exec?.agent?.session?.id ?? 'anonymous'
    let entry = turnIds.get(key)
    if (entry === undefined) {
      entry = { id: makeTurnId(exec), mutations: 0 }
      turnIds.set(key, entry)
    }
    return entry.id
  }

  function openTurn(exec, args) {
    const key = exec?.agent?.session?.id ?? 'anonymous'
    const entry = turnIds.get(key)
    if (entry !== undefined && entry.mutations > 0) {
      entry.mutations += 1
      return entry.id
    }
    const id = turnFor(exec, args.turn_id)
    const record = entry ?? { id, mutations: 0 }
    record.id = id
    record.mutations = (record.mutations ?? 0) + 1
    turnIds.set(key, record)
    return id
  }

  // `defineTool` is what turns the harness schema DSL (`required: true` on a
  // property) into the real JSON Schema the model provider receives, and it
  // installs argument validation as well. Registering a hand-written definition
  // skips both: the DSL leaks to the provider verbatim and every call is
  // rejected with "Invalid schema for function ..." (observed).
  const register = (options) => {
    const definition = defineTool(options)
    const dispose = ctx.tools.register(definition)
    ctx.effect(() => dispose, `cowork-office.${definition.name}`)
  }

  // ── orientation ──────────────────────────────────────────────────────────

  register({
    name: 'document_list',
    description:
      'List the documents currently open in LibreOffice, including which one is ' +
      'active. Call this first when you do not know what the user is looking at.',
    parameters: {},
    output: looseOutput((_args, value) => text(
      value.documents.length === 0
        ? 'No documents are open in LibreOffice.'
        : value.documents.map((d) => `${d.kind}: ${d.title}`).join('\n'))),
    async execute(_args, exec) {
      return bridge.call('list_documents', {}, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'List open documents', kind: 'read' }),
  })

  register({
    name: 'document_read',
    description:
      'Read the live document the user has open. Returns the full text plus the ' +
      'current selection for Writer, or the sheet list for Calc. This is the ' +
      'document on screen, not a copy on disk.',
    parameters: { ...targetParams() },
    output: looseOutput((_args, value) => text(value.text ?? value)),
    async execute(args, exec) {
      return bridge.call('read_text', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Read document', kind: 'read' }),
  })

  register({
    name: 'document_outline',
    description:
      'Show the document structure: for Writer, every paragraph with its paragraph ' +
      'style and index; for Calc, every sheet with its dimensions. Prefer this over ' +
      'document_read when the document is long — it is how you find what to change.',
    parameters: { ...targetParams(), limit: { type: 'integer', description: 'Maximum paragraphs to return.' } },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      return bridge.call('outline', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Read outline', kind: 'read' }),
  })

  register({
    name: 'document_find',
    description: 'Find text in a Writer document. Returns the matching strings.',
    parameters: {
      ...targetParams(),
      query: { type: 'string', required: true, description: 'Text or pattern to find.' },
      regex: { type: 'boolean', description: 'Treat query as a regular expression.' },
      limit: { type: 'integer', description: 'Maximum matches to return.' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      return bridge.call('find', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Find in document', kind: 'read' }),
  })

  // ── Writer edits ────────────────────────────────────────────────────────

  register({
    name: 'document_append',
    description:
      'Append text to the live Writer document. Newlines create real paragraphs. ' +
      'Edits are grouped so the whole turn can be undone with one Ctrl-Z. ' +
      'Set where="selection" to replace the current selection. ' +
      'STRUCTURE YOUR OUTPUT: headings need paragraph_style="Heading 1" (or "Heading 2" ' +
      'for subsections), body text needs "Text Body", and lists should be introduced ' +
      'by a heading. A document of all-Standard paragraphs with UPPERCASE pseudo- ' +
      'headings is wrong — use real styles so the outline and navigation work.',
    parameters: {
      ...targetParams(),
      text: { type: 'string', required: true, description: 'Text to insert.' },
      where: { type: 'string', enum: ['end', 'selection'], description: 'Insert at the end (default) or replace the selection.' },
      paragraph_style: { type: 'string', description: 'Paragraph style name, e.g. "Heading 1" or "Text Body".' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      const turn_id = openTurn(exec, args)
      return bridge.call('append', { ...args, turn_id }, exec.signal)
    },
    presentCall: (args) => ({ card: 'generic', title: 'Edit document', kind: 'write', rawInput: args.text }),
  })

  register({
    name: 'document_replace',
    description:
      'Find and replace text in the live Writer document. One replaceAll is a ' +
      'single undoable action, which is much safer than editing match by match.',
    parameters: {
      ...targetParams(),
      find: { type: 'string', required: true, description: 'Text to find.' },
      replace: { type: 'string', description: 'Replacement text; empty deletes.' },
      regex: { type: 'boolean', description: 'Treat find as a regular expression.' },
      all: { type: 'boolean', description: 'Replace every occurrence (default true).' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      const turn_id = openTurn(exec, args)
      return bridge.call('replace', { ...args, turn_id }, exec.signal)
    },
    presentCall: (args) => ({ card: 'generic', title: 'Replace in document', kind: 'write', rawInput: args }),
  })

  register({
    name: 'document_end_turn',
    description:
      'Close the current editing turn so everything changed in it becomes a single ' +
      'undo step in the user\'s document. Call this once when you have finished all ' +
      'edits for the user\'s request.',
    parameters: { ...targetParams() },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      const key = exec?.agent?.session?.id ?? 'anonymous'
      const entry = turnIds.get(key)
      const turn_id = entry?.id
      turnIds.delete(key)
      return bridge.call('end_turn', { ...args, turn_id }, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Finish editing turn', kind: 'other' }),
  })

  register({
    name: 'document_undo',
    description:
      'Undo the most recent edit in the live document — typically the whole previous ' +
      'turn, because a turn is grouped into one undo step.',
    parameters: {},
    output: looseOutput((_args, value) => text(value)),
    async execute(_args, exec) {
      return bridge.call('undo', {}, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Undo', kind: 'write' }),
  })

  // ── review surface ──────────────────────────────────────────────────────

  register({
    name: 'document_track_changes',
    description:
      'Turn Writer tracked changes on or off. With tracking on, your edits appear as ' +
      'revisions the user can accept or reject in the normal review UI, which is ' +
      'usually what they want for anything substantive.',
    parameters: {
      ...targetParams(),
      enabled: { type: 'boolean', required: true, description: 'True to record changes, false to stop.' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      return bridge.call('set_tracking', args, exec.signal)
    },
    presentCall: (args) => ({ card: 'generic', title: args.enabled ? 'Track changes on' : 'Track changes off', kind: 'write' }),
  })

  register({
    name: 'document_revisions',
    description: 'List the tracked changes currently recorded in the document.',
    parameters: { ...targetParams() },
    output: looseOutput((_args, value) => text(value.redlines.length === 0
      ? 'There are no tracked changes in this document.'
      : value.redlines.map((r) => `${r.type} by ${r.author}: ${r.text}`).join('\n'))),
    async execute(args, exec) {
      return bridge.call('redlines', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'List revisions', kind: 'read' }),
  })

  // ── checking your own work ──────────────────────────────────────────────

  register({
    name: 'document_check_layout',
    description:
      'Run deterministic layout checks on the live Writer document: low-contrast ' +
      'text, tokens too long to fit the text area, and spacing done with blank ' +
      'lines instead of paragraph styles. Cheap, exact, and does not need rendering. ' +
      'Call it after editing, before telling the user you are done.',
    parameters: { ...targetParams() },
    output: looseOutput((_args, value) => text(value.count === 0
      ? 'No deterministic layout problems found.'
      : value.issues.map((i) => `[${i.check}/${i.severity}] ${i.detail}`).join('\n'))),
    async execute(args, exec) {
      return bridge.call('layout_check', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Check layout', kind: 'read' }),
  })

  register({
    name: 'document_render',
    description:
      'Render the live document to PNG page images and return their paths, so you ' +
      'can actually look at the result. Use this to verify visual quality — text ' +
      'overflow, overlaps, contrast, orphaned headings — on anything where ' +
      'appearance matters. Read the returned image paths with the file reader. ' +
      'The document must have been saved at least once.',
    parameters: {
      ...targetParams(),
      outdir: { type: 'string', description: 'Directory for the rendered images.' },
      dpi: { type: 'integer', description: 'Raster resolution; 100 is a good default.' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      return bridge.call('render', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Render document', kind: 'read' }),
  })

  // ── Calc ────────────────────────────────────────────────────────────────

  register({
    name: 'sheet_read_range',
    description:
      'Read a spreadsheet range, returning each cell\'s formula AND its computed ' +
      'value in one call. That pairing is what makes questions like "why is this ' +
      'number wrong" answerable — the value alone hides the formula, and the ' +
      'formula alone hides the result.',
    parameters: {
      ...targetParams(),
      range: { type: 'string', description: 'A1 notation, e.g. "A1:D20".' },
      sheet: { type: 'string', description: 'Sheet name; defaults to the active sheet.' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      return bridge.call('read_range', args, exec.signal)
    },
    presentCall: () => ({ card: 'generic', title: 'Read range', kind: 'read' }),
  })

  register({
    name: 'sheet_write_range',
    description:
      'Write a block of values into a spreadsheet, starting at the given cell. ' +
      'Strings beginning with "=" are treated as formulas and are translated from ' +
      'Excel syntax automatically. Cells are written individually so undo works.',
    parameters: {
      ...targetParams(),
      range: { type: 'string', required: true, description: 'Top-left cell, e.g. "B4".' },
      values: {
        type: 'array',
        required: true,
        description: 'Rows of values; each inner array is one row.',
        // Nested arrays of unconstrained values: the harness schema DSL has no
        // "anything" node, so each level is declared as lossless JSON.
        items: { type: 'array', items: { type: 'json' } },
      },
      sheet: { type: 'string', description: 'Sheet name; defaults to the active sheet.' },
    },
    output: looseOutput((_args, value) => text(value)),
    async execute(args, exec) {
      const turn_id = openTurn(exec, args)
      return bridge.call('write_range', { ...args, turn_id }, exec.signal)
    },
    presentCall: (args) => ({ card: 'generic', title: 'Write range', kind: 'write', rawInput: args }),
  })
}

export const Config = undefined
