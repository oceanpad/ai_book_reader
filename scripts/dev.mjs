import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const cwd = fileURLToPath(new URL("../", import.meta.url));
const apiArgs = ["run", "--locked", "uvicorn", "backend.api:app", "--host", "127.0.0.1", "--port", "8000"];
if (existsSync(new URL("../.env", import.meta.url))) apiArgs.push("--env-file", ".env");

const children = [
  spawn("uv", apiArgs, { cwd, stdio: "inherit", env: { ...process.env, READER_MODE: "local" } }),
  spawn(process.execPath, ["node_modules/vite/bin/vite.js", "frontend", "--config", "frontend/vite.config.ts"], { cwd, stdio: "inherit" }),
];
let stopping = false;
function stop(code) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  for (const child of children) if (child.exitCode === null) child.kill("SIGTERM");
}
for (const child of children) {
  child.on("error", (error) => { console.error(error.message); stop(1); });
  child.on("exit", (code) => { if (!stopping) stop(code || 1); });
}
process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));
