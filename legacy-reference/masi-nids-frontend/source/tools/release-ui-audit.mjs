#!/usr/bin/env node

import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import {
  createWriteStream,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import runLighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";

const outputArg = process.argv[2];
if (!outputArg) throw new Error("usage: release-ui-audit.mjs <new-output-directory>");
const outputDir = resolve(outputArg);
if (existsSync(outputDir)) throw new Error(`refusing to overwrite UI audit directory: ${outputDir}`);
if (!existsSync(resolve(".next/BUILD_ID"))) throw new Error("run npm build before the release UI audit");
mkdirSync(outputDir, { recursive: false });

const bundlePath = resolve(outputDir, "bundle-gzip.json");
const lighthousePath = resolve(outputDir, "lighthouse.json");
const reportPath = resolve(outputDir, "report.json");
const serverLogPath = resolve(outputDir, "next-server.log");

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

async function freePort() {
  const server = createServer();
  await new Promise((accept, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", accept);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((accept, reject) => server.close((error) => (error ? reject(error) : accept())));
  if (!port) throw new Error("could not allocate a loopback UI audit port");
  return port;
}

function run(command, args, options = {}) {
  return new Promise((accept, reject) => {
    const child = spawn(command, args, {
      cwd: process.cwd(),
      env: { ...process.env, ...(options.env ?? {}) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (code === 0) accept(output);
      else reject(new Error(`${command} exited code=${code} signal=${signal ?? "none"}: ${output.slice(-4000)}`));
    });
  });
}

async function waitReady(url, timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(5_000) });
      if (response.status >= 200 && response.status < 500) return;
    } catch {
      // Bounded readiness retry.
    }
    await new Promise((accept) => setTimeout(accept, 250));
  }
  throw new Error(`Next production server did not become ready: ${url}`);
}

function processGroupAlive(pid) {
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    if (error?.code === "EPERM") return true;
    throw error;
  }
}

function signalProcessGroup(child, signal) {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

async function stopProcessGroup(child) {
  if (!child.pid || !processGroupAlive(child.pid)) return;
  signalProcessGroup(child, "SIGTERM");
  const deadline = Date.now() + 10_000;
  while (processGroupAlive(child.pid) && Date.now() < deadline) {
    await new Promise((accept) => setTimeout(accept, 100));
  }
  if (processGroupAlive(child.pid)) {
    signalProcessGroup(child, "SIGKILL");
    const killDeadline = Date.now() + 5_000;
    while (processGroupAlive(child.pid) && Date.now() < killDeadline) {
      await new Promise((accept) => setTimeout(accept, 100));
    }
  }
  if (processGroupAlive(child.pid)) {
    throw new Error(`process group ${child.pid} did not terminate`);
  }
}

function finishStream(stream) {
  return new Promise((accept, reject) => {
    stream.once("error", reject);
    stream.end(accept);
  });
}

const port = await freePort();
const baseUrl = `http://127.0.0.1:${port}`;
const serverLog = createWriteStream(serverLogPath, { flags: "wx" });
const server = spawn("npm", ["run", "start", "--", "--hostname", "127.0.0.1", "--port", String(port)], {
  cwd: process.cwd(),
  env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
  stdio: ["ignore", "pipe", "pipe"],
  detached: true,
});
server.stdout.pipe(serverLog);
server.stderr.pipe(serverLog);

let report;
let chrome;
let chromeLog;
let chromeProfileDir;
try {
  await waitReady(`${baseUrl}/login`);
  await run(process.execPath, [resolve("tools/system-bundle-audit.mjs"), bundlePath], {
    env: { PLAYWRIGHT_BASE_URL: baseUrl },
  });
  const chromePort = await freePort();
  chromeProfileDir = mkdtempSync(join(tmpdir(), "masi-lighthouse-"));
  chromeLog = createWriteStream(resolve(outputDir, "chromium.log"), { flags: "wx" });
  chrome = spawn(
    chromium.executablePath(),
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--remote-debugging-address=127.0.0.1",
      `--remote-debugging-port=${chromePort}`,
      `--user-data-dir=${chromeProfileDir}`,
      "about:blank",
    ],
    { cwd: tmpdir(), env: { ...process.env, HOME: process.env.HOME ?? "/tmp" }, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );
  chrome.stdout.pipe(chromeLog);
  chrome.stderr.pipe(chromeLog);
  await waitReady(`http://127.0.0.1:${chromePort}/json/version`, 30_000);
  const lighthouseResult = await runLighthouse(
    `${baseUrl}/login`,
    {
      port: chromePort,
      logLevel: "silent",
      output: "json",
      onlyCategories: ["performance", "accessibility", "best-practices"],
    },
    desktopConfig,
  );
  if (!lighthouseResult?.lhr) throw new Error("Lighthouse returned no result");
  writeFileSync(lighthousePath, `${JSON.stringify(lighthouseResult.lhr)}\n`, { flag: "wx" });
  const bundle = JSON.parse(readFileSync(bundlePath, "utf8"));
  const lighthouseReport = JSON.parse(readFileSync(lighthousePath, "utf8"));
  const scores = Object.fromEntries(
    ["performance", "accessibility", "best-practices"].map((name) => [
      name,
      Number(lighthouseReport.categories?.[name]?.score ?? 0),
    ]),
  );
  const thresholds = { performance: 0.75, accessibility: 0.9, "best-practices": 0.9 };
  const scoreGates = Object.fromEntries(
    Object.entries(thresholds).map(([name, threshold]) => [name, scores[name] >= threshold]),
  );
  report = {
    schema_version: 1,
    base_url: baseUrl,
    bundle: { passed: bundle.passed === true, path: bundlePath, sha256: sha256(bundlePath) },
    lighthouse: {
      passed: Object.values(scoreGates).every(Boolean),
      path: lighthousePath,
      sha256: sha256(lighthousePath),
      scores,
      thresholds,
      gates: scoreGates,
    },
    passed: bundle.passed === true && Object.values(scoreGates).every(Boolean),
  };
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, { flag: "wx" });
} finally {
  if (chrome) await stopProcessGroup(chrome);
  if (chromeLog) await finishStream(chromeLog);
  if (chromeProfileDir) rmSync(chromeProfileDir, { recursive: true, force: true });
  await stopProcessGroup(server);
  await finishStream(serverLog);
}

if (!report?.passed) process.exitCode = 1;
console.log(JSON.stringify({ report: reportPath, ...report }, null, 2));
