import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync
} from "node:fs";
import { basename, dirname, resolve, sep } from "node:path";

const ACCEPTED_POINTER_STATUSES = new Set([
  "published",
  "processed_no_pulse",
  "unchanged"
]);
const SHA256_PATTERN = /^[a-f0-9]{64}$/;

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function directoryIdentity(stat) {
  return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs];
}

function sameIdentity(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function readStableFile(path, expectedIdentity) {
  const descriptor = openSync(
    path,
    constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0)
  );
  try {
    const before = fstatSync(descriptor, { bigint: true });
    if (!before.isFile()) throw new Error(`Expected a regular file: ${path}`);
    if (
      expectedIdentity &&
      (before.dev !== expectedIdentity.dev || before.ino !== expectedIdentity.ino)
    ) {
      throw new Error(`File changed while it was being opened: ${path}`);
    }
    const bytes = readFileSync(descriptor);
    const after = fstatSync(descriptor, { bigint: true });
    if (
      before.dev !== after.dev ||
      before.ino !== after.ino ||
      before.size !== after.size ||
      before.mtimeNs !== after.mtimeNs ||
      before.ctimeNs !== after.ctimeNs ||
      BigInt(bytes.byteLength) !== after.size
    ) {
      throw new Error(`File changed while it was being read: ${path}`);
    }
    return bytes;
  } finally {
    closeSync(descriptor);
  }
}

function compareUnicodeCodePoints(left, right) {
  const leftPoints = [...left].map((character) => character.codePointAt(0));
  const rightPoints = [...right].map((character) => character.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] - rightPoints[index];
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Canonical JSON cannot contain non-finite numbers.");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort(compareUnicodeCodePoints)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  throw new Error("Canonical JSON contains an unsupported value.");
}

