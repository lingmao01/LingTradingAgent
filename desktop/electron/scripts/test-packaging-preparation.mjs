import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { acquireVerifiedArchive } from "./prepare-electron.mjs";

const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "vibe-electron-preparation-"));
try {
  const source = path.join(temporaryRoot, "source.zip");
  const target = path.join(temporaryRoot, "target.zip");
  const payload = Buffer.from("verified Electron fixture", "utf8");
  const expected = createHash("sha256").update(payload).digest("hex");
  await writeFile(source, payload);

  let attempts = 0;
  await acquireVerifiedArchive({
    target,
    expected,
    attempts: 3,
    sleep: async () => {},
    download: async ({ signal, attempt }) => {
      attempts += 1;
      assert.equal(attempt, attempts);
      assert.equal(signal.aborted, false);
      if (attempts < 3) throw new TypeError("fetch failed");
      return source;
    },
  });
  assert.equal(attempts, 3);
  assert.deepEqual(await readFile(target), payload);

  await acquireVerifiedArchive({
    target,
    expected,
    download: async () => {
      throw new Error("a verified cache hit must not download");
    },
  });

  const corrupt = path.join(temporaryRoot, "corrupt.zip");
  await writeFile(corrupt, "corrupt", "utf8");
  await assert.rejects(
    acquireVerifiedArchive({
      target: path.join(temporaryRoot, "rejected.zip"),
      expected,
      attempts: 2,
      sleep: async () => {},
      download: async () => corrupt,
    }),
    /failed after 2 bounded attempts: checksum mismatch/u,
  );

  console.log("Electron packaging preparation retries and checksum gates: OK");
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
