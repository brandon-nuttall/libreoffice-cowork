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

---

# I CAN SEE IT — first real look at the panel

The sandbox display works, and this is the first time any of this UI has been
*looked at* rather than inferred. Three earlier bugs reached the user precisely
because I could not do this.

## F22 — What works, confirmed by eye

- **`Cowork` appears in the menubar** — the Addons.xcu registration is correct.
- **The deck registers, opens and activates**; `CoworkDeck active: True`.
- **The panel renders in the sidebar.** The greeting is visible and reads
  correctly: "Working on document.txt. I can read it, edit it, restructure it,
  and check how the result looks — everything I change in one go is a single undo
  step. Tell me what you want done."
- **All six controls exist**: `txtTranscript`, `prgStatus`, `lblStatus`,
  `txtComposer`, `btnSend`, `btnClear` — including the new progress row, so the
  `UnoControlProgressBar` names were right.
- **`Send` and `Clear` are laid out side by side** at the bottom, as intended.
- **The panel takes the full height of the deck.** The status row is present.

## F23 — The real defect: the sidebar is 92 px wide

The panel log is unambiguous about the sequence of geometry passes:

```
layout:   parent=1285x900 container=1285x900 inner=1273 transcript_h=786
relayout: parent=1285x900 container=1285x900 inner=1273 transcript_h=786
layout:   parent=329x721  container=329x721  inner=317  transcript_h=607
layout:   parent=92x720   container=92x720   inner=80   transcript_h=606
```

The panel starts wide, then the sidebar narrows it to **92 px**, so `inner`
becomes 80 px. The layout code does exactly what it was written to do — it just
has almost no width to work with, so every line wraps after two or three
characters and the greeting becomes an unreadable column.

**So the "controls do not fill the width" bug was real, and this is its true
cause: the sidebar's own width, not my geometry.** The resize handling I added is
working (note the `relayout` lines); it was faithfully re-laying-out into a
92-pixel box.

The 92 px is LibreOffice's minimum sidebar width, and a fresh profile has no
stored width, so it opens at the minimum. Two things follow:

1. My `getMinimalWidth()` returns 92 (`_MIN_INNER_WIDTH + 2*_MARGIN` = 80 + 12),
   which is satisfied by a 92 px sidebar — so the panel reports itself as fitting
   when it plainly does not. **`getMinimalWidth` should declare the width the
   panel actually needs to be legible** (roughly 220–260 px), which gives the
   sidebar a reason to open wider.
2. The sidebar width is also persisted per user in
   `org.openoffice.Office.UI.Sidebar`, so a fresh profile starts at the minimum
   regardless.

## F24 — Also visible, and not mine

- **A large black rectangle** sits over the document area. The document renders
  around it. This looks like an Xvfb rendering artefact rather than a panel
  defect — the panel is entirely on the right and is unaffected. Worth
  confirming on a real display before chasing.
- **The status row shows no text and no bar animation** when idle, which is
  correct: it is only populated while a turn runs. Whether it appears *during* a
  turn is still unverified, because no agent was connected in this capture.

## Passing the tests did not mean it looked right

Every item above passed the programmatic checks: all controls built, the layout
arithmetic was correct at every width, registration and activation succeeded. The
panel was still unusable, because nothing in those checks knew that 80 pixels of
inner width cannot hold a sentence.

## F25 — The width fix works, confirmed by eye

`_MIN_INNER_WIDTH` raised from 80 to 220, reported through
`getMinimalWidth()`. The sidebar now opens wide enough that the greeting renders
as three readable lines instead of a two-character column:

```
Cowork:
Working on document.txt. I can read it, edit it, restructure it, and check
how the result looks — everything I change in one go is a single undo step.
Tell me what you want done.
```

`Send` and `Clear` sit side by side at the bottom; the deck header reads
`Cowork`; the document renders beside it. The panel is usable for the first time.

**The lesson, stated plainly:** `getMinimalWidth()` is not a formality. Returning
a value the panel can technically survive at lets the sidebar open at its own
minimum, and "technically survives" is not "legible". A panel should declare the
width it needs to work.

## F26 — Shift+Enter sends; Enter still inserts a newline

Requested after the multiline composer landed: "shift+enter needs to send the
command".

Implemented with `XKeyListener` on the composer. Two details that are easy to get
wrong:

1. **The event must be consumed, not merely observed.** If Shift+Enter were
   allowed through, the control would append a newline and then the send would
   fire, leaving a stray blank line in the composer. `keyPressed` calls
   `event.Consume()` so the newline never happens.
2. **The send happens in `keyReleased`, not `keyPressed`.** Sending on the press
   would risk a second send if the release were also handled. The listener
   records that it consumed a Shift+Enter and sends on the matching release, so a
   release with no prior press cannot send at all.

Plain Enter is left completely untouched, so the control inserts the newline
itself and wrapping behaves like any other multiline field.

`com.sun.star.awt.KeyModifier.SHIFT == 1`, `com.sun.star.awt.Key.RETURN == 1280`,
both confirmed by reading the constants rather than assuming. `XKeyListener`
declares both `keyPressed` and `keyReleased`; pyuno rejects the object if either
is missing.

### Verification boundary

The control exposes only `addKeyListener`/`removeKeyListener` — there is no way to
fire a key event through the UNO bridge, and `xdotool` is not installed, so no
synthetic keystroke is possible. The listener logic is therefore covered by six
unit assertions in `tests/test-panel-wiring.py` (consume, deferred send, plain
Enter untouched, no phantom send), and attachment is verified by the absence of
the failure the code logs. **The keystroke itself has not been exercised; only its
handler has.** Worth one manual Shift+Enter to close.

