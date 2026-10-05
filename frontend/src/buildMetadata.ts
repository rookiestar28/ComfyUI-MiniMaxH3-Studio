export type H3ContextBuildMetadata = {
  version: string;
  displayVersion: string;
  repositoryUrl: string;
};

declare const __H3_CONTEXT_BUILD_METADATA__: H3ContextBuildMetadata;

const versionPattern = /^\d+\.\d+\.\d+$/;
const metadata = __H3_CONTEXT_BUILD_METADATA__;
if (
  metadata === null ||
  typeof metadata !== "object" ||
  !versionPattern.test(metadata.version) ||
  metadata.displayVersion !== `v${metadata.version}` ||
  metadata.repositoryUrl !==
    "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio"
)
  throw new Error("H3 Context build metadata is invalid");

export const H3_CONTEXT_BUILD_METADATA: Readonly<H3ContextBuildMetadata> =
  Object.freeze({ ...metadata });
export const H3_CONTEXT_VERSION = H3_CONTEXT_BUILD_METADATA.version;
export const H3_CONTEXT_DISPLAY_VERSION =
  H3_CONTEXT_BUILD_METADATA.displayVersion;
