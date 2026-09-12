#!/usr/bin/env python3
"""cowork-uno — the document bridge between a running LibreOffice and the agent.

Design rules, each one paid for by an experiment (see research/FINDINGS.md):

* **Attach, never spawn.** This connects to the LibreOffice the user already has
  open, so the agent edits what is on screen. It never starts its own office.
* **One undo step per turn.** Every mutation runs inside
  ``XUndoManager.enterUndoContext``/``leaveUndoContext``, so a whole agent turn is
  one Ctrl-Z (F2/F10).
* **Never ``setDataArray``/``setFormulaArray``.** They create an undo entry that
  does not revert the cells (upstream bug). Cells are written individually (D5).
* **Frame-scoped binding.** Operations act on a named frame, never on the desktop
  singleton, so two open documents cannot be confused (T-3).
* **Refuse to guess.** An operation that cannot identify its target fails loudly
  rather than editing something plausible.

Speaks newline-delimited JSON over stdio:

    {"id": 1, "op": "read_text", "args": {...}}   ->   {"id": 1, "ok": true, ...}
"""

import json
import os
import re
import subprocess
import sys
import time

# ── UNO bootstrap ───────────────────────────────────────────────────────────
# LibreOffice's bundled python ships uno on the path; a system python3 needs
# python3-uno installed. Both are supported; we only require one of them.

try:
    import uno
    from com.sun.star.beans import PropertyValue
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    sys.stderr.write(
        "cowork-uno: the 'uno' module is unavailable (%s).\n"
        "Install the LibreOffice python bindings, e.g. 'sudo apt install python3-uno'.\n"
        % exc)
    raise SystemExit(3)


DEFAULT_PIPE = "lo-cowork-" + re.sub(
    r"[^a-z0-9-]", "-", (os.environ.get("USER") or "user").lower())


class UnoError(RuntimeError):
    """An operation failed for a reason the caller should see verbatim."""


# ── connection ──────────────────────────────────────────────────────────────

def _connect_ctx(ctx=None):
    """Resolve the running office's component context.

    Prefers the named pipe the extension opens at startup; falls back to a
    socket if the deployment configured one.
    """
    if ctx is not None:
        return ctx
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    candidates = []
    if os.environ.get("COWORK_ACCEPT"):
        candidates.append(os.environ["COWORK_ACCEPT"])
    else:
        candidates.append("pipe,name=%s" % os.environ.get("COWORK_PIPE", DEFAULT_PIPE))
    port = os.environ.get("COWORK_PORT")
    if port:
        candidates.append("socket,host=127.0.0.1,port=%s" % port)

    last = None
    for spec in candidates:
        url = "uno:%s;urp;StarOffice.ComponentContext" % spec
        try:
            return resolver.resolve(url)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise UnoError(
        "cannot reach LibreOffice. Tried %s. The last error was: %s\n"
        "If LibreOffice is not running, start it. If it is running, the Cowork "
        "extension may not be installed or active in that office."
        % (", ".join(candidates), last))


def _desktop(ctx):
    return ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", ctx)


def _pv(name, value):
    prop = PropertyValue()
    prop.Name = name
    prop.Value = value
    return prop


def _enum(container):
    """desktop.Components exposes only XEnumerationAccess — it has no getCount()."""
    out = []
    enumeration = container.createEnumeration()
    while enumeration.hasMoreElements():
        out.append(enumeration.nextElement())
    return out


def _frames(desktop):
    return _enum(desktop.Components)


def _is_writer(doc):
    return doc.supportsService("com.sun.star.text.TextDocument")


def _is_calc(doc):
    return doc.supportsService("com.sun.star.sheet.SpreadsheetDocument")


def _kind(doc):
    if _is_writer(doc):
        return "writer"
    if _is_calc(doc):
        return "calc"
    if doc.supportsService("com.sun.star.presentation.PresentationDocument"):
        return "impress"
    if doc.supportsService("com.sun.star.drawing.DrawingDocument"):
        return "draw"
    return "unknown"