export function canonicalJsonHash(value) {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function assertRegularDirectory(path, label) {
  const stat = lstatSync(path, { bigint: true });
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular, non-symlink directory.`);
  }
  return stat;
}

function assertDirectChild(path, parent, expectedName, label) {
  const real = realpathSync(path);
  if (dirname(real) !== parent || basename(real) !== expectedName) {
    throw new Error(`${label} escaped its project-owned parent.`);
  }
  return real;
}

function assertSafeSiteSegment(name) {
  if (
    !name ||
    name === "." ||
    name === ".." ||
    name.includes("/") ||
    name.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(name)
  ) {
    throw new Error(`Site build contains an unsafe path segment: ${JSON.stringify(name)}`);
  }
}

function walkSiteTree(root, directory, prefix, files) {
  const directoryStat = assertRegularDirectory(directory, `site build directory ${prefix || "."}`);
  const directoryDescriptor = openSync(
    directory,
    constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0)
  );
  try {
    const opened = fstatSync(directoryDescriptor, { bigint: true });
    if (
      !opened.isDirectory() ||
      opened.dev !== directoryStat.dev ||
      opened.ino !== directoryStat.ino
    ) {
      throw new Error(`Site build directory changed while opening: ${prefix || "."}`);
    }
    const realDirectory = realpathSync(directory);
    if (realDirectory !== root && !realDirectory.startsWith(`${root}${sep}`)) {
      throw new Error(`Site build directory escaped its root: ${prefix || "."}`);
    }

    const names = readdirSync(directory).sort(compareUnicodeCodePoints);
    for (const name of names) {
      assertSafeSiteSegment(name);
      const relative = prefix ? `${prefix}/${name}` : name;
      const path = resolve(directory, name);
      const stat = lstatSync(path, { bigint: true });
      if (stat.isSymbolicLink()) {
        throw new Error(`Site build contains a forbidden symlink: ${relative}`);
      }
      if (stat.isDirectory()) {
        walkSiteTree(root, path, relative, files);
        continue;
      }
      if (!stat.isFile()) {
        throw new Error(`Site build contains a forbidden node: ${relative}`);
      }
      const bytes = readStableFile(path, stat);
      files[relative] = createHash("sha256").update(bytes).digest("hex");
    }

    const afterDescriptor = fstatSync(directoryDescriptor, { bigint: true });
    const afterPath = lstatSync(directory, { bigint: true });
    if (
      !sameIdentity(directoryIdentity(directoryStat), directoryIdentity(afterDescriptor)) ||
      !sameIdentity(directoryIdentity(directoryStat), directoryIdentity(afterPath))
    ) {
      throw new Error(`Site build directory changed while reading: ${prefix || "."}`);
    }
  } finally {
    closeSync(directoryDescriptor);
  }
}

export function hashSiteTree(siteRoot) {
  const root = realpathSync(siteRoot);
  assertRegularDirectory(siteRoot, "site build root");
  const files = Object.create(null);
  walkSiteTree(root, root, "", files);
  if (!Object.prototype.hasOwnProperty.call(files, "index.html")) {
    throw new Error("Site build must contain a root index.html file.");
  }
  return { sha256: canonicalJsonHash(files), files };
}

function readCurrentPointer(currentPath) {
  const currentStat = lstatSync(currentPath, { bigint: true });
  if (!currentStat.isFile() || currentStat.isSymbolicLink()) {
    throw new Error("data/current.json must be a regular, non-symlink file.");
  }
  let parsed;
  try {
    parsed = JSON.parse(readStableFile(currentPath, currentStat).toString("utf8"));
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error("data/current.json is not valid JSON.");
    }
    throw error;
  }
  if (!isRecord(parsed)) throw new Error("data/current.json must contain a JSON object.");
  return parsed;
}

function selectPreviewFallback(projectRoot) {
  const distPath = resolve(projectRoot, "dist");
  assertRegularDirectory(distPath, "dist");
  const dist = assertDirectChild(distPath, projectRoot, "dist", "dist");
  const indexPath = resolve(dist, "index.html");
  const indexStat = lstatSync(indexPath, { bigint: true });
  if (!indexStat.isFile() || indexStat.isSymbolicLink()) {
    throw new Error("dist must contain a regular, non-symlink index.html file.");
  }
  return { mode: "preview", path: dist };
}

export function selectPreviewSiteBuild(projectRoot = process.cwd()) {
  const root = realpathSync(projectRoot);
  const currentPath = resolve(root, "data", "current.json");
  try {
    lstatSync(currentPath);
  } catch (error) {
    if (error?.code === "ENOENT") return selectPreviewFallback(root);
    throw error;
  }

  const pointer = readCurrentPointer(currentPath);
  if (!ACCEPTED_POINTER_STATUSES.has(String(pointer.status ?? ""))) {
    throw new Error("data/current.json does not have an accepted checkpoint status.");
  }
  const sha256 = pointer.site_build_sha256;
  if (typeof sha256 !== "string" || !SHA256_PATTERN.test(sha256)) {
    throw new Error("data/current.json has an invalid site_build_sha256.");
  }
  const expectedRelative = `data/site-builds/site-${sha256}`;
  if (pointer.site_build_path !== expectedRelative) {
    throw new Error("data/current.json has an inconsistent site_build_path.");
  }

  const dataPath = resolve(root, "data");
  const buildsPath = resolve(dataPath, "site-builds");
  assertRegularDirectory(dataPath, "data");
  const data = assertDirectChild(dataPath, root, "data", "data");
  assertRegularDirectory(buildsPath, "data/site-builds");
  const builds = assertDirectChild(
    buildsPath,
    data,
    "site-builds",
    "data/site-builds"
  );
  const siteName = `site-${sha256}`;
  const selectedPath = resolve(builds, siteName);
  assertRegularDirectory(selectedPath, "selected site build");
  const selected = assertDirectChild(selectedPath, builds, siteName, "selected site build");
  const tree = hashSiteTree(selected);
  if (tree.sha256 !== sha256) {
    throw new Error("Selected site build does not match the pointer SHA-256.");
  }
  return { mode: "published", path: selected, sha256 };
}
