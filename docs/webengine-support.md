# Qt WebEngine support, per QGIS build

Running record of what [`tools/test_webengine.py`](../tools/test_webengine.py)
finds on real machines. Kept for the same reason the README records EQuIS API
behaviours: the vendor documentation and the public bug reports both turned
out to be unreliable here, and a design decision resting on either would have
been wrong.

**Run the probe once per QGIS restart.** Stage 7 registers a process-wide URL
scheme, which cannot be re-registered, so only the first run after a restart
can test that stage honestly. The probe now detects a re-run and says so.

## Results

| Date | OS | QGIS | Qt / PyQt | Python | Chromium | webengine | channel | modules |
|---|---|---|---|---|---|---|---|---|
| 2026-09-18 | Windows | 4.2.1 Belém do Pará | 6.11.0 / 6.11.0 | 3.12.13 | 140.0.0.0 | ✅ | ✅ | ✅ |

Still to check: **macOS and Linux on 4.2+**. Builds below 4.2 are out of
scope by decision (2026-09-18), so the 3.34 LTR and 4.0 are no longer worth
measuring.

The **Python** column doubles as the packaging survey. QGIS 4.2.1 on Windows
ships 3.12.13; if macOS and Linux 4.2 builds agree, the DuckDB wheel matrix
for US-22 is one cp312 wheel per platform rather than a grid. That is the
cheapest open question in the project and this table answers it.

## What each column answers

| Column | The question | Why it decides something |
|---|---|---|
| **webengine** | Do the PyQt bindings import, and does a `QWebEngineView` construct, load a page and call back from `runJavaScript`? | Without this there is no web view plane at all. This is the one that was reported broken on Windows OSGeo4W under QGIS 4.0 (qgis/QGIS#65398). |
| **channel** | Does `QWebChannel` round-trip both directions, including a **slot with a return value**? | This is the transport the whole design rests on, and the return-value form is the exact shape of Mosaic's `query(sql, type) -> result` connector contract. A channel that only works one way would not do. |
| **modules** | Can a custom URL scheme **registered from inside a plugin, after QGIS has already started**, serve an ES-module app whose import graph resolves? | `file://` forbids module imports, so a bundled vgplot build needs either this or a local HTTP server. Qt documents `QWebEngineUrlScheme` as needing registration before the application exists — which a plugin cannot do — so this was the stage most likely to fail. |

## What the 4.2.1 result settles

**The QGIS 4.0 Windows blocker is gone.** The compatibility matrix in
[the architecture notes](mosaic-qgis-plugin-architecture.md) called Windows
OSGeo4W "broken as of 4.0" and named the 4.2 LTR as the thing to watch. It
landed and it fixed it.

**The bridge design is viable as written.** `QWebChannel` carries values in
both directions on Windows, so §2 and §4 of the architecture notes need no
redesign.

**Open question 4 of the architecture notes is answered: bundle it.** A Vite
build of vgplot can ship inside the plugin and be served from a custom scheme.
No local HTTP server, no port to choose, nothing for a corporate firewall or
an endpoint agent to object to. The `restConnector` fallback in §6 stays a
contingency for builds that fail this stage rather than the expected route.

## WebEngine needs QGIS's own application, not just a QApplication

Found 2026-09-23 while building the round-trip test, and worth its own
section because every symptom points somewhere else.

Under `python-qgis.bat` with a hand-made `QApplication`, a custom scheme
loads **nothing**, and every intermediate step reports success:

    scheme registered           -> True
    handler reached             -> REQUEST: equis://app/index.html
    path resolved               -> ...\equis_loader\web\index.html, exists
    QBuffer opened              -> True, 1061 bytes
    job.reply() returned        -> no exception
    loadFinished                -> ok=False, url="", misses=[]

No error, no console message, no failed request. Chromium is initialised as
part of the application QGIS builds, so a bare `QApplication` gets a
WebEngine that accepts every call and draws nothing.

Two consequences:

* `tools/test_web_roundtrip.py` detects the case and skips with instructions
  rather than reporting four failures that look like broken code.
* The same constraint is why `tools/test_webengine.py` says to paste it into
  the QGIS Python Console. That warning is easy to read as fussiness; it is
  not.

Importing `QtWebEngineWidgets` before the application exists is a separate,
genuine requirement - PyQt raises a clear error for it - and satisfying it
does not fix the above.

## A hidden QWebEngineView hangs every Arrow query, silently

Found 2026-09-23. It cost most of a day and none of the symptoms point at it.

Mosaic's query consolidator defers **only** Arrow requests
(`QueryConsolidator.js`):

```js
if (entry.request.type === 'arrow') {
  id ||= wait(() => run());   // batch compatible queries next frame
} else {
  enqueue(entry, priority);   // json goes straight to the queue
}
```

and `wait()` is `requestAnimationFrame` in a browser. **Chromium fires
animation frames only for a visible surface.** A view that is constructed but
never shown therefore:

* answers `json` queries perfectly,
* propagates errors perfectly,
* and leaves every `arrow` request sitting in `pending` forever, with no
  error, no rejection, and nothing in any log.

It cannot be reproduced outside a browser: in Node `wait()` falls back to
`setImmediate` and everything works, which is what made three separate
theories look plausible before the real cause was measured.

**The required combination** for a view with no parent widget:

```python
view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
view.resize(640, 480)      # a zero-sized widget gets no frames
view.show()                # NOT optional
```

Measured with `WA_DontShowOnScreen` set: 16 animation frames in ~4 s, and the
Arrow round trip then passes. Off-screen is fine; hidden is not.

**How it was found**, because the method matters more than the fact: a probe
wrapped the connector and recorded every call Mosaic made to it. The trace
showed `type=arrow` arriving only from the test's own direct helper and never
from the manager - so the request was never issued, which pointed at the
queue rather than at the bridge, the base64, or the decoder. Reading source
had produced three wrong answers before that.

## What it does not settle

One machine, one platform. Windows 4.2.1 is measured; macOS and Linux 4.2 are
not, and there is no reason to assume a Qt build behaves identically across
them — the whole reason this file exists is that assuming was wrong twice
already.

`metadata.txt` still declares `qgisMinimumVersion=3.34`. That is now stale
against the scope decision and should become `4.2` when the substrate work
starts. It is deliberately not changed yet: it is the install gate on the
*currently shipped* plugin, and bumping it would stop v3.31 installing for
anyone on an older QGIS who is using it today.

## Note on the probe's own history

Three runs on this machine gave three different answers before the fourth was
clean, and in each case the fault was the probe rather than Qt:

| Run | Reported | Cause |
|---|---|---|
| 1 | `channel=NO modules=yes` | The probe emitted a Python signal on `loadFinished`, before the JS side had constructed the channel and connected to it. Exactly the trap §5 of the architecture notes documents. Fixed with an explicit `ready()` handshake. |
| 2 | `channel=yes modules=NO` | Re-run in the same QGIS process. `STATE.clear()` dropped the Python reference to the scheme handler while the profile still pointed at it, so requests went nowhere and the stage silently never completed — and the verdict counted a check that never ran as a negative. Fixed by keeping handlers in module-level state, reusing an installed one, giving the stage its own timeout, and reporting `DID NOT RUN` separately from `FAIL`. |
| 3 | all green | Clean first run after a QGIS restart. |

Worth recording because it is the argument for the probe existing at all. Each
of those wrong answers would have been believed if the test had been the
one-line `import QWebEngineView` this started as — the first would have talked
us out of a design that works, and the second out of bundling that works.
