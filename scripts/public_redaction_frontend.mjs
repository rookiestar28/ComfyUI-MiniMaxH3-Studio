import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const request = JSON.parse(readFileSync(0, "utf8"));
if (Number(process.versions.node.split(".")[0]) !== 24) {
  throw new Error("P2_NODE_RUNTIME_REFUSED_EXPECT_24");
}
const require = createRequire(resolve(request.root, "frontend/package.json"));
const manifest = require("vite/package.json");
const declared = JSON.parse(readFileSync(resolve(request.root, "frontend/package.json"), "utf8"));
if (manifest.version !== declared.devDependencies.vite) {
  throw new Error("P2_TOOLCHAIN_VERSION_MISMATCH");
}
const { parseSync } = await import(pathToFileURL(require.resolve("vite")).href);
if (typeof parseSync !== "function") throw new Error("P2_PARSER_API_UNAVAILABLE");
const codePattern = new RegExp(request.codePattern, "gi");

function semantic(value, text, classes) {
  if (typeof value === "bigint") return { bigint: String(value) };
  if (Array.isArray(value)) return value.map((child) => semantic(child, text, classes));
  if (value && typeof value === "object") {
    if (value.type === "Identifier") {
      classes.C_identifiers += (value.name.match(codePattern) || []).length;
    } else if (["Literal", "StringLiteral", "TemplateElement", "JSXText"].includes(value.type)) {
      classes.B_strings += (text.slice(value.start, value.end).match(codePattern) || []).length;
    }
    return Object.fromEntries(
      Object.keys(value)
        .filter((key) => !["start", "end", "loc", "range", "comments"].includes(key))
        .sort()
        .map((key) => [key, semantic(value[key], text, classes)]),
    );
  }
  return value;
}

const output = request.files.map(({ path, text }) => {
  const parsed = parseSync(path, text, { sourceType: "unambiguous", preserveParens: true });
  if (parsed.errors.some((error) => error.severity === "Error")) {
    return { path, error: "P2_PARSE_REFUSED" };
  }
  // IMPORTANT: parser ranges distinguish regex, template and JSX text from comments;
  // replacing this with slash matching can rewrite executable or rendered content.
  const comments = parsed.comments.map(({ start, end }) => {
    const raw = text.slice(start, end);
    // IMPORTANT: the parser also returns the leading hashbang as a comment;
    // preserve this executable tool directive rather than rejecting a valid module.
    if (!raw.startsWith("//") && !raw.startsWith("/*") && !(start === 0 && raw.startsWith("#!"))) {
      throw new Error("P2_COMMENT_RANGE_UNSUPPORTED");
    }
    return {
      start: Array.from(text.slice(0, start)).length,
      end: Array.from(text.slice(0, end)).length,
      line: text.slice(0, start).split(/\r\n|\r|\n|\u2028|\u2029/u).length,
    };
  });
  const classes = { B_strings: 0, C_identifiers: 0 };
  const signature = createHash("sha256")
    .update(JSON.stringify(semantic(parsed.program, text, classes)))
    .digest("hex");
  return { path, comments, signature, classes };
});
process.stdout.write(JSON.stringify({ parser: "vite/parseSync", version: manifest.version, files: output }));
