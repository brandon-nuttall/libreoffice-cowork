# LibreOffice Cowork — Plan & Backlog

**Goal:** an interactive, agentic "cowork" capability *inside* LibreOffice — a sidebar
where a user works with an agent that can see and edit the document they have open, with
every agent turn revertible in one Ctrl-Z — driven by the DeepSeek Harness (DSH).

**Status of this document:** the architecture is **built and working end to end**.
An agent has already read the user's live LibreOffice document, loaded a skill, edited
it in the document's own style, verified its own work, and produced a single-undo turn.
Evidence: `research/FINDINGS.md` (first-hand experiments),
`research/prior-art.md` (9 prior projects), `research/claude-office.md` (Claude for
Microsoft 365 / Cowork architecture).

**MCP was removed from this design.** The harness is reached directly over its SDK
JSON-RPC protocol and the document tools are native rows on the harness tool registry.
See decisions D7/D7a below and `research/FINDINGS.md` §"DIRECT DSH CONNECTIVITY".

---

## 1. Prior art — what already exists, and the gap

### 1.1 The landscape (from `research/gh_sweep.md` and `research/prior-art.md`)

Roughly 40 LibreOffice+AI projects exist. They fall into four groups:

| Group | Examples | What they are |
|---|---|---|
| **Sidebar chat assistants** | `localwriter` (178★), `librethinker` (40★), `LibreMuse`, `LibreCompass`, `LibreAssist` (10★) | A panel that sends selected text to an LLM and writes the reply back as **plain text** |
| **MCP adapters** | `WaterPistolAI/libreoffice-mcp` (26★), `jwingnut/mcp-libre` (11★), `krondor-corp/libre-mcp`, `sandraschi/libreoffice-mcp` | Expose LibreOffice to an *external* agent (Claude Desktop, Cursor) — headless batch or a socket |
| **Agent connectors** | `swe-sanad/LibreOffice-Claude-Connector` (3★), `NikolaiRadke/LibreAssist` (10★) | An `.oxt` plus a server that lets an agent CLI drive a running LibreOffice |
| **Converters/wrappers** | `aiworkdeck` (140★, LibreOffice-WASM in Electron), `neura-office`, `ppt2desc` | LibreOffice as a rendering/convert engine, not as the workspace |

### 1.2 The gap — nobody has combined these

Read together, the prior art shows every *component* exists but the **integration** does
not:

