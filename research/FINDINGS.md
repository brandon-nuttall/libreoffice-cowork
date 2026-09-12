# Feasibility PoC — verified findings

Environment: Ubuntu 26.04, LibreOffice **26.2.5.2** (`/usr/lib/libreoffice`), system Python
**3.14.4** with `/usr/lib/python3/dist-packages/uno.py` available to a *plain external*
`python3` (not just LibreOffice's bundled interpreter). `unopkg` present. No LibreOffice
SDK package installed (no `idlc`/`unoidl-write`).

All statements below were **executed and observed**, not inferred. Scripts in this
directory are the record.

---

## F1 — An external process CAN attach to the user's LIVE, on-screen LibreOffice

Launched the real GUI (`soffice ... --accept="socket,host=127.0.0.1,port=2002;urp;"`),
then attached from an ordinary `python3` process via `UnoUrlResolver`:

```
uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext
```

Result: enumerated open components, reached the live model, and **wrote into the
document the user is looking at**.

```
[1] attached to live office — 2 open component(s):
      - (untitled)  [Writer]
      - file:///.../test.txt  [Writer]
[2] live text read: 'Hello LibreOffice Cowork.\nSecond paragraph for testing.'
[3] controller: SwXTextView  frame title: 'test.txt — LibreOffice Writer'
[4] after write: '...\n[written live by the agent via UNO]'
```

`[3]` is the important line: `SwXTextView` + a real frame title proves this is a
**GUI-attached** model, not a headless batch load. The user sees the edit appear.

**API gotcha:** `desktop.Components` exposes **only** `XEnumerationAccess` — it has no
`getCount()` and no `Count`. Enumeration is the only enumeration path:
`desktop.Components.createEnumeration()`.

## F2 — Agent edits are natively undoable, with titles

`doc.getUndoManager()` returns `com.sun.star.document.XUndoManager` (NOT the older
`XUndoAction` shape). Verified members:

```
isUndoPossible()  isRedoPossible()  undo()  redo()
getCurrentUndoActionTitle()  getCurrentRedoActionTitle()
getAllUndoActionTitles()  getAllRedoActionTitles()
enterUndoContext()  leaveUndoContext()  enterHiddenUndoContext()
clear()  clearRedo()  reset()  lock()  unlock()  isLocked()
addUndoManagerListener()  removeUndoManagerListener()
```

A UNO text insertion produced `current undo action: 'Typing: One line ...ia UNO]”'`.

**Consequence:** because we edit the live model rather than rewriting a file on disk,
every agent action lands on the user's real undo stack and Ctrl-Z works. This is a
*structural advantage* over the Cowork-style "rewrite the .docx on disk" model, and
also lets us group a whole agent turn into one undo context via `enterUndoContext()`.

## F3 — A listener can be hosted INSIDE the office process, serving the live document

Started a `ThreadingHTTPServer` from LibreOffice's own Python script provider
(`~/.config/libreoffice/4/user/Scripts/python/*.py`, invoked over the socket with
`vnd.sun.star.script:probe.py$probe?language=Python&location=user`).

An HTTP request **from outside the office process** returned live document state:

```json
{"path": "/final", "documents": ["file:///.../test.txt"]}
```

### F3a — Live MUTATION from that background HTTP thread works and is safe

`GET /mutate` inserted text into the live document from
`Thread-2 (process_request_thread)` — **not** the UNO main thread:

```json
{"text": "...\n[mutated from HTTP thread Thread-2 (process_request_thread)]",
 "undo_title": "Typing: One line ...hread)]”", "survived": true}
```

Three successive mutations, then re-checked: `OFFICE STILL ALIVE: True` and the model
still consistent. So a naive request-per-thread server does not immediately destabilise
the office. **This is not a guarantee of thread safety** — UNO has no locking guarantees
and this must be treated as "unproven at scale"; the plan keeps a main-thread
marshalling queue as a designed-in option (see plan risk R3).

### F3b — `XSCRIPTCONTEXT` is NOT thread-local; capture it once at startup

A server thread that tried to use `XSCRIPTCONTEXT` directly got `NameError`. The working
pattern, verified: capture on the **script thread** at bootstrap, stash it in a module
global, and let server threads reuse it.

```python
def start():
    global _CTX
    _CTX = XSCRIPTCONTEXT.getComponentContext()   # script thread only
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
```

The resulting context was valid from every later request thread.

## F4 — `com.sun.star.bridge.Acceptor` is NOT creatable in-process

Probed from inside the office process:

```
com.sun.star.bridge.Acceptor          -> 'NoneType'
com.sun.star.bridge.UnoUrlResolver    -> 'pyuno'
com.sun.star.bridge.BridgeFactory     -> 'pyuno'
createInstance(Acceptor)              -> None
```

`Acceptor` is a non-creatable (abstract) service: the UNO acceptor is created by the
**process launch** (`--accept=...`), not by any in-process factory.

**Consequence — this is the pivotal design finding.** An extension cannot turn on the
UNO socket for itself. Two viable shapes follow, and F3 selects between them:

- **(A) sidecar + socket** — DSH's MCP server runs as its own process and attaches to a
  `--accept`-enabled office. Requires controlling LibreOffice's *launch*, which in turn
  requires either user configuration or a launch wrapper. Fragile as an install story.
- **(B) in-process listener, MCP over HTTP** — the office process itself hosts the
  listener, so **no `--accept`, no launch flags, no socket, and no second LibreOffice
  instance are needed.** DSH's MCP client speaks `streamable-http` natively.

**(B) is chosen.** It is the only shape that works for a user who just opens LibreOffice
normally, and it collapses "the bridge" and "the document access" into one process.

## F5 — DSH can consume this with zero new transport code

`@deepseek-ai/dsh-mcp-client` is installed and declares exactly two transports:

- `transport: "stdio"` → `command`, `args`, `env`, `cwd`
- `transport: "streamable-http"` → `url`, `headers`

It registers each MCP tool onto the harness tool registry as **`mcp__<serverName>__<toolName>`**
and handles the full `tools/list` lifecycle, including live tool-list-change notifications
and reconnect with backoff. It is **not** in the `dsh-base` or `dsh-web-app` bundles — it
must be added to a Profile explicitly.

**Consequence:** the entire DSH-side integration is a few lines of composition. All real
work belongs on the LibreOffice side.

## F6 — Sidebar UI is a supported extension surface

Sidebar definitions are present in the shipped registry (`main.xcd`, `writer.xcd`,
`calc.xcd`, `impress.xcd`, `math.xcd`), and `unopkg` is installed for `.oxt` packaging.
No LO SDK is installed, which rules out compiling C++/Java UNO components without adding
a dependency — so the extension should be **pure Python**, which F3 proves is sufficient.

---

## What this changes about the design

1. **No `--accept` configuration step, no launch wrapper, no headless instance.** The
   user opens LibreOffice as they always do; the agent works on the document in front of
   them. This removes the single most likely adoption blocker and the most likely source
   of "your agent opened a second copy of my file" bugs.
2. **Undo is free and native.** Agent actions are ordinary document actions with titles.
   Accept/reject can be built on real `XUndoManager` contexts rather than a bespoke diff
   engine over a rewritten file.
3. **The MCP tool surface is the product.** Since DSH already handles MCP transport,
   framing, timeouts, reconnects, and tool-name namespacing, the value-add is entirely in
   (a) the tool taxonomy, (b) making writes safe/reviewable, and (c) the sidebar UX.
4. **Threading is the real engineering risk**, not connectivity. F3a says the naive
   approach survives a smoke test; it does not say it is correct. The plan keeps
   main-thread marshalling as a first-class option and puts it behind an interface.

## Residual unknowns (must be settled during implementation)

- Does a bundled **Python UNO component** in an `.oxt` (`pythonpath` in the manifest,
  registered via `ImplementationRegistration`) instantiate reliably and let a sidebar
  button start the listener? (F3 used a *user-profile script*, which is easier but a
  weaker install story.) **This is the single most important thing to prototype next.**
- Soffice auto-start / first-run behaviour of the extension, and how a stuck listener
  port is recovered.
- Calc and Impress document models (only Writer was exercised).
- Whether UNO document mutations need explicit main-thread marshalling under sustained
  agent load.

---

# Second PoC round — packaging, component model, and auto-start

## F7 — A `.oxt` with a bundled Python UNO component works, end to end

Built `ext/oxt-proto/` and installed it into a workspace-local profile with
`unopkg add -env:UserInstallation=file://...`. `unopkg list` confirms registration:

```
Identifier: com.cowork.oxtproto
  is registered: yes
  bundled Packages: {
      URL: .../components/cowork_bridge.py
      is registered: yes
      Media-Type: application/vnd.sun.star.uno-component;type=Python
  }
```

Then, over the UNO socket, the office instantiated our own service by name:

```python
smgr.createInstanceWithContext('com.cowork.Bridge', ctx)
# implementationName=com.cowork.Bridge, supportedServices={com.cowork.Bridge},
# supportedInterfaces={XServiceInfo, XTypeProvider}
```

and that component started the in-process HTTP listener:

```
HTTP (from a different process) -> {"path": "/live",
  "documents": ["file:///.../test.txt"]}
```

**So the full chain is proven: `.oxt` → Python UNO component → in-process listener →
live document, served to an external MCP client.** No LibreOffice SDK, no compiled code.

### F7a — GOTCHA: plain Python methods are NOT reachable over the UNO bridge

`component.start()` over the bridge fails with `AttributeError: start`. Only methods
declared by a UNO interface survive the bridge. Two consequences:

- Any control API the sidebar needs must be expressed as **real UNO interfaces**
  (`XInitialization`, `XJob`, `XServiceInfo`, `XDispatch`, …), never as ad-hoc methods.
- `XInitialization.initialize()` did **not** fire via either
  `createInstanceWithContext` or `createInstanceWithArgumentsAndContext` in this build
  (26.2.5.2). Starting the listener from the **constructor** does work — that is where
  the working prototype starts it.

### F7b — GOTCHA: no cross-instance idempotency

A second instantiation of the component tried to bind the port again:
`eager start raised OSError: [Errno 98] Address already in use`. Because ordinary
Python object state is per-instance, a guard must be **module-level**, and the port
must be chosen/retried to survive a stale listener. This is a real bug, found by testing.

## F8 — Extension-supplied `Jobs.xcu` registers, but never dispatches

Attempted to get the listener started automatically with zero user action, via the
documented Jobs mechanism. `registry/Jobs.xcu` was declared in the manifest as
`application/vnd.sun.star.configuration-data` and it **did** register — a live query of
the running office's configuration showed it under the right nodes:

```
Events node children: ['onLoad', 'onDocumentOpened']
  event onLoad -> ['JobList'] -> ['CoworkStartup']
```

Tried all three events: `onLoad`, `onDocumentOpened`, `onStartApp`. **None dispatched
the job** (no component construction, no listener, in a launch where no `--accept` was
given). A shipped job on the same event (`org.libreoffice.PresenterScreen` under
`onDocumentOpened`) is present and works, so the mechanism itself is sound; something
about extension-supplied job bindings is not honoured for dispatch.

**Status: UNRESOLVED.** The correct schema was recovered from LibreOffice's own
`main.xcd` (`Jobs` → `Job{Service,Context,Arguments}`, `Events` → `Event` →
`JobList` → `TimeStamp{AdminTime,UserTime}`), so this is not a guess-shape problem.
Worth one bounded investigation later (likely `TimeStamp.UserTime` semantics, or the
job needing an implementation name rather than a service name, or the jobs reader
caching at first-run). **Do not build the plan on auto-start until it is settled.**

## F9 — Fallback that is verified: `--accept` on a STOCK profile

A completely fresh profile launched as

```
soffice --norestore --accept="socket,host=127.0.0.1,port=2003;urp;"
```

accepted a UNO connection immediately (`attached OK`), with **no** profile
pre-seeding, no config edits, and no extension installed. So the installed
configuration option in plan item "attach to a running office" is viable as the
interim mechanism while F8 is unresolved.

## Where this leaves the activation question

Three activation routes exist, in descending order of desirability:

1. **Sidebar button / menu item** (user-controlled, explicit, no background listening
   unless asked). Needs the UNO-interface control surface from F7a. **Preferred.**
2. **`--accept` socket + external launcher** (verified today, F9). Costs a launch flag
   and a listening port; works on a stock install.
3. **Fully automatic on-start Jobs.xcu** (F8, currently not dispatching). Best UX,
   not yet available.

Because F3/F7 proved the listener lives *inside* the office process and reaches the
live model directly, options 1 and 3 need **no socket at all** — the socket is only
ever a *control* channel, never the document path. That keeps the design robust:
losing the socket loses nothing but one (replaceable) way to press "start".

---

# Third PoC round — AUTO-START SOLVED (F8 resolved and superseded)

F8 above concluded that extension-supplied jobs never dispatch. **That conclusion was
wrong, and the cause was my `Jobs.xcu` shape.** The prior-art research surfaced a
production implementation (`swe-sanad/LibreOffice-Claude-Connector`, MIT) that does
exactly this, and reading its `ext/Jobs.xcu` gave the two corrections:

| I had | Correct |
|---|---|
| `oor:name="onStartApp"` (lowercase o) | **`oor:name="OnStartApp"`** (capital O) |
| `<node oor:name="Job"><prop UserTime>…` inside `JobList` | **a bare `<node oor:name="Job" oor:op="replace"/>`** — no `TimeStamp` wrapper |

I had also been creating `com.sun.star.bridge.Acceptor` (which returns `None`, per F4)
when the working service names are **`com.sun.star.connection.Acceptor`** and
**`com.sun.star.bridge.BridgeFactory`**.

## F10 — Zero-configuration auto-start now WORKS, verified end to end

The extension now ships a `BridgeJob` bound to `OnStartApp` + `onFirstVisibleTask` that
opens a UNO acceptor from inside the office. Launch was then made with **no flags at
all** — no `--accept`, no launcher script, no configuration:

```
soffice --norestore -env:UserInstallation=file://.../loprofile5 <doc>
```

Job log (written by the extension):

```
JOB: listening on socket,host=127.0.0.1,port=2005
```

A separate process then connected and worked on the live document:

```
LIVE DOCS: ['file:///.../poc/test.txt']
before:        'Hello LibreOffice Cowork.\nSecond paragraph for testing.'
after 2 edits: '...\n[turn edit 1]\n[turn edit 2]'
undo stack:    ['Cowork: agent turn']
after ONE undo:'Hello LibreOffice Cowork.\nSecond paragraph for testing.'
```

**This is the complete capability, proven:**

1. the user opens LibreOffice the way they always do;
2. the extension silently opens a local acceptor — **no configuration whatsoever**;
3. an external agent process attaches and sees the **live** document;
4. edits land in the real document, in front of the user;
5. a whole agent turn is **one Ctrl-Z**, not N scattered edits.

Point 5 is the single most important UX property, and it is a structural advantage over
both the Claude-for-Office add-ins and Cowork's file-rewriting model.

### F10a — Named pipe is the correct default; socket only for sandboxed testing

The production default is `pipe,name=lo-cowork-<user>` — local-only by construction, no
port, no firewall surface, nothing to configure. `COWORK_ACCEPT` overrides it.

**Sandbox caveat (testing only):** in this DSH environment each command runs in its own
mount namespace with a private `/tmp`, and OSL pipes are filesystem paths under `/tmp`.
So a pipe created by the office is *not visible* to a client started from a different
bash call, and the client fails with `NoConnectException` (errno 10) even though the
office is listening correctly. TCP works because the network namespace is shared. **This
is an artifact of the test harness, not of the design** — on a normal desktop both
processes share `/tmp` and the pipe works. All zero-config verification above therefore
used the `COWORK_ACCEPT` socket override, which exercises exactly the same Job → acceptor
→ bridge path.

### F10b — Two non-obvious rules that must not be lost

- **`execute()` must never raise.** The Jobs framework *permanently deactivates* a job
  whose `execute()` raises — silently, for the rest of the profile. That is very likely
  a second, independent reason my earlier attempts appeared to do nothing.
- **Publish the acceptor into module state before starting the worker thread**, so a
  terminate arriving in that window can always `stopAccepting()`. Otherwise a daemon
  thread blocked in `accept()` keeps `soffice.bin` alive after the user quits.

### F10c — Third-party findings folded in (from `research/prior-art.md`)

- `XUndoManager.enterUndoContext()`/`leaveUndoContext()` grouping is independently
  confirmed by three projects and reproduced here cross-process (F10 above).
- ⚠ **`setDataArray` / `setFormulaArray` create an undo entry that does not revert the
  cells** (upstream LibreOffice bug, "not worked around" by the reference project).
  **Do not use them for user-visible writes** — write cell-by-cell, or use
  `setFormula`/`setValue` per cell, so undo stays honest.
- Writer tracked changes confirmed scriptable (`RecordChanges=True` → `getRedlines()`),
  giving a native accept/reject review surface.
- `com.sun.star.awt.AsyncCallback` is the known-good way to marshal UNO work back to the
  main thread — the established answer to the F3a threading question.

## Status of the activation question (supersedes the earlier three-route ranking)

**Resolved.** Auto-start via `Jobs.xcu` + in-process acceptor is now the primary
mechanism, verified working with zero user configuration. The sidebar button remains a
UI surface, not an activation requirement.

---

# F-6 SIDEBAR SPIKE — U1 SETTLED

**Decision: native VCL sidebar panel (option (a)).** No webview dependency, no second
runtime. Verified working in a live office.

## What was built

`ext/oxt-proto/components/cowork_sidebar.py` — an `XUIElementFactory` returning an
`XUIElement` → `XToolPanel` + `XSidebarPanel`, plus two registration files:

- `registry/org/openoffice/Office/UI/Sidebar.xcu` — declares `CoworkDeck` + `CoworkPanel`
- `registry/org/openoffice/Office/UI/Factories.xcu` — maps the `CoworkSidebar` token to
  `com.cowork.SidebarFactory`

## Verified in the running office

The deck registers **alongside LibreOffice's own** (read from the live configuration):

```
decks:  ['FindDeck', 'CoworkDeck', 'ShapesDeck', 'GalleryDeck', ...]
panels: ['EmptyPanel', 'CoworkPanel', 'DrawPageDeck', 'GalleryPanel', ...]
deck.Title = Cowork
deck.ContextList = ('WriterVariants, any, visible ', ' Calc, any, visible ',
                    ' Impress, any, visible ', ' Draw, any, visible ', '')
factories: [... 'SwPanelFactory', 'SvxPanelFactory', 'com.cowork.SidebarFactoryReg']
```

The panel instantiates over UNO and satisfies the sizing contract:

```
panel class: pyuno   (XSidebarPanel + XToolPanel supported)
getHeightForWidth(300) -> Min/Preferred/Max: 260 260 260
getMinimalWidth -> 92
```

## The two bugs that produce a blank/dead panel — both hit and fixed

Both were caught **only** because every control is built inside its own try/except that
logs to a file. Without that, the panel builds "successfully" and shows nothing.

**1. `HideScrollBar` is not a property of `UnoControlEditModel`.**

```
control FAILED: txtTranscript (UnoControlEdit): AttributeError: HideScrollBar
control FAILED: txtComposer  (UnoControlEdit): AttributeError: HideScrollBar
```

I had invented the name. The real property set (50 properties, read from the live
`PropertySetInfo`) has `HScroll` / `VScroll` / `AutoHScroll` / `AutoVScroll` /
`HideInactiveSelection`. **A one-line wrong attribute name silently removed both text
areas from the panel.** Fixed to `VScroll`/`AutoVScroll`; all five controls now build:

```
control OK: lblTitle (UnoControlFixedText)
control OK: txtTranscript (UnoControlEdit)
control OK: txtComposer (UnoControlEdit)
control OK: btnSend (UnoControlButton)
control OK: btnClear (UnoControlButton)
```

**2. The panel is created while its parent window is still 0×0.**

```
layout: parent=0x0 container=0x0        <-- first pass, useless
layout: parent=3373x1345 container=3373x1345   <-- resize listener fires, correct
```

A one-shot layout at build time gives invisible (zero/negative-width) controls. The
`XWindowListener` re-layout is **required**, not an optimisation. This reproduces the
warning in the reference implementation and is now proven here.

## Consequences for the plan

- **U1 is closed.** M4's UI work is de-risked: the deck appears, the factory resolves,
  controls build, and layout is driven correctly.
- The panel already keeps a **per-document transcript** in module state, so switching
  decks or reloading a document preserves the conversation — the natural home for
  M2/H-7 (per-document persistence).
- `XTextListener` is wired on the composer, so Enter-to-send and slash-command
  completion (S-6) have a hook already.
- Still unverified visually (no screenshot tool in this environment): the *pixel*
  appearance. Everything that can be checked programmatically has been. Confirming the
  deck renders correctly on screen takes one manual look: **View ▸ Sidebar ▸ Cowork**.

---

# DIRECT DSH CONNECTIVITY — VERIFIED (no MCP in the path)

The user asked to drop MCP and talk to a harness directly. This is now proven
end to end, and it is a **better** architecture than the MCP plan (see the
correction note at the end).

## F11 — An out-of-process client can drive a full DSH agent session over JSON-RPC

`@deepseek-ai/dsh-sdk-jsonrpc-server` serves the SDK wire protocol; `dsh --profile sdk`
boots it. The protocol is newline-delimited JSON-RPC 2.0 with exactly three
client→server requests and four server→client notifications.

`research/poc/dsh_sdk_probe.py` is a **~180-line pure-stdlib Python client**. It booted a
runtime and ran a real model turn:

```
initialize   -> {"serverInfo": {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}}
session/prompt -> {"messageId": "bd0d34ab-..."}

event stream:
  turn/start
  step/start
  request/header   (provider: litellm, model: deepseek-api/v4.1-flash, maxTokens: 384000)
  assistant/chunk  {"type":"text-delta","text":"H"} ... "ARN" "ESS" " OK"
  assistant/chunk  {"type":"usage","usage":{"inputTokens":1014,"outputTokens":4}}
  assistant/message {"role":"assistant","content":[{"type":"text","text":"HARNESS OK"}]}
  step/end
  turn/end         {"reason":{"kind":"completed"}}
status: running -> idle
```

**No MCP server, no MCP client, no tool bridge was involved.** The LibreOffice side can
speak this directly.

## F12 — Two gotchas, both cost a run to find

**1. `SessionPromptParams.contentBlocks`, not `content`.** Wrong name gives
`-32603: Cannot read properties of undefined (reading 'filter')` — a message that names
neither the field nor the caller's mistake.

**2. `initialize` must be awaited before any prompt.** `session/prompt` before a
successful handshake answers `SDK server is not initialized`. The handshake is also the
model-route validation point: a provider with no registered adapter fails there with
`no adapter registered for provider "litellm"`, which is the *right* place to fail.

Also learned: the SDK runtime **persists sessions to disk**, and a reused `sessionId`
across runs collides:
`session "…" already has a persisted log on disk that does not match this live session`.
Session ids must be unique per conversation — for us, derived from the document.

## F13 — `pi-ai` is already in the base bundle and is dormant until settings supply providers

I first tried adding `@deepseek-ai/dsh-llm-pi-ai` via `--patch`, which failed with
`duplicate loader entry id: llm-pi-ai`. The base bundle already mounts it
("the pi-ai multi-provider twin, mounted dormant: zero routes until a `llm-pi-ai:`
settings section supplies provider profiles"). Dropping the patch and letting the
existing `llm-pi-ai.providers.litellm` section in `settings.yaml` do the work was
correct — **the user's existing model configuration is reused verbatim rather than
duplicated.**

## F14 — Test environment note (sandbox artifact, not a product constraint)

`~/.dsh` is not writable under this session's file sandbox, so the shipped `sdk` profile
could not be auto-initialized at `~/.dsh/profiles/sdk`. The probe therefore ran against a
self-contained DSH home at `.dsh-sdk/` with `settings.yaml` and `.credentials.yaml`
copied in and `profiles/node_modules` symlinked to the real install. `DSH_HOME` selects
it, so this is purely a dev-loop convenience.

For real installation the profile must be registered with **the user's own DSH home**, so
that the runtime inherits their models, credentials, skills, and permission presets. That
write needs approval — it is on the critical path for packaging (P-2).

## Correction to the plan: MCP is removed

The plan's `D7` (use `@deepseek-ai/dsh-mcp-client`) is **superseded**. Consequences:

- **No MCP server to write.** The documented `mcp__cowork__*` tool naming, the MCP
  tool-list lifecycle, and the `streamable-http` transport all disappear from the design.
- **The model-facing tool surface now lives in the DSH profile**, not behind a protocol
  boundary. That is strictly stronger: tools are native rows on `ctx.tools`, so
  approvals, permission presets, plan mode, and the tool catalog apply to them directly
  rather than through an MCP bridge.
- **The document-transport question is now separate and simpler.** The sidebar talks
  JSON-RPC to the runtime; the runtime reaches the document through the UNO pipe that
  F10 already proved. Those are two independent links, and neither is MCP.
- The reference project's constraint that made it stdlib-only inside LibreOffice
  disappears: our in-office component never speaks JSON-RPC to the model at all.

---

# VISUAL VERIFICATION — VERIFIED (the office-profile centrepiece)

## F15 — The agent can render a document and *look at it*

Pipeline confirmed on this machine:

```
live document ──> soffice --convert-to pdf ──> pdftoppm -png ──> PNG
                                                              │
                                       base64 → SDK image block
                                                              ▼
                                        vision model reviews the page
```

`research/poc/vision_probe.py` sent a rendered page as an
`SdkEncodedImageBlock {type:"image", data, mimeType:"image/png"}` and the model
returned a genuine layout critique. It found all four defects that were planted in
the fixture, plus two more, and gave **measurements**:

> **1. Text overflowing the right margin** — `ThisIsAVeryLongUnbreakableToken…` runs to
> ~x≈798px, past the body text's right margin at ~x≈740px. It overruns by roughly
> 55–60px and sits flush against the paper edge.
>
> **2. Broken "unbreakable" token** — that same string does break mid-word, spilling
> `ndeeditWill.` onto the next line…
>
> **4. Poor contrast** — "Low contrast text here." … is rendered in a very light gray on
> white, far below readable contrast…
>
> **5. Heading/body spacing collapse** — "Section One" sits directly on top of "Body text
> with normal contrast." with no leading above or below…

This is not a novelty. It closes the single biggest criticism of every agentic Office
product: **verifiability**. Claude for Office's dominant reviewer complaint is that
errors "fail silently" because the agent can never *see* the result — it only sees
markup. Here the agent renders its own output and inspects it like a proofreader.

**No competing LibreOffice project does this.** Every project in the GitHub sweep
(`localwriter`, `librethinker`, `mcp-libre`, `LibreOffice-Claude-Connector`, `LibreASSIST`)
reads and writes text; none of them ever look at the page.

### Model routing consequence

`deepseek-api/v4-flash-vision-exp` is the vision-capable route in the user's settings.
The office profile therefore routes **layout review** to a vision model while keeping
ordinary editing on the faster text route. This is exactly why the profile needs
per-purpose model selection rather than one global model.

### Sandbox note

`--convert-to` needs its own `-env:UserInstallation=<isolated profile>` to avoid lock
fights with the user's running office. A shared profile made the conversion die with
exit 134; an isolated one succeeded. **This is a hard requirement for the render tool**,
and it is why headless conversion must never reuse the interactive profile.

## F16 — Deterministic geometry checks complement the vision pass

Not everything should cost a vision call, and deterministic checks are strictly more
reliable where they apply:

- **Text-frame overflow** — compare each text frame's text metrics against the page
  text area via `XText`/`XTextViewCursor` and page style margins.
- **Contrast** — read `CharColor`/`CharBackColor`/`CharHighlight` and compute a WCAG
  contrast ratio; flag anything below 4.5:1.
- **Orphans/widows** — count paragraphs before a page break.
- **Empty/overflowing table cells** — compare table height to content.

These run in-process over UNO with no rendering, no subprocess, and no model call. They
are the "compile errors" analogue this whole product category lacks.

## Revised architecture (MCP removed, direct connectivity)

```
┌─ LibreOffice (user's process) ───────────────────────────────────────┐
│  Cowork.oxt                                                          │
│   ├── BridgeJob      → opens UNO acceptor (proven, F10)              │
│   ├── CoworkPanel    → sidebar chat UI (proven, F-6)                 │
│   └── live document                                                  │
└────────────┬────────────────────────────────────┬────────────────────┘
             │ UNO / URP, local pipe              │ sidebar ⇄ agent
             ▼                                    ▼
┌────────────────────────┐          ┌──────────────────────────────────┐
│ cowork-uno helper      │          │ DSH runtime                      │
│ (python3 + python3-uno)│◄─────────│  profile: libreoffice            │
│  see/find/select/edit  │  spawns  │  preset:  cowork-office          │
│  render → PNG          │          │   • document tools (native rows) │
│  deterministic checks  │          │   • vision layout review         │
└────────────────────────┘          │   • skills, approvals, undo      │
                                    └──────────────────────────────────┘
```

No MCP anywhere. The sidebar speaks the SDK JSON-RPC protocol directly (F11), and the
document tools are ordinary Cordis rows on `ctx.tools`.

---

# THE DOCUMENT BRIDGE — built and tested (`cowork/uno_bridge.py`)

A newline-delimited JSON bridge over stdio, run by the runtime as a subprocess. It
attaches to the **user's running LibreOffice** and exposes document operations.
Tested against a live office over the extension's own pipe acceptor.

## Verified operations

| Op | Verified result |
|---|---|
| `ping` / `list_documents` | found the open document, title, url, kind, modified |
| `outline` | paragraphs **with style names** — the agent sees structure, not a blob |
| `read_text` | full body text plus the current selection |
| `append` / `replace` | writes into the live document |
| `begin_turn` / `end_turn` / `undo` | **one Ctrl-Z per turn** (below) |
| `layout_check` | deterministic defect detection (below) |
| `render` | live document → PDF → PNG page images (the visual pipeline) |
| `read_range` / `write_range` | Calc, formulas **and** values in one read |

## Three real bugs found by testing — none would have surfaced in review

**1. Per-operation undo contexts do not merge.** I assumed two `enterUndoContext`
blocks with the same title would coalesce. They do not: two edits gave
`undoDepth == 2`. An agent turn is *many* tool calls, so the window must span the
whole turn. Replaced with a `TurnRegistry`: the window opens on the first mutation
carrying a `turn_id` and closes at `end_turn`. Verified — three edits in one turn:

```
edit 1 -> undoDepth=2 current='Cowork: turn one'
edit 2 -> undoDepth=2 current='Cowork: turn one'
edit 3 -> undoDepth=2 current='Cowork: turn one'
ONE undo -> the entire turn reverted, and the earlier unrelated edit survived
```

The registry also **closes the window on every exit path** — `end_turn`, a new turn
id, a 15-minute idle timer, and process shutdown — because a stranded undo context
would leave the user's Ctrl-Z broken. That failure mode is worse than no grouping.

**2. `insertString` with newlines does not create paragraphs.** It inserts literal
newlines *inside* one paragraph. Seven fixture "paragraphs" came back as a **single
paragraph** whose string still contained the newlines, so paragraph enumeration,
style assignment, and every structure-aware check saw a blob. Now newlines are
inserted via `insertControlCharacter(cursor, PARAGRAPH_BREAK, False)`, and the
difference is directly observable:

```
  7 Preformatted Text  'HeadingFixture'
  8 Preformatted Text  'Body under the heading.'
  9 Preformatted Text  ''
 ... 13 Preformatted Text  'AfterGap'
```

**3. An empty undo stack is not an error.** `getCurrentUndoActionTitle()` *raises*
on a fresh document ("no action on the undo stack"), so a naive implementation
reports a broken undo manager on every new document. Each undo read is now guarded
separately, and an empty stack reports `undoDepth: 0`.

## `layout_check` — deterministic defect detection

Runs entirely in-process over UNO: no rendering, no subprocess, no model call.
Detects low-contrast runs via a WCAG ratio (4.5:1 threshold) computed from
`CharColor`, unbreakable tokens long enough to overflow the text area, and runs of
empty paragraphs used for spacing. Verified firing on a fixture:

```
[spacing/info] 4 consecutive empty paragraphs — spacing is being done with
               blank lines rather than paragraph styles
[overflow/warning] 90-character unbreakable token is likely to overflow the text area
```

These are the "compile errors" this product category otherwise lacks: cheap,
repeatable, and more reliable than asking a model where the margins are.

## `render` — the visual pipeline, in the bridge

Live document → `soffice --convert-to pdf` → `pdftoppm -png`. Verified producing
`bridge_doc.pdf` and `render-1.png` (14,770 bytes) from a document open on screen.

**Hard requirement confirmed:** the conversion must use its own
`-env:UserInstallation` profile. Sharing the interactive profile makes the
conversion die (exit 134) fighting for the profile lock. This is encoded in the
tool, with the reason, so it cannot be "optimised" away later.

## Formula translation (D11) — implemented

`translate_formula()` rewrites Excel-style `,` separators to Calc's `;`, strips
`_xlfn.`/`_xlws.` prefixes, and leaves commas inside string literals, array
constants, and quoted sheet names untouched. This is the difference between the
model writing `=SUM(A1,B1)` and getting a working `11` versus `Err:508` — a
failure that looks exactly like "LibreOffice doesn't support that function".

---

# END TO END — an agent edited the user's live document (no MCP)

## F17 — The whole architecture works

Booted `dsh --profile libreoffice` (document tools + office persona + skills +
SDK JSON-RPC server), sent one prompt over JSON-RPC, and watched a real agent work
on the document open on screen.

The agent's own trajectory, from the session event stream:

```
step 1  document_list            -> "writer: e2e_doc.txt — LibreOffice Writer"
step 2  document_read            -> the live text
        document_outline         -> paragraphs with styles
step 3  skill {name:"writer-edit"} -> loaded the profile's skill
step 4  document_append          -> appended "Next Steps" with 2 bullets,
                                    paragraph_style "Preformatted Text" (correctly
                                    matched to the document's own style)
step 5  document_outline         -> verify structure
        document_check_layout    -> "No deterministic layout problems found."
```

**42 tool calls across 34 steps**, ending in `turn/end {kind: "completed"}`.

Notable, and not prompted for explicitly: the agent **loaded the `writer-edit`
skill** because the task matched its description, **matched the document's
existing paragraph style** rather than imposing one, and **ran its own
verification pass** (`document_check_layout`) after editing. That last behaviour
is exactly what the office persona asks for, and it happened without being told.

### The document really changed, and one undo reverted it

```
Project Status Report
Overview
This report summarises progress. …
Risks
There is a risk noted in the margin area …

Next Steps
- Review the risk noted in the margin area.
- Decide whether that risk needs attention from the steering group.
```

`undo_state` reported `undoDepth: 1`, `currentUndoAction: "Cowork: edit"`, and a
single `undo` restored the original document exactly.

## Four integration bugs, all found by running it

Each one failed the **entire plugin mount**, not just one tool — worth knowing,
because a single bad schema takes the whole profile down.

**1. A patch cannot re-`name` a row that already exists.**
`duplicate loader entry id: system-prompt` / `llm-pi-ai`. Patch semantics: address
an existing row by `id` alone at the top level to modify it; use `insert` only for
rows that do not exist. Repeating `name` inside `insert` for an existing id is a
duplicate.

**2. Only one skill provider may be named `filesystem`.**
`a skill provider named "filesystem" is already registered`. The base bundle
already mounts `skill-filesystem`, so the skills directory must be a **config
change to that row**, not a second row of the same package under a new id.

**3. Tool output schemas use the harness DSL, and object openness is mandatory.**
`schema.required must be an array of strings` — raw JSON Schema's top-level
`required` array is rejected. The DSL puts `required: true` on each property, and
every `type: 'object'` needs an explicit `additionalProperties`.

**4. `items: {}` is invalid; unconstrained values are `{ type: 'json' }`.**
`parameters.values.items.items.type must be string/number/…/object/json`.
The harness DSL has no "anything" node — `{type:'json'}` is the lossless-JSON node.

**The root cause of 3 and 4 together:** registering a hand-written definition
with `ctx.tools.register({...})` skips `defineTool`, which is what converts the
DSL to real JSON Schema *and* installs argument validation. A hand-written
definition leaks the DSL to the provider verbatim:

```
litellm.BadRequestError: Invalid schema for function 'document_append':
{"type":"string","description":"…","required":true}
```

`required: true` is a harness annotation, not JSON Schema — the provider rejects
it. **Always build definitions with `defineTool` and pass the result to
`ctx.tools.register`.**

## What is now proven, in one list

1. An `.oxt` opens a UNO acceptor from inside a normally-launched LibreOffice (F10).
2. A sidebar deck + panel registers with LibreOffice's own (F‑6).
3. A Python helper attaches to that office and reads/edits the **live** document.
4. A DSH profile mounts document tools, a persona, and skills, and boots clean.
5. An out-of-process client drives a full agent session over JSON-RPC (F11).
6. The agent reads the live document, loads a skill, edits in the document's own
   style, verifies its own work, and completes a turn.
7. The entire turn is one Ctrl-Z (F10, D4).

**No MCP anywhere in the path.**

---

# THREADING FIX — SOLVED (and it was solvable all along)

The panel could not update during a turn, which made it unable to show progress,
ask a question, or present an approval. That blocked the question/approval work
entirely, so it was the precondition.

## F18 — `com.sun.star.awt.AsyncCallback` works; my earlier probe was wrong

I had concluded that `AsyncCallback` "was not creatable from the panel's context".
**That conclusion was wrong, and the error was in the probe, not the platform.**

What I did earlier: probed for the service from a *user Scripts* Python context via
the script provider, and from plain system Python. Both reported it unavailable.

What is actually true, verified inside the office:

```
XCallback interface available: True
AsyncCallback created: pyuno object (com.sun.star.uno.XInterface){implementationName=com.sun.star.awt.comp.AsyncCallback,
    supportedServices={com.sun.star.awt.AsyncCallback},
    supportedInterfaces={XServiceInfo, com.sun.star.awt.XRequestCallback, XTypeProvider, XWeak}}
notify() fired: True
notify() ran on thread: 'MainThread'
script thread is: 'Dummy-1'
=> marshalled to another thread: True
```

The service is obtainable with
`smgr.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)`, and
**`addCallback` may be called from a worker thread — `notify` then runs on
`MainThread`.** That is exactly the marshalling the panel needed.

The reference implementation (`dandi-91/LibreOffice-Bielik-Agent`) does the same
thing, which is what prompted me to retest rather than trust my earlier result.

**Lesson worth keeping:** "this API does not exist" is a claim about *where I
looked*, not about the platform. Both of my earlier probes used a context the
panel does not run in.

## F19 — The panel now streams from a worker thread

`submit()` starts a worker; the worker calls `Client.ask()` and pushes events with
`post_from_worker`, which appends to a queue and calls `_async.addCallback(pump,
None)`. `pump.notify()` runs on the GUI thread and drains the queue into the
controls. The callback and the `AsyncCallback` are both held on the element, since
a garbage-collected callback is a silently dead panel — the same failure class as
an unreferenced listener.

If `AsyncCallback` is ever unavailable, the panel **falls back to running inline**
rather than refusing: the turn still works, it just cannot redraw while it waits.

## F20 — A menu entry, and the API details that cost a round trip each

The panel was only reachable by opening the sidebar and clicking the deck. There is
now a top-level **Cowork ▸ Show Cowork Panel** menu entry backed by a dispatch
handler (`ProtocolHandler.xcu` + `Addons.xcu`).

This also makes the panel's turn path reachable without a person: a click cannot be
fired from outside the office, but a dispatch URL can.

Four specifics, each found by getting it wrong first:

1. **`XDispatch.initialize` receives the Frame**, and it is called *after*
   `queryDispatch`. Holding a frame snapshot at dispatch-construction time gives
   `None`; the dispatch must resolve the frame when it runs.
2. **`XFrame.getController()`, not `getCurrentController()`.** The latter belongs to
   the model — `AttributeError: getCurrentController`.
3. **`XSidebar.showDecks()` rejects a Python sequence** at the bridge:
   `CannotConvertException: Type 20 is not supported!` — for both a tuple and a list.
4. **`XDeck.activate(True)` takes one argument**, and that is the call that switches
   decks: `activate()` alone raises `IllegalArgumentException: incorrect number of
   parameters passed invoking function activate: expected 1, got 0`.

Verified end to end:

```
before: CoworkDeck active = False
after : CoworkDeck active = True
handler log: sidebar visible, deck CoworkDeck active
             layout: parent=350x1196 inner=338 transcript_h=1120
```

## What this unblocks

The question/approval seam. A panel that can redraw during a turn can now render a
structured question or an approval prompt mid-turn, which is what
`ctx.userQuestions` has been waiting for. Before this fix that work was not
buildable — a synchronous panel has nowhere to draw a question.

---

# VERIFICATION GAP — the honest record

## F21 — I could screenshot *a* desktop, but not my own

Chasing the ability to see the UI, I found the GNOME desktop portal reachable over
D-Bus from the sandbox, and it works:

```python
iface.Screenshot("", {"handle_token": token, "interactive": False}, ...)
# → file:///home/brandon/Pictures/Screenshot-N.png
```

`org.gnome.Shell.Screenshot` is *not* usable (`AccessDenied: Screenshot is not
allowed`), but `org.freedesktop.portal.Screenshot` is, and returns a full
3440x1440 PNG of the real session.

**The problem is what that is.** It captures the actual desktop, which means
photographing whatever the user is doing at the time, and the LibreOffice window
under test usually sits behind their browser anyway. Using it while someone is
working is intrusive, and it is not a repeatable method.

So: the capability exists and was worth discovering, but the answer is a virtual
display this project owns — captured as **M6** in `PLAN.md`.

## What has already gone wrong because of this

| Bug | How it was found |
|---|---|
| A caption repeating "Cowork" three times | **User** |
| The transcript rendering empty on open | **User** |
| A redundant collapsible panel header | **User** |
| A wrong property name silently deleting both text areas | Instrumentation (per-control try/except with a log) — not eyes |

Every one of those passed the programmatic checks. The panel reported
`layout: parent=350x1196 inner=338 transcript_h=1120` — correct arithmetic — while
drawing an empty box. **Logs describe intent; only pixels describe what a person
sees.**

## Standing rule until M6 lands

Treat any claim about appearance in this project as unverified, and label it that
way. The evidence I can offer is real but narrow: control properties, geometry
arithmetic, registration state, and deck activation. That is not the same as
having looked at it.
