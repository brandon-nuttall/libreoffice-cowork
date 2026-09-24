---
name: document-taste
description: What makes a document look professional and what makes it look machine-generated — the visual taste, the anti-patterns, and the concrete style rules for LibreOffice Writer. Use when creating or restructuring any document, or whenever the formatting of output matters.
---

# Document taste

## The taste

Professional documents share one property above all others: **consistency**. If one
principle separates professional work from amateur output, it is that every element
of the same kind looks the same way, every time. A reader should never wonder
whether two things at the same level are at the same level.

The second property is **restraint**: one typeface per document, three sizes at
most, standard margins, and no decoration. The mark of amateur work is variety —
five fonts, centered one place and flush-left the next, spaced inconsistently,
treating every paragraph as a fresh design decision.

The third is **semantic structure**. In Writer, styles mean you "shift the emphasis
from what the text looks like to what the text *is*." A heading is a Heading style,
not uppercase text; body text is `Text body`; a list is a list style. Semantic
structure is what makes the outline navigator, table of contents, screen readers
and cross-references work at all. A document of all-`Standard` paragraphs — so every
paragraph is visually identical and the heading hierarchy exists only as
typographic guesses — is the clearest single sign that a machine produced it, and
it is the thing that makes nobody want to use the output.

## What good looks like

- The title is on the first line in the Title style; a man could read only the
  outline and know the document's shape.
- Heading 1 for the document's major sections, Heading 2 for subsections, Heading 3
  for sub-subsections (rarely needed; two levels is enough for most work).
- Descriptive headings that say something ("Breath: how many insurers use AI")
  rather than functional ones ("Section 4").
- Body text in Text Body throughout; one typeface; size 10–12pt.
- Space between paragraphs comes from the paragraph style's spacing, not from
  pressing Enter an extra time.
- The whole document could be restyled by changing six definitions, because the
  text is tagged semantically.

## What bad looks like — never do these

- **ALL CAPS text pretending to be a heading.** If it is a heading, it is a
  Heading style, and it is not uppercase.
- **Manual formatting where a style exists.** Character weight on a "heading",
  blank paragraphs for spacing, numbered sections typed by hand.
- **Every paragraph in Standard.** That is the default and contains no information
  about the document's structure.
- **Blank lines between paragraphs.** Set the spacing in the style; do not emit
  empty paragraphs to fake a gap.
- **Ad-hoc decoration**: emoji, exclamation marks, horizontal rules typed with
  dashes — these don't belong in a professional document.
- **A dense wall of text.** If a section runs past a page, look for a list or a
  table trying to happen.

## The concrete rules

### Styles by role (LibreOffice Writer)

| Role | Style | Notes |
|---|---|---|
| Document title | `Title` | One per document, on line one. |
| Subtitle / byline | `Subtitle` | Below the title, e.g. author or date. |
| Major sections | `Heading 1` | Also feeds the outline navigator and TOC. |
| Subsections | `Heading 2` | The workhorse level. |
| Sub-subsections | `Heading 3` | Only when genuinely needed. |
| Body text | `Text body` | Everything that is not a heading, list, quote or code. |
| Pull quote / block quote | `Quotations` | For blocks of quoted text, not inline citations. |
| Code / commands / fixed-width | `Preformatted Text` | Preserves line breaks; monospace by default. |
| Ordered list | `List 1` | When order matters. |
| Unordered list | `List 1` / bullets | When order doesn't. |

### Character-level emphasis

Use direct character formatting sparingly (the tools expose `CharWeight` and
`CharPosture`), and use the character styles where they fit:

- `Strong Emphasis` — the built-in semantic bold.
- `Emphasis` — the built-in semantic italic.

Bold for a term being defined, italic for a citation or a document title being
referenced; not both throughout a paragraph.

### Layout conventions

- **Margins:** the default (2.54 cm / 1 inch) is correct. Do not change them
  without being asked.
- **Line spacing:** 1.15 for business documents. Do not emit double-spacing
  unless the document is academic work being submitted for review.
- **Paragraph spacing:** LibreOffice's `Text Body` already has spacing after;
  rely on that rather than adding blank paragraphs.
- **Alignment:** left-aligned (not justified) for body text in most business
  documents — justified text needs hyphenation to avoid rivers.
- **Colour:** default charcoal/black. Use colour deliberately (a heading accent)
  or not at all; never more than two.

## Recipes

### New document / outline (e.g. a research outline)
1. `Title` — the subject, in one line.
2. `Subtitle` — the byline, date or one-sentence framing.
3. `Heading 1` for each major section: descriptive, not numbered ("Evidence I —
   Breadth" not "Section 4").
4. `Text Body` for prose. `List 1` for lists. `Quotations` for quoted material.
5. If a section needs subsections, use `Heading 2` — and ask yourself whether
   three levels is genuinely better than two.

### Editing an existing document
1. `document_outline` first: read the styles the document already uses.
2. Match the document's own conventions. If it uses `Heading 2` for its top
   sections and nothing deeper, do the same.
3. Pass `paragraph_style` on every `document_append` so new material matches
   instead of inheriting whatever the last paragraph used.

### The one-sentence test
Look at the outline navigator. If its tree reads like a summary — sections with
real names, in a sensible order — the document has structure. If it shows one
flat line of body text, the document is unstructured and needs fixing before
anyone writes another sentence into it.