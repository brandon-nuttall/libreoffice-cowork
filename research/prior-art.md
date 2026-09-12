# Prior art: interactive AI agents inside LibreOffice

**Scope.** Nine named repositories plus adjacent projects, researched to answer: how do you connect an *external agent process* to a **running LibreOffice** and let it read/edit the **live document the user is looking at**?

**Method.** GitHub REST API (repo metadata + `git/trees?recursive=1`) for structure/counts, then `raw.githubusercontent.com` for every file quoted. No `web_search` was available in this environment. Plus **first-hand experiments on this machine** (LibreOffice 26.2.5.2, `python3` with `uno` from `python3-uno`) — see §9.

**Evidence labels.** `VERIFIED` = read in source/README (short excerpt or path given); `VERIFIED (local)` = reproduced on this machine; `INFERENCE` = reasoning not directly stated by the source. Line references are to the revision fetched during this research (2026-09-12).

**Headline.** No project connects an external agent CLI to LibreOffice over MCP into a *live GUI* window. The projects split cleanly into four archetypes, and only one of them (B) is both live-GUI and agent-driven:

| # | Archetype | Live GUI doc? | External agent? | Examples |
|---|---|---|---|---|
| **A** | **File-level handoff** — agent CLI edits the saved file on disk; LibreOffice reloads | yes, but by *reload*, not by API | yes, native CLI agents | **LibreAssist** |
| **B** | **In-office UNO acceptor + MCP server** — extension opens a UNO pipe/socket; an external MCP client speaks URP | **yes** | yes, any MCP client | **Claude-Connector**, **mcp-libre** (HTTP variant) |
| **C** | **External MCP server + UNO socket** — MCP server attaches to a *separate, usually headless* soffice | no (or incidental) | yes, any MCP client | **WaterPistolAI** |
| **D** | **In-extension LLM client** — no agent process; the extension calls an LLM HTTP API itself | **yes** | no (a prompt→text→write loop) | **localwriter**, **LibreThinker**, **Bielik** |
| **E** | **No native LibreOffice at all** — LibreOffice compiled to WASM in an Electron webview | **yes** (canvas) | yes, but over SSE+IPC, not MCP | **aiworkdeck** |
| — | **File-format engine** — reads/writes ODP/OOXML directly; `soffice` only for conversion | no | yes, MCP stdio | **PPT-Master** |

---

## 0. Repository facts at a glance

All `VERIFIED` via `https://api.github.com/repos/OWNER/REPO` on 2026-09-12.

| Repo | ★ | Lang | Branch | License | Created → pushed | Size | Archetype |
|---|---|---|---|---|---|---|---|
| `NikolaiRadke/LibreAssist` | 10 | Python | `main` | Apache-2.0 | 2026-02-20 → 2026-07-23 | 810 KB | A |
| `swe-sanad/LibreOffice-Claude-Connector` | 3 | Python | `master` | MIT | 2026-07-11 → 2026-09-10 | 1.2 MB | **B** |
| `TANGZHUO12/PPT-Master` | 2 | Swift+Python | `main` | MIT | 2026-09-07 → 2026-09-07 | 273 KB | file-format + DSH |
| `balisujohn/localwriter` | **178** | Python | `master` | MPL-2.0 (in README) | 2024-07-24 → 2026-02-22 | 496 KB | D |
| `mihailthebuilder/librethinker-extension` | 40 | Python | `main` | MPL-2.0 text (GitHub: none) | 2025-12-08 → 2026-07-14 | 624 KB | D |
| `WaterPistolAI/libreoffice-mcp` | 26 | Python | `master` | **none** | 2025-05-23 → **2025-05-28 (dead)** | 61 KB | C |
| `jwingnut/mcp-libre` | 11 | Python | `main` | MIT | 2025-12-12 → 2026-09-10 | 27 MB (git history) | B/D hybrid |
| `zeweihan/aiworkdeck` | 140 | Java+Vue | `master` | AGPL-3.0 | 2026-01-21 → 2026-09-11 | 274 MB | E |
| `dandi-91/LibreOffice-Bielik-Agent` | 0 | Python | `main` | MIT | 2026-07-26 → 2026-07-26 | 492 KB | D |

---

## 1. `NikolaiRadke/LibreAssist` — "Agentic Working with LibreOffice"

**Archetype A: the CLI agent edits the document *file*; LibreOffice reloads.** This is the highest-concept-fit project for the user's brief (Claude Code *and* Codex *and* a third CLI) and it uses **neither MCP nor a socket nor UNO for the agent link**. It is a filesystem contract.

### 1.1 What it does (VERIFIED, `README.md`)
A sidebar chat panel in Writer/Calc/Impress/Draw/Math. You type a request; the extension spawns a CLI agent in the document's directory and the CLI **edits the .odt/.ods/.odp file directly with its own file tools**. Documented features: multi-provider (Claude Code CLI, Codex CLI, Mistral Vibe CLI), opt-in Track Changes display, session-based conversations (provider session IDs persisted per document), direct document manipulation "even design elements", a full file-backup Undo/Redo, per-document persistent chat history, 5 locales, configurable timeouts.

Provider prefix routing is in-prompt: `claude Write an executive summary`, `codex Fix all typos`, `mistral Change background color to blue`.

### 1.2 Architecture (VERIFIED)
`.oxt` containing a Python UNO component registered as a **`com.sun.star.task.Job`**:

`src/main.py`:
```python
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    ElementFactory, "org.libreoffice.libreassist.LibreAssistFactory", ("com.sun.star.task.Job",))
```
`src/META-INF/manifest.xml` registers `main.py` (Python UNO component), `pythonpath/` (UNO pythonpath — the `libreassist` package), `Sidebar.xcu`, `Factory.xcu`, 5 locale JSONs, icons, `providers.json`.

Sidebar wiring (the standard four-file dance):
- `src/Factory.xcu`: `Registered/UIElementFactories/LibreAssistPanelFactory` → Type `toolpanel`, Name `LibreAssistFactory`, FactoryImplementation `org.libreoffice.libreassist.LibreAssistFactory`.
- `src/Sidebar.xcu`: Deck `LibreAssist`/`LibreAssistDeck` + Panel `LibreAssist_MainPanel` with `ImplementationURL = private:resource/toolpanel/LibreAssistFactory/LibreAssistPanel`, `ContextList` for **Writer, Calc, Impress, Draw and Math** under one deck.

### 1.3 The agent link — pure subprocess + mtime (VERIFIED, the key finding)
`src/pythonpath/libreassist/core.py::callLLMAsync()`:

1. **All UNO access happens first, on the main thread.** `doc.store()` is called to force the file to disk; a backup copy is made; `modTimeBefore = os.stat(fullPath).st_mtime` is captured; `os.listdir(directory)` is snapshotted; the `XFrame` is captured and *named* if unnamed.
2. A **`threading.Thread(daemon=True)`** then runs a subprocess via `provider_base.executeProvider()` with `cwd = the document's directory`.
3. The prompt tells the CLI what to do, in prose:
```python
basePrompt = (
    f"You have access to {filename} in the current directory. "
    f"This is a {os.path.splitext(filename)[1]} file. "
    f"User request: {userPrompt}. "
    "IMPORTANT: Write your response directly into the document by editing the file, "
    "UNLESS the user is asking a pure information question ..."
    "For content creation, editing, or writing tasks, always modify the document directly. "
    "If the user explicitly asks for a separate or new document, create a new "
    "ODF file (e.g. .odt) in the current directory instead of editing this one. "
    "Response format: Plain text only, no Markdown.")
```
4. After the subprocess exits, change detection is **`os.stat(fullPath).st_mtime != modTimeBefore`**, and newly created documents are `set(os.listdir(dir)) - dirBefore` filtered to `.odt/.ods/.odp/.odg`.
5. Back on the main thread through **`com.sun.star.awt.AsyncCallback`**:
```python
ctx = uno.getComponentContext()
asyncCb = ctx.ServiceManager.createInstance("com.sun.star.awt.AsyncCallback")
...
asyncCb.addCallback(completionCallback, None)
```

### 1.4 Provider adapters (VERIFIED)
`src/providers.json` is the registry — a CLI **executable name** plus flags:
```json
{ "claude_code":  {"executable": "claude", "alias": "claude", "needs_nodejs": false, "post_process": false},
  "codex_cli":    {"executable": "codex",  "alias": "codex",  "needs_nodejs": true,  "post_process": false},
  "mistral_vibe": {"executable": "vibe",   "alias": "mistral","needs_nodejs": false, "post_process": true} }
```
`providers/claude_code.py`:
```python
args = [executable, "--verbose", "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--include-partial-messages"]
if sessionId: args.extend(["--resume", sessionId])
args.append(prompt)
```
`extractResponse()` parses newline-delimited `stream-json`: `type=="assistant"` → concatenate `message.content[].text`; `type=="result"` → capture `session_id`. Codex: `codex exec --skip-git-repo-check --json --dangerously-bypass-approvals-and-sandbox <prompt>`, parsing `item.completed` events.
`discovery.py` locates the executables via `shutil.which`, npm global prefixes (`npm config get prefix`), `~/.nvm/versions/node/*/bin`, Windows `%APPDATA%\npm`, and nvm-windows. Codex is run as `node <codex-js> ...`.

**It is the CLI's own cwd that supplies document context** — not a tool surface, not a prompt-stuffed document. The agent gets the whole file with its normal Read/Edit tools.

### 1.5 Track Changes and Undo (VERIFIED)
- **Track Changes**: `ui/events.py` after a modified file — dispatch `.uno:Reload`, then, if Writer and the setting is on:
```python
prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
prop.Name = "URL"; prop.Value = uno.systemPathToFileUrl(backupPath)
dispatcher.executeDispatch(frame, ".uno:CompareDocuments", "", 0, (prop,))
```
i.e. LibreOffice's own **document-compare** feature produces the redlines, because the CLI wrote a whole new file.
- **Undo/redo** is *file-level backup*, not `XUndoManager`. State lives under the LibreOffice user profile (`com.sun.star.util.PathSubstitution` → `user` → `libreassist/`), per document hashed into a settings dir; `undo_available`, `redo_available`, `last_action` (`edit`|`create`|`none`), `created_files` are persisted. Undo = `shutil.copy2(backupPath, fullPath)` then reload.
- Provider sessions and timeouts are stored per document directory: `settingsData["session_ids"][providerName]`, `settingsData["timeout"]` (default 600 s).

### 1.6 Documenting its own limits (VERIFIED, `README.md`)
> "Mistral Vibe ... ⚠️ Experimental: Mistral Vibe does not natively support LibreOffice files. LibreAssist works around this via automatic ODT post-processing, which may not always produce correct results. **Files can be corrupted.**"
> "The following providers were tested but **cannot edit LibreOffice .odt files**: Gemini CLI, Groq Code."

Also: "**Save the document first** (required for AI providers to access the file)" and "Wait for the AI to process (may take 1-2 minutes)" — the workflow cost of the file-handoff model.

### 1.7 Reusability
- **High**: the exact shape of a *live-GUI, external-agent* integration; the "all UNO before the thread, `AsyncCallback` after" discipline; per-document state; CLI discovery; the `stream-json` parsers; the Track-Changes-via-`.uno:CompareDocuments` trick.
- **Not reusable for a cowork-style agent**: there is no tool surface. The agent cannot ask "what does the user have selected?", cannot make a *targeted* edit, cannot see unsaved changes, and every turn is a full-file rewrite (corruption risk, formatting loss, catastrophic diff). The README's own "1–2 minutes per turn" is the cost.

---

## 2. `swe-sanad/LibreOffice-Claude-Connector` — 220-tool MCP server + pipe acceptor

**Archetype B, and the most sophisticated prior art found.** Repo description says "161-tool"; the registry has **220 tools, 74 advertised by default** (`docs/MCP-TOOLS.md`, generated from the live registry). Single author, ~2 months of intensive work, 16 PRs, CI release automation.

### 2.1 TRANSPORT — an in-office UNO **named-pipe** acceptor (VERIFIED)
There is **no HTTP anywhere** and **no raw Python socket**. Two hops:

