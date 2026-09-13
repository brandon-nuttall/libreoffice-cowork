#!/usr/bin/env python3
"""Does the component define every method it calls, right now?

Same rule as tests/test-component-statics.py, but fast and terse, meant to be run
*after each edit* rather than at the end. Four separate edits during panel work
deleted `_scroll_to_newest` or `_set_busy` accidentally; each time the panel
imported cleanly and then failed inside LibreOffice with an AttributeError, and
each cost a rebuild, a sandbox restart and a capture to discover.

    python3 tests/check-methods.py
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(os.path.dirname(HERE), "ext", "oxt-proto", "components")

# Bases whose methods a component is not required to define.
KNOWN_BASES = {
    "unohelper.Base", "Base",
    "XUIElement", "XToolPanel", "XSidebarPanel", "XUIElementFactory",
    "XActionListener", "XWindowListener", "XTextListener", "XKeyListener",
    "XCallback", "XAdjustmentListener", "XDispatch", "XDispatchProvider",
    "XInitialization", "XServiceInfo", "XJob", "XInstanceProvider",
    "XTerminateListener",
}


def main():
    failures = []
    for name in sorted(os.listdir(COMPONENTS)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(COMPONENTS, name)
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)

        classes = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = {n.name for n in node.body
                                      if isinstance(n, ast.FunctionDef)}
        every_method = set().union(*classes.values()) if classes else set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if any(b not in classes and b not in KNOWN_BASES for b in bases):
                continue
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)):
                    continue
                target = sub.func
                if not (isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    continue
                called = target.attr
                if called in classes.get(node.name, set()) or called in every_method:
                    continue
                # Plain attributes (self._controls, self._busy) are assigned
                # somewhere; only a method CALL with no definition is an error.
                failures.append("%s: self.%s() is not defined" % (node.name, called))

    # Class attributes read through self — uppercase by convention — must exist on
    # the class too. `self._SUGGESTIONS` went missing during an edit and crashed the
    # panel on build with AttributeError, exactly like a missing method.
    for name in sorted(os.listdir(COMPONENTS)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(COMPONENTS, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if any(b not in KNOWN_BASES and not b[0].isupper() for b in bases):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                    if (isinstance(sub.value, ast.Name) and sub.value.id == "self"
                            and sub.attr.isupper() and sub.attr not in defined):
                        failures.append("%s: self.%s is not defined"
                                        % (node.name, sub.attr))

    if failures:
        for line in sorted(set(failures)):
            print("  FAIL " + line)
        return 1
    print("  ok   every self.method() and self.CONSTANT has a definition")
    return 0


if __name__ == "__main__":
    sys.exit(main())
