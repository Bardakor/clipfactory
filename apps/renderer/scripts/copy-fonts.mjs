// Copies the caption fonts (all SIL Open Font License) from @fontsource into public/fonts,
// so renders never depend on a font CDN being reachable.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const out = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "fonts");
mkdirSync(out, { recursive: true });

const fonts = [
  ["montserrat", ["800", "900"]],
  ["inter", ["600", "800"]],
  ["bebas-neue", ["400"]],
  ["anton", ["400"]],
];
for (const [name, weights] of fonts) {
  const dir = join(dirname(require.resolve(`@fontsource/${name}/package.json`)), "files");
  for (const weight of weights) {
    for (const subset of ["latin", "latin-ext"]) {
      const file = `${name}-${subset}-${weight}-normal.woff2`;
      if (existsSync(join(dir, file))) copyFileSync(join(dir, file), join(out, file));
      else console.warn(`[fonts] missing ${file}`);
    }
  }
}
