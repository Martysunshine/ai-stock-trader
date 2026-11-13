// Parquet decoding runs here, off the server's event loop: ZSTD decompression and column decoding
// of ~120k bars is synchronous CPU work that would otherwise stall the SSE stream and every request.
// Plain JS on purpose (no TypeScript loader needed inside the worker).
import { parentPort } from 'node:worker_threads';
import zlib from 'node:zlib';
import { asyncBufferFromFile, parquetReadObjects } from 'hyparquet';

const compressors = { ZSTD: (input) => new Uint8Array(zlib.zstdDecompressSync(input)) };

parentPort.on('message', async ({ id, file, splits }) => {
  try {
    const buf = await asyncBufferFromFile(file);
    const rows = await parquetReadObjects({
      file: buf,
      compressors,
      columns: ['start_ms', 'open', 'high', 'low', 'close', 'volume'],
    });
    const n = rows.length;
    const t = new Float64Array(n);
    const o = new Float64Array(n);
    const h = new Float64Array(n);
    const l = new Float64Array(n);
    const c = new Float64Array(n);
    const v = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const r = rows[i];
      const tms = Number(r.start_ms);
      // Prices before a split's ex-date are divided by its ratio (volume multiplied).
      let f = 1;
      for (const s of splits) if (tms < s.at) f *= s.ratio;
      t[i] = Math.floor(tms / 1000);
      o[i] = r.open / f;
      h[i] = r.high / f;
      l[i] = r.low / f;
      c[i] = r.close / f;
      v[i] = r.volume * f;
    }
    parentPort.postMessage({ id, ok: true, t, o, h, l, c, v }, [t.buffer, o.buffer, h.buffer, l.buffer, c.buffer, v.buffer]);
  } catch (err) {
    parentPort.postMessage({ id, ok: false, error: String(err?.message ?? err) });
  }
});