def _find_doc(desktop, title=None, url=None):
    """Locate one open document by title substring or URL; never guesses.

    With exactly one document open that document is used. With several, an
    ambiguous request is an error rather than a coin flip.
    """
    docs = [d for d in _frames(desktop) if _kind(d) != "unknown"]
    if url:
        for doc in docs:
            if doc.getURL() == url:
                return doc
        raise UnoError("no open document with URL %r" % url)
    if title:
        needle = title.lower()
        matches = [d for d in docs if needle in (d.getURL() or "").lower()
                   or needle in _doc_title(d).lower()]
        if not matches:
            raise UnoError("no open document matching title %r" % title)
        if len(matches) > 1:
            raise UnoError("title %r matches %d documents; be more specific"
                           % (title, len(matches)))
        return matches[0]
    if len(docs) == 1:
        return docs[0]
    if not docs:
        raise UnoError("no document is open in LibreOffice")
    raise UnoError("%d documents are open (%s); pass title or url to choose one"
                   % (len(docs), ", ".join(_doc_title(d) for d in docs)))


def _doc_title(doc):
    try:
        return doc.getCurrentController().getFrame().getTitle()
    except Exception:
        return doc.getURL().rsplit("/", 1)[-1] or "(untitled)"


def _doc_info(doc):
    try:
        modified = bool(doc.isModified())
    except Exception:  # noqa: BLE001
        modified = None
    return {
        "title": _doc_title(doc),
        "url": doc.getURL(),
        "kind": _kind(doc),
        "modified": modified,
    }


# ── undo discipline ─────────────────────────────────────────────────────────

class TurnRegistry:
    """Group every mutation of one agent turn into exactly one undo action.

    Why this exists rather than a context per operation: LibreOffice does **not**
    merge two separate ``enterUndoContext`` blocks even when they carry the same
    title. Two edits through two calls produced ``undoDepth == 2`` — two Ctrl-Z
    presses for one agent turn. An agent turn is many tool calls, so grouping has
    to span the whole turn.

    The window therefore opens on the first mutation of a turn and closes when
    the runtime says the turn ended. Because an unclosed window would leave the
    document inside an undo context, every exit path closes it: ``end_turn``, a
    new turn id, process shutdown, and a bounded idle timer.

    Undo windows are per document, because a turn may touch more than one.
    """

    def __init__(self, idle_timeout=900.0):
        self._open = {}          # doc url -> {"manager", "turn", "opened"}
        self._idle_timeout = idle_timeout

    def begin(self, doc, turn_id, title):
        key = doc.getURL() or "(untitled)"
        self._expire()
        entry = self._open.get(key)
        if entry is not None and entry["turn"] == turn_id:
            return entry
        if entry is not None:
            # A different turn started without the previous one closing.
            self._close(key)
        try:
            manager = doc.getUndoManager()
            manager.enterUndoContext(title)
        except Exception:  # noqa: BLE001
            return None
        entry = {"manager": manager, "turn": turn_id, "opened": time.time()}
        self._open[key] = entry
        return entry

    def end(self, turn_id=None):
        closed = []
        for key in list(self._open):
            entry = self._open[key]
            if turn_id is None or entry["turn"] == turn_id:
                self._close(key)
                closed.append(key)
        return closed

    def _close(self, key):
        entry = self._open.pop(key, None)
        if entry is None:
            return
        try:
            entry["manager"].leaveUndoContext()
        except Exception:  # noqa: BLE001
            pass

    def _expire(self):
        """A crashed client must not strand the document in an undo context."""
        now = time.time()
        for key in list(self._open):
            if now - self._open[key]["opened"] > self._idle_timeout:
                self._close(key)

    def close_all(self):
        for key in list(self._open):
            self._close(key)


_TURNS = TurnRegistry()


def _turn(doc, args, default_title="Cowork: edit"):
    """Open (or join) the undo window for the turn this call belongs to."""
    return _TURNS.begin(doc, args.get("turn_id") or "default",
                        args.get("undo_title", default_title))


