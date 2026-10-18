// Bundles src/main.jsx into dist/assets and writes dist/index.html.
//   node build.mjs           production build (minified)
//   node build.mjs --watch   rebuild on change (refresh the browser served by the API)
import * as esbuild from "esbuild";
import { mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";

const watch = process.argv.includes("--watch");
rmSync("dist", { recursive: true, force: true });
mkdirSync("dist/assets", { recursive: true });

const options = {
  entryPoints: ["src/main.jsx"],
  bundle: true,
  outdir: "dist/assets",
  entryNames: "app",
  format: "esm",
  jsx: "automatic",
  target: ["es2020"],
  minify: !watch,
  sourcemap: true,
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  loader: { ".css": "css" },
  logLevel: "info",
};

writeFileSync("dist/index.html", readFileSync("index.html", "utf8"));

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching src/ ... (open the app via the API server, e.g. http://localhost:8000)");
} else {
  await esbuild.build(options);
}
