#!/usr/bin/env node
/**
 * Capture platform.xiaomimimo.com session cookies for herdr.quota.
 *
 * Preferred path: `refresh_cookies.py mimo` — real Chrome/Brave + CDP.
 * Google OAuth rejects Playwright's automation flags and "test" Chrome, so
 * this file keeps only the DevTools paste fallback.
 *
 * Usage:
 *   node capture-mimo-cookies.mjs --stdin   # paste Cookie header from DevTools
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(__dirname, "..", ".secrets");
const outFile = path.join(outDir, "mimo-web-session.json");

function saveCookies(cookieHeader, extra = {}) {
  const names = cookieHeader.split(";").map((x) => x.split("=")[0].trim()).filter(Boolean);
  fs.mkdirSync(outDir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(
    outFile,
    JSON.stringify(
      {
        provider: "mimo-web",
        cookie: cookieHeader,
        captured_at: new Date().toISOString(),
        cookie_names: names,
        ...extra,
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
  console.log(`Saved ${names.length} cookies → ${outFile}`);
}

async function captureStdin() {
  console.log("Paste the Cookie request header from DevTools (Network → any /api/v1/tokenPlan/* → Request Headers → cookie). End with Ctrl-D:");
  console.log("(Prefer: bin/refresh_cookies.py mimo — real Chrome, auto-renew from profile)");
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    console.error("empty cookie");
    process.exit(1);
  }
  saveCookies(raw.replace(/^cookie:\s*/i, ""), { source: "devtools" });
}

if (process.argv.includes("--stdin")) {
  await captureStdin();
} else {
  console.error("Playwright/Google OAuth login was removed — it fails Google's browser check.");
  console.error("Use: bin/refresh_cookies.py mimo   (real Chrome/Brave + CDP, profile persists)");
  console.error("  or: bin/capture-mimo-cookies.sh --stdin   (paste Cookie from DevTools)");
  process.exit(1);
}

