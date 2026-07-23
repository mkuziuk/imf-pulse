import { lstatSync, realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, resolve } from "node:path";

const SAFE_STAGING_ID = /^run-[a-f0-9]{32}$/;

function regularDirectory(path: string, label: string): void {
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular, non-symlink directory.`);
  }
}

export function resolveBuildOutDir(
  projectRoot = process.cwd(),
  configured = process.env.IMF_PULSE_BUILD_OUT_DIR
): string {
  const root = realpathSync(projectRoot);
  if (configured === undefined) {
    const dist = resolve(root, "dist");
    try {
      regularDirectory(dist, "dist");
      const realDist = realpathSync(dist);
      if (dirname(realDist) !== root || basename(realDist) !== "dist") {
        throw new Error("dist escaped the project root.");
      }
      return realDist;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return dist;
      throw error;
    }
  }
  if (!configured || configured.includes("\\") || /[\u0000-\u001f\u007f]/.test(configured)) {
    throw new Error("IMF_PULSE_BUILD_OUT_DIR is not a safe staging path.");
  }

  const dataPath = resolve(root, "data");
  const stagingPath = resolve(dataPath, ".site-staging");
  regularDirectory(dataPath, "data");
  regularDirectory(stagingPath, "data/.site-staging");
  if (dirname(realpathSync(dataPath)) !== root || dirname(realpathSync(stagingPath)) !== realpathSync(dataPath)) {
    throw new Error("IMF_PULSE_BUILD_OUT_DIR staging ancestors escaped the project root.");
  }

  const requested = isAbsolute(configured) ? resolve(configured) : resolve(root, configured);
  const stagingId = basename(requested);
  if (
    dirname(requested) !== realpathSync(stagingPath) ||
    !SAFE_STAGING_ID.test(stagingId) ||
    (isAbsolute(configured)
      ? configured !== requested
      : configured !== `data/.site-staging/${stagingId}`)
  ) {
    throw new Error(
      "IMF_PULSE_BUILD_OUT_DIR must be an exact data/.site-staging/<safe-id> directory."
    );
  }
  regularDirectory(requested, "IMF_PULSE_BUILD_OUT_DIR");
  const realRequested = realpathSync(requested);
  if (dirname(realRequested) !== realpathSync(stagingPath) || basename(realRequested) !== stagingId) {
    throw new Error("IMF_PULSE_BUILD_OUT_DIR escaped data/.site-staging.");
  }
  return realRequested;
}