def _undo_state(doc):
    """Report the undo stack. An empty stack is a normal state, not an error.

    LibreOffice raises on ``getCurrentUndoActionTitle`` when nothing is on the
    stack, so each read is guarded individually rather than wrapping the whole
    call — otherwise a fresh document looks like a broken undo manager.
    """
    try:
        manager = doc.getUndoManager()
    except Exception as exc:  # noqa: BLE001
        return {"error": "no undo manager: %s" % exc}
    state = {}
    for key, call in (
        ("undoPossible", manager.isUndoPossible),
        ("redoPossible", manager.isRedoPossible),
    ):
        try:
            state[key] = bool(call())
        except Exception:  # noqa: BLE001
            state[key] = None
    try:
        state["currentUndoAction"] = manager.getCurrentUndoActionTitle()
    except Exception:  # noqa: BLE001
        state["currentUndoAction"] = None
    try:
        state["undoDepth"] = len(manager.getAllUndoActionTitles())
    except Exception:  # noqa: BLE001
        state["undoDepth"] = None
    return state


# ── Writer operations ───────────────────────────────────────────────────────

def _writer_text(doc, limit=None):
    text = doc.getText().getString()
    return text if limit is None else text[:limit]


def _writer_paragraphs(doc, limit=200):
    """Enumerate paragraphs with style, so the agent sees structure not a blob."""
    out = []
    enum = doc.getText().createEnumeration()
    index = 0
    while enum.hasMoreElements() and index < limit:
        para = enum.nextElement()
        try:
            out.append({
                "index": index,
                "style": para.ParaStyleName,
                "text": para.getString(),
            })
        except Exception:  # noqa: BLE001
            pass
        index += 1
    return out


def _writer_selection(doc):
    controller = doc.getCurrentController()
    sel = controller.getSelection()
    if sel is None:
        return None
    try:
        return {"text": sel.getByIndex(0).getString()}
    except Exception:  # noqa: BLE001
        try:
            return {"text": sel.getString()}
        except Exception:  # noqa: BLE001
            return {"text": ""}


def _writer_find(doc, query, limit=50, regex=False):
    descriptor = doc.createSearchDescriptor()
    descriptor.SearchRegularExpression = bool(regex)
    descriptor.SearchString = query
    hits = doc.findAll(descriptor)
    results = []
    for i in range(min(hits.getCount(), limit)):
        found = hits.getByIndex(i)
        try:
            results.append(found.getString())
        except Exception:  # noqa: BLE001
            results.append("")
    return results


PARAGRAPH_BREAK = 0
LINE_BREAK = 1


def _insert_rich(cursor, text, paragraph_style=None):
    """Insert text, turning newlines into REAL paragraph breaks.

    ``insertString(cursor, "a\\nb")`` inserts a literal newline *inside one
    paragraph*. The document then holds a single paragraph with embedded breaks,
    so paragraph enumeration, style assignment, and every structure-aware check
    see one blob — verified: seven fixture "paragraphs" came back as one
    paragraph with the newlines still in its string. Structure has to be
    created, not implied by characters.
    """
    text_obj = cursor.getText()
    parts = text.split("\n")
    for index, part in enumerate(parts):
        if index:
            text_obj.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            if paragraph_style:
                try:
                    cursor.ParaStyleName = paragraph_style
                except Exception:  # noqa: BLE001
                    pass
        if part:
            text_obj.insertString(cursor, part, False)
    return len(text)


def _writer_insert(doc, text, where="end", paragraph_style=None):
    """Insert text at the end of the body, or replacing the current selection."""
    body = doc.getText()
    if where == "selection":
        controller = doc.getCurrentController()
        sel = controller.getSelection()
        if sel is None:
            raise UnoError("nothing is selected, so there is nothing to replace")
        try:
            target = sel.getByIndex(0)
            cursor = target.getText().createTextCursorByRange(target)
        except Exception as exc:  # noqa: BLE001
            raise UnoError("cannot obtain a cursor for the current selection: %s" % exc)
        inserted = _insert_rich(cursor, text, paragraph_style)
    else:
        cursor = body.createTextCursor()
        cursor.gotoEnd(False)
        inserted = _insert_rich(cursor, text, paragraph_style)
    return {"inserted": inserted, "where": where}


