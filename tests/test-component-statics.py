#!/usr/bin/env python3
"""Static checks for the extension components.

Written after a bad hour: several automated edits to `cowork_sidebar.py` each
deleted more than intended, and the result was not a clean syntax error but a
panel that imported fine and then failed at runtime with `AttributeError:
'CoworkUIElement' object has no attribute '_set_busy'` — visible only by opening
LibreOffice and reading a log. Three separate edits produced three separate
missing methods.

`ast.parse` cannot catch that: the file was valid Python every time. What catches
it is comparing what the code CALLS against what the module DEFINES, and checking
that every global name resolves to something — an import, a builtin, or a
definition in the file. `import time` went missing the same way and surfaced as
`NameError: name 'time' is not defined` inside the office.

    python3 tests/test-component-statics.py
"""

import ast
import builtins
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components")

FILES = ["cowork_sidebar.py", "cowork_client.py", "cowork_job.py",
         "cowork_commands.py"]

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        FAILURES.append(label)


def module_level_names(tree):
    """Everything the module defines or imports at top level."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Try):
            # A try/except ImportError fallback is how the component loads its
            # sibling module when the extension directory is not on sys.path.
            # Both branches count.
            for part in (node.body, node.orelse,
                         [n for h in node.handlers for n in h.body],
                         node.finalbody):
                for sub in part:
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        for alias in sub.names:
                            names.add(alias.asname or alias.name.split(".")[0])
                    elif isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                        names.add(sub.name)
    return names


def bound_names(node):
    """Every name bound anywhere inside a function body, plus its parameters.

    Undefined-name detection is only meaningful if locals, parameters and `self`
    count as defined. A first version of this check walked the whole tree looking
    for `ast.Name` loads and reported 70 false positives — every local variable in
    the file. A check that noisy is worse than no check, because it trains you to
    ignore it.
    """
    names = {"self", "cls"}
    for arg in getattr(node, "args", ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                  kw_defaults=[], defaults=[])).args:
        names.add(arg.arg)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            names.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(sub.name)
            for arg in sub.args.args + sub.args.kwonlyargs + sub.args.posonlyargs:
                names.add(arg.arg)
            if sub.args.vararg:
                names.add(sub.args.vararg.arg)
            if sub.args.kwarg:
                names.add(sub.args.kwarg.arg)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, (ast.comprehension,)):
            for t in ast.walk(sub.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(sub, ast.Lambda):
            for arg in sub.args.args:
                names.add(arg.arg)
        elif isinstance(sub, ast.ClassDef):
            names.add(sub.name)
    return names


def class_methods(tree):
    """class name -> set of method and attribute names it touches."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        attrs = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store):
                attrs.add(sub.attr)
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        attrs.add(t.id)
        # Names bound anywhere in the class body, including __init__ parameters.
        for sub in ast.walk(node):
            if isinstance(sub, ast.arg):
                attrs.add(sub.arg)
        out[node.name] = (methods, attrs)
    return out


# Bases a component may inherit whose methods this file is not required to
# define. Everything else must be declared locally.
COMPONENT_BASES = {
    "unohelper.Base", "Base",
    "XUIElement", "XToolPanel", "XSidebarPanel", "XUIElementFactory",
    "XActionListener", "XWindowListener", "XTextListener", "XKeyListener",
    "XCallback", "XDispatch", "XDispatchProvider", "XInitialization",
    "XServiceInfo", "XJob", "XInstanceProvider", "XTerminateListener",
}


def analyse(path):
    label = os.path.basename(path)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        check("%s parses" % label, False, exc)
        return
    check("%s parses" % label, True)

    # 1. Every name a FUNCTION loads must resolve: module-level, local, a
    #    parameter, or a builtin. Checked per function so locals are not mistaken
    #    for missing globals — a first version walked the whole tree and reported
    #    70 false positives, and a check that noisy trains you to ignore it.
    defined = module_level_names(tree)
    known = defined | set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    unresolved = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = known | bound_names(node)
        for outer in ast.walk(tree):
            if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)) and outer is not node:
                scope |= {n.id for n in ast.walk(outer)
                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in scope:
                    unresolved.add(sub.id)
    check("%s has no unresolved names" % label, not unresolved, sorted(unresolved))

    # 2. Every self.<method>() a class calls must exist somewhere: on the class,
    #    on another class in this file, or inherited from a KNOWN component base.
    #
    #    An earlier version skipped a class entirely whenever ANY base was
    #    external, which quietly exempted every UNO component — they always
    #    inherit a UNO interface. It reported "defines every method" while
    #    `CoworkUIElement` called `self._set_busy()` without defining it, and the
    #    panel then failed inside LibreOffice with AttributeError.
    classes_here = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    all_methods = set()
    for methods, _attrs in class_methods(tree).values():
        all_methods |= methods

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        if any(b not in classes_here and b not in COMPONENT_BASES for b in bases):
            continue                       # genuinely foreign, e.g. http.server
        methods, attrs = class_methods(tree).get(node.name, (set(), set()))
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                target = sub.func
                if isinstance(target.value, ast.Name) and target.value.id == "self":
                    name = target.attr
                    if (name not in methods and name not in attrs
                            and name not in all_methods):
                        missing.append("%s.%s()" % (node.name, name))
    check("%s defines every method it calls" % label, not missing, sorted(set(missing)))


def check_manifest():
    """Every component file must be declared, and every declaration must exist.

    Both directions matter. A file the sidebar imports but the manifest omits is
    simply absent from the installed extension — which is how `cowork_client.py`
    and `cowork-runtime.sh` went missing after an automated edit rewrote the
    manifest. A declaration without a file makes the office fail to load the
    bundle at all.
    """
    comp_dir = pathlib.Path(COMPONENTS)
    manifest = pathlib.Path(COMPONENTS).parent / "META-INF" / "manifest.xml"
    text = manifest.read_text()

    files = sorted(p.name for p in comp_dir.iterdir() if p.is_file())
    unlisted = [f for f in files if f not in text]
    check("every component file is in the manifest", not unlisted, unlisted)

    declared = set(re.findall(r'full-path="components/([^"]+)"', text))
    absent = sorted(d for d in declared if not (comp_dir / d).exists())
    check("every manifest entry has a file", not absent, absent)


def main():
    print("extension component statics\n")
    check_manifest()
    print()
    for name in FILES:
        path = os.path.join(COMPONENTS, name)
        if not os.path.exists(path):
            continue
        analyse(path)
        print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All component statics passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
