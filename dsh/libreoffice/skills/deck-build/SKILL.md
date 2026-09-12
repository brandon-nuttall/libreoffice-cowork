---
name: deck-build
description: Build or restructure a LibreOffice Impress presentation — outline to slides, consistent layout, readable text. Use for decks, presentations, and slide content.
---

# Building a presentation

Impress support is the least mature part of the document tools. Verify what is
available with `document_outline` before promising a deck, and tell the user
plainly if an operation is not yet supported rather than producing something
partial.

## Structure first

Agree the outline before generating slides: a title, then one idea per slide.
A deck that states one thing per slide beats a deck that states five.

## Text discipline

- Headings short enough to read at a glance — a few words, not a sentence.
- Body text as fragments, not paragraphs. If a slide needs a paragraph, it is
  probably two slides.
- Never shrink type to fit. Cut words instead.

## Verify visually

Call `document_render` and read the images. On slides, check specifically for:

- text overflowing a placeholder or the slide edge,
- overlapping shapes or text boxes,
- a title that collides with the body area,
- inconsistent margins between slides,
- low-contrast text against a coloured background.

This matters more on slides than anywhere else: a slide is looked at, not read.