**Hop 1 — Claude ↔ MCP server: MCP JSON-RPC 2.0 over stdio**, newline-delimited JSON, in `mcp/libreoffice_mcp.py` (~240 lines, protocol only):
```python
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    response = handle(json.loads(line))
    if response is not None: sys.stdout.write(json.dumps(response) + "\n")
```
It must run under **LibreOffice's bundled Python** (the only interpreter shipping `uno`). `sys.stdin/stdout.reconfigure(encoding="utf-8")` is forced (cp1252 broke Arabic sheet names).

**Hop 2 — MCP server ↔ LibreOffice: the real UNO URP bridge over a per-user named pipe**, opened *from inside the office process* by the extension. `src/agent_acceptor.py` (186 lines):
```python
acceptor = smgr.createInstanceWithContext("com.sun.star.connection.Acceptor", ctx)
bridges  = smgr.createInstanceWithContext("com.sun.star.bridge.BridgeFactory", ctx)
accept_str = "pipe,name=%s" % pipe_name
while True:
    conn = acceptor.accept(accept_str)              # blocks
    if conn is None: return                         # stopAccepting() during shutdown
    bridges.createBridge("", "urp", conn, provider) # fresh anonymous bridge per connection
```
- Pipe/`Job` entry point: `AgentAcceptorJob(unohelper.Base, XJob, XServiceInfo)`, `IMPL_NAME = SERVICE_NAME = "com.swepioneers.claudeconn.AgentAcceptor"`. `execute()` **never raises** (a raising Job is permanently deactivated by the Jobs framework). Idempotent under `threading.Lock`; daemon thread `claude-agent-acceptor`.
- Shutdown safety: `_Terminator(unohelper.Base, XTerminateListener)` never vetoes termination and calls `acceptor.stopAccepting()` so the blocking `accept()` can never keep `soffice.bin` alive.
- Pipe name: `"lo-claude-" + re.sub(r"[^a-z0-9-]", "-", getpass.getuser().lower())` → e.g. `lo-claude-sanad`. Duplicated by design in `src/agent_acceptor.py::default_pipe_name()` and `mcp/loconn/core.py::_default_pipe_name()` with a "MUST stay identical" comment.
- `ext/Jobs.xcu` binds the Job to **both** `OnStartApp` and `onFirstVisibleTask` ("onFirstVisibleTask never fires headless; OnStartApp covers it").
- `ext/META-INF/manifest.xml` registers 3 Python components (`connector.py`, `sidebar_panel.py`, `agent_acceptor.py`) + 5 configuration-data entries (`Jobs.xcu`, `UI/Sidebar.xcu`, `UI/Factories.xcu`, `Addons.xcu`, `ProtocolHandler.xcu`).

**Client-side 3-rung ladder** (`mcp/loconn/core.py::_connect()`): **(1)** named pipe `uno:pipe,name=<pipe>;urp;StarOffice.ComponentContext` (1 try — "reaches a LibreOffice the user opened normally, no flags"); **(2)** TCP `uno:socket,host=localhost,port=$LO_UNO_PORT;urp;StarOffice.ComponentContext` (default **2002**); **(3)** auto-launch `soffice --norestore --nologo --accept=socket,host=localhost,port=2002;urp;` (Windows `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`), retry ×30. The failure text names the real trap: *"Most likely another LibreOffice instance was already running WITHOUT a listener (single-instance swallows the new launch)."* `_state["transport"]` caches `"pipe"|"socket"`.

**Visible GUI by default**: `--headless` is inserted only when `LO_HEADLESS=1`. The MCP `initialize` response ships `SERVER_INSTRUCTIONS`:
> "You are editing documents that are open in LibreOffice on the user's own screen. Edits appear immediately and there is no separate commit step, so treat the document as the user's live work, not a draft you own."

Env vars: `LO_UNO_PORT` (2002), `LO_UNO_PIPE` (0/off skips the pipe rung), `CLAUDE_AGENT_PIPE`, `CLAUDE_AGENT_ACCEPTOR=0`, `LO_TOOLS` (`basic`|`full`), `LO_AUTOSTART=0`, `LO_HEADLESS=1`, `LO_SOFFICE`, `LO_CALL_TIMEOUT` (120 s), `ANTHROPIC_API_KEY`. Config at `%APPDATA%\LibreOffice-Claude-Connector\config.json`; the API key separately in `apikey.dpapi` (Windows DPAPI via `ctypes`→`crypt32`) or `apikey.plain` (base64, 0600, documented as *not* encryption). **No Windows registry keys at all** (verified negative).

### 2.2 Tool taxonomy — 220 / 74 advertised (VERIFIED, `docs/MCP-TOOLS.md`)
| module | tools | advertised |
|---|---|---|
| `calc_analysis` | 16 | 2 |
| `calc_data` | 28 | 9 |
| `calc_format` | 17 | 2 |
| `calc_sheets` | 16 | 2 |
| `shared_automation` | 16 | 3 |
| `shared_lifecycle` | 16 | 10 |
| `shared_properties` | 12 | 5 |
| `shared_recovery` | 5 | 5 |
| `writer_format` | 19 | 4 |
| `writer_structure` | 18 | 4 |
| `writer_tables` | 10 | 1 |
| `writer_text` | 21 | 7 |
| `impress` | 19 | 14 |
| `draw` | 7 | 6 |
| **total** | **220** | **74** |

Representative names: `writer_get_text`, `writer_replace_selection`, `writer_append_text`, `writer_insert_paragraphs`, `writer_find_replace`, `writer_page_map`, `writer_track_changes`, `writer_get_paragraphs`, `writer_apply_list`, `writer_insert_toc`, `writer_insert_table`, `writer_read_table`; `calc_read_range`, `calc_write_range`, `calc_create_pivot`, `calc_goal_seek`, `calc_autofilter`, `calc_conditional_format`; `impress_add_slide`, `impress_set_transition`, `impress_add_animation`; `draw_insert_connector`; `shared_lifecycle`: `list_documents`, `open_document`, `reload_document`, `set_active_document`, `document_undo`, `checkpoint_document`; `shared_automation`: `uno_exec`, `dispatch_uno`, `run_macro`, `run_python_macro`, `lo_screenshot`, `batch`. Registration is declarative with fail-fast validation (`mcp/loconn/registry.py::register()` raises `ImportError` on schema-without-handler, duplicate name, or a tier naming an unknown tool).

### 2.3 What the transport costs/earns (VERIFIED)
- **Undo**: every mutating tool call is wrapped in one `doc.getUndoManager().enterUndoContext("Claude: <tool>")`, so each tool call collapses to one Ctrl+Z.
- **Timeouts**: each tool runs on a daemon thread `lo-tool-call`; on expiry the cached bridge is dropped and a `ToolTimeout` raised with: *"LibreOffice did not answer … It is almost certainly showing a dialog that is waiting for a person … A UNO call waiting on a modal LibreOffice dialog never returns and cannot be interrupted from Python."*
- **Reconnect**: substring markers (`"urp bridge"`, `"disposedexception"`, `"connection refused"`, `"broken pipe"`, …) → reset + **retry exactly once**; a genuine tool error is not retried (`mcp/test_reconnect.py` asserts both).
- **Errors**: `_classify_error()` → `{code, error_type, retryable, message, hint}`; `tools/call` returns two content blocks (human narration + JSON) so a watching CLI shows what happened.

### 2.4 Two products in one repo (VERIFIED — corrects a natural assumption)
The extension does **not** launch Claude Code/Codex. Grepping `subprocess|pty|Popen|spawn(` across `src/`, `mcp/`, `mcpb/` finds only (a) `Popen([soffice, …])` (launching LibreOffice) and (b) `mcpb/index.js` spawning the Python MCP server. Instead:
- **(a)** an independent **in-extension direct-API path** — `connector.py` ProtocolHandler commands (`Transform`, `Summarize`, `Translate`, `FixGrammar`, `GenerateFormula`, `ExplainRange`, `Settings`) dispatch URLs `com.swepioneers.claudeconnector:*`, calling Anthropic or any OpenAI-compatible endpoint (`src/claude_client.py`, `src/providers.py`, `DEFAULT_OPENAI_BASE_URL = "http://localhost:11434/v1"`) with stdlib `urllib` only, on a worker thread behind a **modal AWT progress dialog**, marshalling back via `AsyncCallback`;
- **(b)** the acceptor — the *inverse* direction, which is what makes MCP work on a flag-free office.

### 2.5 Reported limitations (`docs/KNOWN-GAPS.md`, 28 KB of field reports) — VERIFIED quotes
- **Implicit active document**: *"All `writer_*`/`calc_*` tools act on the implicit active document (`desktop.getCurrentComponent()`). Mid-build the source doc closed and an already-open Calc file grabbed focus; the very next `writer_append_text` died … **This is the single biggest Writer-session hazard: any user click or background doc event silently redirects writes.**"* Fixed with `set_active_document` + explicit targets.
- **Undo, upstream hole, "Not worked around"**: table verified on 25.2.3.2 — `setString` reverts, property sets revert, `enterUndoContext` grouping works, but **`setDataArray` and `setFormulaArray` create an Undo entry that does not revert the cells**. `SERVER_INSTRUCTIONS` warns the model: *"LibreOffice does NOT record bulk cell-range writes for undo, so Ctrl+Z will not bring back data that calc_write_range overwrote — a checkpoint is the only way back."*
- **Threading**: *"UNO document APIs are not safe to call from arbitrary threads."*
- **Packaging**: *"Extensions activate on the NEXT LibreOffice start, not the install-time one"*; the four-file sidebar agreement (*"or the deck silently never appears"*); *"a panel that doesn't implement `getHeightForWidth` gets zero height"*; the sidebar creates the panel while its parent window is 0×0 so one-shot layout yields invisible controls; `-env:UserInstallation=` can be silently dropped on Windows.
- **What does not work**: HTTP/Streamable-SSE transport (*"the single most impactful item"*), document-type filtering of `tools/list`, `capabilities.tools.listChanged`, LibreOffice **Base**, `pdf_merge`, streaming, publish to extensions.libreoffice.org.
- **Security**: *"Everything the user can do in LibreOffice … There is no capability sandbox — the escape hatch (`uno_exec`, `run_macro`) is an intentional feature of the product."*
- **Platform**: Windows (full), macOS/Linux (all tools except `lo_screenshot`); 0 open issues; exactly one real issue ever (author-filed, author-closed).

### 2.6 `docs/COMPETITOR-STUDY.md` — third-party prior art it surveyed (VERIFIED excerpts)
- **`quazardous/nelson-mcp`** (~45k LOC): **HTTP-in-extension** transport; measured tool-tiering numbers on a live Writer session (94 tools/17,107 tokens default vs `minimal` 8/1,821 = 11%); uses **`setDataArray` zero times** (hence no undo hole); per-tool **`mutates`** flag; `tool.doc_types` filtering; `instructions` in `initialize`; `capabilities.tools.listChanged`.
- **`KeithCu/writeragent`** (~80k LOC, 1,531 files): **HTTP-in-extension + stdio**; vendored deps are five pure-Python packages, with the data-science layer as a **standalone Docker sidecar** POSTed at `/v1/execute`.
- Others named: `sandraschi/libreoffice-mcp`, `patrup/mcp-libre`, `WaterPistolAI/libreoffice-mcp`, `balisujohn/localwriter` (*"**The `.oxt` template to copy.**"*), `aronweiler`/`devilish84` libre-ai (keys in OS keychain — "copy this"), `mostlyblocks/CalcuLLM`, `smonux/libreoffice-llm-plugin`, LibOCon-2023 "AI assistant with ChatGPT" (*"Uses exactly `uno`+`urllib`+`json`"*), and `LibreOfficeAICopilot` (*"hardcoded its key — the anti-pattern to avoid"*).

### 2.7 Reusability
**The single most reusable artifact in this whole survey.** Named-pipe UNO acceptor ≈ 186 lines, no user flags, no ports, shutdown-safe, per-user. Plus: request framing reuses URP (no bespoke protocol), per-tool undo contexts, timeout-with-bridge-drop for modal-dialog hangs, single-retry reconnect classification, tool tiering, and the honesty of `KNOWN-GAPS.md`. **Costs to plan around**: it lives inside LibreOffice's bundled Python (stdlib only — no third-party libs in-process), it is Windows-verified, and its 220 tools are a large context bill.

