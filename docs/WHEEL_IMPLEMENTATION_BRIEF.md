# Wheel support for the Cowork sidebar — implementation brief

**Audience:** a coding agent taking over this task. Read together with
`docs/WHEEL_RESEARCH.md` (the source forensics this brief builds on) and
F59–F66 in `research/FINDINGS.md` (the rendering platform this plugs into).

**Verdict up front: feasible.** Two viable routes; recommend trying B' first
(small, contained, in-repo) with A (upstream) as the durable fix. There is NO
zero-code fix — §3 explains why — but B' is roughly a dozen lines plus guarding.

---

## 1. The problem, in one paragraph

Wheel scrolling over the Cowork chat transcript does nothing. The wheel is a
**VCL C++ event** (`SalEvent::Wheel` → `CommandEvent(CommandEventId::Wheel)` →
`Window::Command()`), handled only by windows that override `Command()` and own
`vcl::ScrollBar`s. UNO AWT exposes **no wheel event and no listener** (verified:
module reference + live 26.2 registry rejections). The transcript is built from
`UnoControlFixedText` inside an `UnoControlContainer` — none of which see the
wheel. The event dies at the mouse window unless the **focus window** happens
to be scrollable and consumes it.

## 2. Verified upstream facts (do not re-litigate)

- `winproc.cxx`: `ImplHandleWheelEvent` → `HandleWheelEvent::FindTarget()`
  → `HandleGestureEventBase::Dispatch()` → `ImplCallWheelCommand` fires
  `CommandEvent(CommandEventId::Wheel)` with a `CommandWheelData`
  (delta / notches / scrollLines / horizontal — all present).
- Unhandled wheel → **one retry on the focus window**, then dropped. **No
  parent walk.**
- 500 ms same-position reuse heuristic exists (comment cites sidebar widgets
  reacting to wheel as the motivating problem).
- `css::awt.MouseWheelBehavior` constants exist (DISABLED/FOCUS_ONLY/ALWAYS)
  as a *policy* for built-in-scrollable controls. There is no event surface.
- `Deck.cxx` wraps deck content in a `weld::ScrolledWindow`, but
  `DeckLayouter` sizes panels to the available height — a panel that fills the
  deck never overflows, so the deck scrollbar can never scroll our pane.
- Runtime probes on LO 26.2.5.2: `import XMouseWheelListener` fails;
  `MouseWheelEvent` struct unknown. No `MouseWheelBehavior` property exists on
  any of our controls (probed all seven live).

## 3. Why zero-code is impossible

The focus fallback lands today on the **Writer canvas** (the panel starts
unfocused) — so wheeling over the sidebar currently scrolls the *document
behind it*. There is no policy surface (like `MouseWheelBehavior`) on a plain
`UnoControlContainerModel`/`FixedTextModel` to redirect that, and no
configuration to change the focus default. Any fix is code.

## 4. Route B' — click-anchored focus (interim fix, in-repo)

**Mechanism:** clicking anywhere in the transcript sets focus to the
`UnoControlScrollBar` (the only UNO control VCL scrolls natively). Wheel events
over any transcript element are then unhandled at the mouse window, fall back
to the focused scrollbar, scroll it, and the panel's existing
`XAdjustmentListener` repositions the conversation.

**Scaffolding already in the tree (uncommitted)** — `_TranscriptClick`
(listens on the container and every pooled label) calls
`element._control_by_name("scrTranscript").setFocus()` on press and logs
`CLICK: scroll focus anchored`. Registered at build time behind an
`_HAS_MOUSE` import guard, with `ADJUSTED value=` logged from
`_ScrollListener.adjusted()`.

**The unverified hinge:** whether a focused VCL `ScrollBar` window consumes
`Command(Wheel)` delivered to it. Plausible (scrollbar scroll code lives in
VCL C++), but **this is the first thing to test** — all downstream work is
moot if it fails. The failure mode is benign: nothing scrolls.

**Click-anchoring was chosen over hover-anchoring deliberately:** hover focus
fires when the pointer merely *crosses* the transcript, which rips focus from
the composer mid-sentence and loses keystrokes. Click is an unambiguous intent
signal, and focus returns on whichever window the user clicks next.

**Limitations to state honestly in the UI:** wheel requires one click in the
pane after opening; wheel-over-the-scrollbar-strip is already native.

