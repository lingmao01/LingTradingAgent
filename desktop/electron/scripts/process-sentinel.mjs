import { spawn } from "node:child_process";

export async function startUnrelatedPythonSentinel() {
  const executable = process.env.VIBE_TRADING_DESKTOP_TEST_PYTHON || "python.exe";
  const child = spawn(
    executable,
    ["-c", "import time; time.sleep(600)"],
    { windowsHide: true, stdio: "ignore" },
  );
  await new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", reject);
  });
  if (!child.pid) throw new Error("Unrelated Python sentinel started without a PID");
  return {
    pid: child.pid,
    isAlive: () => isProcessAlive(child.pid),
    stop: () => terminateExactProcessTree(child.pid),
  };
}

function isProcessAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function terminateExactProcessTree(pid) {
  if (!pid || !isProcessAlive(pid)) return Promise.resolve();
  if (process.platform !== "win32") {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // The sentinel already exited.
    }
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const killer = spawn("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
    killer.once("error", () => resolve());
    killer.once("exit", () => resolve());
  });
}