---

## 3. `TANGZHUO12/PPT-Master` — DSH embedded in a native macOS app (most direct DSH precedent)

Two forms in one repo: `plugin/` (the MCP server, usable standalone as a DSH plugin bundle) and `app/` (a SwiftUI macOS app). The `plugin/README.md` documents a **9-tool FastMCP server**; the top-level `README.md` documents the **DSH extension bundle** (`dsh-ppt-expert`). Both are accurate for their subtree.

### 3.1 What it does (VERIFIED, `README.md`, `docs/项目文档.md`)
A macOS app "PPT大师" where you type one request in Chinese and get a finished animated deck. It embeds **three vendored runtimes** and drives them as separate processes.

### 3.2 How DSH is embedded (VERIFIED, `app/Sources/PPTMaster/Supervisor.swift`)
A Swift `Supervisor: ObservableObject` **spawns the `dsh` CLI once per user request** and streams stdout — DSH is used exactly as a CLI process, not as a library:
```swift
static var dsh: (exe: URL, prefix: [String]) {
    let node = root.appendingPathComponent("vendor/node/bin/node")
    let binJS = root.appendingPathComponent("vendor/node/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
    if FileManager.default.fileExists(atPath: node.path), FileManager.default.fileExists(atPath: binJS.path) {
        return (node, [binJS.path])          // bundled Node runs DSH's bin.js
    }
    return (URL(fileURLWithPath: "/opt/homebrew/bin/dsh"), [])
}

var dshArgs = dshPrefix + ["--profile", "ppt-agent"]
if let sid = task.sessionDirName, !sid.isEmpty, task.flow.count > 1 {
    dshArgs += ["--resume", sid, "--"]        // multi-turn memory via session resume
}
dshArgs.append(fullPrompt)
p.arguments = dshArgs
p.currentDirectoryURL = workDir
...
env["DSH_HOME"]    = Self.dshHome.path        // Application Support/PPT大师/dsh-home
env["DEEPSEEK_API_KEY"] = AppSettings.shared.apiKey
env["PPTMASTER_RESOURCES"] = Runtime.root.path
env["PPTMASTER_SOFFICE"]   = Runtime.soffice.path
```
Notable, directly reusable details:
- **`DSH_HOME` redirection.** The app owns its DSH root (`Application Support/PPT大师/dsh-home`), so profiles/settings/sessions are app data and `~/.dsh` is untouched. A bundle-baked persona profile is `rsync`'d in on first launch / app upgrade, gated by a **version stamp = hash of `cordis.patch.yml`**, and `__DSH_HOME__` placeholders inside the patch are rewritten to the real path.
- **First-run `settings.yaml`** is generated with `agent-default-model: {provider: deepseek-official, model: deepseek-v4-flash, reasoningEffort: high}`.
- **Session continuity** is `dsh --resume <sessionId> -- <prompt>`; the app watches the session log directory for the process/tool stream and builds the UI timeline from it (`SessionLogWatcher`, `zcat_jsonl.py`).
- **The profile is `ppt-agent`**: `cordis.patch.yml` with an inlined slide-expert persona (`system-prompt`), an `mcp-libreoffice` bridge row, permissions `danger-full-access` + no approval, and a local override of `dsh-headless` startup (~40 lines).
- **MCP bridge**: the patch declares an MCP server launching `~/.dsh/ppt-expert/bin/mcp-launch` → vendored CPython → `mcp_server.py`. `README.md` warns: *"zero npm dependencies: the `@deepseek-ai/dsh-mcp-client` referenced by the patch entry is resolved from the dsh install (healProfilesModuleFallback) — do **not** declare npm dependencies for this bundle; flat install shadows dsh-internal module versions (API breakage, measured)."*

### 3.3 The 9 MCP tools (VERIFIED, `plugin/src/mcp_server.py`, `FastMCP(..., version="2.1.0")`)
```python
@mcp.tool() impress_document   # new / open (.odp/.pptx/.ppt) / save / save_as / close / info
@mcp.tool() impress_slide      # add / delete / info / count / background / notes_set / notes_get / audit
@mcp.tool() impress_shape      # add / add_image / delete / list / fill / line / rotate / rename
@mcp.tool() impress_text       # set / read / format / inspect
@mcp.tool() impress_transition # set (14 transitions) / advance (click|auto)
@mcp.tool() impress_animation  # add (with delay_s) / list / clear / catalog (145 presets)
@mcp.tool() impress_export     # pdf / png (any page) / pptx
@mcp.tool() table_read         # xlsx/xls/ods/csv → structured data + type inference
@mcp.tool() chart_make         # 18 business chart types → PNG
```
Errors are always wrapped as `{"ok": false, "error": ...}` because *"v1 taught us: throwing raw engine exceptions at the MCP framework turns into gibberish like `content.xml`"*.

### 3.4 How LibreOffice is used — **not UNO at all** (VERIFIED, the key finding)
`plugin/README.md` (translated):
> "Pure Python implementation (plan C): reads/writes ODP (**ZIP + XML**) directly with `zipfile + ElementTree`, **no dependency on pyuno / UNO / Basic macros**. The `soffice` command line is used **only for format conversion** (pptx→odp, odp→pptx/pdf/png)."

