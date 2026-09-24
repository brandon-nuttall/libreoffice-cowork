# Chat UI design — bubbles and rendered markdown in the Cowork sidebar

Status: **design, not yet implemented**. Written per instruction to think through
and document before starting any work. The previous bubble design (commits
`30590f5`–`1aa48eb`) was reverted in `d1135df`; this document explains why it
failed, what is now known that it did not, and what will be built instead.

---

## 1. The measured constraints

Everything here was measured in a live sandbox office; nothing is estimated.

| # | Fact | Evidence |
|---|---|---|
| C1 | Sidebar panel inner width ≈ 220 units (~12–18 characters/line at 10pt) | `layout: parent=232x721 inner=220` in panel logs |
| C2 | `UnoControlFixedText` with `MultiLine=True` **wraps natively** — 6 physical lines rendered for a 140-char label at width 220 | probe 2 (accessible bounds) |
| C3 | A realised control exposes its rendered line geometry: accessible `getCharacterBounds()` reports first char at y=5, last at y=60 → line pitch ≈ 11 units at 10pt, readable at runtime | probe 2 (accessible bounds) |
| C4 | `FixedText` paints `BackgroundColor` (bubble fill) — probe probe 2 sets `0x3A3A3A`; visible in earlier sandbox captures | probe 2 |
| C5 | `getPreferredSize()` works **only when the control has a real parent window**; a parentless `createPeer(tk, None)` crashed the UNO bridge. Preferred size reports the *unwrapped* width (845 units for 144 chars), so it is usable for line height, useless for wrap boundaries | probe 1; `metrics.py` incident |
| C6 | `XDevice.createFont` is not reachable from the Python bridge (`AttributeError`), so classic text measurement via `XGraphics` is unavailable | probe 2b |
| C7 | A readonly multi-line `UnoControlEdit` wraps, scrolls, and is text-selectable | the current production panel |
| C8 | `FixedText` is not text-selectable | toolkit behaviour |
| C9 | The design/acceptance loop costs ~1–2 min per cycle (rebuild oxt → reinstall → relaunch → capture) | whole session history |
| C10 | LibreOffice emits `agent/assistant-stream` frames (`{type:'chunk', chunk:{type:'text-delta', text}}`) on the agent scope — streaming tokens are receivable | serve-fix commits |

C2 + C3 are the load-bearing facts. The previous design hand-wrapped text against
a guessed per-character width (`CHAR_WIDTH`) and positioned one label per line.
C2 removes hand-wrapping entirely; C3 replaces *predicted* heights with
*observed* heights. Together they retire most of the failure modes from
FINDINGS F42–F55.

## 2. What the user experiences (target)

WhatsApp-style / Claude-cowork-style conversation:

