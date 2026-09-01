import { readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/virtual-computer/app.ts"],
  bundle: true,
  minify: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  write: false,
  legalComments: "none",
});

const bundle = result.outputFiles?.[0]?.text;
if (!bundle) throw new Error("Virtual computer bundle was not generated");

const template = await readFile("src/virtual-computer/index.html", "utf8");
const safeBundle = bundle.replace(/<\/script/gi, "<\\/script");
const html = template.replace(
  "<!-- SCRIPT -->",
  () => `<script type="module">${safeBundle}</script>`,
)
  .replace(/[ \t]+$/gm, "")
  .replace(/^ +(?=\t)/gm, "");
await writeFile("src/kilntainers/dashboard.html", html, "utf8");
