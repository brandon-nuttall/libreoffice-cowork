---
name: document-taste
description: The visual and prose taste for LibreOffice Writer documents — match the document's own conventions first, write prose not bullet walls, and refuse the machine-generated tells. Use when creating or restructuring any document, or whenever formatting matters.
---

# Document taste

## The first rule: the document is already right

Before imposing anything, read what the document already does — `document_outline`
shows every paragraph with its style. A document that uses `Heading 2` for its
sections wants more `Heading 2`; one that uses `Title` followed by `Text body`
wants exactly that. New content **inherits the document's heading and paragraph
styles**; it does not arrive with its own. This is the whole trick in Claude for
Word, and it is why its output looks native rather than pasted in: the template
was never replaced, only filled.

Only when a document is empty, or the user asks for a new structure, do the
defaults below apply.

## The taste

Professional documents share three properties:

- **Consistency.** Every element of the same kind looks the same way, every time.
  If two things are at the same level, no reader should have to wonder whether
  they are. Inconsistency is what makes work look amateur.
- **Restraint.** One typeface. Three sizes at most. Standard margins. No
  decoration. The mark of amateur work is variety — treating every paragraph as a
  fresh design decision.
- **Semantic structure.** A heading is a Heading style, not uppercase text. Styles
  shift the emphasis from what text looks like to what the text *is* — which is
  what makes the outline, the table of contents, screen readers and
  cross-references work *at all*. An all-`Standard` document has no structure,
  only guesses.

## The machine tells — never do these

These are the tells that mark a document as machine-generated. They are named,
with examples, because a named anti-pattern is easier to refuse than an abstract
rule:

- **The uppercase pseudo-heading.** "SECTION 4: INTRODUCTION" in body text. If it
  is a heading, it is a `Heading 1` style, and it is not uppercase.
- **The bullet wall.** A section of nothing but bullets. Write prose for
  narrative, analysis and explanation; use a list only when the content is
  genuinely enumerable — steps, options, items. A bullet is not a paragraph.
- **The fake gap.** Blank paragraphs used for spacing. The paragraph style already
  carries spacing after; emitting empty paragraphs doubles the gap and breaks
  list numbering.
- **The functional heading.** "Section 4" tells the reader nothing. Describe what
  the section says: "Evidence I — breadth: how many insurers use AI".
- **The dense paragraph.** Three ideas braided into one sentence so long it must
  be re-read. Dense prose does not mean the idea is deep; it usually means the
  idea has not been compressed yet. Short sentence. Then the point.
- **The opener nobody asked for.** "Great question!" "Certainly!" "Here's the
  thing." Documents begin with their subject, not with a conversational warm-up.
- **The process caption.** "The environment's web search and web fetch tools were
  non-functional; all retrieval was done over HTTP from the shell plus structured
  APIs…" — that is agent telemetry, and it belongs in the sidebar chat, never in
  the document. The document records *consequences for the reader* ("This figure
  could not be verified against a primary source"); the *story of how the work
  went* — which tools failed, what was retried, which endpoints rate-limited — is
  a status update for the conversation, not prose in the deliverable.

## The defaults (for a new or unstructured document)

### Styles by role — exact Writer names

| Role | Style |
|---|---|
| Document title | `Title` |
| Subtitle / byline | `Subtitle` |
| Major sections | `Heading 1` |
| Subsections | `Heading 2` |
| Sub-subsections | `Heading 3` (rarely needed — two levels is usually enough) |
| Body text | `Text body` |
| Pull quote / block quote | `Quotations` |
| Code, commands, fixed-width | `Preformatted Text` |
| Lists (bulleted or numbered) | `List 1` |

The casing is exact: `Text body` (lowercase b), `Quotations` (plural). A wrong
name silently falls back to `Standard` — the very failure this skill exists to
prevent. Verify against the live document with `document_outline` if unsure.

### Character emphasis

Bold for a term being defined; italic for a cited work. Not both at once, not
through a whole paragraph. The built-in character styles are `Emphasis` (italic)
and `Strong Emphasis` (bold); use direct character formatting sparingly.

### Layout

- Margins: the default (2.54 cm) is correct. Do not change them unless asked.
- Body size: 10–12pt. Line spacing: 1.15 for business documents.
- Alignment: left-aligned (not justified) for body text — justified text needs
  hyphenation to avoid rivers, and Writer's default has none.
- Colour: default black. Use colour deliberately (one accent) or not at all.
- Spacing comes from the style, never from blank paragraphs.

## The finish check

Look at the outline navigator. If its tree reads like a summary of the document
— sections with real names in a sensible order — the document has structure. If
it shows one undifferentiated mass, the document is unstructured, and no amount
of well-written prose will make it usable. Fix the structure *before* writing
another sentence into it.