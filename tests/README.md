# Tests

## `test-remote-install.sh`

Exercises the install path a real user gets, which is *not* the same as running
`./setup.sh` from a checkout.

`setup.sh` has two source strategies: use a local checkout when it finds one
beside itself, otherwise download a tarball. Running it from the repository
always takes the first branch, so the download branch never gets tested — and
that branch is the one every `curl | bash` user depends on.

This test copies the published script into a directory containing nothing else
and runs it there, then asserts the profile and the bridge actually landed.

```sh
./tests/test-remote-install.sh
```

It installs into throwaway directories under the workspace and needs no
credentials. Set `COWORK_LO_PROFILE`/`DSH_HOME` to redirect it elsewhere.

## Manual checks

Some things here cannot be asserted from a script and are worth doing by hand
after a change to the extension:

1. **The deck appears.** *View ▸ Sidebar ▸ Cowork* in Writer, Calc, and Impress.
   A deck that silently never appears is the classic failure — `Sidebar.xcu`,
   `Factories.xcu`, and the component's `IMPL_NAME` must all agree, and a
   mismatch produces no error anywhere.
2. **The panel has contents.** Blank controls mean a control failed to build
   (the panel swallows per-control errors so one bad property cannot take the
   whole panel down). Set `COWORK_SIDEBAR_LOG` to a path to see what built.
3. **The panel fills its width.** If it is a thin strip, the resize listener is
   not firing — the sidebar creates the panel while its parent window is still
   0×0, so a one-shot layout gives invisible controls.

## `test-panel-wiring.py`

Exercises the sidebar panel's call path without LibreOffice.

The panel is the one piece that cannot be smoke-tested from a script — it needs a GUI,
a live document, and someone to press Send. That makes the seam between the panel and the
agent service the least tested and most silently breakable part of the project.

This test stubs UNO just enough to import the component, hands the **real**
`_run_turn` code a fake element and a fake client, and asserts what the panel would put
on screen: that the document URL (not the prompt) is passed as the document, that a
streamed reply lands in the transcript, that tool calls are described in the user's terms
rather than shown as raw names, and that an unreachable service produces an honest
message instead of a claim that something changed.

```sh
python3 tests/test-panel-wiring.py
```

It needs no GUI, no network, and no LibreOffice.
