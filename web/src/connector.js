/**
 * Mosaic's connector, backed by the QWebChannel bridge.
 *
 * Mosaic asks a connector for `query({ type, sql })` and expects a promise of
 * Arrow bytes, JSON rows, or nothing. `web_bridge.py` exposes exactly that
 * shape as a slot with a return value - the form the webengine probe measured
 * specifically because this contract needs it.
 *
 * WHY THE ENVELOPE. A Qt slot cannot raise across the channel: PyQt logs the
 * exception and the caller receives null, which is indistinguishable from an
 * empty result. So the Python side always returns
 * `{ok, kind, data}` or `{ok:false, kind, error}`, and this unwraps it into a
 * real rejected promise. A chart that silently draws nothing because the SQL
 * was wrong is the failure both halves exist to prevent.
 */
import { tableFromIPC } from "@uwdata/flechette";

/** Base64 to bytes, without pulling in a polyfill. */
function decodeBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/**
 * Call the bridge's `query` slot and resolve with its return value.
 *
 * QWebChannel slots are asynchronous in JavaScript: the value comes back
 * through a CALLBACK passed as the last argument, not as a return value.
 * Some qwebchannel builds also return a promise when no callback is given,
 * and some do not - `await bridge.query(...)` therefore works in one build
 * and hangs forever in another, which is exactly how this presented: the
 * connector resolved when called directly and never settled underneath
 * Mosaic's manager.
 *
 * Passing the callback explicitly is the documented form and behaves the
 * same everywhere, so this stops depending on which build is present. The
 * promise is also raced against a timeout, because a slot that never calls
 * back would otherwise hang a chart with no error anywhere.
 */
function callSlot(bridge, sql, type, timeoutMs = 120000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`the bridge did not answer within ${timeoutMs} ms`));
    }, timeoutMs);
    const done = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    try {
      const returned = bridge.query(sql, type, done);
      // A build that returns a promise as well: whichever settles first.
      if (returned && typeof returned.then === "function") {
        returned.then(done, (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(error);
        });
      }
    } catch (error) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    }
  });
}

export function qtConnector(bridge) {
  return {
    async query(request) {
      // BOTH spellings, because Mosaic uses both. Coordinator.query builds
      // `{type, query, cache, options}` and hands it to the manager, but
      // QueryManager.submit then calls the connector with `{type, sql,
      // ...options}`. A connector that reads only one of them works through
      // one entry point and not the other.
      const { type = "arrow" } = request || {};
      const sql = (request || {}).query ?? (request || {}).sql;
      if (typeof sql !== "string" || !sql) {
        throw new Error(
          `connector got no SQL: keys were ${JSON.stringify(Object.keys(request || {}))}`,
        );
      }
      const raw = await callSlot(bridge, sql, type);
      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch (cause) {
        // Only happens if the slot was not reached at all - a missing
        // @pyqtSlot, or a channel that is not connected yet.
        throw new Error(
          `bridge returned something that is not an envelope: ${String(raw).slice(0, 120)}`,
          { cause },
        );
      }
      if (!envelope.ok) {
        throw new Error(envelope.error || "query failed with no message");
      }
      if (type === "exec") return undefined;
      if (type === "json") return JSON.parse(envelope.data);
      // Arrow. flechette is already a vgplot dependency, so decoding costs
      // no extra bytes in the bundle.
      return tableFromIPC(decodeBase64(envelope.data));
    },
  };
}

/**
 * Resolves once the page can talk to Python.
 *
 * qwebchannel.js is injected by Qt into every page on a profile that has a
 * channel; it is not an npm package and must not be bundled.
 */
export function connectBridge() {
  return new Promise((resolve, reject) => {
    if (typeof QWebChannel === "undefined") {
      reject(new Error("QWebChannel is not available; is this page running "
                       + "outside the plugin?"));
      return;
    }
    // eslint-disable-next-line no-undef
    new QWebChannel(qt.webChannelTransport, (channel) => {
      const bridge = channel.objects.bridge;
      if (!bridge) {
        reject(new Error("the channel carries no 'bridge' object"));
        return;
      }
      resolve(bridge);
    });
  });
}