def _writer_replace(doc, find, replace, regex=False, all_occurrences=True):
    """Replace via the replace descriptor — one undoable action, not N edits."""
    descriptor = doc.createReplaceDescriptor()
    descriptor.SearchRegularExpression = bool(regex)
    descriptor.SearchString = find
    descriptor.ReplaceString = replace
    if all_occurrences:
        count = doc.replaceAll(descriptor)
    else:
        count = 1 if doc.replaceFirst(descriptor) else 0
    return {"replaced": count, "find": find}


def _writer_set_tracking(doc, enabled):
    try:
        doc.RecordChanges = bool(enabled)
    except Exception as exc:  # noqa: BLE001
        raise UnoError("this document cannot record changes: %s" % exc)
    return {"recordChanges": bool(doc.RecordChanges)}


def _writer_redlines(doc):
    try:
        redlines = doc.getRedlines()
    except Exception as exc:  # noqa: BLE001
        raise UnoError("cannot read tracked changes: %s" % exc)
    out = []
    for i in range(redlines.getCount()):
        red = redlines.getByIndex(i)
        out.append({
            "type": str(red.RedlineType),
            "author": red.RedlineAuthor,
            "comment": getattr(red, "RedlineComment", ""),
            "text": red.getText().getString() if hasattr(red, "getText") else "",
        })
    return out


