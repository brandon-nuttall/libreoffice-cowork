---
name: document-review
description: Review an open LibreOffice document for layout and presentation defects — overflow, contrast, spacing, orphaned headings, inconsistency. Use when the user asks for a review, a polish pass, or "does this look right".
---

# Reviewing a document

A review is two passes, and both are required. The deterministic pass finds what
can be measured; the visual pass finds what only an eye catches.

## Pass 1 — deterministic checks

Call `document_check_layout`. It reports low-contrast text (WCAG ratio under
4.5:1), unbreakable tokens long enough to overflow the text area, and spacing
done with blank lines rather than paragraph styles.

Fix nothing yet. Read `document_outline` first so you know which paragraph
carries each problem, and whether the styles are being used consistently.

## Pass 2 — look at the page

Call `document_render`, then read the returned image paths. Judge the result on
its appearance, not on the markup:

- **Overflow** — does any line cross the text area, and does a long token push
  past the margin?
- **Overlap** — does anything collide with an image, table, or frame?
- **Contrast** — is any text hard to read at normal size?
- **Hierarchy** — can you tell headings from body at a glance, or is everything
  the same weight?
- **Rhythm** — is spacing consistent between sections, or does it collapse in
  places?
- **Orphans** — is a heading stranded at the foot of a page from its body?

## Pass 3 — report, then offer to fix

Report what you actually saw, with location. Separate "this is a defect" from
"this is a style preference". Then offer to fix the defects — do not rewrite a
document the user only asked you to review.

## Rules

- Never claim a page looks right without having rendered it.
- Do not fix layout by adding blank paragraphs. Use paragraph styles; that is
  what the spacing check is warning you about.
- If the document has never been saved, `document_render` cannot run. Say so and
  ask the user to save it rather than guessing.