- Editing is done by `odp_engine.py` (~104 KB) manipulating `content.xml` inside the `.odp` zip; the model is addressed by slide index and shape name; 145 animation presets and 14 transitions are written as spec-correct `anim:`/`smil:` XML (the README's v2 fix list is a catalogue of OOXML/ODF encoding traps found by round-trip testing).
- **Conversion only** via the CLI, always with an isolated profile:
  `soffice -env:UserInstallation=file://<tmp>` — *"otherwise it fights over the lock with the user's open LibreOffice"* (documented known pitfall #3).
- **Live preview** is *file-watch*, not live-document: the agent is instructed to `save_as` every completed page to a fixed scratch path; an Electron-style preview server converts the deck to PDF via headless soffice and renders pages, auto-advancing to the page the agent is working on (`127.0.0.1:18465`, exits after 15 min idle; `PPT_PREVIEW=0` disables). `render_page.py` calls `odp_engine.render_thumbnail` directly.
- **Documented known limitation**: *"soffice pptx conversion loses element animations: `--convert-to pptx` keeps only slide transitions, element-level animations are not exported (a known LibreOffice behaviour)"* → deliverables are saved as both `.odp` (animations intact) and `.pptx`.
- The `chart_make` path **abandoned** its v1 LibreOffice pipeline (job file → soffice macro → Calc → PDF → Draw → PNG, 5–10 s per chart) for **matplotlib Agg** (40–80 ms per chart); the v1 code is retained only for rollback.

### 3.5 Reusability
**Highest strategic value for this project** — it is a working example of DSH embedded in a host application, with `DSH_HOME` isolation, bundle-baked profiles, `--resume` multi-turn, MCP bridging via `cordis.patch.yml`, structured error envelopes, and a documented failure catalogue. **But it never touches a live LibreOffice document**: it is a *file-format engine plus headless converter*, chosen deliberately for determinism ("plan C"). Its live preview is a file-watch PDF re-render, i.e. it solves the "user sees progress" problem **without** UNO.

---

## 4. `balisujohn/localwriter` — the most popular LibreOffice AI extension (178★)

**Archetype D: no agent process; a menu command calls an LLM HTTP API and writes into the live document.** Small (634-line `main.py` + a 6 KB `pythonpath/llm.py`) and therefore the clearest reference for the *extension mechanics*.

### 4.1 What it does (VERIFIED, `README.md`)
Two commands + settings, exposed as a top-level **`localwriter` menu**, `Tools ▸ Extension Manager` install, `.oxt`:
- **Extend Selection** (`Ctrl+Q`) — the LLM continues the selected text; the completion is **appended to the selection as it streams**.
- **Edit Selection** (`Ctrl+E`) — a dialog asks for instructions; the selection is **replaced** by the model's rewrite.
Backends: `ollama` (default `http://localhost:11434`) or `text-generation-webui`'s OpenAI-compatible API, plus any OpenAI-compatible endpoint.

### 4.2 Extension architecture (VERIFIED)
Real `.oxt` (`description.xml`: identifier `org.extension.localwriter`, version 0.0.9). `META-INF/manifest.xml` registers **two** Python UNO components (`main.py`, `prompt_function.py`), an **RDB type library** `XPromptFunction.rdb` (`uno-typelibrary;type=RDB`), plus `Addons.xcu`, `Accelerators.xcu`, **`CalcAddIn.xcu`**.
- `Addons.xcu`: menu `org.extension.sample.menubar`, title `localwriter`, `Context` = `SpreadsheetDocument, GlobalDocument, TextDocument, WebDocument`; three items whose URLs are the service-protocol form:
```xml
<prop oor:name="URL"><value>service:org.extension.sample.do?ExtendSelection</value></prop>
```
(then `?EditSelection`, `?settings`).
- `Accelerators.xcu` binds Ctrl+Q / Ctrl+E; `CalcAddIn.xcu` + `idl/XPromptFunction.idl` expose an in-sheet function, built via `unoidl-write` in `build.sh` (which is why the extension can be a spreadsheet formula).
- Entry point is `class MainJob(unohelper.Base, XJobExecutor)` whose `trigger(self, args)` switches on `args` (`"ExtendSelection"`, `"EditSelection"`, `"settings"`).
- **No sidebar.** Its open issue #5 "[Feature Request] Quick Chat Sidebar" is the most-commented open request — evidence that a menu-only surface is a felt limitation.
- Config: JSON at `PathSettings.UserConfig` → `localwriter.json` (macOS `~/Library/Application Support/LibreOffice/4/user/`, Linux `~/.config/libreoffice/4/user/`, Windows `%APPDATA%\LibreOffice\4\user\`). ~11 settings: endpoint URL, model, API key, `api_type` (`completions`|`chat`), OpenWebUI flag, OpenAI-compat flag, disable-SSL-verify, max tokens and system prompts for each command.
- **Dev workflow worth copying**: `unopkg add /path/to/localwriter/` registers the *source directory* so edits need only a LibreOffice restart — no repackaging.

### 4.3 Provider abstraction (VERIFIED, `pythonpath/llm.py` — deliberately UNO-free)
`llm.py` is a pure-Python module ("no UNO dependencies") exporting `as_bool`, `is_openai_compatible`, `build_api_request`, `extract_content`, `make_ssl_context`, `stream_response`. Endpoint shaping:
- `api_type == "chat"` → `POST {endpoint}{/api|/v1}/chat/completions` with `{messages, max_tokens, temperature: 1, top_p: 0.9, stream: true}`.
- `api_type == "completions"` → `POST …/completions` with the system prompt flattened into the prompt (`SYSTEM PROMPT\n…\nEND SYSTEM PROMPT\n…`), plus `seed: 10` for non-OpenAI-compatible servers.
- OpenWebUI detection rewrites `/v1/`→`/api/`; `urllib.request` only (no `requests`).
- `stream_response()` iterates the response object line by line, parsing `data: ` SSE frames, calling `append_callback(content)` **and `on_idle()` per chunk**.

### 4.4 Text insertion/replacement — the part most worth studying (VERIFIED, `main.py::trigger`)
```python
if hasattr(model, "Text"):
    text = model.Text
    selection = model.CurrentController.getSelection()
    text_range = selection.getByIndex(0)

    if args == "ExtendSelection":
        prompt = text_range.getString()
        request = self.make_api_request(prompt, system_prompt, max_tokens, api_type=api_type)
        def append_text(chunk_text):
            text_range.setString(text_range.getString() + chunk_text)
        self.stream_request(request, api_type, append_text)

    elif args == "EditSelection":
        prompt = ("ORIGINAL VERSION:\n" + text_range.getString() + "\n Below is an edited version according "
                  "to the following instructions. ... \nEDITED VERSION:\n")
        request = self.make_api_request(prompt, system_prompt, max_tokens, api_type=api_type)
        text_range.setString("")                       # clear first
        def append_text(chunk_text):
            text_range.setString(text_range.getString() + chunk_text)
        self.stream_request(request, api_type, append_text)
```
Properties of this approach, all consequences of `XTextRange.setString`:
- **It does edit the live document** — the user watches text appear as it streams. This is genuinely the "cowork" feel.
- **`setString` is a plain-text write**: it does **not** use `XText.insertString`, `XTextCursor`, `XReplaceDescriptor` or `XSearchable`, and it carries **no formatting**. Confirmed by open issue **#43**: *"When AI-generated text replaces a selection, it currently comes in as plain text, stripping any formatting (font, fontsize, fontcolor, bold/italic, paragraph style, text-alignment, etc.) from the original."*
- **No Undo integration** — grep finds no `XUndoManager`/`enterUndoContext`. Undo granularity is whatever `setString` produces.
- **Live-stream rewrite is O(n²)**: every chunk re-reads and re-writes the whole range string.
- **Calc path mirrors it per cell** (`cell.setString(cell.getString() + chunk_text)`), iterating the range row-major — issue #7 "[BUG] Answer not complete" (responses truncated mid-sentence) is consistent with hitting `max_tokens = len(original) + max_new_tokens`. The prompt even contains an explicit anti-thinking instruction for the Calc path: *"Don't waste time thinking, be as fast as you can."*
- **Table/shape limitations** are known: issue #4 "Doesn't work in table" (closed), issue #12 "[Feature Request] Support for Text-boxes and Shapes" (open).

### 4.5 Reported failure modes / limitations (VERIFIED, `/issues?state=all` — 31 issues)
| # | Title | State |
|---|---|---|
| 3 | Please support Ollama or OpenWebui (22 comments) | closed |
| 37 | Installs but no menu entry / no options (LO 25.8.4.2, Fedora 43, Wayland) — *"no trace in user extensions folder"* | closed |
| 5 | **[Feature Request] Quick Chat Sidebar** | **open** |
| 2 | localwriter menu not showing | closed |
| 42 | *"localwriter not visible in tabbed UI mode"* | open |
| 7 | **[BUG] Answer not complete** — *"answers often are not complete and stop in the middle of the sentence. That is independent of the used model."* | closed |
| 4 | Doesn't work in table | closed |
| 43 | **Keep original text formatting after AI replacement** | **open** |
| 9 | Streaming response in the Editor itself / UI bug | open |
| 20 | UI Issues | open |
| 33 | *"I click extend selection. My CPU fan spins up and windows screen recorder dies"* | open |
| 12 | Support for Text-boxes and Shapes | open |
| 45 | Support llama.cpp / 24 model-selection-instead-of-manual-input / 15 custom headers / 16 configurable shortcuts / 11 right-click context menu | open |

**Recurring themes**: (1) *menu/deck not appearing* is the #1 install failure class (#37, #2, #42, #29) — with tabbed UI, Wayland, and "no trace in the user extensions folder" as specific triggers; (2) **formatting loss on replace**; (3) **truncated output**; (4) **no sidebar/chat surface**; (5) **no cancellation**; (6) **CPU/paint blowup** from synchronous streaming writes.

---

## 5. `mihailthebuilder/librethinker-extension` — "AI Copilot for LibreOffice Writer" (40★)

**Archetype D with a proper sidebar.** Distribution/UX reference: 8 releases, latest `0.2.16`, `.oxt` 44 KB, 223 downloads, and the docs are honest.

### 5.1 Architecture (VERIFIED)
`.oxt`, four manifest entries: `src/LibreThinker.py` as a Python UNO component + `Factory.xcu`, `ProtocolHandler.xcu`, `Sidebar.xcu`.
- Deck `LibreThinker` / `LibreThinkerDeck`, `ContextList = Writer, any, visible ;`; panel `Panel_Panel1`, `ImplementationURL = private:resource/toolpanel/LibreThinkerExtension/Panel1`, `DefaultMenuCommand = org.librethinker:Panel1`, `OrderIndex=700`.
- Sidebar classes: `XUIElementFactory` (`ElementFactory`) and `XUIElement + XToolPanel + XSidebarPanel + XComponent` (`XUIPanel`); panel content is a **dialog** hosted via `com.sun.star.awt.ContainerWindowProvider` on `vnd.sun.star.extension://…/empty_dialog.xdl`, with every control built in Python.
- `ProtocolHandler.xcu` claims `org.librethinker:*` for the panel title-bar "More options" command.
- Panel UI: multiline Prompt, Submit, radio **Selected text / Entire document** (default = **Entire document**), Save prompt, status, help/ko-fi links, and a BYOK block: Model ID (e.g. `claude-opus-4-6`), Model URL, masked API key, Save settings.

### 5.2 Provider layer (VERIFIED, `ui_logic/api.py`, `utils.py`, `settings.py`)
Two hand-written clients, routed by **model-id prefix** (`sh/…` → self-hosted; the older `sh/ollama/…` still works):
- **`LtClient`** — the vendor's **async job API** (`https://api.librethinker.com/api/v1/jobs/`): `POST jobs/free` (no signup) or `jobs/byok` (header `X-Third-Party-Key`), then poll `jobs/fetch`; jittered backoff 2→30 s; 180 s timeout; `CHARACTER_LIMIT = 110_000` characters enforced client-side.
- **`OllamaClient`** — one synchronous POST to `{modelUrl|http://localhost:11434/api/chat}`, `stream: false`, `Authorization: Bearer` if a key is set; **no timeout argument on `urlopen`**; `extract_answer()` accepts both `data["choices"][0]["message"]["content"]` and `data["message"]["content"]`, so LM Studio (`http://127.0.0.1:1234/v1/chat/completions`), llama.cpp and vLLM work. No streaming anywhere.
- Config: `LibreThinkerConfig.json` in `thePathSettings → UserConfig`; keys `modelId, apiKey, modelUrl, savedPrompt`; **API key stored in plaintext**.

### 5.3 Document interaction — and its clean failure (VERIFIED)
- "Entire document" read is **an export to a temp .txt, then read back**:
```python
document.storeToURL(file_url, (PropertyValue(Name="FilterName", Value="Text (encoded)"),
    PropertyValue(Name="FilterData", Value=(PropertyValue(Name="Encoding", Value="UTF8"),))))
with open(out_path, "r", encoding="utf-8-sig", errors="replace") as f: txt = f.read()
os.remove(out_path)
```
  (Loses all structure; tables/graphics/fields flattened or dropped.)
- Write is `text_range.setString(response.answer)` on the current selection — same primitive as localwriter, same consequences: **formatting loss** (issues #13, #6) and **no undo integration** (no `getUndoManager`/`XUndoManager`/`RecordChanges` anywhere).
- **Read scope ≠ write target**: with the default "Entire document" mode and an empty selection, `getByIndex(0)` is a zero-length caret range, so the rewrite is *inserted* at the cursor.
- **Threading**: `threading.Thread(target=self.submit_background, ...)`, and `CLAUDE.md` admits *"UNO control mutation from a background thread is done directly here"* — the panel's edit control, status label, button states **and `text_range.setString`** are all touched from the worker. No `AsyncCallback`, no `XJob`, no cancellation, no HTTP timeout → a hung Ollama request disables Submit forever (open issue #14 "Add ability to cancel in-flight request"; release 0.2.9 titled *"Indefinite timeout on requests to Ollama"*).
- **Unconventional build**: an external `unodit` tool at `../unodit/unodit.py` with a **hardcoded Windows path** in the Makefile; version duplicated in three files; `Panel1_UI.py` is hand-maintained and must not be regenerated. License file is **MPL-2.0** even though GitHub reports none.
- **Reported failures**: issue #3 — empty panel, root cause *"because of the WritingTool extension … they're blocking each other"*; issue #10 blank tab; #15 Cloudflare blocking the free tier; #7 LM Studio incompatibility (fixed 0.2.14); #16 the free model is not disclosed.

---

## 6. `WaterPistolAI/libreoffice-mcp` and `jwingnut/mcp-libre` — the MCP adapters

### 6.1 `WaterPistolAI/libreoffice-mcp` (26★, **dead since 2025-05-28**, **no LICENSE**)

**Archetype C: external MCP server + UNO socket to a (documented headless) soffice.** 5 files total; the whole server is one 504-line `libreoffice.py`.

**Transport (VERIFIED)** — OooDev's `Lo.load_office` with a socket connector:
```python
self.loader = Lo.load_office(
    connector=Lo.ConnectSocket(host="localhost", port=int(os.getenv("LIBREOFFICE_PORT", "2083"))),
    opt=Options(log_level="INFO"))
```
Documented setup (README, "Old Documentation"):
```bash
sudo -u mcp-libreoffice soffice --headless --accept="socket,host=localhost,port=${LIBREOFFICE_PORT:-2083};urp;"
```
**Port 2083**, not 2002. No `.oxt`, no extension, no HTTP server, no `--convert-to`, no spawn of soffice. Because it is a bare socket connect, pointing it at a *visible* GUI soffice would also work — but nothing in the repo does that (`INFERENCE`).

**Tools — 24 `@mcp.tool()` functions** (VERIFIED): `open_document`, `new_document`, `save_document`, `close_document`, `get_sheet_names`, `get_cell_value`, `set_cell_value`, `create_new_sheet`, `create_pivot_table`, `sort_range`, `calculate_statistics`, `run_query`, `list_tables`, `create_table`, `insert_data`, `create_form`, `create_report`, `insert_text`, `apply_style`, `run_macro`, `insert_form_control`, `format_cell_range`, `conditional_format`, `create_chart`.
Breakdown: **Writer 2**, **Calc 13**, **Base 7**, generic 4, **Impress 0, Draw 0**. The tagline "supports Writer, Calc, Impress, and Draw" is misleading — Impress/Draw exist only as `doc_type` enum values (`{"impress": DrawDoc}`), and the README advertises ~10 tools that do not exist in code.

**Implementation notes.** Reads/writes go through OooDev (`WriteDoc`/`CalcDoc`/`DrawDoc`, `sheet.rng("A1:B10")`, `Write.append`, `Write.style`), with raw UNO only for `com.sun.star.util.SortField`, Base JDBC, `vnd.sun.star.script:` macro invocation, and `com.sun.star.report.pentaho.SOReportJobFactory`. Documents live in an in-memory `self.documents` dict keyed `doc_{n}` — all operations are on live document objects, no temp copies. Notably, it **monkey-patches away FastMCP's real HTTP transport** with a stub FastAPI that answers `POST /` with a hardcoded JSON-RPC body listing the tool names (`"id": 1  # Static ID for simplicity; mcpo may require dynamic IDs`) — an Open WebUI/mcpo shim, not real MCP — and there is **no `__main__` entry point**, so `python libreoffice.py` cannot work.

**Failure modes (VERIFIED, 4 issues total)**: #2 the pinned dependency name is wrong (`ooodev` vs `ooo-dev-tools`); #3 no `mcp.json` documentation; #4 a 2026 security PR (SQL injection in `run_query`/`create_table`/`insert_data`, path traversal in `open_document`/`save_document`, macro-URI injection in `run_macro`, leaked DB connections, missing entry point) that was **closed but not merged** (`pushed_at` predates it). **Cite as a cautionary data point, not a base.**

### 6.2 `jwingnut/mcp-libre` (11★, MIT) — an in-process HTTP server inside LibreOffice

**The clearest "live GUI + MCP" design after Claude-Connector**, and worth studying because its transport is the *simplest possible*: a stdlib HTTP server on a thread inside the office process.

**Extension side (VERIFIED).** `plugin/META-INF/manifest.xml` registers `pythonpath/registration.py` as a Python UNO component plus `Addons.xcu` and `ProtocolHandler.xcu`. `registration.py` implements **`com.sun.star.frame.ProtocolHandler`** (`XDispatchProvider`/`XDispatch`), impl name `org.mcp.libreoffice.MCPExtension`, and **auto-starts the HTTP server at module import** (*"=== Auto-starting MCP server... ==="*). Menu: `Tools ▸ MCP Server ▸ Start/Stop/Restart/Show Status`, all `service:org.mcp.libreoffice.MCPExtension?start_mcp_server` dispatch URLs.

The listening socket is **not** `com.sun.star.connection.Acceptor` — it is Python's stdlib:
```python
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
self.server = socketserver.TCPServer(("", self.port), MCPRequestHandler)
```
- **Protocol is custom REST/JSON, not MCP JSON-RPC**: `GET /`, `GET /tools`, `GET /health`, `POST /tools/{tool_name}`, `POST /execute {tool, parameters}`; CORS `Access-Control-Allow-Origin: *`.
- **Port 8765**, `host="localhost"` in the call, but the bind address is `""` = **0.0.0.0** (VERIFIED in code — a real exposure note).
- **Single-threaded** `TCPServer` (not `ThreadingTCPServer`), each request doing `asyncio.run(execute_tool(...))` on the HTTP handler thread.
- UNO is reached **in-process**: `uno.getComponentContext()` → `createInstanceWithContext("com.sun.star.frame.Desktop", ctx)` → `getCurrentComponent()`. **It edits the live document the user is looking at** — the project's whole selling point ("Real-time Editing … instant visual feedback", `docs/LIVE_VIEWING_GUIDE.md`).
- `uno_bridge.py` (102 KB) uses `loadComponentFromURL("private:factory/swriter|scalc|simpress|sdraw")`, `storeAsURL/storeToURL/store` with `PropertyValue("FilterName", …)` (`pdf→writer_pdf_Export`, `docx→MS Word 2007 XML`, `odt→writer8`, `rtf→Rich Text Format`, …), `createSearchDescriptor`/`createReplaceDescriptor`, **`XRedlinesSupplier.getRedlines()`**, `com.sun.star.text.TextField.Annotation` for comments, `com.sun.star.style.ParagraphStyle`, and `supportsService(...)` type dispatch.

**Tool surface (VERIFIED).** Three layers exist in one repo:
- **43 HTTP tools** (`plugin/pythonpath/mcp_server.py`) — all named `*_live`: `create_document_live`, `insert_text_live`, `get_document_info_live`, `format_text_live`, `save_document_live`, `export_document_live`, `get_text_content_live`, `list_open_documents`, plus paragraph styles (6), structure (4), cursor (4), selection (4), search/replace (3), **track changes (7: status/enable/list/accept/reject/accept_all/reject_all)**, comments (2), **Calc (4)**. Writer-dominant; Calc = 4 read/write tools; Impress/Draw = create only.
- **9 consolidated MCP tools** exposed to the model via a FastMCP **stdio** bridge (`libreoffice_mcp_server.py`), one per group with an `action` parameter — `document`, `structure`, `cursor`, `selection`, `search`, `track_changes`, `comments`, `save`, `text` — *"consolidated from 32 individual tools … for fewer permission prompts"*. The bridge POSTs to `LIBREOFFICE_URL` (default `http://localhost:8765`) with `httpx`.
- A **legacy external server** (`src/libremcp.py`, 14 tools) using headless batch + `--convert-to` **and** a socket-UNO code path against `port=2002`, spawning `libreoffice --headless --accept=socket,host=127.0.0.1,port=2002;urp;`.

**Failure modes (VERIFIED).** `docs/KNOWN_ISSUES_AND_ROADMAP.md`: *"**Track Changes Awareness** … search/replace tools operate on all text including tracked deletions … Impact: High — Makes automated editing unreliable when Track Changes is enabled"*; *"Comment Reply Feature Missing … Workaround: None"*; *"**Calc/Impress/Draw Limited Support** — Most tools only work with Writer documents"*; *"**No Undo/Redo Support**"*; *"Table Support Limited"*. `WINDOWS_SETUP_NOTES.md`: *"**The LibreOffice plugin extension has compatibility issues on Windows: LibreOffice's embedded Python on Windows lacks required modules (`asyncio`, threading HTTP servers). Menu items appear but do nothing when clicked. No error messages displayed. Port 8765 never opens.**"* → *"Use the external MCP server approach instead of the plugin for Windows."* Also: hardcoded `/home/patrick/` paths in the build scripts, and a config inconsistency — `plugin/.mcp.json` advertises `{"type":"http","url":"http://localhost:8765"}` but that port speaks custom REST, not MCP JSON-RPC.

---

## 7. `zeweihan/aiworkdeck` (140★, AGPL-3.0) — LibreOffice **WASM** in an Electron webview

**Not a LibreOffice-extension project at all**, and its MCP layer is a *client*, not a server. But it produced the best *design document* on agent-driven editing found in this survey.

### 7.1 What it actually is (VERIFIED)
A legal-document IDE (Spring Boot + Vue + Electron). Layout: `backend/`, `frontend/`, `desktop/`, `office-addin/` (an **Office.js/WPS taskpane**, not LibreOffice), several Python sidecars, and `data/` (270 MB of sample Chinese legal `.docx`). **There is no `.oxt` anywhere in the tree.**

### 7.2 LibreOffice integration = ZetaOffice/LOWA (LibreOffice compiled to WASM) (VERIFIED)
- Editor page `frontend/src/zetaoffice/{editor.html, editor-main.js, public/{zeta.js, office_thread.js}}`, built by a dedicated Vite config to `dist/zetaoffice/`.
- Runtime `soffice.{js,wasm,data}` from `https://cdn.zetaoffice.net/zetaoffice_latest/`; **`zeta.js` is not on the CDN and is vendored**. Served by a **local same-origin HTTP server on fixed port 47613** (`desktop/main/zetaoffice-server.js`) because `SharedArrayBuffer` requires COOP `same-origin` + COEP `require-corp`; the fixed port exists so the **150 MB `soffice.wasm` HTTP + V8 code caches survive across launches** (a random port re-downloaded and recompiled it every cold start).
- `office_thread.js` runs **inside the emscripten pthread worker** (`Module.uno_scripts`); there `Module.zetajs` is the JS→UNO bridge, while on the main thread `Module.uno_main` is the *thread port* — a documented gotcha.
- `desktop/lowa-build/` is a **from-source LibreOffice-WASM build system** (`--with-lang=en-US zh-CN`) because upstream LOWA bakes the UI language at compile time.

### 7.3 Transport answers (VERIFIED)
UNO Python bridge: **no**. `--accept=socket…` / `pipe,name=` / port 2002: **none**. `.oxt`: **none**. `unoconv`/`unoserver`/`--convert-to`: **not for the editor** (a rejected fallback, "route C"). `--headless` vs GUI: **neither** — LO-WASM's VCL paints into a `<canvas>` inside `<webview partition="persist:zetaoffice">`, so the user *watches* the agent edit the live document.

### 7.4 The agent→document command pipeline (VERIFIED) — a pattern worth copying
```
LLM @Tool (backend DocumentEditTools.java / SlideEditTools.java)
 → EditorBridgeService.executeEditorCommand (requestId + CompletableFuture + per-action timeout)
 → SSE `client_action` event
 → frontend useAgentStream → handleEditorCommand → useEditorBridge
 → libreofficeExecutorClient.executeCommand({action, params})
 → zetaOfficeRelay (ipcRenderer.sendToHost, "lo-relay") → editor.html endpoint
 → office_thread.js EXEC[action] (UNO)     ← result back up the same chain
 → POST /api/ai/agent/editor-result → EditorResultController → completeEditorAction
```
Timeouts are **per-action, not flat** (default 30 s; 120 s for `find_replace`, `insert_at_cursor`, `stream_insert`; 180 s for `doc_open_file_sync`/`export_document`), because *"a flat timeout is the cause of 'backend gives up first, model resends, content is edited twice'"* — and the timeout result payload explicitly tells the model **"do not resend the same command"** after a long report was inserted twice as redlines.

### 7.5 Tool taxonomy (VERIFIED, parsed from source)
- `DocumentEditTools.java` — **89** `@Tool` methods = **64 `doc_*` (Writer) + 25 `sheet_*` (Calc)**.
- `SlideEditTools.java` — **22 `slide_*` (Impress)**.
- `OfficeEditTools.java` — **76 `office_*`** = the MS Office/WPS taskpane bridge (**not** LibreOffice), gated per session by `ClientCapabilityService` (`Capability.LOWA|OFFICE|NONE`) — *"a LOWA session sees no `office_*`, and an office session sees no `doc_*`/`sheet_*`; a remote tool with no executor is a 30-second dead path."*
- Frontend `EDITOR_ACTIONS` whitelist ≈ **170** action names, including `load_document` (`{bytes,name}` → MEMFS + `loadComponentFromURL`), `export_document` (`storeToURL` → bytes), `ui_command` (a `.uno:` allowlist), `undo`/`redo`, `set_track_changes`, `set_revision_view`.

Representative `doc_*` names: `doc_get_document_text`, `doc_get_selection`, `doc_find_text`, `doc_find_replace`, `doc_insert_at_cursor`, `doc_replace_selection`, `doc_get_paragraph`, `doc_modify_paragraph`, `doc_get_outline`, `doc_select_anchor`, `doc_select_paragraph`, `doc_collapse_cursor`, `doc_delete_selection`, `doc_format_selection`, `doc_set_paragraph_format`, `doc_insert_table`, `doc_table_read`, `doc_add_comment`, `doc_list_revisions`, `doc_accept_all_revisions`, `doc_undo`, `doc_redo`.

### 7.6 `docs/AI_EDITOR_PRIMITIVES.md` + `libreofficeExecutorClient.js` — the design ideas worth stealing (VERIFIED)
- **"Anchors, not offsets."** The RFC's §0.1 is a post-mortem: the old WPS-era pipeline took the document's plain text, did `indexOf` in JS, and used integer offsets to locate/replace in the rich model — *"'inaccurate locationing' is not a WPS-specific defect; it is the original sin of the current architecture — porting it to LibreOffice would only make it worse."* The fix: `XSearchable`/`XReplaceable` model-native search, `XTextCursor`/`XParagraphCursor`, and **hidden bookmarks as stable anchors** (`com.sun.star.text.Bookmark`, `insertTextContent(range, bm, true)`), with the worker **explicitly rejecting integer offsets**.
- **Redlines on by default.** `RecordChanges=true` set at boot/load so every AI edit is a reviewable tracked change — *"the previous default (turn revisions off before AI edits) leaves no trace, which is backwards for lawyer review."* Author set to `AI WorkDeck` via UserProfile.
- **Verification snapshot on every mutating primitive**: `paragraphAfterEdit` — and a subtle warning: with `RecordChanges=true` the *deleted* text stays in the flow, so verification must check the new text **appeared**, not that the old text vanished.
- **Anthropomorphic primitive vocabulary** (see → find → select → edit → verify): `get_document_text`, `get_cursor_context`, `find_text_locations` (anchors + context), `set_selection` (with view scroll so the user sees the agent's "hand"), `select_paragraph`, `collapse_selection`, `delete_selection`, `insert_at_cursor`, `replace_selection`, `format_selection`, `set_paragraph_format`, `undo`/`redo`.
- **IME reality check**: Chinese input on WASM canvas required a custom overlay — a real `<input>` capturing the system IME → `compositionend` → UNO `insertString` at the cursor; and CJK fonts must be injected before fontconfig's startup scan. Phase-0 gate results: IME ✅ (with that work), zetajs programmatic capability ✅, **performance ⚠️ partial** (*"UNO batch-generating 50 pages ≈ 26.5 s — programmatic writes only, not real editing"*), **bundle size ⏳ untested** (hundreds of MB from the CDN, on top of an already ~560 MB installer).
- **MCP layer is inbound only**: `McpProperties`/`StreamableHttpMcpProvider`/`McpResponseParser` call remote legal-database MCP servers (PKULaw) with hand-written JSON-RPC 2.0 over HTTP (`Accept: application/json, text/event-stream`, SSE-aware parsing), because *"the official MCP Java SDK's SSE transport is incompatible with the PKULaw gateway's endpoint."* `docs/PLUGIN_SPEC.md`: *"planned: out-of-process plugin form (MCP server)"* — i.e. **not built**.
- **License caution**: AGPL-3.0-or-later (with an MIT-derived portion of `office_thread.js`) — material for reuse decisions.

---

## 8. `dandi-91/LibreOffice-Bielik-Agent` — the best small native sidebar agent (0★, MIT)

**Archetype D, but engineered like a prototype of archetype B.** One 1,158-line `bielik_agent.py` is the entire runtime. Created and last pushed the same day (2026-07-26); 1 release; 0 stars. **Read this file before writing any sidebar agent** — it solves the four hard problems the other small projects get wrong.

### 8.1 Architecture (VERIFIED)
`META-INF/manifest.xml` registers `bielik_agent.py` at archive root as a Python UNO component + `Factories.xcu`, `Sidebar.xcu`, `Addons.xcu` (**no ProtocolHandler**).
- Deck `BielikDeck`, `ContextList = WriterVariants, any, visible ;`; panel `BielikPanel`, `ImplementationURL = private:resource/toolpanel/BielikPanelFactory/BielikPanel`.
- Panel is a raw **`UnoControlContainer`** (not an XDL dialog): `UnoControlContainer` + `UnoControlContainerModel`, then `UnoControl{Edit,FixedText,Button,CheckBox}Model` + control instances added via `container.addControl(name, control)`; manual `setPosSize(..., POSSIZE)` layout recomputed in `windowResized`; `getHeightForWidth → LayoutSize(220,-1,-1)`.
- **Correct factory registration**: `g_ImplementationHelper.addImplementation(PanelFactory, "org.libreoffice.bielikagent.PanelFactory", ("com.sun.star.ui.UIElementFactory",))`.
- **Document discovery via the frame handed to the factory** — not the Desktop singleton, not `XSCRIPTCONTEXT`:
```python
doc = self.frame.getController().getModel()
doc.getText().getString()
```
  This is multi-window safe. The menu entry uses the built-in dispatch **`.uno:SidebarDeck.BielikDeck`** (comment: *"a built-in dispatch, so no extra protocol handler is needed"*).
- Model/endpoint: `BIELIK_OLLAMA_URL` (default `http://localhost:11434/api/chat`), `BIELIK_MODEL`, `temperature 0.2`, `REQUEST_TIMEOUT = 600`; stdlib `urllib` only; **forced JSON schema** via Ollama's `format` so a small model always returns `{"comment": ..., "actions": [...]}`; defensive parsing (`_loosen_json` trailing-comma stripper, raw-text fallback, `_fix_actions` back-filling `align.value` from Polish words in the comment).

### 8.2 Document interaction (VERIFIED) — the richest UNO surface of the small projects
- **Target resolution** `_find_ranges(doc, target)`: `all`/Polish synonyms → cursor from `getStart()` + `gotoEnd(True)`; `selection` → `frame.getController().getSelection().getByIndex(0)`; otherwise **`createSearchDescriptor()` + `SearchCaseSensitive=True` + `findAll(sd)`** returning *every* exact match. Placeholder echoes like `"tekst|all|selection"` are guarded back to `selection`.
- **replace**: `createReplaceDescriptor()` + `setSearchString/setReplaceString` + `doc.replaceAll(desc)`; on zero hits retries with an ICU regex where typographic quotes/dashes/whitespace are interchangeable (`_flexible_pattern`), with `$`/`\` escaped in the replacement — i.e. **explicitly hardened against regex injection from model text**.
- **format**: `ParaAdjust` (LEFT/RIGHT/CENTER/BLOCK), `CharFontName`, `CharHeight`, `CharWeight` (150/100), `CharPosture`, `CharUnderline`, `CharColor` (Polish colour names + `#RRGGBB`); **styles**: `ParaStyleName`.
- **insert**: `XText.insertString(cursor, line, False)` + `insertControlCharacter(cursor, PARAGRAPH_BREAK, False)` — deliberate real paragraphs, never fusing into an existing one.
- **table**: `doc.createInstance("com.sun.star.text.TextTable")` → `initialize(rows, cols)` → `text.insertTextContent(cursor, table, False)` (rows clamped 1–200, cols 1–63). The system prompt forbids markdown tables outright.
- **unlink**: paragraph/portion enumeration clearing `HyperLinkURL/HyperLinkName/HyperLinkTarget` and resetting `CharStyleName`.
- **dispatch escape hatch**: `DispatchHelper.executeDispatch(frame, url, "", 0, args)` with the target selected first via `controller.select(range)`, arguments stripped (`command.split(";")[0].split()[0]` — **no model-supplied args can reach a command**) and a **35-command allow-list** of local formatting `.uno:` commands by default.

### 8.3 Undo, review mode, threading (VERIFIED)
```python
undo = doc.getUndoManager(); undo.enterUndoContext("Bielik: zmiany")
... apply all actions ...
finally: undo.leaveUndoContext()
```
→ **one Ctrl+Z per agent turn**. Review mode swaps `RecordChanges` around the batch (restoring it in `finally`) and dispatches `.uno:AcceptAllTrackedChanges` / `.uno:RejectAllTrackedChanges`.

Threading is the standout pattern: one daemon thread per request does HTTP+JSON **only**; every UI/UNO mutation hops back to the GUI thread:
```python
self._async = self.smgr.createInstanceWithContext("com.sun.star.awt.AsyncCallback", self.ctx)
class MainThreadCallback(unohelper.Base, XCallback):
    def notify(self, data): self.func()
def _run_on_main(self, func):
    cb = MainThreadCallback(wrapper); self._pending_cbs.add(cb); self._async.addCallback(cb, None)
```
README: *"touching AWT/UNO from a worker thread would crash."*

### 8.4 Security posture (VERIFIED, code + `SECURITY.md`)
- **Egress guard**: `_is_local_endpoint()` loopback check; the request raises `EGRESS_BLOCKED` *before sending* unless `BIELIK_ALLOW_REMOTE=1` — *"your document does not leave the device by default."*
- **Prompt-injection containment**: model output is only `json.loads`-parsed into a fixed op vocabulary; no op can open a socket; dispatch is allow-listed with args stripped; regex metacharacters escaped. `SECURITY.md` states the residual risk honestly (injected text can still change *text*, not exfiltrate).
- **Packaging**: stdlib `build.py` zips an explicit `FILES` list after a `py_compile` gate; `build.sh`/`build.bat` wrappers; `.gitattributes` forces LF; GitHub Actions builds on PR and publishes on `v*`. `description.xml` uses the *correct* modern dependency element: `<lo:LibreOffice-minimal-version value="6.4" …/>` with a comment that `OpenOffice.org-minimal-version` is capped at 4.1 inside LibreOffice.
- Documented install difficulties: Linux needs `libreoffice-script-provider-python`/`pyuno`; LibreOffice must be **fully restarted**; the `ollama list` tag must match exactly; logs go to the platform temp dir (on macOS GUI that is `/var/folders/…/T`, **not** `/tmp`).

---

## 9. Cross-cutting question 1 — what transports are actually used to reach a **running** LibreOffice?

### 9.1 The catalogue (all `VERIFIED`)

| Mechanism | Where used | Reaches a running GUI? | Ports/sockets | Notes |
|---|---|---|---|---|
| **UNO named pipe acceptor created *in-process* by an extension** (`com.sun.star.connection.Acceptor` + `com.sun.star.bridge.BridgeFactory` on a Job thread) | **Claude-Connector** | **Yes** — no flags needed | pipe `lo-claude-<user>`; client dials `uno:pipe,name=…;urp;StarOffice.ComponentContext` | Best-in-class. ~186 lines. Shutdown-safe via `XTerminateListener.stopAccepting()`. Local-only by construction. |
| **TCP UNO socket acceptor via `--accept`** | Claude-Connector (rung 2 + auto-launch), WaterPistolAI, mcp-libre legacy, LibreOffice's own docs | Yes, but the flag must be on the process | `soffice --accept="socket,host=127.0.0.1,port=2002;urp;"`; WaterPistolAI uses **2083**; client resolves `uno:socket,host=…,port=…;urp;StarOffice.ComponentContext` | **The single-instance trap**: launching soffice with `--accept` while an office is already running is *swallowed* by the existing instance and the flag is ignored. Users cannot be asked to relaunch with flags. |
| **In-process stdlib HTTP server on a thread inside the extension** (`socketserver.TCPServer(("", 8765), Handler)`) | **mcp-libre** | **Yes** — direct `uno.getComponentContext()` access to the live desktop | **0.0.0.0:8765**, custom REST/JSON (not MCP) | Simplest possible server, but single-threaded, unpatchable protocol, Windows-embedded-Python incompatible, and it puts UNO calls on a non-main thread. |
| **In-process URP acceptor over a pipe** (same as row 1) | Claude-Connector | Yes | — | Reuses UNO's own binary framing: **no bespoke protocol to write**. |
| **File + mtime handoff** (no IPC at all) | **LibreAssist** | Yes, via `.uno:Reload` after the file changes | none | Zero protocol risk; terrible latency (1–2 min), full-file rewrite, corruption risk. |
| **Headless `soffice --convert-to` / headless spawn** | mcp-libre legacy, PPT-Master, most "docx→pdf" projects | **No** | none | Batch only. PPT-Master always uses an isolated `-env:UserInstallation=` profile to avoid lock fights. |
| **`unoserver`/`unoconv`-style long-lived headless listener** | not used by any surveyed agent project | No | unoserver: XML-RPC on **2003** ↔ UNO on **2002** by default | The canonical reference implementation of the socket pattern (`unoconv/unoserver` README + `src/unoserver/server.py`: `interface="127.0.0.1", port="2003", uno_interface="127.0.0.1", uno_port="2002"`). |
| **LibreOffice WASM + zetajs JS→UNO** | **aiworkdeck** | Yes (canvas in a webview) | local HTTP **47613** for the editor page; no UNO port | No native process at all. Highest fidelity, highest cost (custom WASM build, COOP/COEP, CJK fonts, IME bridge). |
| **Basic macro / `com.sun.star.script.provider`** | not used as a *transport* by any surveyed project; only `run_macro`/`run_python_macro` tools | — | — | Runnable in-process via dispatch (Claude-Connector's `dispatch_uno`/`run_macro`), but nobody uses Basic as the agent link. |

### 9.2 First-hand verification on this machine (`VERIFIED (local)`)
Environment: **LibreOffice 26.2.5.2**, system `python3` with `/usr/lib/python3/dist-packages/uno.py` (`python3-uno`), kernel-level socket check.

```bash
soffice --headless --norestore --nolockcheck --nologo \
  --accept="socket,host=127.0.0.1,port=2002;urp;" \
  -env:UserInstallation=file:///tmp/…/profile &
# → ss -ltn:  LISTEN  127.0.0.1:2002
```
Then, **from a completely separate process** (`python3`, not the office's own interpreter):
```python
resolver = ctx.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", ctx)
rc = resolver.resolve("uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext")
desktop = rc.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", rc)
doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
```
Observed results:
- ✅ Attach to the **running** instance; open a new Writer document **in it**; `doc.getCurrentController().getFrame()` reachable (GUI-attached model, not a private headless doc).
- ✅ Write through the live model: `text.insertString(cursor, "Hello from an EXTERNAL agent process over the UNO socket.\n", False)` → `text.getString()` returns it.
- ✅ `doc.setPropertyValue("RecordChanges", True)` **from the external process**, then an insert → `doc.getRedlines().Count == 3`. **Cross-process track-changes recording works.**
- ✅ `doc.createReplaceDescriptor()` + `doc.replaceAll(desc)` → returned `1`. Model-native search works cross-process.
- ✅ **Undo integration across the process boundary**: `um.enterUndoContext("agent edit")` → … → `um.leaveUndoContext()` produced `getCurrentUndoActionTitle() == 'agent edit'` in the office's own undo stack, alongside LibreOffice's own entries (`'Replace: "EXTERNAL" → "external"'`, `'Typing: …'`), and `um.undo()` reverted exactly that group.
- ✅ `XUndoManager` surface on 26.2 is the modern one: `isUndoPossible()`, `getAllUndoActionTitles()`, `getCurrentUndoActionTitle()`, `enterUndoContext()/leaveUndoContext()` — **not** the deprecated `getUndoActionCount()/getUndoActionTitle(i)` (which raises `AttributeError`). Worth knowing before copying older sample code.

**Conclusion**: the "attach an external process to a running LibreOffice GUI over the UNO socket and edit the live document, with real undo integration" path is **verified working on this exact machine**, and it reproduces prior art (archetype B/C) without needing an extension at all for the transport — an extension is only needed if you cannot ask the user to relaunch soffice with `--accept` (which is exactly the gap Claude-Connector's pipe acceptor closes).

---

## 10. Cross-cutting question 2 — MCP or bespoke protocol?

`VERIFIED` classification:

| Project | Agent-facing protocol | Agent↔bridge | Bridge↔LibreOffice |
|---|---|---|---|
| Claude-Connector | **MCP** (stdio, JSON-RPC lines) | stdio | **UNO URP over named pipe** (binary, `com.sun.star.bridge`) |
| mcp-libre | **MCP** (stdio, FastMCP) → then custom REST | stdio | **HTTP/JSON** custom REST on 0.0.0.0:8765 → in-process UNO |
| WaterPistolAI | **MCP** (stdio, FastMCP) | stdio | **UNO over TCP socket** (OooDev `Lo.ConnectSocket`) |
| PPT-Master | **MCP** (stdio, FastMCP, 9 tools) | stdio | **none** — ODP ZIP+XML; `soffice --convert-to` for formats |
| aiworkdeck | none for the editor (**bespoke**: SSE `client_action` + Electron IPC + `{action, params}`) | SSE/IPC | zetajs→UNO in a WASM worker |
| LibreAssist | **none** (CLI argv + prompt + cwd) | subprocess stdout | filesystem + `.uno:Reload` |
| localwriter / LibreThinker / Bielik | **none** (LLM HTTP API; Bielik forces a JSON schema) | HTTP | direct UNO on the live model |

**So: five projects are MCP-based, and in every case MCP is only the *agent-facing* hop.** Nobody tunnels MCP itself into LibreOffice: the office-facing hop is either URP (the two best projects), custom HTTP (mcp-libre), a filesystem, or wasm-bridge JS. This matters because DSH's `@deepseek-ai/dsh-mcp-client` supports exactly the two transports that matter (`stdio` and `streamable-http`, with reconnect/backoff and `mcp__<server>__<tool>` naming) — but note it **does not support MCP resources or prompts**, and `streamable-http` requires an *MCP* JSON-RPC endpoint, which mcp-libre's `:8765` is not.

---

## 11. Cross-cutting question 3 — does anything let the agent see and edit the **LIVE** open document?

`VERIFIED` yes, in descending order of architectural directness:

1. **Claude-Connector** — yes, and it says so in `SERVER_INSTRUCTIONS`: *"You are editing documents that are open in LibreOffice on the user's own screen. Edits appear immediately…"* Uses `desktop.getCurrentComponent()` with a documented fallback to enumerating `getComponents()` when headless/unfocused, and a `set_active_document` tool because *"any user click or background doc event silently redirects writes."*
2. **mcp-libre** — yes; the README's headline is "Real-time Editing: Live document manipulation with instant visual feedback", implemented by in-process `uno.getComponentContext()`.
3. **aiworkdeck** — yes, in a WASM canvas inside its own Electron app (not a native LibreOffice window).
4. **localwriter / LibreThinker / Bielik** — yes: they read the live selection and write to the live model; the user watches text stream in (localwriter) or appear at once (LibreThinker, Bielik).
5. **LibreAssist** — *the document in the user's window changes*, but through save → external CLI edit → `.uno:Reload` + `.uno:CompareDocuments`. This is "live-ish": the user watches the document rewrite itself rather than watching an agent type.
6. **WaterPistolAI** — incidental: it *could* attach to a visible GUI (it's a plain socket connect) but its documented setup is a headless service account; nothing in the repo configures a visible instance.
7. **PPT-Master** — **no**: it is a file-format engine; "live preview" is a file-watch that re-renders the saved `.odp` to PDF pages.

---

## 12. Cross-cutting question 4 — common failure modes and limitations

Consolidated, with the source that reports each. Grouped by how likely they are to bite us.

### A. Install / activation (the #1 user-visible failure class)
- Extension menu/deck **never appears**: localwriter #37 (LO 25.8.4.2 Wayland), #2, #42 (tabbed UI), #29; LibreThinker #3 (*"because of the WritingTool extension … they're blocking each other"*) and #10. Contributing causes documented in Claude-Connector: extensions activate **on the next LibreOffice start**; the four-file wiring (`manifest.xml` ↔ `Sidebar.xcu` ↔ `Factories.xcu` ↔ the Python class's `IMPL_NAME`) must agree *"or the deck silently never appears"*; on Linux the **`python3-uno` / `libreoffice-script-provider-python`** package is required (Bielik README); a full restart including background `soffice` is required (Bielik, Claude-Connector).
- **Sidebar panels are fragile by design**: the panel is created while its parent window is still 0×0 → *"a one-shot layout at build time produces invisible (negative-width) controls"*, and *"a panel that doesn't implement `getHeightForWidth` gets zero height"* → relayout from an `XWindowListener` (Claude-Connector; Bielik implements `windowResized` for the same reason).
- **`ContextList` tokens**: verified against LibreOffice core (`sfx2/source/sidebar/ResourceManager.cxx`, `vcl/source/window/EnumContext.cxx`) — the token must be one of `Writer|Calc|Draw|Impress|Chart|Math|DrawImpress|WriterVariants`, **unknown names are dropped with `SAL_WARN`**; `WriterVariants` covers Writer/Web/Global/XML/Form/Report while bare `Writer` is plain Writer only.
- **Windows + embedded Python**: mcp-libre `WINDOWS_SETUP_NOTES.md` — *"LibreOffice's embedded Python on Windows lacks required modules (`asyncio`, threading HTTP servers). Menu items appear but do nothing when clicked. Port 8765 never opens."*
- **Packaging chores** nobody enjoys: `.oxt` = a ZIP with a precise `manifest.xml`; localwriter's `build.sh` needs `unoidl-write` + `types.rdb`/`offapi.rdb` from `libreoffice-dev`; Claude-Connector's `-env:UserInstallation=` can be *"silently dropped"*; LibreThinker depends on an external `unodit` tool with a hardcoded Windows path.

### B. Correctness of the edit
- **Formatting destruction** from `XTextRange.setString`: localwriter #43 (*"stripping any formatting (font, fontsize, fontcolor, bold/italic, paragraph style, text-alignment…)"*), LibreThinker #13/#6.
- **Whole-file rewrite** (LibreAssist) risks corruption — its own README warns about Mistral Vibe: *"Files can be corrupted."*
- **Targeted edits are hard**: LibreAssist has no selection awareness at all; Bielik had to build `_find_ranges` with a typography-tolerant regex fallback and still notes *"heavily paraphrased targets may still miss."*
- **Tables and shapes**: localwriter #4, #12; **markdown-instead-of-table** is common enough that Bielik's system prompt forbids it explicitly.
- **Truncation**: localwriter #7 — *"answers often are not complete and stop in the middle of the sentence"* (independent of the model).

### C. Undo
- **No undo integration at all**: localwriter, LibreThinker, mcp-libre (*"No Undo/Redo Support … Workaround: User can use Ctrl+Z manually"*).
- **File-level backup instead of `XUndoManager`**: LibreAssist.
- **Per-turn `enterUndoContext` grouping works** and is independently converged on by Bielik, Claude-Connector, and nelson-mcp (*"independent convergence on the design is reassuring"*). Verified locally cross-process (§9.2).
- **A genuine upstream hole**: `setDataArray`/`setFormulaArray` create an Undo entry that **does not revert** the cells — *"Not worked around"*; nelson-mcp uses `setDataArray` zero times; Claude-Connector warns the model in `SERVER_INSTRUCTIONS` and ships a `checkpoint_document` tool as the only way back.

### D. Threading / UNO main-thread affinity
- **The rule**: *"UNO document APIs are not safe to call from arbitrary threads"* (Claude-Connector) — *"touching AWT/UNO from a worker thread would crash"* (Bielik). The correct pattern is worker thread for I/O **only** + marshal back via `com.sun.star.awt.AsyncCallback` (Bielik, Claude-Connector, LibreAssist's `core.py`) or a modal progress dialog with a nested event loop (Claude-Connector).
- **Violations in the wild**: LibreThinker mutates panel controls **and** `text_range.setString` from the worker thread (admitted in its `CLAUDE.md`); mcp-libre runs UNO calls on the HTTP handler thread; localwriter streams `setString` synchronously on the UI thread (its #33 is *"CPU fan spins up and windows screen recorder dies"*).
- **Modal dialogs are uninterruptible**: *"A UNO call waiting on a modal LibreOffice dialog never returns and cannot be interrupted from Python"* → Claude-Connector drops the bridge after a per-call timeout and says so in the error text.
- **Streaming is expensive**: appending to `XTextRange` per SSE chunk is O(n²) (localwriter), and long single tool calls need *per-action* timeouts, not a flat one, or the model retries and double-writes (aiworkdeck's `TIMEOUT_RESULT_JSON`: *"do not resend the same command"*).

### E. Addressability and safety
- **Ambient/implicit target**: *"All `writer_*`/`calc_*` tools act on the implicit active document … the single biggest Writer-session hazard: any user click or background doc event silently redirects writes"* (Claude-Connector) — fixed with `set_active_document` + explicit `index|title|url`. Bielik does it structurally right by binding to the `XFrame` from `createUIElement`.
- **Loose local services**: mcp-libre binds `0.0.0.0:8765` while its docs say `localhost`; WaterPistolAI's unmerged security PR documents SQL injection, path traversal and macro-URI injection; on this machine `--accept="socket,host=127.0.0.1,port=2002;urp;"` correctly bound loopback only (a bare `--accept=socket,port=2002` would not).
- **Secrets**: LibreThinker stores the API key in **plaintext JSON** (the anti-pattern `COMPETITOR-STUDY.md` flags as "LibreOfficeAICopilot hardcoded its key"); Claude-Connector uses OS DPAPI (Windows) with an honest "base64 is not encryption" note for other platforms; Bielik has no keys at all and blocks non-loopback egress by default.
- **Agent capability is unbounded**: Claude-Connector's `uno_exec`/`run_macro` = *"Everything the user can do in LibreOffice … There is no capability sandbox — the escape hatch is an intentional feature."*

---

## 13. What this means for our design

**Facts to build on.**
1. **The transport is a solved, cheap problem — pick the live-GUI one.** An external process attaching to a running LibreOffice over a UNO socket and editing the live document, with real `XUndoManager` integration and cross-process `RecordChanges`, is verified working on this machine (§9.2). For a zero-setup experience (user just has LibreOffice open), copy Claude-Connector's **in-office named-pipe acceptor** (`com.sun.star.connection.Acceptor` + `BridgeFactory`, ~186 lines, Job-bound, `XTerminateListener.stopAccepting()`), with `--accept=socket,…,2002;urp;` as the dev/fallback rung. **Do not invent a protocol for the office hop**: URP already frames it, and both of the best projects learned that.
2. **The agent hop should be MCP with DSH as the client.** DSH already ships `@deepseek-ai/dsh-mcp-client` with `stdio` and `streamable-http`, `mcp__<server>__<tool>` naming, per-call timeouts, `failOnStartupError`, and reconnect backoff — so our LibreOffice bridge should be a **standard MCP server**, not a bespoke protocol. Caveat: only *tools* are bridged (no MCP resources/prompts), and `streamable-http` must be real MCP JSON-RPC — mcp-libre's REST-on-8765 pattern would not connect.
3. **Avoid LibreOffice's bundled Python as the host for our logic.** Every MCP-in-`.oxt` project was forced into stdlib-only (`urllib`, `json`, `socketserver`) and hit the Windows embedded-Python wall. Instead: keep the in-office component **tiny** (accept a UNO connection and expose the live desktop) and put all tool logic, agent loop, and HTTP/HTTPS traffic in an **external Python process** that DSH can launch over stdio. That inverts Claude-Connector's constraint (their MCP server must run under LO's Python) and buys us `fastmcp`, `httpx`, and a real package manager.
4. **Tool surface: small, anchor-based, verification-instrumented.** aiworkdeck's 111 LibreOffice tools and Claude-Connector's 220 are context bills (74 advertised still costs 15 KB of schema per turn) and their own competitor study documents the discoverability cost of tiering. Start with a primitive set in aiworkdeck's vocabulary — *see → find → select → edit → format → verify*: `get_document_text/outline`, `get_selection`, `get_cursor_context`, `find_text_locations` (**return anchors/bookmarks, never integer offsets**), `set_selection` (scroll the view so the user sees the agent act), `insert_at_cursor`, `replace_selection`/`replace_at_anchor`, `format_selection`, `set_paragraph_format`, `insert_table`/`table_read`, `add_comment`, revisions list/accept/reject, `undo`/`redo`. Return a `paragraph_after_edit` verification snapshot on every mutation, and remember that with redlines on, *"the deleted text is still in the flow"* — verify the new text appeared.
5. **Make every agent turn exactly one undo step, and default to reviewable.** Wrap a turn in `enterUndoContext("<agent>: <action>")` (verified cross-process locally; converged on by three projects) and offer a review mode that flips `RecordChanges` and exposes Accept/Reject (Bielik, aiworkdeck). **Never use `setDataArray`/`setFormulaArray` for user-visible bulk writes** — it is an upstream undo hole; use per-cell/property mutators so Ctrl+Z actually restores.
6. **Read context from the model, not from a temp export.** LibreThinker's "export to .txt and read it back" and localwriter's `getString()` both destroy structure (styles, tables, lists, fields). Walk `XText`/`XParagraphCursor` into a structured representation (paragraph index, style name, outline level, run-level bold/italic/color) — and if a whole-document payload is needed, send a *structured* digest, not a flattened string. Bielik's "full document text on every turn, forever" is the wrong side of that tradeoff as soon as documents are real.
7. **Threading discipline is non-negotiable and cheap to get right**: worker thread for model/network I/O only; **all** UNO and AWT work on the main thread via `com.sun.star.awt.AsyncCallback` (Bielik's `_run_on_main` + a lock-guarded pending-callback set is ~15 lines and is the single most reusable snippet found). Add per-call timeouts and treat "no answer" as *probably a modal dialog* (Claude-Connector's diagnosis), and never let a flat timeout cause a silent double-write — say "do not resend" in the error text.
8. **Bind to the frame, not the desktop singleton.** `createUIElement`'s `Frame` argument → `frame.getController().getModel()` gives per-window correctness for free (Bielik). `desktop.getCurrentComponent()` + focus changes is the documented #1 hazard in the most mature project. If we do use the current component, we need an explicit document-selection tool and per-call document identity checks.
9. **DSH embedding precedent exists and is directly copyable**: PPT-Master shows spawning the DSH CLI (bundled Node + `bin.js`), a dedicated `DSH_HOME`, a bundle-baked profile + `cordis.patch.yml` with an inlined persona, MCP-bridge rows resolved through dsh's own `@deepseek-ai/dsh-mcp-client`, `--resume <sessionId>` for multi-turn, and a session-log watcher for streaming progress into a UI. Two warnings from its docs: do **not** declare npm dependencies in a DSH bundle (flat installs shadow dsh-internal modules and break APIs), and keep provider/API keys in the host app's env, not in files.
10. **Security defaults worth adopting wholesale** (Bielik): loopback-only egress guard on by default with an explicit opt-out, model output parsed as *data* into a fixed op vocabulary (never `eval`), `.uno:` dispatch behind an allow-list with arguments stripped, regex metacharacters escaped when model text becomes a pattern, and an honest `SECURITY.md` about residual risk. Add the missing piece none of them has: **a document-scoped capability boundary** (which file(s) the agent may touch).

**Things to deliberately NOT copy.**
- `XTextRange.setString` as the write primitive (formatting loss, no run-level control, O(n²) when streamed).
- Whole-document payloads per turn; whole-file rewrite handoff (LibreAssist) except where corruption risk is acceptable.
- Integer-offset addressing (aiworkdeck's own post-mortem).
- Bundling heavy logic inside LibreOffice's Python; auto-starting an HTTP server on `0.0.0.0`.
- 200+ tools with tiers as the first design move; a flat per-call timeout; unbounded single-shot tool calls.

**Open questions this survey could not settle (worth a spike).**
- **Pipe acceptor on Linux/macOS**: Claude-Connector's plan defers *"macOS/Linux CI runs of the integration test (manual spot-check only for now)"* and its DPAPI path is Windows-only. Whether `com.sun.star.connection.Acceptor` with `pipe,name=` behaves identically on Linux's OSL pipes needs testing here (the socket path is already verified locally).
- **Real undo behaviour of Writer's specific bulk APIs we plan to use** (`replaceAll`, `insertTextContent` for tables, style application) — Claude-Connector's table covers Calc range writes only; the same audit should be run for Writer before we promise Ctrl+Z fidelity.
- **Latency of a per-tool-call UNO round trip** for a long agent turn (e.g. 40 paragraph edits) over a pipe vs. a socket vs. in-process — nobody published numbers; aiworkdeck's only datapoint is 50 pages ≈ 26.5 s *in-process*.
- **Whether DSH's `streamable-http` client is enough** for live progress, or whether we need stdio + a side channel for streaming (localwriter's per-chunk UI feel is worth preserving; mcp-libre/Claude-Connector both ship no streaming at all).

---

## 14. Source index

**Repo metadata / trees / issues** (`https://api.github.com/…`): `/repos/{NikolaiRadke/LibreAssist, swe-sanad/LibreOffice-Claude-Connector, TANGZHUO12/PPT-Master, balisujohn/localwriter, mihailthebuilder/librethinker-extension, WaterPistolAI/libreoffice-mcp, jwingnut/mcp-libre, zeweihan/aiworkdeck, dandi-91/LibreOffice-Bielik-Agent}`, `git/trees/{main,master}?recursive=1`, `issues?state=all&per_page=50`, `search/issues?q=repo:balisujohn/localwriter`.

**LibreAssist** (`raw.githubusercontent.com/NikolaiRadke/LibreAssist/main/`): `README.md`, `NEWS.md`, `CHANGELOG.md`, `src/{main.py, META-INF/manifest.xml, description.xml, Sidebar.xcu, Factory.xcu, providers.json}`, `src/pythonpath/libreassist/{core.py, document.py, provider_base.py, discovery.py, settings.py, backup.py, ui/events.py, ui/ui.py, providers/claude_code.py, providers/codex_cli.py}`.

**Claude-Connector** (`…/swe-sanad/LibreOffice-Claude-Connector/master/`): `README.md`, `CLAUDE.md`, `TODO.md`, `CHANGELOG.md`, `.mcp.json`, `.vscode/mcp.json`, `.claude-plugin/{plugin.json,marketplace.json}`, `mcpb/{manifest.json,index.js}`, `mcp/{README.md, libreoffice_mcp.py, test_reconnect.py, loconn/core.py, loconn/registry.py, loconn/tools/writer_text.py}`, `src/{agent_acceptor.py, connector.py, uno_bridge.py, sidebar_panel.py, providers.py, config.py}`, `scripts/start_office_socket.ps1`, `ext/{META-INF/manifest.xml, Addons.xcu, Jobs.xcu, ProtocolHandler.xcu, description.xml, registry/org/openoffice/Office/UI/{Sidebar.xcu,Factories.xcu}}`, `docs/{ARCHITECTURE.md, PLAN-PIPE-ACCEPTOR.md, KNOWN-GAPS.md, COMPETITOR-STUDY.md, RESEARCH.md, CROSS-AGENT.md, SECURITY.md, BUILDING.md, MCP-TOOLS.md, UPSTREAM-PARITY.md, ANTHROPIC-SUBMISSION.md}`.

**PPT-Master** (`…/TANGZHUO12/PPT-Master/main/`): `README.md`, `plugin/README.md`, `plugin/src/{mcp_server.py, odp_engine.py, chart_tools.py, chart_render.py, render_page.py, animation_presets.py}`, `app/Sources/PPTMaster/Supervisor.swift`, `app/Package.swift`, `scripts/build-app.sh`, `docs/装配三件套.md`, `docs/项目文档.md`.

**localwriter** (`…/balisujohn/localwriter/master/`): `README.md`, `main.py`, `prompt_function.py`, `pythonpath/llm.py`, `Addons.xcu`, `Accelerators.xcu`, `CalcAddIn.xcu`, `META-INF/manifest.xml`, `description.xml`, `build.sh`, `idl/XPromptFunction.idl`.

**LibreThinker** (`…/mihailthebuilder/librethinker-extension/main/`): `README.md`, `CLAUDE.md`, `Makefile`, `extension/{description.xml, config.ini, Sidebar.xcu, Factory.xcu, ProtocolHandler.xcu, META-INF/manifest.xml, registration/license.txt}`, `extension/src/LibreThinker.py`, `extension/src/pythonpath/{ui/Panel1_UI.py, ui_logic/{Panel1.py, api.py, settings.py, utils.py}}`.

**WaterPistolAI** (`…/WaterPistolAI/libreoffice-mcp/master/`): `README.md`, `libreoffice.py`, `requirements.txt`.

**mcp-libre** (`…/jwingnut/mcp-libre/main/`): `README.md`, `libreoffice_mcp_server.py`, `WINDOWS_SETUP_NOTES.md`, `HANDOFF_DOCUMENT.md`, `src/libremcp.py`, `plugin/{README.md, build.sh, install.sh, description.xml, Addons.xcu, ProtocolHandler.xcu, .mcp.json, META-INF/manifest.xml}`, `plugin/pythonpath/{registration.py, ai_interface.py, mcp_server.py, uno_bridge.py}`, `docs/{KNOWN_ISSUES_AND_ROADMAP.md, LIBREOFFICE_PLUGIN_DESIGN.md, LIVE_VIEWING_GUIDE.md, TOOL_REFERENCE.md}`.

**aiworkdeck** (`…/zeweihan/aiworkdeck/master/`): `frontend/src/zetaoffice/{README.md, public/office_thread.js}`, `frontend/src/composables/libreofficeExecutorClient.js`, `desktop/main/zetaoffice-server.js`, `desktop/lowa-build/README.md`, `backend/src/main/java/com/checkba/service/ai/{EditorBridgeService.java, ClientCapabilityService.java, tools/{DocumentEditTools,SlideEditTools,OfficeEditTools}.java, mcp/*.java}`, `backend/src/main/resources/application.yml`, `docs/{LIBREOFFICE_MIGRATION_PLAN.md, AI_EDITOR_PRIMITIVES.md, OFFICE_ADDIN_PLAN.md, AI_ARCHITECTURE.md, PLUGIN_SPEC.md}`.

**Bielik** (`…/dandi-91/LibreOffice-Bielik-Agent/main/`): `README.md`, `SECURITY.md`, `RELEASING.md`, `bielik_agent.py`, `build.py`, `build.sh`, `build.bat`, `Sidebar.xcu`, `Factories.xcu`, `Addons.xcu`, `META-INF/manifest.xml`, `description.xml`, `.github/workflows/release.yml`.

**LibreOffice core / adjacent facts**: `sfx2/source/sidebar/{ResourceManager.cxx, Context.cxx}`, `vcl/source/window/EnumContext.cxx`, `officecfg/registry/data/org/openoffice/Office/UI/Sidebar.xcu`; `https://raw.githubusercontent.com/unoconv/unoserver/master/{README.rst, src/unoserver/server.py}`.

**Third-party prior art named by Claude-Connector's `COMPETITOR-STUDY.md` / `UPSTREAM-PARITY.md`** (not independently read in this survey): `quazardous/nelson-mcp`, `KeithCu/writeragent`, `sandraschi/libreoffice-mcp`, `patrup/mcp-libre`, `mostlyblocks/CalcuLLM`, `smonux/libreoffice-llm-plugin`, `aronweiler`/`devilish84` libre-ai.

**Local experiments** (this machine, LibreOffice 26.2.5.2 + `python3-uno`): UNO socket acceptor on `127.0.0.1:2002`, cross-process attach/load/insert, `RecordChanges`+`getRedlines()`, `createReplaceDescriptor`/`replaceAll`, `XUndoManager.enterUndoContext/leaveUndoContext` + `undo()`. Scripts under `.research-scratch/unotest/`.
