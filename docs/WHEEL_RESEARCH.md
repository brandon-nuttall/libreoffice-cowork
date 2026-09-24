# Mouse wheel support in the sidebar — deep dive

Research question: *why doesn't the wheel work over the Cowork sidebar panel, and
what would it take to add support?* Everything below traces the actual event path
in LibreOffice's source (master branch as of this writing) and the project's own
runtime probes. File: `docs/WHEEL_RESEARCH.md`.

---

## 1. Where a wheel event actually travels

From `vcl/source/window/winproc.cxx` (upstream master):

```
GTK3 backend → SalFrame wheel → ImplWindowFrameProc( ..., SalEvent::Wheel, SalWheelMouseEvent )
  → ImplHandleWheelEvent(pWindow, rEvt)
    → HandleWheelEvent::HandleEvent()
        1. FindTarget()   — window under the pointer; walks UP only past
                           DISABLED windows (`acceptableWheelScrollTarget`).
        2. Reuse heuristics — if the same screen position was scrolled by another
                           window < 500ms ago, reuse that window.
        3. Dispatch(pMouseWindow):
             CallCommand(pWin, ...) → CommandEvent(CommandEventId::Wheel,
                                       CommandWheelData(delta, notches, lines…))
             → pWin->Command(aCEvt)  ← ← ← THE ONLY HOOK. A window "handles"
                                       the wheel by overriding Command() and
                                       consuming the event (sets mbCommand).
        4. If unhandled: retry once on the FOCUS window — then the event DIES.
```

The upstream comment on the reuse heuristic is itself evidence this is a known,
messy area: *"so scrolling down something like the calc sidebar that contains
widgets that respond to wheel events will continue to send the event to the
scrolling widget in favour of the widget that happens to end up under the
mouse."* — sidebar wheel mingling is a real, patched-over problem upstream.

**Key facts, with receipts:**

| # | Fact | Source |
|---|---|---|
| W1 | Wheel is a **VCL `CommandEvent`**, handled by C++ `Window::Command()` overrides | winproc.cxx: `ImplCallWheelCommand`, `CommandWheelData` |
| W2 | Unhandled wheel does **not** walk up the parent chain — it retries the focus window only, then dies | winproc.cxx: `HandleGestureEventBase::Dispatch` |
| W3 | VCL windows that scroll do so by owning `vcl::ScrollBar`s and calling `HandleScrollCommand` from their `Command()` override | Classic VCL pattern used by Edit / ScrollableWindow / trees |
| W4 | The stock sidebar deck wraps ALL panel content in a C++ `weld::ScrolledWindow` | `sfx2/source/sidebar/Deck.cxx`: `mxVerticalScrollBar`, step 10 / page 100 |
| W5 | A UNO extension panel cannot override `Window::Command` — the extension only ever sees UNO controls | This is the structural wall |

## 2. What UNO AWT exposes (and doesn't)

Checked against both the SDK reference and the live 26.2 type registry:

| Probe | Result |
|---|---|
| `css::awt` module reference — full interface list | **No `XMouseWheelListener`, no `MouseWheelEvent`** |
| Runtime: `from com.sun.star.awt import XMouseWheelListener` | ImportError (type unknown, LO 26.2.5.2) |
| Runtime: `uno.createUnoStruct("com.sun.star.awt.MouseWheelEvent")` | "unknown" |
| `css::awt.MouseWheelBehavior` constants group | EXISTS — since OOo 3.2 — but is only a **per-control behaviour toggle**: `SCROLL_DISABLED` / `SCROLL_FOCUS_ONLY` / `SCROLL_ALWAYS` ("the mouse can be used to scroll through the control's content … as long as the mouse pointer is over the control") |

So the platform acknowledges wheels exist, exposes a *policy* for the handful of
controls with built-in scrolling (text areas, grids, trees), and exposes **no
event** from which an extension could implement the same behaviour on its own
widgets. Our transcript is a container of `UnoControlFixedText` — no built-in
scrolling, no wheel hook, dead end at the mouse window, focus-window trial
lands on the composer edit whose own scroll does nothing visible.

## 3. Why the stock decks scroll but ours doesn't

* Stock panels' content widgets are C++ windows with their own `vcl::ScrollBar`s
  and `Command()` overrides (W3).
* The Deck wraps everything in a `GtkScrolledWindow` (W4) — on GTK, wheel events
  *chain* from child widgets to ancestor scrolled windows, and the deck's
  scrollbar (step 10 / page 100) scrolls whatever overflows.
* BUT: `DeckLayouter::LayoutDeck` sizes each panel to exactly the available
  height — a single panel that *fills* the deck never overflows, so the deck
  scrollbar is inactive and the GTK chain has nothing to move. Our chat content
  overflows *inside our own container*, which the deck cannot see.

## 4. Options, ranked after the dive

### Option A — upstreame the missing surface (the real fix)

Add wheel exposure to UNO AWT: `css::awt::XMouseWheelListener` +
`XMouseWheelEvent` (delta / notches / scrollLines / horizontal — all already
fields of `CommandWheelData`), fired from `VCLXWindow`'s existing command
funnel (`Command()` path at `CommandEventId::Wheel`), behind
`XWindow::addMouseWheelListener`. Everything the event carries already exists
in `CommandWheelData`; the listener funnel (`ImplNotifyKeyMouseCommandEventListeners`)
is the same machinery mouse/key listeners already use. Estimated scope: offapi
IDL + `toolkit/source/awt/vclxwindow*` fire/subscribe + api tests. Then the
sidebar subscribes on the transcript container, maps delta → `_on_wheel`, done.

This is a ~100-line upstream patch and is the only solution that is honest
about the platform. Worth writing and submitting; the
`MouseWheelBehavior` precedent shows the API surface already half-admits the
need.

### Option B — the focus-window seam (works today, UX-sensitive)

W2 gives an exploitable seam: unhandled wheel retries the **focus window**.
Make the focus window *our UnoControlScrollBar* while the pointer is over the
transcript (mouse-move listeners on the labels already exist as a pattern —
`XMouseMotionListener` IS exposed), restore focus on leave. Then wheel-over-
panel → mouse window unhandled → focus window = the scrollbar → scrolls → our
existing `XAdjustmentListener` updates the transcript. Risks: focus stealing
conflicts with typing in the composer mid-turn; needs careful hand-off.

### Option C — synthetic wheel from inside is impossible, synthetic tests outside are not

No in-process event injection exists (no API), **but** the harness can drive
*XTEST* synthetic wheel (`python-xlib` is available; no xdotool needed) to
reproduce real wheel events over the sandbox panel — `tests/wheel-inject.py`.
This is the verification loop every candidate hack needs.

## 5. Test plan

1. `tests/wheel-inject.py <display> <x> <y> <+n/-n>` — XTEST button 4/5 at the
   sandbox panel.
2. Log fingerprints: `_on_wheel` / adjusted-entry lines in the sidebar log.
3. Acceptance: wheel at panel centre scrolls transcript by N notches; wheel
   over the composer does not hijack typing; wheel over the scrollbar strip
   stays native.

## 6. Current status

The deep dive is complete; no extension behaviour changed in this step. The
recommendation stands: pursue Option A upstream and Option B as the interim,
in that order, with the XTEST harness as the acceptance loop for both.