def _writer_layout_checks(doc):
    """Deterministic geometry and contrast checks — no model, no rendering.

    These are the 'compile errors' of document work: cheap, repeatable, and
    strictly more reliable than asking a model where the margins are.
    """
    issues = []

    # 1. Low-contrast runs, via a WCAG ratio over the character colours.
    def _lum(color):
        r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
        def channel(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    try:
        enum = doc.getText().createEnumeration()
        seen = 0
        while enum.hasMoreElements() and seen < 2000:
            seen += 1
            para = enum.nextElement()
            try:
                color = para.CharColor
            except Exception:  # noqa: BLE001
                continue
            if color in (-1, 0xFFFFFFFF):  # automatic
                continue
            ratio_low, ratio_high = sorted((_lum(color), _lum(0xFFFFFF)))
            ratio = (ratio_high + 0.05) / (ratio_low + 0.05)
            if ratio < 4.5:
                issues.append({
                    "check": "contrast",
                    "severity": "warning",
                    "detail": "text colour #%06X on white gives a contrast ratio of "
                              "%.1f:1 (below 4.5:1)" % (color, ratio),
                    "text": para.getString()[:80],
                })
    except Exception:  # noqa: BLE001
        pass

    # 2. Page-margin overflow for long unbreakable tokens.
    try:
        body = doc.getText()
        enum = body.createEnumeration()
        count = 0
        while enum.hasMoreElements() and count < 2000:
            count += 1
            para = enum.nextElement()
            for token in re.findall(r"\S{60,}", para.getString()):
                issues.append({
                    "check": "overflow",
                    "severity": "warning",
                    "detail": "%d-character unbreakable token is likely to overflow "
                              "the text area" % len(token),
                    "text": token[:80],
                })
    except Exception:  # noqa: BLE001
        pass

    # 3. Empty paragraphs in a run (spacing done by hand rather than by style).
    try:
        runs = 0
        worst = 0
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            para = enum.nextElement()
            if para.getString().strip() == "":
                runs += 1
                worst = max(worst, runs)
            else:
                runs = 0
        if worst >= 3:
            issues.append({
                "check": "spacing",
                "severity": "info",
                "detail": "%d consecutive empty paragraphs — spacing is being done "
                          "with blank lines rather than paragraph styles" % worst,
            })
    except Exception:  # noqa: BLE001
        pass

    return issues


# ── Calc operations ─────────────────────────────────────────────────────────

def _calc_sheets(doc):
    return [sheet.Name for sheet in doc.getSheets()]


def _calc_sheet(doc, name=None):
    sheets = doc.getSheets()
    if name:
        if not sheets.hasByName(name):
            raise UnoError("no sheet named %r (available: %s)"
                           % (name, ", ".join(_calc_sheets(doc))))
        return sheets.getByName(name)
    return doc.getCurrentController().getActiveSheet()


def _calc_read(doc, range_ref="A1:B10", sheet=None):
    """Read a range returning formulas AND values in one pass.

    That duality is the point: 'explain this number' needs both, and a
    file-level tool cannot give both in one read (F-record §8.4.1(g)).
    """
    target = _calc_sheet(doc, sheet)
    cells = target.getCellRangeByName(range_ref)
    rows, cols = cells.getRows().getCount(), cells.getColumns().getCount()
    data = []
    for r in range(rows):
        row = []
        for c in range(cols):
            cell = cells.getCellByPosition(c, r)
            entry = {"formula": cell.getFormula()}
            try:
                entry["value"] = cell.getValue()
            except Exception:  # noqa: BLE001
                entry["value"] = None
            entry["type"] = str(cell.getType().value) if hasattr(cell.getType(), "value") \
                else str(cell.getType())
            row.append(entry)
        data.append(row)
    return {"sheet": target.Name, "range": range_ref, "rows": rows, "cols": cols,
            "cells": data}


# Excel-style formulas must be translated: Calc's argument separator is ';'.
# Getting this wrong yields Err:508, which looks exactly like "no such function".
_FORMULA_FIXES = (
    ("_xlfn.", ""),
    ("_xlws.", ""),
)


def translate_formula(formula):
    """Normalise an Excel-style formula string to Calc syntax (D11).

    Commas inside string literals are preserved; only argument separators are
    converted. Array constants and quoted sheet names are left alone.
    """
    if not formula.startswith("="):
        return formula
    for prefix, replacement in _FORMULA_FIXES:
        formula = formula.replace(prefix, replacement)

    out = []
    in_string = False
    in_array = False
    depth = 0
    index = 0
    while index < len(formula):
        char = formula[index]
        if char == '"':
            if in_string and index + 1 < len(formula) and formula[index + 1] == '"':
                out.append('""')
                index += 2
                continue
            in_string = not in_string
            out.append(char)
        elif in_string:
            out.append(char)
        elif char == "{":
            in_array = True
            out.append(char)
        elif char == "}":
            in_array = False
            out.append(char)
        elif char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth -= 1
            out.append(char)
        elif char == "," and not in_array:
            out.append(";")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _calc_write(doc, range_ref="A1", values=None, sheet=None):
    """Write cells individually so undo stays honest (D5)."""
    if not values:
        raise UnoError("no values supplied")
    target = _calc_sheet(doc, sheet)
    written = 0
    for row_index, row in enumerate(values):
        for col_index, raw in enumerate(row):
            cell = target.getCellByPosition(
                _col_index(range_ref) + col_index,
                _row_index(range_ref) + row_index)
            if isinstance(raw, str) and raw.startswith("="):
                cell.setFormula(translate_formula(raw))
            elif raw is None or raw == "":
                cell.setFormula("")
            else:
                cell.setValue(float(raw))
            written += 1
    return {"written": written, "sheet": target.Name, "origin": range_ref}


def _split_ref(ref):
    match = re.match(r"^\$?([A-Za-z]+)\$?(\d+)$", ref.strip())
    if not match:
        raise UnoError("expected a single cell reference like A1, got %r" % ref)
    return match.group(1).upper(), int(match.group(2))


def _col_index(letters):
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _col_letters(index):
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _row_index(ref):
    return _split_ref(ref)[1] - 1


# ── rendering (the visual-verification pipeline) ────────────────────────────

_last_export_error = []


def _export_pdf_from_live(doc, pdf_path):
    """Export the OPEN document to PDF, including unsaved edits.

    `storeToURL` writes a copy and does not change the document's own file
    association or mark it unmodified, so this is safe to call on a document the
    user is actively editing.

    This exists because the obvious implementation — shell out to
    `soffice --convert-to pdf <path>` — converts the file ON DISK. The agent
    edits the live document, those edits are unsaved, and so the rendered page
    showed the document as it was before the agent touched it. An agent that
    renders stale content and reports "looks fine" is worse than one that cannot
    render at all, because it is confidently wrong.
    """
    try:
        doc.storeToURL(
            uno.systemPathToFileUrl(pdf_path),
            (_pv("FilterName", "writer_pdf_Export"),
             _pv("Overwrite", True)))
        return True
    except Exception as exc:  # noqa: BLE001
        _last_export_error.append(str(exc))
        return False


def _render(doc, outdir, pages=None, dpi=100):
    """Render the live document to PNG page images for a visual review.

    Prefers an in-process export so unsaved edits are included. Falls back to a
    headless conversion of the file on disk only when the document has never
    been saved, where the in-process export has no baseline to work from.
    """
    os.makedirs(outdir, exist_ok=True)
    pdf = os.path.join(outdir, "render.pdf")

    exported_live = _export_pdf_from_live(doc, pdf)
    source = "live document (includes unsaved edits)"

    if not exported_live:
        url = doc.getURL()
        if not url.startswith("file://"):
            raise UnoError(
                "this document has never been saved, so there is nothing to "
                "render. Save it once and try again.")
        path = uno.fileUrlToSystemPath(url)
        if not os.path.exists(path):
            raise UnoError("the document path does not exist on disk: %s" % path)
        # A private profile is mandatory here: reusing the interactive profile
        # makes the conversion process fight the running office for the profile
        # lock and die (observed: exit 134).
        profile = os.path.join(outdir, "_lo-profile")
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.run(
            ["soffice", "--headless", "--norestore", "--nolockcheck",
             "-env:UserInstallation=file://%s" % profile,
             "--convert-to", "pdf", "--outdir", outdir, path],
            capture_output=True, text=True, timeout=300)
        produced = os.path.join(
            outdir, os.path.splitext(os.path.basename(path))[0] + ".pdf")
        if not os.path.exists(produced):
            raise UnoError(
                "PDF export produced no file.\nstdout: %s\nstderr: %s\n"
                "(in-process export error: %s)"
                % (proc.stdout[-400:], proc.stderr[-400:],
                   _last_export_error[-1] if _last_export_error else "none"))
        pdf = produced
        source = "saved file on disk (the document has never been saved)"

    # Clear stale pages so a shorter document cannot leave old images behind.
    for existing in os.listdir(outdir):
        if existing.startswith("render-") and existing.endswith(".png"):
            os.remove(os.path.join(outdir, existing))

    base = os.path.join(outdir, "render")
    command = ["pdftoppm", "-r", str(dpi), "-png"]
    if pages:
        command += ["-f", str(pages[0]), "-l", str(pages[-1])]
    command += [pdf, base]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise UnoError("pdftoppm failed: %s" % proc.stderr[-400:])

    images = sorted(f for f in os.listdir(outdir)
                    if f.startswith("render-") and f.endswith(".png"))
    return {"pdf": pdf, "source": source,
            "images": [os.path.join(outdir, f) for f in images]}


# ── operation dispatch ──────────────────────────────────────────────────────

def _op_list_documents(ctx, _args):
    desktop = _desktop(ctx)
    docs = [d for d in _frames(desktop) if _kind(d) != "unknown"]
    return {
        "documents": [_doc_info(d) for d in docs],
        "active": _doc_info(desktop.CurrentComponent) if desktop.CurrentComponent else None,
    }


def _op_read_text(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    if _is_writer(doc):
        return {"document": _doc_info(doc), "text": _writer_text(doc),
                "selection": _writer_selection(doc)}
    if _is_calc(doc):
        return {"document": _doc_info(doc), "sheets": _calc_sheets(doc),
                "activeSheet": _calc_sheet(doc).Name}
    raise UnoError("read_text does not support %s documents yet" % _kind(doc))


def _op_outline(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    if _is_writer(doc):
        return {"document": _doc_info(doc),
                "paragraphs": _writer_paragraphs(doc, int(args.get("limit", 200)))}
    if _is_calc(doc):
        return {"document": _doc_info(doc),
                "sheets": [{"name": s.Name,
                            "rows": s.getRows().getCount(),
                            "cols": s.getColumns().getCount()}
                           for s in doc.getSheets()]}
    raise UnoError("outline does not support %s documents yet" % _kind(doc))


def _op_find(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    if not _is_writer(doc):
        raise UnoError("find currently supports Writer documents only")
    return {"matches": _writer_find(doc, args["query"],
                                    int(args.get("limit", 50)),
                                    bool(args.get("regex")))}


def _op_replace(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    _turn(doc, args)
    result = _writer_replace(doc, args["find"], args.get("replace", ""),
                             bool(args.get("regex")),
                             bool(args.get("all", True)))
    result["undo"] = _undo_state(doc)
    result["document"] = _doc_info(doc)
    return result


def _op_append(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    _turn(doc, args)
    result = _writer_insert(doc, args["text"],
                            args.get("where", "end"),
                            args.get("paragraph_style"))
    result["undo"] = _undo_state(doc)
    return result


def _op_set_tracking(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return _writer_set_tracking(doc, args.get("enabled", True))


def _op_redlines(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return {"redlines": _writer_redlines(doc)}


def _op_layout_check(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    if not _is_writer(doc):
        raise UnoError("layout_check currently supports Writer documents only")
    issues = _writer_layout_checks(doc)
    return {"issues": issues, "count": len(issues)}


def _op_read_range(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return _calc_read(doc, args.get("range", "A1:B10"), args.get("sheet"))


def _op_write_range(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    _turn(doc, args)
    result = _calc_write(doc, args.get("range", "A1"),
                         args.get("values"), args.get("sheet"))
    result["undo"] = _undo_state(doc)
    return result


def _op_render(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return _render(doc, args.get("outdir",
                                 "/tmp/cowork-render"),
                   args.get("pages"), int(args.get("dpi", 100)))


def _op_undo(ctx, _args):
    doc = _find_doc(_desktop(ctx), None, None)
    doc.getUndoManager().undo()
    return {"undo": _undo_state(doc)}


def _op_undo_state(ctx, args):
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return {"undo": _undo_state(doc)}


def _op_ping(ctx, _args):
    desktop = _desktop(ctx)
    return {"ok": True, "documents": len([d for d in _frames(desktop)
                                          if _kind(d) != "unknown"])}


def _op_begin_turn(ctx, args):
    """Open the undo window for one agent turn.

    The runtime calls this once at the start of a turn; every subsequent
    mutation carries the same ``turn_id`` and lands in this one window.
    """
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    turn_id = args.get("turn_id") or "default"
    _TURNS.begin(doc, turn_id, args.get("undo_title", "Cowork: edit"))
    return {"turn": turn_id, "undo": _undo_state(doc)}


def _op_end_turn(ctx, args):
    """Close the undo window, making the whole turn a single Ctrl-Z."""
    closed = _TURNS.end(args.get("turn_id"))
    doc = _find_doc(_desktop(ctx), args.get("title"), args.get("url"))
    return {"closed": closed, "undo": _undo_state(doc)}


OPS = {
    "begin_turn": _op_begin_turn,
    "end_turn": _op_end_turn,
    "ping": _op_ping,
    "list_documents": _op_list_documents,
    "read_text": _op_read_text,
    "outline": _op_outline,
    "find": _op_find,
    "replace": _op_replace,
    "append": _op_append,
    "set_tracking": _op_set_tracking,
    "redlines": _op_redlines,
    "layout_check": _op_layout_check,
    "read_range": _op_read_range,
    "write_range": _op_write_range,
    "render": _op_render,
    "undo": _op_undo,
    "undo_state": _op_undo_state,
}


def main():
    ctx = None
    try:
        ctx = _connect_ctx()
    except UnoError as exc:
        # Stay alive and report per request rather than dying: the caller can
        # then distinguish "LibreOffice is not running" from "the helper crashed".
        sys.stderr.write("cowork-uno: %s\n" % exc)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": "bad request line: %s" % exc}),
                  flush=True)
            continue

        rid = request.get("id")
        op = request.get("op")
        args = request.get("args") or {}
        handler = OPS.get(op)
        if handler is None:
            print(json.dumps({"id": rid, "ok": False,
                              "error": "unknown op %r; known: %s"
                                       % (op, ", ".join(sorted(OPS)))}), flush=True)
            continue
        try:
            if ctx is None:
                ctx = _connect_ctx()
            payload = handler(ctx, args)
            payload["id"] = rid
            payload["ok"] = True
        except UnoError as exc:
            payload = {"id": rid, "ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            payload = {"id": rid, "ok": False,
                       "error": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(payload, default=str), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Never strand the document inside an undo context.
        _TURNS.close_all()
