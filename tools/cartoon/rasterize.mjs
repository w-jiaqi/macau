#!/usr/bin/env node
// Rasterize an SVG to PNG with resvg-js, using the bundled OFL fonts in tools/fonts.
//
//   node tools/cartoon/rasterize.mjs <in.svg> <out.png> [width]
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

const [svgPath, pngPath, widthArg] = process.argv.slice(2);
if (!svgPath || !pngPath) {
  console.error('usage: node tools/cartoon/rasterize.mjs <in.svg> <out.png> [width]');
  process.exit(1);
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
if (widthArg) opts.fitTo = { mode: 'width', value: Number(widthArg) };

const t0 = Date.now();
const resvg = new Resvg(fs.readFileSync(svgPath), opts);
const img = resvg.render();
const png = img.asPng();
fs.writeFileSync(pngPath, png);
console.log(`rasterize: ${img.width}x${img.height} -> ${pngPath} ` +
  `(${(png.length / 1048576).toFixed(1)} MB, ${((Date.now() - t0) / 1000).toFixed(1)} s)`);
