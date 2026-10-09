#!/usr/bin/env node
/*
 * Add a "Dashboard" control to the Staff Portal header, left of Configuration.
 *
 * The header lays its right-hand controls out as a fixed list:
 *
 *     (0,r.jsxs)("div",{className:"flex items-center gap-8",children:[
 *         (0,r.jsx)(v.A,{anyOf:p.Ll,children:(0,r.jsx)(g.default,{})}),  // Configuration
 *         (0,r.jsx)(f.default,{}),                                       // language
 *         (0,r.jsx)(m.default,{}),                                       // notifications
 *         (0,r.jsx)(u.default,{})                                        // account
 *     ]})
 *
 * The new button is inserted as the first child, so it lands immediately left of
 * Configuration and — unlike Configuration, which sits inside the `v.A` RBAC
 * guard — stays visible regardless of configuration permissions.
 *
 * The element is injected inline rather than as a component: the surrounding
 * array is built once behind a React Compiler memo sentinel, and the button
 * needs no hooks (the click handler is a plain cross-origin navigation), so
 * there is no component identity to keep stable.
 *
 * The dashboard runs as its own service on its own origin, so the target cannot
 * be a portal route and is baked in from DASHBOARD_URL at image build time. The
 * portal's current page rides along as ?returnUrl=, which is what the
 * dashboard's own Back button reads.
 *
 * The header is a client component, so it is emitted into both a static chunk
 * and a server chunk. Both are patched — leaving the server copy alone would
 * render the header without the button and trip a hydration mismatch. Exits
 * non-zero if fewer than two bundles match, so a base-image change fails the
 * build instead of silently dropping the button.
 */
const fs = require("fs");
const path = require("path");

const ROOT = "/app/.next";
const URL_TARGET = process.env.DASHBOARD_URL || "";
const LABEL = process.env.DASHBOARD_LABEL || "Dashboard";

if (!URL_TARGET) {
  console.error("DASHBOARD_URL is empty — pass it as a build arg");
  process.exit(1);
}

// (0,<jsx>.jsxs)("div",{className:"flex items-center gap-8",children:[
const PATTERN =
  /(\(0,(\w+)\.jsxs\)\("div",\{className:"flex items-center gap-8",children:\[)/g;

// 1.2+: the Configuration component's own button, as the server chunk writes
// it (inline handler and children) and as the client chunk's compiler memo
// writes it (both held in locals).
const CONFIG_BUTTON = [
  /\(0,(\w+)\.jsxs\)\("button",\{onClick:\(\)=>\{\w+\.push\("\/configuration\/registry\/details"\)\},className:"flex items-center gap-2 hover:opacity-80",children:\[.*?\("configuration"\)\}\)\]\}\)/g,
  /\(0,(\w+)\.jsxs\)\("button",\{onClick:\w+,className:"flex items-center gap-2 hover:opacity-80",children:\[\w+,\w+\]\}\)/g,
];
const CONFIG_ICON = "/images/config/config_icon.png";

function patchConfigButton(source) {
  for (const re of CONFIG_BUTTON) {
    const hits = source.match(re) || [];
    if (hits.length !== 1) continue;
    return source.replace(
      re,
      (call, jsx) =>
        `(0,${jsx}.jsxs)("div",{className:"flex items-center gap-8",children:[${button(jsx)}${call}]})`
    );
  }
  return source;
}

// Matches the sibling Configuration control, so the two read as a pair.
const BUTTON_CLASS = "flex items-center gap-2 hover:opacity-80";
const LABEL_CLASS = "text-[16px] text-neutral-first";

function button(jsx) {
  const url = JSON.stringify(URL_TARGET);
  const label = JSON.stringify(LABEL);
  const rect = (x, y, w, h) =>
    `(0,${jsx}.jsx)("rect",{x:${x},y:${y},width:${w},height:${h},rx:1.5})`;

  // A four-panel grid glyph, stroked in the surrounding text colour so it
  // tracks the active theme rather than shipping another PNG asset.
  const icon =
    `(0,${jsx}.jsxs)("svg",{xmlns:"http://www.w3.org/2000/svg",width:24,height:24,` +
    `viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:1.8,` +
    `strokeLinecap:"round",strokeLinejoin:"round","aria-hidden":"true",` +
    `className:"text-neutral-first",children:[` +
    [rect(3, 3, 7, 9), rect(14, 3, 7, 5), rect(14, 10, 7, 11), rect(3, 14, 7, 7)].join(",") +
    `]})`;

  const onClick =
    `()=>{var u=${url};` +
    `window.location.href=u+(u.indexOf("?")>-1?"&":"?")+` +
    `"returnUrl="+encodeURIComponent(window.location.href)}`;

  return (
    `(0,${jsx}.jsxs)("button",{type:"button",onClick:${onClick},` +
    `className:${JSON.stringify(BUTTON_CLASS)},title:${label},children:[` +
    `${icon},(0,${jsx}.jsx)("span",{className:${JSON.stringify(LABEL_CLASS)},children:${label}})` +
    `]}),`
  );
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.name.endsWith(".js")) yield full;
  }
}

let patched = 0;
const renamed = []; // [{from, to}] — static chunks only; used to bust browser caches
for (const file of walk(ROOT)) {
  const before = fs.readFileSync(file, "utf8");
  let after = before;
  if (before.includes("flex items-center gap-8")) {
    after = before.replace(PATTERN, (_m, open, jsx) => open + button(jsx));
  }
  if (after === before && before.includes(CONFIG_ICON)) {
    after = patchConfigButton(before);
  }
  if (after !== before) {
    fs.writeFileSync(file, after);
    patched += 1;
    console.log("  patched " + file.replace(ROOT + "/", ""));

    // Browser caches by the content-hashed filename. The base image already
    // published that hash for the unpatched bytes, so keep serving under the
    // same name and a returning visitor would keep the old buttonless chunk.
    // Renaming the static copy (and rewriting every reference) forces a miss.
    // Server chunks are never fetched by the browser, so they stay put.
    if (file.includes(`${path.sep}static${path.sep}`)) {
      const ext = path.extname(file);
      const next = file.slice(0, -ext.length) + ".dashboard" + ext;
      fs.renameSync(file, next);
      renamed.push({ from: path.basename(file), to: path.basename(next) });
      console.log("  renamed " + path.basename(file) + " -> " + path.basename(next));
    }
  }
}

if (patched < 2) {
  console.error(
    `dashboard-nav patch matched ${patched} bundle(s), expected at least 2 — ` +
      "the base image header layout changed, so the Dashboard button would be missing"
  );
  process.exit(1);
}

for (const { from, to } of renamed) {
  let refs = 0;
  for (const file of walk(ROOT)) {
    const before = fs.readFileSync(file, "utf8");
    if (!before.includes(from)) continue;
    fs.writeFileSync(file, before.split(from).join(to));
    refs += 1;
  }
  console.log(`  rewrote ${refs} reference(s) to ${from}`);
}

console.log(`added the Dashboard nav button in ${patched} bundle(s) -> ${URL_TARGET}`);
