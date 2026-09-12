---
name: calc-audit
description: Audit a LibreOffice Calc spreadsheet — find broken formulas, hardcoded overrides, inconsistencies, and errors. Use for spreadsheet review, "check my formulas", or locating a wrong number.
---

# Auditing a spreadsheet

## Read formulas and values together

`sheet_read_range` returns each cell's **formula and its computed value in one
call**. Use both: a wrong number with a correct formula means bad input data; a
wrong number with a wrong formula means bad logic; a value that is a bare
constant where its neighbours are formulas usually means someone overrode it.

## What to look for, in order of how often it matters

1. **Error values** — `#REF!`, `#DIV/0!`, `#NAME?`, `#VALUE!`, `Err:5xx`. A
   lowercased function name in the stored formula (`=image(...)`) is the tell
   that LibreOffice could not parse it — treat it as a broken formula, not a
   missing feature.
2. **Hardcoded values inside a calculated column.** Read the whole column and
   look for the cell that breaks the pattern. This is the single most common
   real spreadsheet bug.
3. **Inconsistent ranges** — a `SUM` that covers nine rows in a ten-row block,
   or an average whose range has drifted by one.
4. **Off-by-one in copied formulas** — a missing or misplaced `$` that only
   shows up when you compare the formula text across the row or column.
5. **Circular or self-referential chains** that only survive because iteration
   is enabled.
6. **Mixed units or currencies** in one column.

## Writing corrections

Formulas are translated from Excel syntax automatically, so write
`=SUM(A1:B10)` normally — do not hand-convert separators.

Write **cell by cell** (that is what `sheet_write_range` does) so the user's undo
stays honest. Never blanket-overwrite a range you have not read.

When you change a number, say what it was, what it is now, and why. An audit that
silently rewrites figures is worse than no audit.