## 5. Route A — upstream (the durable fix)

Add to offapi + toolkit (+ fire in vcl's `Command()` funnel where
`CommandEventId::Wheel` already carries `CommandWheelData`):

- `css::awt::XMouseWheelListener` (`mouseWheelMoved(XMouseWheelEvent)`)
- `css::awt::XMouseWheelEvent` struct: `WheelDelta`, `WheelNotches`,
  `ScrollLines`, `Horizontal`
- fire in `VCLXWindow`'s command handling for `CommandEventId::Wheel`
- `XWindow::addMouseWheelListener`/`removeMouseWheelListener`

All data exists at the funnel; the listener dispatch is the same machinery
mouse/key listeners use. Then the sidebar subscribes on the transcript
container and calls the same `_on_wheel(delta)` path B' uses. Submit upstream
(gerrit) referencing `docs/WHEEL_RESEARCH.md`; the `MouseWheelBehavior`
precedent shows the API gap is acknowledged.

## 6. Verification protocol (use this; it is proven)

`tests/wheel-inject.py` injects **real** wheel events via XTEST (works; proven
against a live office — the production event path, not a simulation):

```
./tests/sandbox-ui.sh start                       # or any office on :99
python3 tests/wheel-inject.py <x> <y> --click     # anchor focus (B')
python3 tests/wheel-inject.py <x> <y> +5          # 5 notches down
python3 tests/wheel-inject.py <x> <y> -5          # 5 notches up
tail -20 ~/.cache/cowork-sidebar.log              # or $COWORK_SIDEBAR_LOG
```

Success fingerprint for B': log shows `CLICK: scroll focus anchored`, then
`ADJUSTED value=…` lines, and the visible transcript scrolls down then back.
Failure fingerprints distinguish cleanly: no `CLICK:` → listener wiring; no
`ADJUSTED` → the focused scrollbar didn't consume the wheel (hinge failed);
`ADJUSTED` but no movement → renderer repositioning bug.

## 7. Invariants for the implementer (each has scar tissue from F51–F66)

1. **Scrolled axis:** every strip/filler/rect positioned inside the
   transcript must subtract `_used` exactly once. The seam fillers shipped
   broken twice for skipping this.
2. **`chat.plan()` must carry the full geometry** (`right`, `who`) — it
   rebuilds rows and silently dropping a key re-inflates bubble widths.
3. **Fillers paint in insertion-order z** (later `addControl` over earlier) —
   corner notches (if attempted) must be added to the pool *after* their
   neighbour strips.
4. New `import X…` guards live in module-level try/except; `_HAS_*` flag
   pattern is fine (the statics checker now understands try-scoped assigns).
5. `_describe_tool` must stay `@staticmethod` — the tests call it through the
   instance.
6. **One parser doctrine:** transcript markdown renders via
   `cowork_markdown.parse` only; never re-introduce the office roundtrip or a
   second fallback layer.
7. Any wheel handler must set `_autoscroll = False` (renders otherwise re-pin
   to the bottom; that was the "scrollbar doesn't work" bug class).
8. Listener registrations append to `self._listeners` and become inert via
   `disposing(self)` setting `self.element = None`.

## 8. Repository state warning (resolve before starting)

The working tree at hand-off contains **uncommitted changes** on
`ext/oxt-proto/components/cowork_sidebar.py` + `cowork_chat.py` from two
writers: (a) this session's verified work (padding rings top/bottom via
`PAD_V`, the seam-filler axis fix, per-turn parsing, click-anchor experiment
instrumentation, `@staticmethod` fix on `_describe_tool`) and (b) edits
observed **concurrently**, not by this session — the sidebar file was seen
changing between commands ("btnCommands" appearing/disappearing). Confirm with
the user who owns `btnCommands`-style composer work, settle the tree
(commit/stash), and only then branch. Commit nothing unverified.

## 9. Suggested order of work

1. Test the hinge (§4) in the sandbox with the harness — 10 minutes.
2. If the hinge holds: ship B' (wire click-anchor + wheel-scroll through
   `_on_scroll`, fix `Focusable` semantics if needed, document the click hint
   in the empty state).
3. In parallel, prepare the upstream patch (A) and file it; B' remains as the
   shim until a LibreOffice release ships the listener.