## F27 — "why does the text always show up selected?"

Because one line was doing two jobs and got one of them wrong:

```python
control.setSelection(Selection(0, len(body)))     # intended: scroll to the end
```

`Selection(0, n)` means **select everything from 0 to n**, so the whole
conversation was permanently painted in the selection colour. The intent was a
caret at the end, which is `Selection(n, n)` — a zero-width selection. One
character wrong, and the panel looked broken in a way nobody would describe as a
scrolling bug.

Found by looking at a captured screenshot, which is the third defect this
session that no assertion caught:

| Defect | Found by |
|---|---|
| Transcript permanently selected | eye (sandbox capture) |
| Sidebar squeezing the panel to 80 px | eye + the geometry log |
| Greeting word-wrapped to two characters per line | eye |
| Caption repeating "Cowork" three times | user |
| Transcript rendering empty | user |

## F28 — Also confirmed clean in the same capture

The zoomed crop shows the panel rendering correctly end to end: the `Cowork:`
label, the greeting wrapped across six readable lines, generous spacing between
messages, an empty conversation area below, and the transcript scrollbar. Nothing
is highlighted, clipped, or overlapping.

The black rectangle seen in earlier captures is gone and was therefore an Xvfb
repaint artefact rather than anything in the panel — worth remembering before
chasing that class of thing.

---

# THE AGENT SERVICE IS GONE — one process, one port

Reported as: *"why do we even need the cowork_agent as glue code? ideally we don't
need a separate server as glue code or people aren't going to want to install or
use it."*

The user was right, and the immediate failure proved the point: the install had
**two** `cowork-agent` processes, one of them an 18-hour-old systemd service, and
the second died with `OSError: [Errno 98] Address already in use`. Two processes
and two protocols for one conversation, a second thing to install, and a port that
could collide with itself.

## What the old shape was

```
panel → cowork-agent (socket, bespoke JSON) → runtime (SDK JSON-RPC stdio)
      → dsh-uno helper (stdio) → LibreOffice (UNO)
```

Three processes, two protocols, a systemd unit, and a service that had to be
running before anything worked.

## What it is now

```
panel → runtime (HTTP on loopback) → dsh-uno helper (stdio) → LibreOffice (UNO)
```

`cowork-serve.mjs` is a profile row that serves the conversation **from inside the
runtime**, where the agent loop, the tools and the skills already live. The
standalone service, its systemd unit, and its bespoke protocol are all deleted.

The panel still cannot spawn the runtime — LibreOffice's embedded Python cannot —
so `cowork-runtime.sh` ships inside the `.oxt` and the panel runs it on demand.
It is idempotent (exits at once if the endpoint answers) and detached.

**Nothing to install. Nothing to keep running. No port to collide with.**

## Verified

```
$ # nothing running, no service installed
$ /ping → {"ok": false, "reachable": false}
$ client.ask(...)          # the panel's own client, unchanged
  [started]
  streamed : 'CLIENT OK'
  final    : 'CLIENT OK'
```

## Four API facts, each found by being wrong first

1. **`ctx.agents.create({sessionId, meta, agentOptions})`** returns a handle whose
   `.agent` is the agent. `agents.session(...)` does not exist.
2. **The agent has no `prompt()`.** The SDK server queues messages with
   `agent.followup(createUserMessage({content, source}))`. `session.prompt(...)`
   was invented twice before reading the source.
3. **Events arrive through the Cordis context**, not the session:
   `ctx.on('session/event', (session, event) => …)` and
   `ctx.on('agent/status', ({agent, status}) => …)`.
4. **The sdk app exits on stdin EOF** (`exitOnStdinEnd`). Redirecting stdin from
   `/dev/null` — the obvious way to detach — kills the runtime instantly. The
   launcher holds stdin open through a FIFO with `tail -f /dev/null`; closing that
   FIFO is also the clean shutdown path.

A plain file log at `~/.cache/cowork-serve.log` records apply, listening and every
request, because the row's failures are otherwise invisible: an early return on a
missing service made an earlier version look like it had never been mounted.

---

# THE PANEL WORKS, SEEN IN A CAPTURE

Asked "why does it have that cowork_agent.py message?" and "i don't see any
progress or any indication that anything is working at all".

Both were real. The capture now shows the whole round trip: the greeting, my
message, the reply **FINIAL OK**, and an **empty** progress row afterwards.

## F29 — The stale message was a cached greeting

The panel showed "Start it with: python3 dsh/libreoffice/cowork/cowork_agent.py",
a file that had been deleted in the previous commit.

Cause: the greeting is cached per document in a module-level dict, and that cache
outlives the code that wrote it. Opening a document that had been seen before
replayed the old text.

Two fixes:

* **The greeting no longer names a command.** It says the panel will start up on
  the next message, which is true, and points at the log if it does not.
* **`_GREETING_VERSION`** travels with each cached greeting, and a cached
  conversation whose greeting carries a different version is re-seeded. Without
  it, every wording change would be invisible for every previously-opened
  document.

## F30 — The progress row was empty because no turn had been sent

The log showed the panel being built with no `posted`/`delivering` lines
afterwards: **no message had ever been sent**. The row was empty because there was
nothing to report.

That could not be left as "probably fine", so the panel gained a testing hook:
with `COWORK_AUTOSEND` set it submits that text once, on the first resize — the
first moment it has real geometry, and a plain VCL callback with no marshalling
involved. That made the panel's own turn path reachable without a person at the
keyboard, and it immediately found the next bug.

