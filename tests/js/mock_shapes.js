// Prints the JSON shape (keys and value types, recursively) of every GET endpoint in
// dashboard/js/mock.js, so the Python API can be checked against the UI's contract.
const fs = require('fs');
const path = require('path');
global.window = {};
const src = fs.readFileSync(path.join(__dirname, '../../dashboard/js/mock.js'), 'utf8');
new Function('window', src)(global.window);
function shape(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return v.length ? [shape(v[0])] : [];
  if (typeof v === 'object') { const o = {}; for (const k of Object.keys(v)) o[k] = shape(v[k]); return o; }
  return typeof v;
}
const out = {};
for (const [k, fn] of Object.entries(window.FF_MOCK)) {
  if (k.startsWith('GET ')) out[k.slice(4)] = shape(fn(null, null));
}
process.stdout.write(JSON.stringify(out));
