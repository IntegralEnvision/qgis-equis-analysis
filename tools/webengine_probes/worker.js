self.onmessage = async (e) => {
  try {
    const { instance } = await WebAssembly.instantiateStreaming(fetch(e.data));
    self.postMessage({ ok: true, add_20_22: instance.exports.add(20, 22) });
  } catch (err) {
    try {
      const b = await (await fetch(e.data)).arrayBuffer();
      const { instance } = await WebAssembly.instantiate(b);
      self.postMessage({ ok: true, add_20_22: instance.exports.add(20, 22), streamingFailed: String(err) });
    } catch (err2) {
      self.postMessage({ ok: false, error: String(err) + " / bytes: " + String(err2) });
    }
  }
};
