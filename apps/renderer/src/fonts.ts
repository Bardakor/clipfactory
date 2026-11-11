import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

// Unicode ranges of the two subsets shipped by @fontsource (French needs both for œ, Œ, etc.).
const LATIN =
  "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD";
const LATIN_EXT =
  "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF";

const FONTS: Array<[family: string, slug: string, weights: string[]]> = [
  ["Montserrat", "montserrat", ["800", "900"]],
  ["Inter", "inter", ["600", "800"]],
  ["Bebas Neue", "bebas-neue", ["400"]],
  ["Anton", "anton", ["400"]],
];

// Fonts are local files in public/fonts (copied at install time), so a render never needs a font CDN.
// loadFont() holds the render until each face is ready, so no frame is ever drawn with a fallback font.
for (const [family, slug, weights] of FONTS) {
  for (const weight of weights) {
    for (const [subset, unicodeRange] of [
      ["latin", LATIN],
      ["latin-ext", LATIN_EXT],
    ] as const) {
      void loadFont({
        family,
        url: staticFile(`fonts/${slug}-${subset}-${weight}-normal.woff2`),
        weight,
        unicodeRange,
      });
    }
  }
}
