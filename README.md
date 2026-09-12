# LibreOffice Cowork

An AI agent that works on the document you have open in LibreOffice — and **looks at
the result to check its own work**.

There are plenty of "AI in LibreOffice" extensions that send your selected text to a
model and paste back a paragraph. This is not that. This is an agent that reads the
real document structure, edits it in place, renders it to an image, inspects the
rendering for layout defects, fixes them, and leaves the whole thing undoable with a
single Ctrl-Z.

```
┌─ LibreOffice (your process, the document you're looking at) ───────────┐
│   Cowork extension                                                     │
│    ├─ starts a local UNO bridge when LibreOffice starts                │
│    └─ a native sidebar panel                                           │
└───────────────┬────────────────────────────────────┬───────────────────┘
                │ UNO, over a local pipe             │ sidebar ⇄ agent
                ▼                                    ▼
     ┌────────────────────┐            ┌──────────────────────────────┐
     │ cowork-uno helper  │            │ DeepSeek Harness             │
     │  read · find       │◄───────────│  profile: libreoffice        │
     │  edit · format     │  spawns    │   document tools (native)    │
     │  render → PNG      │            │   office persona             │
     │  layout checks     │            │   document skills            │
     │  one undo / turn   │            │   approvals · model routing  │
     └────────────────────┘            └──────────────────────────────┘
```

No MCP. The document tools are ordinary rows on the harness's own tool registry, and
the sidebar talks to the runtime over its SDK protocol. That means approvals,
permission presets, skills, and model routing all apply to document editing directly.

---

## Why this exists

Every agentic Office product shares one weakness: **the agent cannot see its own
output.** It manipulates markup and hopes. Claude for Excel had to bolt on tracked
cells because reviewers kept finding silent mistakes; the dominant criticism of Cowork
is exactly this — errors that "fail silently" because nothing verifies the result.

LibreOffice can do better, because it can render its own document in-process and hand
the agent a picture. So that is what this does.

Asked to review a page containing a few deliberate flaws, the vision model returned:

> **Text overflowing the right margin** — runs to ~x≈798px, past the body text's right
> margin at ~x≈740px. It overruns by roughly 55–60px and sits flush against the paper
> edge.
>
> **Poor contrast** — "Low contrast text here." is rendered in a very light gray on
> white, far below readable contrast.
>
> **Heading/body spacing collapse** — "Section One" sits directly on top of "Body text
> with normal contrast" with no leading above or below.

That is the capability this project is built around, and no other LibreOffice AI
project has it.

## Three things that make it different

**1. It edits the document on your screen, not a copy on disk.**
The extension opens a UNO bridge from *inside* your running LibreOffice, so the agent
attaches to the window you are looking at. No `--accept` flag to configure, no launcher
script, no second headless instance, no "which copy did it edit". You watch the change
happen.

**2. One agent turn is one Ctrl-Z.**
Every edit in a turn is grouped into a single undo step in LibreOffice's own undo
stack. Because edits are real document actions, the standard reviewer experience just
works — and Writer tracked changes (`document_track_changes`) put the agent's work in
front of you as accept/reject revisions.

**3. It checks its own work.**
`document_check_layout` runs deterministic checks over UNO — WCAG contrast ratios,
unbreakable tokens that will overflow the text area, spacing done with blank lines —
with no rendering and no model call. `document_render` produces page images for the
vision model to inspect. The office persona requires both before the agent says it is
finished.

---

## Requirements

| | |
|---|---|
| **LibreOffice** | 7.6 or newer (tested on 26.2.5) |
| **Python with UNO** | `python3-uno` (Debian/Ubuntu), `libreoffice-pyuno` (Fedora), or LibreOffice's bundled interpreter |
| **DeepSeek Harness** | `npm install -g @deepseek-ai/dsh` |
| **Helper tools** | `zip` (to build the extension), `pdftoppm` from poppler-utils (for rendering) |