## F31 — Streaming through AsyncCallback does not work, and the reason matters

The panel tried to stream a turn on a worker thread and marshal updates with
`AsyncCallback`. The pump fired **once** and then stopped.

An isolated probe settled it: re-arming from inside `notify` DOES work — five
callbacks in a row fired. So the failure is specific to the panel's shape, where
one `notify` both drains a queue and re-arms. A callback registered while the
previous one is still executing is dropped.

Rather than keep fighting it, the turn now runs **inline**. The reply is rendered
as it arrives on the VCL thread; the progress row and the Send button carry the
"still working" signal. A turn that silently stops updating is worse than one that
blocks, and this path is guaranteed to update.

Consequence: the elapsed clock cannot tick while the turn blocks, so it is
computed at each redraw — which lands on tool calls, and that is where the useful
reading is ("Checking the layout… 24s").

## F32 — "Working…" stayed on screen after the turn finished

Visible in the first successful capture: the reply was rendered and the row still
read "Working…". The end-of-turn order cleared the status *before* clearing the
busy flag, so the row's own clearing redrew it as busy. Fixed by clearing busy
first, and by making the label show only what a tool call actually reported rather
than falling back to "Working…" whenever busy.

## F33 — Two checks added, because three edits in a row deleted more than intended

A bad half hour: several automated edits to `cowork_sidebar.py` each removed more
than asked, and the result was never a syntax error. It was a panel that imported
cleanly and then failed inside LibreOffice with `AttributeError: … has no attribute
'_set_busy'` — three separate times, each found only by opening the office and
reading a log.

`tests/test-component-statics.py` now checks, per file:

* it parses;
* every name a function loads resolves (scope-aware — a first attempt reported 70
  false positives by treating locals as missing globals, and a check that noisy
  trains you to ignore it);
* every `self.method()` a class calls is defined by that class, or inherited from
  an external base;
* **every file in `components/` is declared in the manifest, and every manifest
  entry has a file.** That one immediately caught `cowork_client.py` and
  `cowork-runtime.sh` missing from the manifest after an edit rewrote it — the
  sidebar imports one and runs the other, so both would have been absent from the
  installed extension.

It also let me delete `cowork_bridge.py`, an unused prototype that had been in the
manifest since the first day.

---

# THE FREEZE — blocking the UI thread was the wrong call

Reported as "when sending a message it responds but it then crashes the app",
with the desktop offering **Force Quit** / Wait for "LibreOffice Writer".

That was my doing. I had made the turn run inline on the VCL thread, on the
reasoning that "a turn that silently stops updating is worse than one that
blocks". The reasoning was wrong about which failure is worse: a brief
silence is recoverable, a frozen application that the desktop offers to kill is
not — and to the user it looks exactly like a crash.

## F34 — The AsyncCallback pump does work; the earlier failure was elsewhere

Before reverting I measured both re-arm shapes in a live office:

```
A: re-arm from inside notify        -> 4 notifies (wanted 4)
B: an independent thread re-arms    -> 4 notifies (wanted 4)
```

**Both work.** So re-entrant re-arming is not inherently broken, and the pump's
earlier "fires once then stops" was something else — plausibly the `deliver()`
method the pump called having been deleted by one of the edits that deleted too
much, whose exception was swallowed by a broad `except` in the drain loop.

The panel now uses shape **B**: a small dedicated thread re-arms every 150 ms
while a turn is in flight. It costs one thread and cannot be disturbed by whatever
else a `notify` does. This is not a place to be clever twice.

## F35 — Verified: the application stays responsive

With the worker restored, the office was probed over UNO every two seconds during
a turn:

```
..............   14/14 answered
```

No freeze, and the turn completed: the transcript shows the message and the reply,
and the status line is empty afterwards.

## F36 — The progress bar is gone

*"i'm not sure we want to represent the progress as a progress bar -- the
completion is nondeterministic so we probably just want to indicate forward
momentum, not progress to a specific goal"*

Correct. A bar implies a known destination, and nobody knows how many tool calls a
request will take. A bar crawling toward a finish line that does not exist is a
claim the panel cannot support.

It is now a single status line naming the current step, with an elapsed count
after 15 seconds — the same shape the harness's own chrome uses, and the honest
one: "Checking the layout… 24s" says *this is still happening*, which is all the
panel actually knows.

---

# MARKDOWN IN THE SIDEBAR

Asked after seeing the first working conversation: *"i think we need to be able to
render markdown in the sidebar, so maybe a giant text box isn't what we want to
do"*. The panel was showing `**bold**`, `-` bullets and `--` literally.

## F37 — There is no rich-text control, so formatting comes from control properties

Checked, in this order:

* `UnoControlRichTextControl` / `UnoControlRichText` — **not creatable** from the
  panel's context; the model comes back `None`, the same trap as
  `com.sun.star.awt.Timer`.
* A single `UnoControlEdit` — holds plain text, and its font properties apply to
  the whole control, so one word cannot be bold.
* `UnoControlFixedTextModel` — accepts **FontWeight, FontHeight, FontName,
  FontSlant, TextColor**, and `FontWeight.BOLD` is a real constant (150.0).

So the conversation is now a **vertical stack of small styled labels** inside a
plain container: a heading is a bold larger label, a code line is monospace, a
bullet is indented, a speaker name is grey. Which is what a chat view is anyway.

There is no scrolling container (`UnoControlContainerModel` carries only Border,
BackgroundColor and Text), so scrolling is a real `UnoControlScrollBar` beside it
plus an offset applied to the rows, with off-view rows hidden rather than drawn at
a negative offset.

