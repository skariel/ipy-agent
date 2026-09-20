#!/usr/bin/env node
/** Trusted srt launcher with a host-mediated network permission callback. */

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";

const MAX_MESSAGE_BYTES = 65536;

function fail(message) {
  process.stderr.write(`py-agent srt launcher: ${message}\n`);
  process.exit(1);
}

function shellQuote(arguments_) {
  return arguments_
    .map((argument) => `'${argument.replaceAll("'", `'\\''`)}'`)
    .join(" ");
}

if (process.argv.length < 7 || process.argv[5] !== "--") {
  fail("usage: launcher SRT_CLI SETTINGS APPROVAL_FD -- COMMAND [ARG ...]");
}

const srtCli = path.resolve(process.argv[2]);
const settingsPath = path.resolve(process.argv[3]);
const approvalFd = Number(process.argv[4]);
const commandArguments = process.argv.slice(6);
if (!Number.isSafeInteger(approvalFd) || approvalFd < 3 || commandArguments.length === 0) {
  fail("invalid approval descriptor or missing command");
}

const distDirectory = path.dirname(srtCli);
const indexUrl = pathToFileURL(path.join(distDirectory, "index.js")).href;
const parentProxyUrl = pathToFileURL(path.join(distDirectory, "sandbox", "parent-proxy.js")).href;

let approvalSocket;
try {
  approvalSocket = new net.Socket({ fd: approvalFd, readable: true, writable: true });
} catch (error) {
  fail(`cannot open approval descriptor: ${error instanceof Error ? error.message : String(error)}`);
}
approvalSocket.setEncoding("utf8");

let nextRequest = 0;
let input = "";
const pending = new Map();

function denyPending() {
  for (const resolve of pending.values()) resolve(false);
  pending.clear();
}

approvalSocket.on("data", (chunk) => {
  input += chunk;
  if (Buffer.byteLength(input, "utf8") > MAX_MESSAGE_BYTES) {
    input = "";
    denyPending();
    approvalSocket.destroy();
    return;
  }
  for (;;) {
    const newline = input.indexOf("\n");
    if (newline < 0) break;
    const line = input.slice(0, newline);
    input = input.slice(newline + 1);
    if (!line) continue;
    try {
      const response = JSON.parse(line);
      if (
        response === null ||
        typeof response !== "object" ||
        Array.isArray(response) ||
        Object.keys(response).sort().join(",") !== "allow,id,version" ||
        response.version !== 1 ||
        typeof response.id !== "string" ||
        typeof response.allow !== "boolean"
      ) {
        throw new Error("invalid response");
      }
      const resolve = pending.get(response.id);
      if (resolve === undefined) throw new Error("unknown response ID");
      pending.delete(response.id);
      resolve(response.allow);
    } catch {
      denyPending();
      approvalSocket.destroy();
      return;
    }
  }
});
approvalSocket.on("end", denyPending);
approvalSocket.on("close", denyPending);
approvalSocket.on("error", denyPending);

function requestApproval(host, port) {
  if (approvalSocket.destroyed || pending.size >= 64) return Promise.resolve(false);
  const id = `net-${++nextRequest}`;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    const request = `${JSON.stringify({ version: 1, id, kind: "network", host, port: port ?? null })}\n`;
    approvalSocket.write(request, "utf8", (error) => {
      if (error && pending.delete(id)) resolve(false);
    });
  });
}

let child;
let cleaned = false;
async function cleanup(manager) {
  if (cleaned) return;
  cleaned = true;
  denyPending();
  approvalSocket.destroy();
  manager.cleanupAfterCommand();
}

try {
  const [{ SandboxManager, SandboxRuntimeConfigSchema }, { canonicalizeHost }] = await Promise.all([
    import(indexUrl),
    import(parentProxyUrl),
  ]);
  const raw = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  const runtimeConfig = SandboxRuntimeConfigSchema.parse(raw);
  await SandboxManager.initialize(runtimeConfig, async ({ host, port }) => {
    let canonical;
    try {
      canonical = canonicalizeHost(host);
    } catch {
      return false;
    }
    return requestApproval(canonical, port);
  });
  const command = shellQuote(commandArguments);
  const wrapped = await SandboxManager.wrapWithSandboxArgv(command);
  child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
    shell: false,
    stdio: "inherit",
    env: wrapped.env,
  });
  child.on("error", async (error) => {
    process.stderr.write(`Failed to execute sandboxed command: ${error.message}\n`);
    await cleanup(SandboxManager);
    process.exit(1);
  });
  child.on("exit", async (code, signal) => {
    await cleanup(SandboxManager);
    if (signal === "SIGINT" || signal === "SIGTERM") process.exit(0);
    if (signal) {
      process.stderr.write(`Sandboxed command killed by signal: ${signal}\n`);
      process.exit(1);
    }
    process.exit(code ?? 0);
  });
  process.on("SIGINT", () => child?.kill("SIGINT"));
  process.on("SIGTERM", () => child?.kill("SIGTERM"));
} catch (error) {
  denyPending();
  approvalSocket.destroy();
  fail(error instanceof Error ? error.message : String(error));
}
