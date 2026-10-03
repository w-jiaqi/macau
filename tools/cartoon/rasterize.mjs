#!/usr/bin/env node
// Rasterize an SVG to PNG with resvg-js, using the bundled OFL fonts in tools/fonts.
//
//   node tools/cartoon/rasterize.mjs <in.svg> <out.png> [width]
//   node tools/cartoon/rasterize.mjs --icons <icons dir> <out.json>
//
// The second form measures every icon's silhouette (render.py uses it to keep
// icons and labels from overlapping): each <name>.svg is drawn at 160 x 160 and,
// for every 8 px band of rows, the left/right extent of the clearly opaque
// pixels is recorded (soft ground shadows and faint glows fall below the
// alpha threshold and are ignored).
//
// Needs @resvg/resvg-js: run `npm install` in tools/ (tools/node_modules is
// gitignored).  NODE_PATH is honoured as a fallback location for the module.
// System fonts are NOT loaded, so the output is identical on every machine.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
let Resvg;
try {
  ({ Resvg } = require('@resvg/resvg-js'));
} catch (err) {
  console.error('rasterize.mjs: cannot load @resvg/resvg-js - run "npm install" in tools/ ' +
    '(or point NODE_PATH at a node_modules that has it).');
  process.exit(2);
}

const here = path.dirname(fileURLToPath(import.meta.url));
const fontsDir = path.resolve(here, '..', 'fonts');
const fontFiles = fs.readdirSync(fontsDir)
  .filter((f) => /\.(ttf|otf)$/i.test(f))
  .map((f) => path.join(fontsDir, f));

const opts = {
  font: { fontFiles, loadSystemFonts: false, defaultFontFamily: 'ZCOOL KuaiLe' },
  shapeRendering: 2,       // geometricPrecision (anti-aliased)
  textRendering: 2,        // geometricPrecision
  imageRendering: 0,
};

function measureIcons(dir, outJson) {
  const BAND = 8, ALPHA = 110, SIZE = 160;
  const out = {};
  for (const fn of fs.readdirSync(dir).filter((f) => f.endsWith('.svg')).sort()) {
    const img = new Resvg(fs.readFileSync(path.join(dir, fn)), { ...opts, fitTo: { mode: 'width', value: SIZE } }).render();
    const { width: w, height: h, pixels } = img;
    const bands = [];
    let bx0 = w, by0 = h, bx1 = -1, by1 = -1;
    for (let y0 = 0; y0 < h; y0 += BAND) {
      let x0 = w, x1 = -1;
      for (let y = y0; y < Math.min(h, y0 + BAND); y++) {
        for (let x = 0; x < w; x++) {
          if (pixels[(y * w + x) * 4 + 3] >= ALPHA) {
            if (x < x0) x0 = x;
            if (x > x1) x1 = x;
            if (y < by0) by0 = y;
            if (y > by1) by1 = y;
          }
        }
      }
      if (x1 >= 0) {
        bands.push([y0, Math.min(h, y0 + BAND), x0, x1 + 1]);
        bx0 = Math.min(bx0, x0); bx1 = Math.max(bx1, x1 + 1);
      }
    }
    out[fn.replace(/\.svg$/, '')] = { bbox: [bx0, by0, bx1, by1 + 1], bands };
  }
  fs.writeFileSync(outJson, JSON.stringify(out));
  console.log(`rasterize: measured ${Object.keys(out).length} icons -> ${outJson}`);
}

const args = process.argv.slice(2);
if (args[0] === '--icons') {
  if (args.length < 3) {
    console.error('usage: node tools/cartoon/rasterize.mjs --icons <icons dir> <out.json>');
    process.exit(1);
  }
  measureIcons(args[1], args[2]);
  process.exit(0);
}

const [svgPath, pngPath, widthArg] = args;
if (!svgPath || !pngPath) {
  console.error('usage: node tools/cartoon/rasterize.mjs <in.svg> <out.png> [width]');
  process.exit(1);
}
if (widthArg) opts.fitTo = { mode: 'width', value: Number(widthArg) };

const t0 = Date.now();
const resvg = new Resvg(fs.readFileSync(svgPath), opts);
const img = resvg.render();
const png = img.asPng();
fs.writeFileSync(pngPath, png);
console.log(`rasterize: ${img.width}x${img.height} -> ${pngPath} ` +
  `(${(png.length / 1048576).toFixed(1)} MB, ${((Date.now() - t0) / 1000).toFixed(1)} s)`);