The agent needs a model route. That is configured in your harness `settings.yaml`
like any other DSH profile, and the profile reuses your existing configuration.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/brandon-nuttall/libreoffice-cowork/main/setup.sh | bash
```

The script checks each requirement, tells you the exact command if something is
missing, installs the extension and the harness profile, and then verifies that the
profile composes before declaring success. Re-running it upgrades.

Then:

1. Start (or restart) LibreOffice and open a document.
2. Open the sidebar: **View ▸ Sidebar ▸ Cowork**.

The bridge starts with LibreOffice. There is nothing else to launch.

```sh
setup.sh --uninstall     # remove both halves
```

## Status

**It works end to end, driven by a script.** An agent has read a live document, loaded
a skill, edited it in the document's own style, verified its own work, and produced a
single-undo turn:

```
document_list      → writer: e2e_doc.txt — LibreOffice Writer
document_read      → the live text
document_outline   → paragraphs with their styles
skill              → loaded "writer-edit" because the task matched
document_append    → added a "Next Steps" section, matching the document's style
document_check_layout → "No deterministic layout problems found."
turn/end           → completed
```

One `undo` restored the original document exactly.

**The sidebar is wired to the agent.** A `cowork-agent` service owns the harness runtime
and one conversation per document; the panel streams replies into the transcript and
shows what the agent is doing while it works.

Turns run on a worker thread and are marshalled onto the GUI thread with
`com.sun.star.awt.AsyncCallback`, so the panel keeps drawing while the agent works.
There is also a **Cowork ▸ Show Cowork Panel** menu entry, so the panel is reachable
without hunting for the sidebar deck.

**Not finished:** Impress support, tables and images in Writer, cancellation of a
running turn, and the write-safety classifier. The model also cannot yet ask you a
structured question mid-turn — see below.

## Known limitations

Real ones, worth knowing before you rely on it:

- **Rendering needs a saved document.** `document_render` exports the *live* document so
  unsaved edits appear in the render — that is deliberate, and it was a bug when it
  exported the file on disk instead. But a document that has never been saved has no
  baseline to export from, and the tool says so rather than rendering something stale.
- **Writer and Calc only.** Impress tools are described but not implemented.
- **One turn at a time.** The service refuses a concurrent turn, because a single
  conversation thread with interleaved replies is unreadable.
- **The agent cannot ask you a structured question.** `ask_user_question` is a tool over
  `ctx.userQuestions`, whose only production answerer is registered by the Web client and
  delivered over its Remote WebSocket. The SDK transport the panel speaks has no
  request/response channel, so there is nothing to answer it with. The same seam governs
  approval prompts. The threading fix above is the precondition for closing this, and it
  is now done.

## What is here

| Path | What it is |
|---|---|
| `ext/oxt-proto/` | The LibreOffice extension: bridge job, sidebar panel, registrations |
| `dsh/libreoffice/` | The harness profile — document tools, persona, and skills |
| `dsh/libreoffice/cowork/uno_bridge.py` | The document bridge (UNO ⇄ JSON over stdio) |
| `dsh/libreoffice/cowork/cowork_agent.py` | The service the panel talks to; owns the runtime and one conversation per document |
| `ext/oxt-proto/components/cowork_client.py` | The panel's client for that service (plain Python, testable without a GUI) |
| `dsh/libreoffice/cowork/cowork-office.mjs` | The document tools, as native harness rows |
| `setup.sh` | Installer |
| `PLAN.md` | Architecture, archived decisions, and the backlog |
| `research/FINDINGS.md` | Every experiment behind the design, including the bugs |

## Development

The installer works from a local checkout:

```sh
git clone https://github.com/brandon-nuttall/libreoffice-cowork
cd libreoffice-cowork && ./setup.sh
```

To exercise the bridge without the agent, start LibreOffice with `--accept` on a socket
and drive `uno_bridge.py` directly:

```sh
soffice --accept="socket,host=127.0.0.1,port=2002;urp;" &
COWORK_ACCEPT="socket,host=127.0.0.1,port=2002" python3 dsh/libreoffice/cowork/uno_bridge.py
# then write {"id":1,"op":"ping"} and read one JSON line back
```

`research/FINDINGS.md` is the useful document if you are changing the bridge: it records
why each non-obvious decision is the way it is, and which wrong turns were already
taken — including that LibreOffice does *not* merge separate `enterUndoContext` blocks,
that `insertString` with `"\n"` does not create paragraphs, and that `setDataArray`
creates an undo entry which does not revert the cells.

## Licence and attribution

MIT. See `LICENSE`.

The UNO pipe-acceptor pattern (opening a local bridge from inside the office process via
a job bound to `OnStartApp`) and the sidebar factory wiring were learned from
[`swe-sanad/LibreOffice-Claude-Connector`](https://github.com/swe-sanad/LibreOffice-Claude-Connector)
(MIT) and [`dandi-91/LibreOffice-Bielik-Agent`](https://github.com/dandi-91/LibreOffice-Bielik-Agent)
(MIT). The tool-surface design borrows from Anthropic's Claude-for-Excel conventions.

One detail worth recording, since it is the kind of thing that costs a day:
`Jobs.xcu` must bind to **`OnStartApp`** — capital O — and its `JobList` entry takes no
`TimeStamp` wrapper. With `onStartApp`, or with the wrapper, the job registers, appears
in the live configuration, and then silently never dispatches.

Not affiliated with LibreOffice, The Document Foundation, or Anthropic.