- The **user's messages sit in a tinted bubble**, indented from the left edge.
- The **assistant's messages are plain text on the panel background** — no label,
  no bubble, matching Claude for Word (research F42: "the assistant's replies are
  plain text; the user's messages are the ones that need distinguishing").
- The assistant's markdown is **rendered structurally**: headings bold and larger,
  bullets as prefixed lists, code in monospace on a slightly darker block,
  a rule as a divider line. Inline emphasis (bold/italic *inside* a sentence) is
  deliberately dropped in the sidebar — headers and structure carry the shape;
  detail lives in the document. (Matches the sidebar's restraint elsewhere.)
- New turns **autoscroll** into view.
- A **Copy** affordance puts the whole conversation on the clipboard as clean
  text (C8 says per-bubble selection won't exist; the button restores
  copy-paste, which the user explicitly needed).

## 3. Architecture

### 3.1 One control per message block — not per line

The fatal choice last time was one label per *rendered line* (stripes, stale
geometry). Now: one control per **message block**.

- A user turn is almost always one paragraph → **one** `MultiLine FixedText`
  with `BackgroundColor = _COLOR_USER_BUBBLE`, x-inset to suggest the side,
  `TextColor` white. One control, one bubble.
- An assistant turn renders one control per markdown block:
  - `paragraph` → plain `FixedText`, no background
  - `heading` → `FixedText` with `FontWeight=150` and `FontHeight+1`
  - `bullet` → `FixedText`, label `"• " + text`, small left indent
  - `code` → `FixedText`, mono font, darker background block
  - `rule` → 2-unit-tall `FixedText` with background (a hairline divider)

The markdown pipeline already exists (`cowork_markdown.py` via LibreOffice's own
filter) and is unchanged by this design.

### 3.2 Layout by rendering — the two-pass measure

C3 makes observed geometry cheap, and C6 blocks the measuring alternative, so
heights are **read back from the controls themselves**:

```
pass 1 (measure):  for each block control, in order:
                     setPosSize(x, y_cursor, width, TALL, POSSIZE)   # TALL = generous
                     y_cursor += pitch(block)                         # provisional
pass 2 (commit):   re-walk in order:
                     (fx, fy) = accessible.getCharacterBounds(last_char_index)
                     content_h = fy + line_pitch                      # observed
                     setPosSize(x, y, width, content_h + pad, POSSIZE)
                     y += content_h + gap[block] + gap_message[new-speaker]
                   total_height = y   → scrollbar range
```

Every number the renderer needs is either a constant (gaps, insets, colours) or
read from the toolkit. No character-width model exists anywhere in the module.

`cowork_chat_layout.py` remains a **pure function** (given measured block heights
→ positions, offsets, scrollbar range, control-pool plan) so the arithmetic keeps
its unit tests, exactly as `cowork_layout.py` does now.

### 3.3 Auto-calibrated line pitch

Line pitch (C3) is measured once per session at startup: render a known two-line
calibration label, take `Δy / (lines−1)`. This self-adapts to font, DPI and theme
instead of hard-coding 11 units.

### 3.4 Scrolling

The existing manual `ScrollBar` + `XAdjustmentListener` wiring (proven, incl. the
listener-loop guard from F-era) is kept. Offset math lives in the pure layout
function; the renderer sets `setValue`. Autoscroll-to-newest on turn end.

### 3.5 Control pool and disposal

Controls are pooled per slot key `msg<turn>-blk<index>` and reused across
re-renders; the pool list is initialised **before** first render (the
`_transcript_bubbles` AttributeError crash from F51 is a named regression with
a statics check now); `clear()` disposes.

### 3.6 Copy

A small `Copy` button next to `Clear` writes the full transcript (built by the
existing `to_plain_text`, which already produces clean paragraphs) to the system
clipboard via `com.sun.star.datatransfer.clipboard.SystemClipboard`.
Fallback if the clipboard service is unavailable from extension context: the same
text is placed in a hidden readonly `Edit` and focused for native Ctrl+A/Ctrl+C.
(Acceptance test will assert on clipboard contents.)

### 3.7 Empty state

Rebuilt on the same block machinery (centred block of labels), replacing the
current text-only empty state inside the Edit — which ceases to exist once the
transcript becomes controls. Suggestion chips become real buttons in Phase 2.

## 4. Options considered and rejected

| Option | Verdict | Why |
|---|---|---|
| **A.** Single readonly `Edit`, prefix-marked turns (status quo) | keep as fallback | selectable + robust, but flat; not the friendly interface requested |
| **B.** MultiLine `FixedText` per block, layout-by-render (**chosen**) | chosen | native wrap (C2), observed heights (C3), bubbles via C4, undoable failure history all addressed |
| **C.** Embed a real Writer view inside the panel | rejected for now | embedding API unproven in this context; heavyweight; Kubernetes-of-uncertainty for little gain over B |
| **D.** Render the chat into a bitmap (`VirtualDevice`) | rejected | no text copy at all — violates the user's explicit copy requirement |
| **E.** Label stack with hand wrap (the old design) | rejected | precisely the reverted failure; superseded by C2/C3 |

## 5. Phases

- **Phase 1 — core transcript.** Block renderer, two-pass layout, bubbles,
  scroll + autoscroll, Copy button, pool/disposal, unit tests, sandbox
  pixel-acceptance (user-bubble colour sampled inside bubble rect; heading bold
  visible; no clipped first chars at min width).
- **Phase 2 — empty state & chips.** Centred empty state on block machinery;
  suggestions as buttons that fill the composer.
- **Phase 3 — polish.** Timestamps, retry affordance, wider-panel refinements.

Phase 1 is the whole of the visible change; nothing in phases 2–3 blocks it.

## 6. Risks and pre-registered mitigations

| Risk | Mitigation |
|---|---|
| Accessible bounds only valid on realised, visible controls | layout passes run only after the container exists; F51-style init-order guard + statics check |
| Very long single paragraphs (e.g. the 6-minute research reply) → control taller than viewport | scroll exists; soft cap: paragraphs > N lines get an auto "show more" collapse in Phase 2 |
| Control count growth over long sessions | pool reuse; blocks beyond K turns trimmed from the top (with a "Earlier messages trimmed" notice), transcript text kept intact for Copy |
| Clipboard API unavailable in extension context | fallback path §3.6; acceptance test asserts whichever path engaged |
| Loop cost C9 (every visual check a full cycle) | acceptance batched per cycle: screenshot + pixel checks + clipboard check + conversation test in one run; unit tests carry the arithmetic so GUI cycles only verify appearance |

## 7. Acceptance tests (Phase 1)

1. `tests/test-chat-layout.py` — pure layout: measured-height stacking, offsets,
   scrollbar range, autoscroll target, pool plan, copy-string building.
2. Sandbox integration — one run captures **all** of:
   - transcript screenshot after a real two-turn conversation (one user bubble
     visible; heading + bullet + paragraph reply rendered distinctly);
   - pixel sample: bubble rect interior ≈ `0x3A3A3A`;
   - clipboard content after `Copy` equals the expected clean text;
   - no `Traceback` in the sidebar log;
   - pre-existing suite green (`run-all.sh`).
3. Narrow-width pass: same run at `getMinimalWidth()` — no clipped characters
   anywhere (the "hello w" regression, guarded).

## 8. Prior art honoured

- Claude for Word: assistant plain, user messages distinguished; content
  inherits document/template conventions rather than imposing its own.
- WhatsApp: bubble on one side, clear gap between speakers, everything readable
  at small width — but its bubble text is system-rendered, and the render engine
  here is `FixedText`, so the visual ceiling is "boxed tinted paragraphs", not
  rounded speech balloons. Rounded corners are not available in the toolkit;
  squares are the honest shape and read fine at this size.