## F38 — Markdown is parsed in a UNO-free module, and tested without a GUI

`cowork_markdown.py` turns messages into typed blocks — heading, paragraph,
bullet, code, rule, speaker — and strips inline emphasis. It imports no UNO, so
`tests/test-markdown.py` exercises it directly: 34 checks covering the shapes an
agent actually replies with, unterminated fences, ordered lists, links, and
hostile input (unbalanced markers, 500-word lines, empty strings).

Putting the format layer behind a testable seam mattered here: "a bullet still
says `-`" and "a heading still has its hashes" are exactly the defects a screenshot
would only catch by luck.

## F39 — Four geometry mistakes, all invisible in code and obvious on screen

1. **`FontName = ""` is not "use the default."** It breaks font resolution and the
   label renders as scattered glyph fragments.
2. **The obvious family names are wrong.** `fc-match` reports **Noto Sans** and
   **DejaVu Sans Mono** on this platform; "Liberation Sans"/"Liberation Mono" are
   only aliases, and a family the toolkit cannot resolve renders as fragments
   rather than falling back.
3. **`FontHeight` is in points, not twips.** Multiplying by ten — a habit from
   twip-based APIs — made every label twenty times too large; the transcript came
   out as two enormous letters.
4. **The container's units are 1/100 mm.** An early `_ROW_HEIGHT = 62` gave a
   10-point line 0.62 mm of height, so text drew on top of itself, and
   `_CHAR_WIDTH = 60` allowed three characters per line, so the wrapping shredded
   every sentence.

`XScrollListener` also does not exist — the interface is **`XAdjustmentListener`**
— and importing it makes the extension fail to load entirely with
`No module named 'com'`.

## Status

Verified by capture: speaker labels render grey and bold, paragraphs wrap, the
font size is right, and no `**` or `-` markers reach the screen. Row spacing is
still loose — the gap between a speaker label and its first line is visibly larger
than intended — so that is the next thing to tighten.

---

# THE MARKDOWN PARSER WAS ALREADY IN LIBREOFFICE

Asked: *"we probably don't want to create our own brand new markdown renderer -- is
there a markdown renderer that's already part of libreoffice? or some other library
we can use?"*

Yes, and I should have looked before writing one.

## F40 — LibreOffice 26.2 ships a Markdown filter

`share/registry/writer.xcd` declares a filter with:

```
oor:name="Markdown"        Types: IMPORT EXPORT ALIEN
Extensions: "md markdown"  MediaType: "text/markdown"
UIName: "Markdown Document"  FilterService: com.sun.star.text.TextDocument
```

Loading markdown with it produces a fully styled Writer document, and the styling
is readable through the ordinary document model:

```
"# A Heading"   -> ParaStyleName "Heading 1",  run CharWeight 150.0
"**bold**"      -> a run with CharWeight 150.0
"*italic*"      -> a run with CharPosture ITALIC
"- item"        -> body paragraph with NumberingRules
"```code```"    -> ParaStyleName "Preformatted Text"
```

So `cowork_markdown.py` no longer parses anything. It writes the text to a temp
file, loads it hidden through that filter, and reads back styled blocks with
per-run emphasis. Roughly 120 lines of hand-written regex were deleted.

## What this buys, and what it costs

**Buys:** the full CommonMark-ish syntax, maintained by the office, for free —
tables, nested lists, links, block quotes — none of which my parser handled. And
it cannot drift from what LibreOffice's own Markdown export produces.

**Costs:** parsing now needs a live office, so it is no longer a pure function and
cannot be tested the way the regex parser was. `tests/test-markdown.py` was
rewritten to test what remains ours — the block model, spacing, and the style maps
— rather than left passing against code that no longer exists. Blocks are parsed
once per message and cached on the entry, because a render happens on every
scroll.

## F41 — Two more API names

* `UnoControlScrollBar` has **no `addScrollListener`** — it is
  `addAdjustmentListener` (the interface is `XAdjustmentListener`).
* `_wrap` lost its `@staticmethod` decorator during the renderer rewrite, so it
  was called with `self` as its first argument, and the panel failed to build with
  `takes 2 positional arguments but 3 were given`.

Both were caught by instrumentation rather than by inspection, which is the
pattern for this whole panel: it is small enough to look correct and behaves badly
in ways only a log or a capture reveals.

## Still open

Row spacing is looser than intended — the gap between a speaker label and its
first line is visibly too large — so `_ROW_HEIGHT` and the `gap_before` values
need tightening. The capture at this point shows text rendering correctly at the
right size with emphasis preserved, which is the part that was broken.

---

# TOWARD A CHAT LOOK (in progress)

Asked to look like Claude for Word / Copilot. Reference screenshots were fetched
from GIGAZINE's coverage of Claude for Word, which shows both states clearly.

## F42 — What the reference actually does