1. **The popular extensions are not agentic.** `localwriter` — the category leader at
   178★ — writes with `text_range.setString()` per streamed chunk: no runs, so it
   **destroys formatting** (their open issue #43), **no undo integration**, O(n²)
   re-reading, and it runs synchronously on the UI thread (issue #33: "CPU fan spins up
   and windows screen recorder dies"). It is a text-replacement tool, not a coworker.
   Its most-commented open request (#5) is *a sidebar that isn't just a chat box*.
2. **The MCP adapters are mostly headless.** They launch their own `soffice` and batch
   convert. The agent never touches the document the user is looking at.
3. **The agent connectors stop at a shell.** `LibreOffice-Claude-Connector` exposes ~74
   raw UNO tools and lets Claude Code drive the app. It is plumbing, not a product: no
   document-scoped context, no review surface, no skills, no per-turn undo discipline,
   and it requires the user to run an agent CLI outside LibreOffice.
4. **`PPT-Master` embeds DSH but never uses UNO.** It runs the DSH CLI with a custom
   profile and 9 MCP tools — the one true DSH precedent — but its "LibreOffice
   interaction" is `zipfile`+`ElementTree` on ODP XML, with `soffice` used only to
   convert. It is a generator, not an interactive coworker.
5. **Nobody has ported the Claude-for-Office *product* model.** Claude's add-ins are
   genuine in-app task panes that write **real formulas/edits into the live document**,
   keep **cell-level citations**, present **anchor → rationale → jump** instead of diffs,
   and expose **skills as slash-chips**. No LibreOffice project does this.

**Our position:** LibreOffice + DSH, with an in-office agent surface that writes to the
live document, groups each turn into one undo, and is driven by skills on the open
`agentskills.io` standard. That combination does not exist.

### 1.3 Where LibreOffice is genuinely *better* than the Office stack

Verified in `research/claude-office.md` §8.4.1 — all reproduced on this machine:

- **Anthropic's own `xlsx` skill shells out to headless LibreOffice** to recalculate
  formulas, with an `LD_PRELOAD` shim faking AF_UNIX sockets. We do it in-process:
  no subprocess, no profile bootstrap, no 30s timeout.
- **No lossy round-trip.** The entire `openpyxl` gotcha list (merged cells, destructive
  `data_only=True`, `.xlsm` macro loss, stripped external links) simply does not exist.
- **Full modern function set.** `XLOOKUP`, `XMATCH`, `SORT`, `FILTER`, `UNIQUE`,
  `SEQUENCE`, `IFS`, `LET` all evaluate correctly. Anthropic's "never use these" list is
  an artifact of writing formula strings into XML, not of LibreOffice.
- **Formula *and* value in one call** on the same cell (`getFormula()` + `getValue()`),
  where their path needs two passes and cannot get both.
- **`IMAGE()` does not exist in LibreOffice Calc**, so the CellShock data-exfiltration
  primitive that defeated Claude for Excel is structurally absent here.
- **Native review surface**: Writer tracked changes (`RecordChanges` + `getRedlines()`)
  and real `XUndoManager` — both verified scriptable.

---

## 2. Archived decisions (each backed by an executed experiment)

| # | Decision | Evidence |
|---|---|---|
| **D1** | **The agent talks to LibreOffice over a UNO acceptor opened from inside the office process by the extension itself** — so the user just opens LibreOffice normally. No `--accept` flag, no launcher script, no second instance. Default transport is a **per-user named pipe**. | F10 — verified working with zero flags. Pipe is local-only by construction. |
| **D2** | **Never use a private headless instance for interactive work.** The agent must attach to the office the user is looking at. | F1 — `SwXTextView` + real frame title prove GUI attachment |
| **D3** | **The in-office component stays tiny: it opens the acceptor and draws the panel.** All tool logic lives in an external helper process. | Prior art: the reference project is forced into stdlib-only because it runs its whole server under LibreOffice's embedded Python. The `.oxt` never speaks to a model. |
| **D4** | **Every agent turn is wrapped in `enterUndoContext()`/`leaveUndoContext()`** → exactly one Ctrl-Z per turn. | F10 — `undo stack: ['Cowork: agent turn']`, one `undo()` reverted both edits |
| **D5** | **Never use `setDataArray`/`setFormulaArray` for user-visible writes.** They create an undo entry that does not revert the cells (upstream bug). Write per cell. | `research/prior-art.md`; three independent projects avoid them |
| **D6** | **Marshal UNO mutations to the main thread via `com.sun.star.awt.AsyncCallback`.** Naive per-request threads survive a smoke test but are not proven correct. | F3a (survived) + established practice in the reference implementation |
| **D7** | **~~DSH integration uses the shipped `@deepseek-ai/dsh-mcp-client`~~ — SUPERSEDED.** No MCP anywhere. The sidebar drives the harness over the SDK JSON-RPC stdio protocol. | F11 — a ~180-line stdlib Python client booted a runtime and ran a real model turn over `initialize`/`session/prompt` |
| **D7a** | **Document tools are native Cordis rows on `ctx.tools`,** contributed by a local module row in the profile (the same mechanism the `web` profile uses for its search provider). | F17 — the agent called `document_list`/`read`/`outline`/`append`/`check_layout` as ordinary tools |
| **D7b** | **Build tool definitions with `defineTool`, then register the result.** Registering a hand-written definition leaks the schema DSL to the provider and installs no argument validation. | `Invalid schema for function 'document_append'` from the provider |
| **D12** | **The office persona must require visual verification**, and the profile must ship document skills. | F15 — the vision model found real layout defects; F17 — the agent loaded `writer-edit` unprompted and ran `document_check_layout` after editing |
| **D8** | **Review surface = native Writer redlines / Calc track changes**, not a bespoke diff engine. | `RecordChanges=True` → `getRedlines()` verified |
| **D9** | **Deterministic write-time safety classification**, never model-judged. Flag `WEBSERVICE`, `FILTERXML`, `HYPERLINK`, external refs. | Claude's post-fix guard "did not always trigger" — an LLM was deciding |
| **D10** | **Skills on the open `agentskills.io` standard** (`SKILL.md` + YAML frontmatter), with a **statically closed tool set** so a skill can never introduce a new capability. | `research/claude-office.md` §3.5, §6.1 |
| **D11** | **Formula translation layer is a P0, built first.** The model emits Excel-style `,`-separated formulas; Calc needs `;`. `=SUM(A1,B1)` → `Err:508`. | §8.4.1(a) — verified; the failure looks exactly like "function not supported". Implemented in `translate_formula()`. |
| **D13** | **Undo windows span a whole turn and are owned by a registry**, not opened per operation. | LibreOffice does *not* merge separate `enterUndoContext` blocks: two edits gave `undoDepth == 2`. Registry closes on `end_turn`, on a new turn id, on idle, and on shutdown. |

### U1 — SETTLED: native VCL sidebar panel

The sidebar spike is done and **option (a) was chosen and verified**. `CoworkDeck` and
`CoworkPanel` register alongside LibreOffice's own decks, the factory resolves, all five
controls build, and the resize-driven layout fires at real dimensions. Two bugs that
produce a blank/dead panel were hit and fixed (a wrong property name silently removed both
text areas; a one-shot layout while the parent window is 0×0 yields invisible controls).
See `research/FINDINGS.md` §"F-6 SIDEBAR SPIKE".

**Consequence:** no webview, no second runtime. The remaining UI work is composition, not
research.

---

## 3. Architecture

```
┌─ LibreOffice (the user's own process) ─────────────────────────────────┐
│  Cowork.oxt                                                            │
│   ├── BridgeJob        → opens the UNO acceptor at startup (proven)    │
│   ├── CoworkPanel      → native VCL sidebar chat UI (proven)           │
│   └── live document model  ◄── edited in place, on screen              │
└──────────────┬─────────────────────────────────────┬───────────────────┘
               │ UNO / URP over a local pipe         │ sidebar ⇄ agent
               ▼                                     ▼
┌──────────────────────────────┐   ┌────────────────────────────────────┐
│ cowork-uno helper            │   │ DSH runtime                        │
│  (python3 + python3-uno)     │◄──│  profile: libreoffice              │
│   see · find · select        │sp │   • cowork-office → document tools │
│   edit · format · verify     │awn│   • office persona (visual checks) │
│   render → PNG               │   │   • skills: writer-edit, calc-audit│
│   deterministic layout checks│   │   • SDK JSON-RPC server            │
│   ONE undo window per turn   │   │  the agent loop, approvals, goals  │
└──────────────────────────────┘   └────────────────────────────────────┘
                                               ▲
                                               │ SDK JSON-RPC (stdio)
                                               │ initialize / session/prompt
                                               │ ← session.event stream
```

**No MCP anywhere.** This is the architecture as built and verified, not as proposed.

**Why the helper is a separate process:** the in-office Python is LibreOffice's embedded
interpreter — stdlib-only in practice, and a bug there can take the user's editor down.
The `.oxt` should only open the acceptor and draw the panel. Everything fallible lives on
the other side of the pipe, where a crash costs a restart and nothing else.

**Why not serve the model tools from inside LibreOffice:** the harness already owns the
tool registry, approvals, permission presets, skills, and model routing. Contributing
document tools as native rows reuses all of it. Tunnelling them through a protocol would
mean rebuilding that surface behind a bridge, for no benefit.

**Why this beats the headless MCP adapters:** the document being edited is the one on
screen. There is no save/reload cycle, no "which copy did it edit", no file-locking fight,
and the user watches the change happen and can undo it with one Ctrl-Z.

---

## 4. Backlog

Sizes: **S** ≤ 1 day · **M** 1–3 days · **L** 3–7 days.
Every item names its acceptance test — the thing that proves it, not the thing that implements it.

### M0 — Foundation ✅ **complete**

Every risky unknown this project had is now settled by an executed experiment.

| ID | Item | Acceptance test | Status |
|---|---|---|---|
| F-1 | Live-GUI UNO attach + read + write | text changed in the open window | ✅ |
| F-2 | Native undo semantics | `enterUndoContext` groups; one Ctrl-Z reverts a turn | ✅ |
| F-3 | `.oxt` with bundled Python UNO component installs | `unopkg list` shows it; office instantiates it | ✅ |
| F-4 | **Zero-config auto-start** | launch with no flags; external process connects and edits | ✅ |
| F-5 | Sidebar deck + panel | `CoworkDeck`/`CoworkPanel` register; 5 controls build; layout fires | ✅ |
| F-6 | Visual verification pipeline | render → vision model found real layout defects | ✅ |
| F-7 | Direct harness connectivity, no MCP | a Python client drove a full agent turn over JSON-RPC | ✅ |
| F-8 | **End-to-end agent edit** | agent read, edited in the document's style, self-verified, one-undo turn | ✅ |
| F-9 | Calc + Impress document models | read/write/undo proven on Writer; **Calc read/write implemented, Impress not started** | ⚠ partial |

### M1 — The bridge and the tool surface ⏳ **mostly built**

`dsh/libreoffice/cowork/uno_bridge.py` is written and tested against a live office.

| ID | Item | Status |
|---|---|---|
| T-1 | Bridge over stdio, attach to the acceptor | ✅ |
| T-2 | **Formula translation (D11)** — `,`→`;`, `_xlfn.`, string literals preserved | ✅ |
| T-3 | Document discovery + **frame-scoped binding**; ambiguous target fails loudly | ✅ |
| T-4 | Writer: read, outline (with styles), find, append, replace, tracking, redlines | ✅ |
| T-5 | Calc: `read_range` (**formula + value in one call**), `write_range` per-cell | ✅ |
| T-6 | Deterministic `layout_check`: contrast (WCAG), overflow, spacing | ✅ |
| T-7 | `render` → PDF → PNG, with the isolated-profile requirement encoded | ✅ |
| T-8 | **Turn-scoped undo (D13)** — registry, closes on every exit path | ✅ |
| T-9 | Refuse `setDataArray`/`setFormulaArray` (D5) — per-cell writes only | ✅ |
| T-10 | Impress tool set | ⬜ not started |
| T-11 | Table, image, and style-manipulation tools | ⬜ not started |
| T-12 | `AsyncCallback` main-thread marshalling (D6) | ⬜ naive threading has held so far; still unproven under sustained load |
| T-13 | Cancellation of a running turn | ⬜ not started |
| T-14 | Verification snapshot (re-read after write) | ⚠ the agent does this itself; not yet enforced by the tool |

### M2 — DSH integration ⏳ **core done**

| ID | Item | Status |
|---|---|---|
| H-1 | Custom `libreoffice` profile with the document-tool row | ✅ |
| H-2 | Office persona (live document, visual verification, one undo, no invention) | ✅ |
| H-3 | Skills on the open standard: `writer-edit`, `calc-audit`, `document-review`, `deck-build` | ✅ (the agent loaded one unprompted) |
| H-4 | SDK JSON-RPC server, driven out of process | ✅ |
| H-5 | **Sidebar ⇄ runtime client** (the missing link: the panel does not yet talk to a session) | ⬜ **next** |
| H-6 | Model routing per purpose: fast text route vs vision route for layout review | ⬜ designed, not wired |
| H-7 | Three-mode approvals with a read/write tool split | ⬜ |
| H-8 | Per-document conversation persistence (session id derived from the document) | ⬜ |
| H-9 | Installer that registers the profile in the user's own `DSH_HOME` | ⬜ |

### M3 — Review, safety, and trust

| ID | Item | Size | Acceptance test |
|---|---|---|---|
| R-1 | Native review mode: `RecordChanges=True`, accept/reject via the office's own UI (D8) | M | Edits appear as redlines the user can accept or reject |
| R-2 | **Deterministic write classifier (D9)**: `WEBSERVICE`, `FILTERXML`, `HYPERLINK`, external refs, macros | M | Each is flagged; classification does not depend on the model |
| R-3 | Approval text names the *capability*, not the user's intent | S | The dialog says "write a formula that fetches a URL", not "add a visualization" |
| R-4 | Sanitise non-printing Unicode in approval views | S | Zero-width/bidi payloads are visible or stripped |
| R-5 | Deterministic, out-of-band audit log of every mutation | M | The log is not model-generated and survives a crash |
| R-6 | One-click "undo this entire conversation turn" from the sidebar | S | Button reverts the last turn via `XUndoManager` |

### M4 — Sidebar product surface

| ID | Item | Size | Acceptance test |
|---|---|---|---|
| S-1 | Sidebar deck + panel registered for Writer/Calc/Impress | L | Deck appears in all three with the right context list |
| S-2 | Chat transcript + composer | L | A turn round-trips from the panel |
| S-3 | **Selection-as-context chip** ("`Sheet2!B4:F20` selected ✕") | M | Chip reflects the live selection; dismissable |
| S-4 | **Citations as clickable chips** (`[B13:F16](#cite:Sheet1!B13:F16)`) → select + scroll | M | Clicking a citation selects that range in the document |
| S-5 | **Anchor → rationale → jump** edit presentation (not diff hunks) | M | Each edit is listed with a one-line reason and a jump control |
| S-6 | Skill slash-chips in the empty state | S | Typing `/` lists the installed skills |
| S-7 | Model + mode pickers | S | Selecting a model changes the route |
| S-8 | Cancel / stop control for long turns | S | Cancels cleanly (depends on T-13) |

### M6 — An isolated desktop to look at the UI in

**Highest-priority infrastructure item.** Everything in this project that is
visual has been verified only by inference: the layout is checked by arithmetic
and logs, the panel is checked through its controls, and the deck is checked by
`XDeck.isActive()`. Not one pixel has ever been *looked at*. Two design
mistakes have already shipped because of that — a duplicate caption, and a
transcript that rendered empty — and both were caught by the user, not by me.

**Constraint that forced this onto the plan:** there is no way to see the UI
without contending with the person using the machine. The only route found was
the GNOME desktop portal over D-Bus, which captures **the real desktop** — so
taking a screenshot means photographing whatever the user is doing, and the
window under test usually sits behind their browser anyway. That is fine once,
as a diagnostic, and unacceptable as a working method.

**Goal:** a virtual display this project owns, where LibreOffice can be launched,
driven, and photographed without touching the real session.

| ID | Item | Size | Acceptance test |
|---|---|---|---|
| V-1 | Stand up a headless X server (`Xvfb` or `Xephyr`) on a private display, plus `xwd`/`ImageMagick` to capture and convert | M | A known test pattern can be drawn and read back as a PNG |
| V-2 | Launch LibreOffice on that display with the extension and a document, and capture the sidebar | M | `View ▸ Sidebar ▸ Cowork` is visible in the capture, legible at 2× zoom |
| V-3 | Drive the UI in the sandbox: activate the deck, type in the composer, press Send, wait, capture the result | M | A full turn is visible in the transcript **in a picture**, not in a log line |
| V-4 | Capture the empty state, a streaming turn, an error, and a long reply — the states that are easy to get wrong | S | Four images reviewed, not asserted |
| V-5 | Screenshot regression: keep the images and diff them after panel changes | M | A deliberate layout change shows up as a diff |
| V-6 | A minimal window manager, so window size and focus are controllable rather than accidental | S | The window can be given an exact geometry before capture |

**Notes for whoever picks this up:**

- Package installs (`xvfb`, `x11-apps`, `imagemagick`) may need approval; check
  whether `Xvfb` can be replaced by `weston --backend=headless` or a nested
  `sway`, both of which draw on the machine already.
- LibreOffice on a bare X server needs a window manager for correct sizing;
  without one it may open at an unhelpful default size. Hence V-6.
- The extension's own log is not a substitute for a screenshot and never was:
  it reported `layout: parent=350x1196` while the panel was, in fact, drawing an
  empty transcript.
- Do **not** use the desktop portal for this. It was the right tool to discover
  the capability and the wrong tool to depend on.

### M5 — Packaging, ops, and reach

| ID | Problem | Size | Acceptance test |
|---|---|---|---|
| P-1 | `.oxt` build script + versioning | M | `make oxt` produces a reproducible artifact |
| P-2 | Installer that registers the .oxt *and* the DSH profile/preset | M | One command installs everything on a clean machine |
| P-3 | Idempotency: stale pipe/port recovery, second-instance detection | M | Restarting LibreOffice never wedges the bridge |
| P-4 | Uninstall leaves no residue | S | Removing the .oxt removes decks, menus, and jobs |
| P-5 | **Headless mode** (the "Cowork proper" analogue): batch/offline document work via `soffice --headless` | L | An agent processes a folder of documents with no GUI |
| P-6 | Cross-app context handoff (Writer→Calc→Impress in one conversation) | M | An outline in Writer becomes a deck in Impress |
| P-7 | Localisation groundwork (deck titles, panel strings) | S | Strings come from resources, not literals |
| P-8 | Failure checklist from the prior art: deck never appears, truncated output, no sidebar, no cancellation | M | Each documented failure mode is a regression test |

---

## 5. Sequencing — where we actually are

**Done:** the whole risky foundation. An agent has edited a live document, loaded a skill,
verified its own work, and produced a one-undo turn. No unknowns remain that could change
the architecture.

**The one missing link** is **H-5**: the sidebar panel does not yet talk to a runtime
session. Everything on both sides of that link works; the panel has a `submit()` seam and a
stubbed reply, and the runtime answers JSON-RPC correctly. Joining them is the next piece
of work, and it is the difference between "this works when driven by a script" and "the
user opens LibreOffice and talks to it".

Then, in order:

0. **M6/V-1…V-3** — an isolated desktop, before any more UI work. Every remaining
   UI task is currently unverifiable, and the two visual bugs that have shipped
   were both found by the user rather than by me. Doing this first also makes
   M4's remaining surface work testable as it lands.
1. **H-5** — a small local agent service the panel can reach, holding one runtime session.
2. **M4/S-2…S-4** — transcript, composer, selection chip, and clickable citations in the
   panel, so the conversation is usable rather than merely correct.
3. **M3** — review mode (native redlines), the deterministic write classifier, and the
   audit log. Trust work belongs before broad use, not after.
4. **M1 remainder** — Impress, tables/images, cancellation.
5. **M5** — packaging, and an installer that registers the profile and preset in the
   user's own `DSH_HOME`.

**First demoable milestone** (nearly reachable): open a Writer document, ask the sidebar
for a review, watch redlines appear with citations, accept or reject them natively, and
undo the whole turn with one Ctrl-Z.

## 6. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Visual work cannot be verified** | **Certain — it is happening now** | **M6.** Mitigated today only by careful inference: layout arithmetic, control introspection over UNO, `XDeck.isActive()`. Two visual bugs have already reached the user this way. Until M6 lands, treat every visual claim in this project as unverified and say so. |
| **Sidebar is more expensive than expected (U1)** | High | Time-boxed F-6 spike first; hybrid fallback (c) |
| UNO thread-safety bites under real load | Medium | D6 `AsyncCallback` marshalling from the start (T-10), load test F-7 |
| Formula translation has long-tail cases | Medium | T-2 first; test Excel-isms (`_xlfn.`, quoted sheets, array spilling) |
| Agent makes an unwanted edit | Medium | D4 one-undo turn, D8 redlines, D9 classifier, R-6 turn undo |
| Embedded Python limits the in-office component | Low | Already mitigated by D3 — the component only opens the acceptor |
| LibreOffice upgrade breaks internals | Medium | Pin the tested version; keep UNO surface small; regression tests |
| Prompt injection from a malicious document | Medium | D9/R-2 deterministic classifier, H-8 egress discipline, R-3 capability-named approvals |

## 7. Explicitly out of scope

- Reimplementing Office.js `SharedRuntime`/custom functions — no LibreOffice equivalent.
- An AppSource-style enterprise admin plane. Policies yes, that plumbing no.
- Hypervisor-grade sandboxing. `bwrap` + `seccomp` + dropped caps is the portable part.
- Tunnelling MCP *into* LibreOffice. The office hop is UNO; MCP is only the agent hop.
- A Windows/macOS port before the Linux path is solid (the pipe/`AsyncCallback` code is
  portable, but nothing has been tested there).
