import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const VERSION_PATTERN = /^\d+\.\d+\.\d+$/;
export const H3_CONTEXT_REPOSITORY_URL =
  "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio";

export type H3ContextBuildMetadata = {
  version: string;
  displayVersion: string;
  repositoryUrl: string;
};

function defaultRootDirectory(): string {
  const current = process.cwd();
  return existsSync(join(current, "pyproject.toml"))
    ? current
    : dirname(current);
}

function readText(path: string, label: string): string {
  try {
    return readFileSync(path, "utf8");
  } catch (error) {
    throw new Error(`cannot read ${label} metadata`, { cause: error });
  }
}

function validatedVersion(value: unknown, label: string): string {
  if (typeof value !== "string" || !VERSION_PATTERN.test(value))
    throw new Error(`${label} version is missing or invalid`);
  return value;
}

function projectVersion(pyproject: string): string {
  const start = pyproject.indexOf("[project]");
  if (start < 0) throw new Error("project version section is missing");
  const nextSection = pyproject.indexOf("\n[", start + "[project]".length);
  const section = pyproject.slice(
    start,
    nextSection < 0 ? pyproject.length : nextSection,
  );
  const matches = [
    ...section.matchAll(/^version\s*=\s*[\"']([^\"']+)[\"']\s*$/gm),
  ];
  if (matches.length !== 1)
    throw new Error("project version is missing or ambiguous");
  return validatedVersion(matches[0]?.[1], "pyproject");
}

function pythonPackageVersion(source: string): string {
  const matches = [
    ...source.matchAll(/^__version__\s*=\s*[\"']([^\"']+)[\"']\s*$/gm),
  ];
  if (matches.length !== 1)
    throw new Error("python package version is missing or ambiguous");
  return validatedVersion(matches[0]?.[1], "python package");
}

function frontendPackageVersion(source: string): string {
  let value: unknown;
  try {
    value = (JSON.parse(source) as { version?: unknown }).version;
  } catch (error) {
    throw new Error("frontend package metadata is invalid JSON", {
      cause: error,
    });
  }
  return validatedVersion(value, "frontend package");
}

function installationProfileVersion(source: string): string {
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch (error) {
    throw new Error("installation profile metadata is invalid JSON", {
      cause: error,
    });
  }
  if (value === null || typeof value !== "object")
    throw new Error("installation profile metadata is not an object");
  const profiles = (value as { profiles?: unknown }).profiles;
  if (!Array.isArray(profiles))
    throw new Error("installation profile metadata has no profiles");
  const matches = profiles.filter(
    (profile): profile is { profile_id: string; distribution: string } =>
      profile !== null &&
      typeof profile === "object" &&
      (profile as { profile_id?: unknown }).profile_id === "core_manual" &&
      typeof (profile as { distribution?: unknown }).distribution === "string",
  );
  if (matches.length !== 1)
    throw new Error("core installation profile is missing or ambiguous");
  const match = /^minimax-h3-studio==(.+)$/.exec(
    matches[0]?.distribution ?? "",
  );
  if (match === null)
    throw new Error("core installation profile distribution is invalid");
  return validatedVersion(match[1], "core installation profile");
}

export function resolveH3ContextBuildMetadata(
  rootDirectory = defaultRootDirectory(),
): H3ContextBuildMetadata {
  const frontendPackage = `${rootDirectory}/frontend/package.json`;
  const pyproject = `${rootDirectory}/pyproject.toml`;
  const pythonPackage = `${rootDirectory}/comfyui_h3_context/__init__.py`;
  const installationProfile = `${rootDirectory}/comfyui_h3_context/contracts/installation_profiles_v1.json`;
  const versions = [
    projectVersion(readText(pyproject, "pyproject")),
    frontendPackageVersion(readText(frontendPackage, "frontend package")),
    pythonPackageVersion(readText(pythonPackage, "python package")),
    installationProfileVersion(
      readText(installationProfile, "installation profile"),
    ),
  ];
  if (new Set(versions).size !== 1)
    throw new Error("version authorities disagree");
  const version = versions[0];
  if (version === undefined) throw new Error("version authorities are empty");
  return {
    version,
    displayVersion: `v${version}`,
    repositoryUrl: H3_CONTEXT_REPOSITORY_URL,
  };
}
