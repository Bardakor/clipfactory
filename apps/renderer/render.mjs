// Renders one clip with Remotion. Called by the Python worker:
//
//   node render.mjs --props props.json --out out.mp4 --root <work dir> [--scale 0.5] [--crf 18] [--concurrency N]
//
// stdout carries one JSON object per line ({"type":"progress","value":0.42}); errors go to stderr.
import { bundle } from "@remotion/bundler";
import { ensureBrowser, renderMedia, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { createReadStream, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => (arg.startsWith("--") ? [...pairs, [arg.slice(2), all[i + 1]]] : pairs), []),
);
for (const required of ["props", "out", "root"]) {
  if (!args[required]) {
    console.error(`missing --${required}`);
    process.exit(2);
  }
}
const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);

/** Bundle the composition once per source revision; later renders reuse it. */
const getBundle = async () => {
  const hash = createHash("sha256");
  const walk = (dir) => {
    for (const name of readdirSync(dir).sort()) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else hash.update(name).update(readFileSync(path));
    }
  };
  walk(join(here, "src"));
  walk(join(here, "public"));
  hash.update(readFileSync(join(here, "package.json")));
  const target = join(here, ".bundle", hash.digest("hex").slice(0, 16));
  if (existsSync(join(target, "index.html"))) {
    return target;
  }
  mkdirSync(join(here, ".bundle"), { recursive: true });
  // Build next to the target and rename, so two renders starting together never see a half-written bundle.
  const staging = `${target}.${process.pid}.tmp`;
  await bundle({ entryPoint: join(here, "src", "index.ts"), outDir: staging, publicDir: join(here, "public") });
  try {
    renameSync(staging, target);
  } catch {
    rmSync(staging, { recursive: true, force: true }); // another process won the race; use its bundle
  }
  return target;
};

/** Serve the clip's work directory to the headless browser (range requests + CORS, loopback only). */
const serveMedia = (root) =>
  new Promise((resolveServer) => {
    const types = { ".mp4": "video/mp4", ".jpg": "image/jpeg", ".json": "application/json" };
    const server = createServer((req, res) => {
      const requested = normalize(decodeURIComponent((req.url ?? "/").split("?")[0]));
      const path = resolve(join(root, requested));
      if (!(path === root || path.startsWith(root + sep)) || !existsSync(path) || !statSync(path).isFile()) {
        res.writeHead(404).end();
        return;
      }
      const size = statSync(path).size;
      const headers = {
        "Content-Type": types[extname(path)] ?? "application/octet-stream",
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
      };
      if (req.method === "OPTIONS") {
        res.writeHead(204, headers).end();
        return;
      }
      const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? "");
      if (range) {
        const start = range[1] === "" ? Math.max(0, size - Number(range[2])) : Number(range[1]);
        const end = range[1] !== "" && range[2] !== "" ? Math.min(Number(range[2]), size - 1) : size - 1;
        if (start > end || start >= size) {
          res.writeHead(416, { ...headers, "Content-Range": `bytes */${size}` }).end();
          return;
        }
        res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${size}`, "Content-Length": end - start + 1 });
        if (req.method === "HEAD") res.end();
        else createReadStream(path, { start, end }).pipe(res);
        return;
      }
      res.writeHead(200, { ...headers, "Content-Length": size });
      if (req.method === "HEAD") res.end();
      else createReadStream(path).pipe(res);
    });
    server.listen(0, "127.0.0.1", () => resolveServer(server));
  });

const main = async () => {
  const props = JSON.parse(readFileSync(args.props, "utf8"));
  const root = resolve(args.root);
  const server = await serveMedia(root);
  const base = `http://127.0.0.1:${server.address().port}`;
  const inputProps = { ...props, src: `${base}/${props.src}`, background: `${base}/${props.background}` };
  try {
    await ensureBrowser(); // downloads Remotion's headless browser on first use
    const serveUrl = await getBundle();
    emit({ type: "progress", value: 0.02 });
    const composition = await selectComposition({ serveUrl, id: "Clip", inputProps });
    await renderMedia({
      composition,
      serveUrl,
      inputProps,
      codec: "h264",
      audioCodec: "aac",
      audioBitrate: "192k",
      pixelFormat: "yuv420p",
      crf: args.crf ? Number(args.crf) : 18,
      x264Preset: "fast",
      jpegQuality: 92,
      scale: args.scale ? Number(args.scale) : 1,
      concurrency: args.concurrency ? Number(args.concurrency) : null,
      muted: !props.hasAudio,
      enforceAudioTrack: Boolean(props.hasAudio),
      outputLocation: resolve(args.out),
      overwrite: true,
      logLevel: "error",
      onProgress: ({ progress }) => emit({ type: "progress", value: Math.max(0.02, progress) }),
    });
    emit({ type: "done" });
  } finally {
    server.close();
  }
};

main().then(
  () => process.exit(0),
  (error) => {
    console.error(error?.stack ?? String(error));
    process.exit(1);
  },
);
