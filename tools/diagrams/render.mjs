#!/usr/bin/env node
// Renders docs/**/*.excalidraw to docs/img/**/*.svg with Excalidraw's own exportToSvg, run in headless
// Chromium so the images match what excalidraw.com shows (rough.js strokes, Nunito/Cascadia fonts
// inlined as subsets). Fully offline: the fonts come from node_modules and every other request is blocked.
//
// Each SVG starts with a stamp holding the sha256 of its source; a diagram whose stamp still matches
// is skipped, and tests/test_docs.py uses the same stamp to check that no image is stale.
//
//   node tools/diagrams/render.mjs                       every diagram, and delete images whose source is gone
//   node tools/diagrams/render.mjs docs/db.excalidraw    just these (what the pre-commit hook passes)
//   node tools/diagrams/render.mjs --force               re-render even if stamps match (after upgrading Excalidraw)
//   node tools/diagrams/render.mjs --fail-on-change      exit 1 if any image was written or deleted (the pre-commit
//                                                        hook: pre-commit alone wouldn't notice a new, untracked image)

import { createHash } from "node:crypto";
import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";
import { chromium } from "playwright";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const DOCS = path.join(ROOT, "docs");
const IMG = path.join(DOCS, "img");
const ASSETS = path.join(HERE, "node_modules/@excalidraw/excalidraw/dist/prod");
const STAMP = /^<!-- Rendered from (\S+) sha256=([0-9a-f]{64}) by tools\/diagrams\/render\.mjs\. Edit the source, not this file\. -->\n/;
const TYPES = { ".js": "text/javascript", ".woff2": "font/woff2", ".css": "text/css", ".json": "application/json" };

const rel = (p) => path.relative(ROOT, p).split(path.sep).join("/");
const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const stampOf = (source, hash) =>
  `<!-- Rendered from ${rel(source)} sha256=${hash} by tools/diagrams/render.mjs. Edit the source, not this file. -->\n`;

/** docs/adr/ADR-001.excalidraw -> docs/img/adr/ADR-001.svg */
const imageFor = (source) => path.join(IMG, path.relative(DOCS, source).replace(/\.excalidraw$/, ".svg"));

async function walk(dir, ext) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await walk(full, ext)));
    else if (entry.name.endsWith(ext)) found.push(full);
  }
  return found.sort();
}

async function readStamp(image) {
  const text = await readFile(image, "utf8").catch(() => "");
  const match = text.match(STAMP);
  return match ? { source: match[1], hash: match[2] } : null;
}

/** Bundle Excalidraw's exporter for the browser and serve it, plus its fonts, on a random local port. */
async function startServer() {
  const bundle = await esbuild.build({
    stdin: { contents: 'export { exportToSvg } from "@excalidraw/excalidraw";', resolveDir: HERE },
    bundle: true,
    format: "esm",
    splitting: true,
    outdir: "/bundle",
    write: false,
    minify: true,
    logLevel: "error",
    define: { "process.env.NODE_ENV": '"production"' },
  });
  const files = new Map(bundle.outputFiles.map((f) => [f.path.replace(/\\/g, "/"), f.contents]));
  const page = `<!doctype html><meta charset="utf-8"><body><script type="module">
    window.EXCALIDRAW_ASSET_PATH = location.origin + "/assets/";
    const { exportToSvg } = await import("/bundle/stdin.js");
    window.exportToSvg = exportToSvg;
  </script>`;

  const server = createServer(async (req, res) => {
    const url = decodeURIComponent(new URL(req.url, "http://x").pathname);
    let body = null;
    if (url === "/") body = page;
    else if (files.has(url)) body = files.get(url);
    else if (url.startsWith("/assets/")) {
      const file = path.join(ASSETS, path.normalize(url.slice("/assets/".length)));
      if (file.startsWith(ASSETS + path.sep)) body = await readFile(file).catch(() => null);
    }
    if (body === null) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": url === "/" ? "text/html" : TYPES[path.extname(url)] ?? "application/octet-stream" });
    res.end(body);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

async function render(jobs) {
  const server = await startServer();
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  const problems = [];
  try {
    const page = await browser.newPage();
    // Offline: anything not served above (e.g. Excalidraw's CDN fallback for a missing font) is an error.
    await page.route("**/*", (route) => {
      const url = route.request().url();
      if (url.startsWith(origin + "/")) return route.continue();
      problems.push(`blocked request to ${url}`);
      return route.abort();
    });
    page.on("response", (r) => r.status() >= 400 && problems.push(`HTTP ${r.status()} for ${r.url()}`));
    page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
    await page.goto(origin + "/");
    await page.waitForFunction(() => typeof window.exportToSvg === "function");

    for (const job of jobs) {
      const scene = JSON.parse(job.text);
      const svg = await page.evaluate(async (scene) => {
        const el = await window.exportToSvg({
          elements: scene.elements.filter((e) => !e.isDeleted),
          appState: { ...scene.appState, exportBackground: true, exportWithDarkMode: false, exportEmbedScene: false },
          files: scene.files ?? {},
          exportPadding: 20,
        });
        return el.outerHTML;
      }, scene);
      if (problems.length) throw new Error(`${rel(job.source)}:\n  ${problems.join("\n  ")}`);
      await mkdir(path.dirname(job.image), { recursive: true });
      await writeFile(job.image, stampOf(job.source, job.hash) + svg + "\n");
      console.log(`rendered ${rel(job.image)}`);
    }
  } finally {
    await browser.close();
    server.close();
  }
}

async function main() {
  const args = process.argv.slice(2);
  const force = args.includes("--force");
  const failOnChange = args.includes("--fail-on-change");
  const named = args.filter((a) => !a.startsWith("--")).map((a) => path.resolve(a));
  const sources = named.length ? named : await walk(DOCS, ".excalidraw");

  const jobs = [];
  for (const source of sources) {
    if (!source.startsWith(DOCS + path.sep) || source.startsWith(IMG + path.sep) || !source.endsWith(".excalidraw")) {
      throw new Error(`not a diagram under docs/: ${rel(source)}`);
    }
    const text = await readFile(source, "utf8");
    const hash = sha256(text);
    const image = imageFor(source);
    if (!force && (await readStamp(image))?.hash === hash) continue;
    jobs.push({ source, image, text, hash });
  }
  if (jobs.length) await render(jobs);

  // Full runs also drop images whose diagram was deleted or renamed (only images this script made).
  let deleted = 0;
  if (!named.length) {
    const wanted = new Set(sources.map(imageFor));
    for (const image of await walk(IMG, ".svg")) {
      if (!wanted.has(image) && (await readStamp(image))) {
        await rm(image);
        deleted++;
        console.log(`deleted ${rel(image)} (its diagram is gone)`);
      }
    }
  }
  if (failOnChange && jobs.length + deleted) {
    console.error(`${jobs.length + deleted} image(s) changed: git add docs/img and commit again.`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(`render.mjs: ${err.message}`);
  process.exit(1);
});