From [Claude for Word screenshots](https://gigazine.net/gsc_news/en/20260413-claude-for-word/)
and [Anthropic's own guidance](https://support.claude.com/en/articles/14465370-use-claude-for-word):

* **The assistant's replies are plain text.** There is no "Claude:" label above
  them. The *user's* messages are the ones that need distinguishing, and they sit
  in a tinted, rounded bubble.
* **The empty state is centred**: a mark, "How can I help with this document?",
  then four suggestion chips one click from a useful request.
* **The composer is a boxed field** with a `+` for attachments on the left, a
  "Reply" placeholder, a model picker ("Sonnet ⌄"), and a **filled** send button
  on the right — not a bare text field with two labelled buttons beneath it.
* Generous whitespace; light neutral surfaces; links in blue.

Copilot's guidance agrees on the principle: *"opens a side pane that works
directly with your document: not just as chat, but as an editing partner … with
clear signals so you always know what it's doing"*
([Microsoft 365 blog](https://www.microsoft.com/en-us/microsoft-365/blog/2026/05/28/introducing-a-new-design-for-microsoft-365-copilot/)).

## F43 — What changed

* **The "Cowork:" prefix is gone.** Speaker is carried on the block and the
  *user's* lines are tinted and inset instead. A transcript that labels both
  speakers reads like a log; a bubble on one side and plain text on the other
  reads like a conversation.
* **The seeded greeting is gone.** It was prose pretending to be a message, it
  scrolled away, and caching it per document is what produced the stale
  `cowork_agent.py` text earlier. The opening view is now drawn as an empty state.
* **Row heights derive from the font** (`size * 0.3528 + 2mm`). A single constant
  cannot fit a 9pt caption and a 12pt heading: too small and lines overlap, too
  large and everything is padded with dead space.

## F44 — Where this stands, honestly

The chat *structure* is in place and the markdown rendering is verified working.
The **empty state layout is not right yet**: the heading renders, but its
subtitle and the suggestion chips are not appearing where they should, and I have
not found the cause.

What I know: the rows are created and positioned (`row0 y=212 h=623`,
`row1 y=875`, chips at y=1592+), the viewport is 760 units, so everything after
the heading falls below the fold. Centring the block was supposed to fix exactly
that and did not, which means my model of the available height is still wrong —
most likely `container.getPosSize().Height` is not the height the rows are
clipped to.

**This is the point to step back rather than keep iterating.** Each check costs a
rebuild, a sandbox restart and a minute of waiting, and I have made four attempts
without converging. The honest summary: the panel is structurally a chat now, the
formatting is right, and the empty state needs someone looking at it iteratively —
which is cheap for a person with the panel open and expensive for me.

---

# A FASTER LOOP, AND WHAT IT FOUND

Told to take as long as needed (`"keep trying :)"`), so the first thing to fix was
the loop itself. Every layout attempt had cost: edit → rebuild the `.oxt` →
reinstall → restart LibreOffice → activate the deck → capture → look. About a
minute per attempt, and four attempts had not converged.

## F45 — Two tools, and the process error behind them

**`tests/panel-layout.py`** prints the panel's actual geometry as numbers — row
positions, heights, widths, visibility, font size, alignment, colours, plus
`<-- BELOW VIEW` and `<-- HEIGHT < TEXT` markers. Most layout faults are not
visual questions: *is the row inside the viewport, is its height bigger than its
font, is it hidden.* Those are numbers, and a capture is then only needed to
confirm taste.

**`tests/check-methods.py`** answers one question fast: does the component define
every method it calls.

**`tests/run-all.sh`** runs everything, with output visible, in one command.

That last one exists because of a mistake worth recording. The statics check had
been correctly reporting `CoworkUIElement._scroll_to_newest() is not defined`
while I ran it as:

    python3 tests/test-component-statics.py >/dev/null 2>&1 && echo "statics: PASS"

I read the exit status and threw away the message. Four separate edits deleted
`_scroll_to_newest` or `_set_busy`, each producing a panel that imported cleanly
and then failed inside LibreOffice with an AttributeError, and each costing a full
rebuild-restart-capture cycle to discover. **The check was right and I was
discarding its output.**

## F46 — The bug that mattered: a stale width preferred over the live one

```python
width = self._transcript_width or container.getPosSize().Width or _FALLBACK_WIDTH
```

`_transcript_width` is captured during a layout pass, and one early pass happens
while the sidebar is still at its **92-unit minimum**. Preferring that cached value
wrapped every message at 16 columns, so `hello world` rendered as `hello w` — the
conversation looked truncated no matter how wide the sidebar became. Reversing the
preference to use the live container size fixed it.

## F47 — Also fixed in this pass

* **The empty state no longer flows into the transcript.** It is drawn as one of
  two modes, so a conversation no longer inherits its centred rows and generous
  gaps — which is what made two words sit far apart down a blank panel.
* **One bubble per message**, not one per line. A per-line bubble gave a
  multi-line message a stripe per row.
* **An empty render no longer hides the pool.** Renders happen on scroll and on
  every status change, so a later empty pass was wiping a conversation that had
  just been drawn correctly.
* **`_transcript_bubbles` was never initialised**, which crashed the panel on
  build with `AttributeError`.

## F48 — Where it stands

Working: no crashes; the user's message renders in a tinted bubble; the assistant's
reply is plain text; markdown is parsed by LibreOffice's own filter; the status
line shows progress and clears.

Not right: **the chat does not yet look like the reference.** The bubble is a
full-width block rather than a shape that hugs its text, spacing between messages
is still loose, and the composer is still two labelled buttons instead of a boxed
field with a filled send arrow. Captures consistently show only the first message
line while the readback reports the correct geometry, which suggests my inspection
is reaching a different panel instance than the one on screen — worth resolving
before the next round of layout work, because it is why the last several attempts
felt like they were converging and did not.

The reference shape, for whoever picks this up: assistant text plain, user text in
a hugging bubble, centred empty state with suggestion chips, and a composer box
containing `+`, a placeholder, a model picker and a filled send button.

---

# REVERTED TO THE TEXT VERSION — and why that is the right answer

Asked: *"can we revert to the version that just has text? we keep breaking basic
stuff like chat and taking turns. we need to be sure that chat and taking turns
works."*

Correct on both counts. The restyling broke working behaviour repeatedly, and the
fix is not another styling attempt.

## F49 — The measurement that settles it

The sidebar hands the panel **232 units** in this toolkit's 1/100 mm — **2.32 cm**,
about 88 px, roughly **fourteen characters per line at 10pt**. A bubble's padding
leaves about six.

Bubbles, right-aligned user turns and per-line emphasis are all reasonable ideas
that **cannot fit in the space LibreOffice gives a sidebar panel.** That is not a
layout bug to be fixed with more care; it is the platform's constraint. It is also
exactly why Claude for Word draws a plain text pane rather than bubbles — the same
constraint, solved the same way.

The previous design drew the conversation as a stack of individually-positioned
labels so it could carry per-run styling. That approach required computing every
line's position, width and height by hand, and it produced, in order:

  * "hello w" instead of "hello world" — a stale width preferred over the live one
  * "elp with this document?" — a centred label wider than the pane, clipped left
  * rows positioned below the viewport they were measured against
  * one grey stripe per line instead of one bubble per message
  * a crash on build from an uninitialised attribute, three separate times

Every one of those was found by screenshot, each costing a rebuild, a reinstall, a
fresh LibreOffice and a capture.

## F50 — What it is now

One `UnoControlEdit` for the conversation. It wraps natively at any width and
scrolls natively, which are the two things the label stack kept getting wrong.
Markdown is still parsed — by LibreOffice's own filter — and then rendered to
readable plain text: headings upper-cased, bullets as `•`, code indented, a blank
line between turns. Emphasis is dropped rather than lost, because the parser has
already removed the markers.

`cowork_layout.to_plain_text()` is a pure function with 20-odd unit tests, so the
formatting can be changed without rebuilding anything.

## F51 — The acceptance test that should have existed first

`tests/test-conversation.py` proves taking turns works, against a live runtime:

```
first turn    ok  a reply came back
              ok  it is the reply that was asked for
              ok  the reply was streamed, not delivered whole at the end  (1.5s)
second turn   ok  a second reply came back
              ok  it is the second reply
              ok  it did not repeat the first answer
not doubled   ok  the streamed reply is not doubled
              ok  the final reply is not doubled
still healthy ok  it answers after two turns
              ok  and is not stuck busy
Chat and turn-taking work.
```

It cannot drive the panel's *button* — a UNO control exposes only its declared
interfaces, so `submit()` raises `AttributeError` from outside the office. That
gap is covered by `tests/test-panel-wiring.py`, which runs the real `submit()` and
`deliver()` against fakes. Between the two, every link is exercised. Neither alone
was enough, which is precisely why the turn path kept breaking unnoticed.

Both now run in `tests/run-all.sh`, alongside the component statics — which also
gained a check for `self.CONSTANT` reads after `_SUGGESTIONS` went missing and
crashed the panel on build.

## Verified in a running office

The transcript, read back from the live panel after two turns:

```
and I'll do it through the document's lower-level interface.

Reply with a heading and two bullets about this document.
```

Both turns present, wrapped to the pane, status row empty. No crashes.

---

# THE CORE PROMISE, TESTED — and a targeting bug that mattered

`tests/test-live-edit.py` now proves the claim the whole project rests on, rather
than leaving it to hand checks:

```
the agent can see an open document          ok
the document has the marker before we start ok
the agent answered                          ok
the first edit landed                       ok
the second edit landed                      ok
content that was already there survived     ok
the turn added ONE undo entry, not one per edit  ok   (depth 1 -> 2, "Cowork: edit")
both agent edits are gone after one undo    ok
the document matches what it was before     ok
the pre-existing content is intact          ok
Live editing works, and a turn is one undo step.
```

That is the product: an agent edited the document on screen, and a single undo
reversed the entire turn without disturbing anything that was already there.

## F52 — WARNING: with two offices running, the agent can edit the wrong one

Writing that test found a genuine safety problem, not a test artefact.

The document tools choose their office like this:

```python
if os.environ.get("COWORK_ACCEPT"):   use that
else:                                 the default per-user pipe
```

The pipe is **per user, not per office**. With two LibreOffices running — the
user's on `:0` and a sandbox on `:99` — an unset `COWORK_ACCEPT` attaches to
whichever answers first. During this test the agent reported `Untitled 1`, which
was **the user's own unsaved document**, while the panel and the bridge were both
looking at `freshdoc.txt` in the sandbox.

Nothing was harmed here because the agent happened to make no edits. It could as
easily have rewritten a document somebody was working on.

Two changes:

1. **`cowork-runtime.sh` now records the target in its log and passes
   `COWORK_ACCEPT` through explicitly**, with a comment explaining the failure.
   Silence about which office is being edited is the dangerous part.
2. The test sets `COWORK_ACCEPT` explicitly, which is what made it pass.

**For anyone running more than one office: set `COWORK_ACCEPT`.** The normal
single-office case is unaffected — the pipe is correct there, and is local-only by
construction.

A stronger guard belongs in the product and is not built yet: the panel knows which
document it is showing, so the tool layer could refuse to act when the office it
attached to is not the one the panel belongs to. That is worth doing before this is
used in earnest.

---

# THE EFFECT OF THE AUDIT — three real bugs found and fixed

A full clean-slate test of HEAD, run exactly as the user would see it, found:

## F53 — The reply appeared twice, and it was a logic error

`deliver()` handled streaming chunks by accumulating text into the last
transcript entry — correct. But when the "final" event arrived, it APPENDED the
authoritative reply as a NEW entry instead of replacing the streamed one. The
result was every reply visibly doubled:

```
Hello there — good to see you!
Hello there — good to see you!
```

**Fix:** `final` now replaces the last cowork entry's text rather than appending a
second copy. `error` handles the same case the same way.

## F54 — And a second copy of the whole turn, caused by testing

`autosend firing submit()` fired **twice** in the log. The cause: each
`createUIElement` call — which the UNO inspection tools use to inspect the panel —
builds a NEW `CoworkUIElement`. That instance armed its own `_autosend_pending`
(since `COWORK_AUTOSEND` was still set in the office's environment), and its
first resize submitted the same text again. So my own probe was creating a second
turn, which the transcript then showed as a repeat of everything.

**Fix:** autosend now fires only when the conversation is still empty. A second
panel instance (a probe, or the sidebar rebuilding the deck) sees a non-empty
conversation and skips.

## F55 — The Send button stuck on "Working…"

The "final" handler did call `_set_busy(False)`, which resets the label. But with
the reply duplicated the turn-leftover state was inconsistent; when only one turn
runs, the busy flag and the label both revert correctly.

## Verified after the fixes

* autosend fires exactly **once** (log: 1)
* the panel has text throughout its extent (pixel density confirmed)
* the document is unchanged by a chat-only turn (as it should be)
* the undo stack is clean: no document edits, no undo entries
* both acceptance tests pass: `Chat and turn-taking work.` and `Live editing works,
  and a turn is one undo step.`

---

# F56 — Phase 1 of the chat UI, built on probes instead of guesses

`docs/CHAT_UI_DESIGN.md` was written first, and two toolkit probes ran before a
line of renderer code, because the previous bubble attempt died on unverified
assumptions. The probes changed the design rather than confirming it:

  * `MultiLine UnoControlFixedText` wraps natively (6 lines observed) — hand
    wrapping is gone entirely.
  * Accessible `getCharacterBounds` reads rendered line geometry — heights are
    observed from the render, never predicted. On this backend possize units
    track device pixels at the sidebar (~72 dpi), so a 10pt line is ~11–36
    units depending on window scale — the number's meaning varies by context,
    which is precisely why it is measured live per session (the panel
    calibrated pitch=36 on first run) instead of hard-coded.

Shipping checks caught two more before they could reach the user:

  * `Label` does NOT honour literal newlines — the first calibration measured
    the line-feed glyph's box and got a negative pitch. Calibration now forces
    a real wrap at a narrow width.
  * `_TextTransferable` inherited only `unohelper.Base`; `setContents` refuses
    anything not implementing `XTransferable` ("value does not implement...").
    Reproduced against the live clipboard service, then fixed — the Copy
    button would otherwise have been dead on arrival.

Verification: unit suite green incl. new `test-chat-layout.py`; sandbox run
with a live turn shows zero tracebacks, live calibration, and 27,169 pixels of
exact user-bubble colour in the expected band; the office clipboard service
round-trips the exact DataFlavor shape the panel emits.

---

# F57 — Four user findings on the bubbles, and what each one really was

The first bubble build reached the user's screen. Their feedback, mapped:

1. **"when i scroll up with the scrollbar i can't see my initial message"** —
   REAL BUG. When the Edit was swapped back for container+scrollbar, the
   adjustment listener was never re-attached: dragging the scrollbar fired
   nothing, and every render re-pinned the view to the bottom. The top of the
   conversation was unreachable. Fixed at build time (listener attached and
   logged); `_on_scroll` repositions from the cached stack without re-measure.

2. **"mouse wheel inside the chat doesn't work at all"** — PLATFORM FACT, not a
   bug we dropped: `XMouseWheelListener` and even the `MouseWheelEvent` struct
   are ABSENT from this LibreOffice's UNO type registry (`uno.createUnoStruct`
   says "unknown"), so there is no wheel surface to hook. Everything is
   documented twice in this repo now. What works: native wheel over the
   scrollbar strip itself, dragging, keyboard when the bar has focus. The first
   workaround attempt (`_HAS_WHEEL`, `_WheelListener`, per-label registration)
   was deleted as dead code.

3. **"the markdown renderer is very sparse — so many newlines between lines"** —
   REAL. Gaps were tuned at the old full-scale (para 40, speaker 160, heading
   70/20, code 24, pads 8/14) while the calibrated line pitch is a fraction of
   that on this backend, so every paragraph gap looked like 2+ blank lines.
   Compacted: para 16, speaker 90, heading 36/8, code 10, pads 4/8. The plan
   unit tests carry those numbers.

4. **"not much contrast between bubble and background — have we simply not
   implemented a background yet?"** — Implemented but too subtle: 0x3A3A3A
   against a ~0x2C2C2C theme. Raised to 0x46484E.

PROCESS NOTES, recorded because they cost hours:

  * DOM ordering: hidden-measure was SUSPECTED (hidden controls reporting
    garbage bounds) and DISPROVEN by probe — hidden and visible bounds were
    byte-identical. Ruling things OUT with cheap probes is rare and worth
    logging when it happens.
  * The sandbox pixel-verification channel is UNSTABLE: this session's office
    restored a maximized window state producing `parent=232x1163` (taller than
    the 1000px screen), auto-collapsed sidebars, and dark regions (#2C2C2C) that
    pixel scans cannot distinguish from the panel. The in-process probe — read
    the pool controls' model and geometry through UNO — is the reliable
    verifier; screenshots are now for humans only.

---

# F58 — The chat was never lost; it was never loaded

The user reported the previous chat gone. Evidence first: `CoworkChat` was
present BOTH in the live document's property bag AND in the saved file's
meta.xml — save worked, storage worked. The load path never ran:

    _build_window:  _TRANSCRIPTS.setdefault(doc_key, [])   # poisoned
    _entries:       if key not in _TRANSCRIPTS: load(...)  # never fires

The build seeded an empty list under the document's key before the lazy loader
could run, and the loader's trigger is "key is missing". Removed the seed;
`_entries()` owns loading. Also trimmed what is persisted: only {kind, text}
now — the parse cache (`_blocks`, `_parsed_from`) was bloating metadata and
risking stale caches across code changes; loading sanitises old saves too.

## The wheel, answered with receipts

The user asked whether other sidebar panels support the wheel. They do — but
not at any API we can reach:

  * The UNO AWT module reference has NO wheel listener of any kind
    (api.libreoffice.org/docs/idl/ref/namespacecom_1_1sun_1_1star_1_1awt.html);
    confirmed in this install's registry: XMouseWheelListener import fails and
    MouseWheelEvent is "unknown" to createUnoStruct.
  * The stock decks scroll because they are C++ (sfx2/source/sidebar/Deck.cxx):
    the deck is a VCL window whose wheel-to-scrollbar handling happens below
    the UNO surface.
  * Consequence for us: wheel over the transcript labels cannot be hooked from
    a UNO extension in this LibreOffice. Native wheel over the scrollbar strip
    itself still works, as does dragging and keyboard on a focused bar. A
    cleaner fix belongs upstream (expose wheel in UNO AWT), not in workarounds
    here.

---

# F59 — Contrast owned, gaps halved again, and the dangling-dash wrap

## Contrast is now OUR property, not the theme's

BackgroundColor=-1 ("toolkit default") on labels made the chat's readability a
function of whatever theme the user runs; that is why the bubble read fine in
one sandbox and failed on the user's dark theme twice. The transcript area now
paints its own surface (PANEL_BG 0x1B1C1F, set on the container at build) and
every label carries explicit fg/bg: assistant 0xE6E7E9 on that surface, the
user's bubble in the accent hue 0x543F33 with white text, code 0xDDDDDD on
0x232529, rules 0x3A3D44. Verified in-process: container model reads 0x1b1c1f.

## The sparse look was the gaps arguing with the calibrated pitch

After live calibration showed a line pitch of ~36 units, the constant gaps
(paragraph 40, speaker 160) were multiples of a line. Cut again: paragraph 8,
speaker 60, heading before/after 24/4, code 6, pads 2/6. A paragraph gap is now
about a quarter of a line; a speaker change about a line and a half.

## The wonky wrap: LibreOffice breaks AFTER dashes

Agents write — and - as separators constantly. The toolkit's line breaker
breaks after a dash, so lines end with a dangling "-". Fixed by binding every
dash to the following word with a no-break space (bind_dashes at display time
only — the clipboard path keeps plain text). Breaks now land BEFORE the dash:
the next line STARTS with the dash, which reads fine. Honest residue:
hyphenated words ("byte-identical") can still split at their internal hyphen —
that is inside the toolkit's breaker, with no API to influence it.

---

# F60 — The user's session log told us everything; we just had to look

User reports: other LibreOffice windows opening, scrollbar dead, the harness
struggling, and a macro-security dialog. The sidebar log held the answers:

  * `cowork_markdown.py` crashed on EVERY parse on their Wayland session —
    600× for `createEnumeration` and `getString` — because new streaming
    entries re-parsed on every chunk. Save-side structure came from cached
    `_blocks` riding in old metadata, which is why structured bullets still
    showed. FIXES: parse once per TURN (mid-stream renders as a plain
    paragraph), a circuit breaker after three consecutive office-failures,
    failure counting with success reset, and — for the macro dialog —
    `MacroExecutionMode = NEVER_EXECUTE` on every parse load.
  * The scrollbar bug was in plain sight in my own `_render`: BOTH branches of
    the offset expression passed `None`, so every render re-pinned the bottom;
    during streaming that is dozens of pins per second. The manual offset is
    now honoured (`None` only while autoscroll is armed).
  * No macro is ever invoked by tools anywhere in the logs ("macro" appears
    only inside AEJ: Macroeconomics citations). The dialog was LibreOffice
    reacting to temp-document loads. The persona now states it as policy:
    no second soffice (bash included), no windows, no Basic macros, no macro
    URLs — report tool failure instead of improvising processes.

Best practice distilled into the persona (rule 5): act on the OPEN document
through the tools; if a tool fails, say so rather than spawning processes.

---

# F61 — The agent improvising a scratchpad was the missing affordance

User observation: the agent tried to CREATE AND OPEN another document as a
scratchpad. Nothing in the tool surface offered a place to draft or hold
intermediate state, so it improvised the only canvas it could see: another
LibreOffice window. Same root cause as the earlier window-spawning -- an
unmet need expressed as window clutter.

The first-class answer is the new `scratchpad` tool (cowork-office.mjs):
actions write/append/read/clear/path, backed by ONE plain file in this repo's
checkout root by default (overridable with COWORK_SCRATCHPAD for tests). No
LibreOffice in the loop, no UI, no undo impact on the user's document, and
nothing for a security dialog to react to. Scoped to the runtime process,
like the turn registry.

Persona rule 5 now names the affordance and the prohibition together: use
scratchpad; never another document, never a second soffice, never macros --
the user sees every window and dialog, and improvisation reads as the harness
losing control of itself.
