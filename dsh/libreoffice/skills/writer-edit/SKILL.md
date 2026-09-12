---
name: writer-edit
description: Edit a LibreOffice Writer document while preserving its structure and formatting — rewrite sections, fix prose, tighten wording, apply styles. Use for any substantive text change.
---

# Editing a Writer document

## Read the structure before touching the text

`document_outline` gives every paragraph with its style. Work from that, not from
the flat text, so you can see which paragraphs are headings, which are body, and
where the document's own conventions are.

## Prefer the smallest correct edit

1. `document_find` to confirm the text is what you think it is.
2. `document_replace` for a surgical change — one `replaceAll` is a single
   undoable action and preserves surrounding formatting.
3. `document_append` only for genuinely new content, and pass
   `paragraph_style` so it matches the document instead of inheriting whatever
   the last paragraph used.

Match the document's existing voice, tense, and spelling convention. Do not
upgrade its register unless asked.

## Preserve formatting deliberately

Replacing a selection wholesale can drop character formatting. If a passage has
mixed formatting — bold terms, a citation, a colour — make several small
replacements instead of one large overwrite.

## Review mode

For anything substantive, turn on `document_track_changes` **before** editing.
The user then sees your work as revisions they can accept or reject, which is the
difference between an assistant and something that silently rewrites their
document. Turn it off when the user asked for a direct edit.

## Finish

Call `document_end_turn` once at the end so the whole edit is one undo step.
Then summarise: what you changed, where, and anything you deliberately left
alone. If you were asked to improve how it reads, render and check it.
