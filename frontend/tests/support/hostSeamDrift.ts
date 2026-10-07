import { createHash } from "node:crypto";

import { parseHostSeamContract } from "../../src/host/hostSeamContract";
import {
  validateContentFreeEvidence,
  type BrowserRequestAudit,
} from "./hc09CapturePolicy";
import type {
  HostSeamFixtureWire,
  HostSeamObservationWire,
} from "./hostSeamObservation";

const BASELINE_KEYS = ["schema", "fixture_version", "source", "artifacts"];
const SOURCE_KEYS = ["commit", "tree"];
const ARTIFACT_KEYS = ["census_sha256", "fixture_sha256"];
const BASELINE_SOURCE_KEYS = ["commit_parts", "tree_parts"];
const BASELINE_ARTIFACT_KEYS = ["census_sha256_parts", "fixture_sha256_parts"];
const TRANSITION_KEYS = ["schema", "item", "source", "artifacts", "changes"];
const TRANSITION_ARTIFACT_KEYS = [
  "from_census_sha256_parts",
  "to_census_sha256_parts",
  "fixture_sha256_parts",
  "seam_contract_sha256_parts",
];
const TRANSITION_CHANGE_KEYS = ["seam_id", "added_source_paths"];
const TRANSITION_REMOVAL_CHANGE_KEYS = [
  "seam_id",
  "removed_source_paths",
  "from_owner",
  "to_owner",
];
const OBSERVATION_KEYS = [
  "seam_id",
  "presence",
  "kind",
  "key_shape",
  "element_kind",
  "readiness_state",
  "count_bucket",
  "byte_bucket",
  "latency_bucket",
] as const;
const EVIDENCE_KEYS = [
  "schema",
  "item",
  "result",
  "reason",
  "source",
  "probe",
  "artifacts",
  "subject",
  "summary",
  "seams",
  "request_policy",
  "privacy",
];
const SUBJECT_KEYS = ["comfyui_version", "frontend_version"];
const SUMMARY_KEYS = ["passed", "drifted", "unavailable"];
const SEAM_RESULT_KEYS = ["seam_id", "status"];
const REQUEST_POLICY_KEYS = [
  "api_get_requests",
  "redirects",
  "browser_cookie_records_before",
  "browser_cookie_records_after",
  "model_or_queue_operations",
  "queue_before_running",
  "queue_before_pending",
  "queue_after_running",
  "queue_after_pending",
  "blocked_cross_origin_attempts",
  "blocked_non_get_attempts",
  "blocked_credential_attempts",
  "blocked_prompt_attempts",
  "blocked_workflow_attempts",
  "blocked_media_attempts",
];
const PRIVACY_KEYS = [
  "prompts",
  "workflows",
  "media",
  "credentials",
  "cookies",
  "paths_or_urls",
  "arbitrary_host_values",
];
const HEX_40 = /^[0-9a-f]{40}$/;
const HEX_64 = /^[0-9a-f]{64}$/;
const SEAM_ID = /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$/;
const VERSION = /^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$/;
// IMPORTANT: these public fragments are the accepted HC-09 checkpoint, not
// secret material. Keep them independent from the mutable baseline document.
const ACCEPTED_HC09_SOURCE = Object.freeze({
  commit: ["f6d30e0e", "3e2df72f", "15dec446", "94bf7347", "d1a9b706"].join(""),
  tree: ["a621c778", "9f6e2c42", "e28a08af", "42cd4f3c", "85d03285"].join(""),
});
// IMPORTANT: this is the accepted M23-14 checkpoint that still contains the
// frozen HC-09 census bytes. A later compatibility item must add a new closed
// transition; never make arbitrary current census bytes self-authorizing.
const ACCEPTED_M23_14_SOURCE = Object.freeze({
  commit: ["d65771cf", "baa1640c", "cd5e29ec", "9e82be44", "73814919"].join(""),
  tree: ["73c0a8a4", "282ec306", "23f3e5ec", "99c6bdb1", "71ba729b"].join(""),
});
const ACCEPTED_M23_17_SOURCE = Object.freeze({
  commit: ["150d30a9", "3df1504b", "2b2344b1", "51843bbb", "1ec44416"].join(""),
  tree: ["ae5fe11e", "f0eb89df", "9edaf315", "537a370a", "d18b983a"].join(""),
});
const ACCEPTED_M23_18_SOURCE = Object.freeze({
  commit: ["38510d53", "a89a33df", "0387e98c", "f8e88931", "a9ada556"].join(""),
  tree: ["af4e6ca2", "418d304f", "ed638abc", "7a5f7792", "47c05cff"].join(""),
});
const ACCEPTED_M23_21_SOURCE = Object.freeze({
  commit: ["5c2b93c2", "710efd19", "b11addf8", "80477ede", "f0c44fc8"].join(""),
  tree: ["0b5590e5", "49564246", "c7b5ee3d", "ca6abb50", "51f3f739"].join(""),
});
const ACCEPTED_M25_01_SOURCE = Object.freeze({
  commit: ["ae0c3cdc", "176b352c", "e7b673b4", "32240d8a", "f934dbfb"].join(""),
  tree: ["b36921bf", "3d473fcf", "b81ee5e2", "c239ac82", "e535188f"].join(""),
});
// IMPORTANT: M23-24 starts from this exact local-green M23-26 base. Keep the
// transition closed; accepting arbitrary current census bytes defeats HC-10 drift detection.
const ACCEPTED_M23_24_BASE_SOURCE = Object.freeze({
  commit: ["99a4b48a", "ac7e4a85", "563d923a", "6dcd21bf", "922c5f8b"].join(""),
  tree: ["bac95bad", "93f129c2", "9ab9a97d", "3aade279", "6990c321"].join(""),
});
const ACCEPTED_M23_47_BASE_SOURCE = Object.freeze({
  commit: ["eba83217", "eda24177", "10d254bc", "a9081dbe", "fb5054fd"].join(""),
  tree: ["6a7d824c", "ebecc902", "41544e1b", "b349d8a0", "7df506b6"].join(""),
});
// IMPORTANT: M23-28 starts from the accepted M23-27 tree. Its source-path relocation is
// authorized only by this immutable checkpoint; never derive this identity from the candidate.
const ACCEPTED_M23_28_BASE_SOURCE = Object.freeze({
  commit: ["cfa38b40", "2d9b9e31", "dedd30c2", "9659c32a", "ffbc9edc"].join(""),
  tree: ["aea2939c", "fb92d211", "d46cd455", "f9482acb", "0ffe37ca"].join(""),
});
// IMPORTANT: the M26-04 census addition starts from the committed replacement candidate. Keep
// this fixed identity independent from later repair commits or current HEAD.
const ACCEPTED_M26_04_BASE_SOURCE = Object.freeze({
  commit: ["19d7e065", "f18e705f", "edfc3155", "1f5d115f", "170d3e06"].join(""),
  tree: ["854c67d9", "c72033f5", "3883ea83", "1a3132e4", "727d0929"].join(""),
});
// IMPORTANT: M26-03 starts from the accepted merge checkpoint, before its three census use-site
// additions. Keep this identity independent from the mutable candidate and current census bytes.
const ACCEPTED_M26_03_BASE_SOURCE = Object.freeze({
  commit: ["4ccea337", "3f285b5c", "d8af1b30", "24203c2f", "3382a580"].join(""),
  tree: ["601427af", "221c5d03", "669df599", "f3c06186", "8b417006"].join(""),
});
// IMPORTANT: M25-13 starts from this exact accepted render-source checkpoint. Keep this identity and the
// authorized lease consumer independent from the mutable candidate and current census bytes.
const ACCEPTED_M25_13_BASE_SOURCE = Object.freeze({
  commit: ["2e0cba0c", "4135359a", "4a630163", "f17d4a48", "77843d67"].join(""),
  tree: ["88aa057c", "e2e5da21", "524116a2", "f5150daf", "2ca95268"].join(""),
});
// IMPORTANT: this exact accepted base and two-path transition must remain independent of
// current census bytes; deriving either at runtime would self-authorize unrelated host seams.
const MANAGED_QUALIFICATION_BASE_SOURCE = Object.freeze({
  commit: ["958ba3d1", "dee6bca7", "4bfe2a0a", "d2e054e9", "4bd6b592"].join(""),
  tree: ["e5fdf4b6", "dbb54ef1", "351e5490", "8f164180", "0d908a9e"].join(""),
});
const MANAGED_QUALIFICATION_CENSUS_SHA256 = [
  "3b0b1070",
  "9e790c13",
  "82900d6d",
  "03eafb40",
  "c61199ee",
  "b201d305",
  "47132a5e",
  "6bd4a07c",
].join("");
const MANAGED_QUALIFICATION_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "backend.node_class_mappings",
    added_source_paths: [
      "comfyui_h3_context/adapters/managed_asset_resolution.py",
    ],
  },
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: ["frontend/src/host/managedQualificationActions.ts"],
  },
]);
// IMPORTANT: this exact accepted base and three-path transition must remain independent of
// current census bytes; deriving either at runtime would self-authorize unrelated host seams.
const M25_16_BASE_SOURCE = Object.freeze({
  commit: ["25ad26c5", "3bd25780", "999ea9f0", "4f490060", "97448f29"].join(""),
  tree: ["c3f7d3c5", "bafa79b7", "9821d377", "50b77d16", "87538e47"].join(""),
});
const ACCEPTED_M25_16_CENSUS_SHA256 = [
  "1b66a063",
  "995124e6",
  "2e8d830a",
  "8a607fdc",
  "38daa266",
  "1669fdbe",
  "9c1d01d6",
  "7d1f9dcf",
].join("");
const M25_16_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: [
      "frontend/src/host/authoringOutputCapabilityClient.ts",
      "frontend/src/host/productionAuthoringImportClient.ts",
      "frontend/src/lifecycle/nleWorkspaceSession.ts",
    ],
  },
]);
// IMPORTANT: this exact accepted base and one-path transition must remain independent of current
// census bytes; M25-22 added the open-workflow read to the NLE session without its census row.
const M16_05_BASE_SOURCE = Object.freeze({
  commit: ["2c0e3300", "57dc35af", "014cd404", "0797b840", "433c05ba"].join(""),
  tree: ["4349423e", "60c77483", "8c849c43", "608b815c", "801191c3"].join(""),
});
const ACCEPTED_M16_05_CENSUS_SHA256 = [
  "caacf67e",
  "6920c672",
  "2ad436c9",
  "01b9f402",
  "f7b0d45a",
  "68f495de",
  "46374541",
  "1059fa64",
].join("");
const M16_05_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.app.extension_manager.workflow.open_workflows",
    added_source_paths: ["frontend/src/lifecycle/nleWorkspaceSession.ts"],
  },
]);
// IMPORTANT: this exact accepted base and one-path transition must remain independent of current
// census bytes; M25-33's media runtime client reads the host `fetchApi`.
const M25_33_BASE_SOURCE = Object.freeze({
  commit: ["39425bb4", "2c63dd3d", "db475846", "8f9f362d", "3e3489f5"].join(""),
  tree: ["1dc12b61", "2c63c570", "49499f9c", "40e9c997", "fd6a7d24"].join(""),
});
const ACCEPTED_M25_33_CENSUS_SHA256 = [
  "0ef890f6",
  "a6c3f2d3",
  "9ecfb010",
  "4d6f6ef5",
  "a218128c",
  "b3e7b0c3",
  "b5bd3a05",
  "e25e3a3e",
].join("");
const M25_33_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: ["frontend/src/host/mediaRuntimeClient.ts"],
  },
]);
// IMPORTANT: this exact accepted release base and five-path transition must remain independent of
// current census bytes; deriving either at runtime would self-authorize missed host-store reads.
const M16_05_RELEASE_BASE_SOURCE = Object.freeze({
  commit: ["1814597b", "355e2e17", "780d61d7", "2daf0e08", "0e907490"].join(""),
  tree: ["ff867dc8", "2ef78c90", "700a4431", "aa3023b6", "47997e99"].join(""),
});
const ACCEPTED_M16_05_RELEASE_CENSUS_SHA256 = [
  "efd2dc9f",
  "293d0973",
  "7211948f",
  "4475ef76",
  "f8b091e1",
  "979d373b",
  "c28ce33e",
  "70c073a6",
].join("");
const M16_05_RELEASE_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.app.extension_manager.workflow.active_workflow",
    added_source_paths: [
      "frontend/src/host/appMode.ts",
      "frontend/src/host/productionSession.ts",
      "frontend/src/lifecycle/appModeSession.ts",
      "frontend/src/lifecycle/managedProjectMember.ts",
      "frontend/src/lifecycle/presentationBinding.tsx",
    ],
  },
]);
// IMPORTANT: this exact accepted base and one-path removal must remain independent of current
// census bytes; M25-38's accumulated-project envelope stopped reading `app.graph` directly from
// `appModeSession.ts`, so the seam's exact final owner (`hostSeams.ts`, set by M23-28 and never
// moved since) has to be re-asserted here or a later, unrelated re-add of that path would be
// rejected by this historical removal (see the "final ordered state" guard below).
const M25_38_BASE_SOURCE = Object.freeze({
  commit: ["d051d1b0", "28d553ed", "a32a99cb", "58195eeb", "42f6dc38"].join(""),
  tree: ["1e4a08fd", "22f8a453", "6cd892a1", "2b041695", "47d7b645"].join(""),
});
const ACCEPTED_M25_38_CENSUS_SHA256 = [
  "7dedcf4e",
  "52d930ef",
  "2076fee4",
  "b3569ab2",
  "ab7102d3",
  "a59ad473",
  "f4b9e667",
  "2c55b1c2",
].join("");
const M25_38_SOURCE_PATH_REMOVALS = Object.freeze([
  {
    seam_id: "frontend.app.graph",
    removed_source_paths: Object.freeze([
      "frontend/src/lifecycle/appModeSession.ts",
    ]),
    from_owner: "frontend/src/lifecycle/appModeSession.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  },
]);
const ACCEPTED_M23_15_CENSUS_SHA256 = [
  "189911ab",
  "0df36a7c",
  "d1aad71c",
  "241c4594",
  "9359af6b",
  "a5b7bb6c",
  "1f320403",
  "ba165e09",
].join("");
const ACCEPTED_M23_18_CENSUS_SHA256 = [
  "c6fee1aa",
  "eed908a4",
  "ca14c5c6",
  "a214d3e9",
  "f71ba7ac",
  "a7b32d46",
  "f59e67ed",
  "881436fa",
].join("");
const ACCEPTED_M23_19_CENSUS_SHA256 = [
  "b14387c9",
  "1938eae5",
  "5cce67b0",
  "b32f0af3",
  "12968364",
  "590ce06c",
  "64aebc98",
  "2a453562",
].join("");
const ACCEPTED_M23_25_CENSUS_SHA256 = [
  "855ec4a2",
  "dce238cf",
  "c642dcd3",
  "fcc66394",
  "cdb1f93c",
  "64d4e9a1",
  "c4bb6886",
  "774db6e6",
].join("");
const ACCEPTED_M25_02_CENSUS_SHA256 = [
  "0cbc2c9e",
  "48cdb608",
  "da01f1cc",
  "bf5a2081",
  "99513d03",
  "99c52848",
  "2d477169",
  "6c9b5482",
].join("");
const ACCEPTED_M23_24_CENSUS_SHA256 = [
  "f488f7ec",
  "e0d47e50",
  "5b85cd21",
  "3deaa28a",
  "e337e2bd",
  "1f168778",
  "d50e13b8",
  "038368bf",
].join("");
const ACCEPTED_M23_47_CENSUS_SHA256 = [
  "5a2ecc45",
  "257c98d9",
  "2c2964fc",
  "edc080b6",
  "6c8689c1",
  "750cb04d",
  "c3b064fb",
  "31befe16",
].join("");
const ACCEPTED_M23_28_CENSUS_SHA256 = [
  "9a265cbc",
  "5837c5d3",
  "38cc9089",
  "7f7f21a5",
  "63887592",
  "f7744f31",
  "3cfe05d8",
  "0e60e75a",
].join("");
const ACCEPTED_M26_04_CENSUS_SHA256 = [
  "d5b57e75",
  "6ed6ff1a",
  "cf261a75",
  "af4104e5",
  "73aa15ef",
  "f1b70a28",
  "7e90edcf",
  "c4f84e1b",
].join("");
const ACCEPTED_M26_03_CENSUS_SHA256 = [
  "afad11c6",
  "e0889484",
  "419723eb",
  "b809ad8c",
  "467afa55",
  "bc9246b1",
  "79c9c215",
  "392f3e6f",
].join("");
const ACCEPTED_M25_13_CENSUS_SHA256 = [
  "3a7d07c7",
  "0b6d3707",
  "00fbd717",
  "5c6cdef8",
  "ed29a06c",
  "4b6b5e6d",
  "aa4691db",
  "20e7a42a",
].join("");
const ACCEPTED_M23_19_FIXTURE_SHA256 = [
  "6c5b39b2",
  "d8681074",
  "e5742cef",
  "b81a574e",
  "5087834f",
  "df89fdb5",
  "aea23c70",
  "2cf2a023",
].join("");
const ACCEPTED_M23_18_SEAM_CONTRACT_SHA256 = [
  "64a5512d",
  "ee9bcc57",
  "4630c770",
  "753e8816",
  "05bc6f1f",
  "06fda8bb",
  "42c5d361",
  "fad65895",
].join("");
const ACCEPTED_M23_19_SEAM_CONTRACT_SHA256 = [
  "54a5b364",
  "eb46aa92",
  "57cf477b",
  "6e3bf487",
  "9bf4ab18",
  "2624f5e7",
  "6ed865b4",
  "db2a65de",
].join("");
const ACCEPTED_M23_25_SEAM_CONTRACT_SHA256 = [
  "5fd4a1cf",
  "57b7144d",
  "125a1528",
  "65aa8664",
  "50afbdf6",
  "0a08cb55",
  "02174049",
  "45f8ebec",
].join("");
// M23-47 moved the `backend.prompt_server.routes` seam from eleven hand-written HTTP edges to one,
// so both the seam's owner and its source-path list change and the contract projection moves with
// them. The projection deliberately excludes `source_paths`, so this digest moves only because the
// owner did -- which is the part of the change a host-facing consumer can observe.
const ACCEPTED_M23_47_SEAM_CONTRACT_SHA256 = [
  "17fac8c0",
  "41c75069",
  "1df96010",
  "6c26bc44",
  "e0093d3f",
  "6a768ab7",
  "fcff7639",
  "4b42e01b",
].join("");
const ACCEPTED_M23_28_SEAM_CONTRACT_SHA256 = [
  "c6522421",
  "bebc84e1",
  "b68d716f",
  "b600edfb",
  "fe035ed8",
  "0068c58b",
  "8c0ab3c2",
  "6e9e07f9",
].join("");
const M23_15_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "backend.prompt_server.routes",
    added_source_paths: Object.freeze([
      "comfyui_h3_context/adapters/comfyui_sequence_coordinator.py",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/sequenceCoordinator.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.api.queue_prompt",
    added_source_paths: Object.freeze(["frontend/src/entry.tsx"]),
  }),
]);
const M23_18_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "backend.prompt_server.routes",
    added_source_paths: Object.freeze([
      "comfyui_h3_context/adapters/comfyui_input_geometry.py",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze(["frontend/src/host/inputGeometry.ts"]),
  }),
]);
const M23_19_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "frontend.app.extension_manager.workflow.active_workflow",
    added_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.workflow.open_workflows",
    added_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.change",
    added_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.get_node_by_id",
    added_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.set_dirty_canvas",
    added_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.load_graph_data",
    added_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
  }),
]);
const M23_25_SOURCE_PATH_REMOVALS = Object.freeze([
  Object.freeze({
    seam_id: "frontend.app.graph.change",
    removed_source_paths: Object.freeze(["frontend/src/host/appMode.ts"]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host-modules.d.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.get_node_by_id",
    removed_source_paths: Object.freeze(["frontend/src/host/appMode.ts"]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host-modules.d.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.set_dirty_canvas",
    removed_source_paths: Object.freeze(["frontend/src/host/appMode.ts"]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host-modules.d.ts",
  }),
]);
const M25_02_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "backend.prompt_server.routes",
    added_source_paths: Object.freeze([
      "comfyui_h3_context/adapters/comfyui_authoring_media_preview.py",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/authoringMediaPreview.ts",
    ]),
  }),
]);
const M26_04_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/comfyPromptHistory.ts",
      "frontend/src/host/managedSequenceClient.ts",
    ]),
  }),
]);
const M26_03_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/productionManagedChildResolver.ts",
      "frontend/src/host/productionPlanningActions.ts",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.workflow.open_workflows",
    added_source_paths: Object.freeze([
      "frontend/src/host/productionManagedChildResolver.ts",
    ]),
  }),
]);
const M25_13_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/authoringMediaSourceLease.ts",
    ]),
  }),
]);
const M23_47_SOURCE_PATH_CHANGES = Object.freeze([
  Object.freeze({
    seam_id: "backend.prompt_server.routes",
    removed_source_paths: Object.freeze([
      "comfyui_h3_context/adapters/comfyui_authoring_workspace.py",
      "comfyui_h3_context/adapters/comfyui_build_provenance.py",
      "comfyui_h3_context/adapters/comfyui_duration_resolution.py",
      "comfyui_h3_context/adapters/comfyui_generation_profile.py",
      "comfyui_h3_context/adapters/comfyui_input_geometry.py",
      "comfyui_h3_context/adapters/comfyui_production_workspace.py",
      "comfyui_h3_context/adapters/comfyui_provider_settings.py",
      "comfyui_h3_context/adapters/comfyui_sequence_coordinator.py",
      "comfyui_h3_context/adapters/comfyui_sidebar_workspace.py",
    ]),
    from_owner: "comfyui_h3_context/adapters/comfyui_authoring_workspace.py",
    to_owner: "comfyui_h3_context/adapters/comfyui_route_seam.py",
  }),
]);
const M23_28_SOURCE_PATH_CHANGES = Object.freeze([
  Object.freeze({
    seam_id: "frontend.api.event_target",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host/sidebarHost.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/entry.tsx",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.api.file_url",
    removed_source_paths: Object.freeze(["frontend/src/host/appMode.ts"]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.api.queue_prompt",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/queueSeam.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.get_sidebar_tabs",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host/sidebarHost.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.register_sidebar_tab",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host/sidebarHost.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.unregister_sidebar_tab",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host/sidebarHost.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.workflow.active_workflow",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/canvasOwnedWrite.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.extension_manager.workflow.open_workflows",
    removed_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/canvasOwnedWrite.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/entry.tsx",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.change",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host-modules.d.ts",
    to_owner: "frontend/src/host/hostModuleTypes.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.events",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host/sidebarHost.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.get_node_by_id",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host-modules.d.ts",
    to_owner: "frontend/src/host/hostModuleTypes.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.serialize",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph.set_dirty_canvas",
    removed_source_paths: Object.freeze(["frontend/src/host-modules.d.ts"]),
    from_owner: "frontend/src/host-modules.d.ts",
    to_owner: "frontend/src/host/hostModuleTypes.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.graph_to_prompt",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.load_api_json",
    removed_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.load_graph_data",
    removed_source_paths: Object.freeze([
      "frontend/src/host-modules.d.ts",
      "frontend/src/host/appMode.ts",
    ]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/canvasOwnedWrite.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.register_extension",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
    ]),
    from_owner: "frontend/src/entry.tsx",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.app.ui_settings",
    removed_source_paths: Object.freeze([
      "frontend/src/entry.tsx",
      "frontend/src/host-modules.d.ts",
    ]),
    from_owner: "frontend/src/entry.tsx",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
  Object.freeze({
    seam_id: "frontend.litegraph.registered_node_types",
    removed_source_paths: Object.freeze(["frontend/src/host/appMode.ts"]),
    from_owner: "frontend/src/host/appMode.ts",
    to_owner: "frontend/src/host/hostSeams.ts",
  }),
]);

const M23_24_SOURCE_PATH_ADDITIONS = Object.freeze([
  Object.freeze({
    seam_id: "backend.prompt_server.routes",
    added_source_paths: Object.freeze([
      "comfyui_h3_context/adapters/comfyui_build_provenance.py",
    ]),
  }),
  Object.freeze({
    seam_id: "frontend.api.fetch_api",
    added_source_paths: Object.freeze([
      "frontend/src/host/buildProvenanceClient.ts",
    ]),
  }),
]);
const ALL_READINESS_STATES = [
  "absent",
  "present_not_ready",
  "ready",
  "unavailable",
] as const;

export type HostSeamBaselineWire = Readonly<{
  schema: "h3.context.host_seam_drift_baseline.v1";
  fixture_version: 1;
  source: Readonly<{ commit: string; tree: string }>;
  artifacts: Readonly<{ census_sha256: string; fixture_sha256: string }>;
}>;

const MODAL_KEYBOARD_BASE_SOURCE = Object.freeze({
  commit: ["e0651ce8", "d5df797a", "bfc34d6f", "3a3f3948", "1da3649c"].join(""),
  tree: ["ef30d062", "28de09e2", "a955c225", "ac5452a1", "188ff509"].join(""),
});
const ACCEPTED_MODAL_KEYBOARD_CENSUS_SHA256 = [
  "add5957b",
  "1a3ebc77",
  "741fd1e3",
  "c17954cb",
  "c2de3bf8",
  "b8082c9f",
  "92d5550f",
  "7fa12f31",
].join("");
const ACCEPTED_MODAL_KEYBOARD_FIXTURE_SHA256 = [
  "ff152fbb",
  "78ca29b4",
  "4a0c439e",
  "9281a0ef",
  "5bafc89c",
  "943a3965",
  "41f8478e",
  "763e6ad7",
].join("");
const ACCEPTED_MODAL_KEYBOARD_SEAM_CONTRACT_SHA256 = [
  "40132e41",
  "d71efd79",
  "5a853ecc",
  "1d6e0eef",
  "572a207c",
  "8460dfe8",
  "d4b0edff",
  "01336a12",
].join("");
const MODAL_KEYBOARD_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.app.canvas.keyboard_capture",
    added_source_paths: [
      "frontend/src/host/canvasKeyboardGuard.ts",
      "frontend/src/lifecycle/presentationBinding.tsx",
    ],
  },
  {
    seam_id: "frontend.app.modal_keyboard_guard",
    added_source_paths: [
      "frontend/src/host/modalKeyboardGuard.ts",
      "frontend/src/lifecycle/presentationBinding.tsx",
    ],
  },
]);

// IMPORTANT: only this single metadata client may extend the existing fetchApi readers.
// Fixed source and artifact pins prevent an unrelated reader from self-authorizing drift.
const WORKSPACE_STATE_BASE_SOURCE = Object.freeze({
  commit: ["c8ff47a6", "214ad7cf", "b566afee", "b55cd6bf", "4904fa9a"].join(""),
  tree: ["087be98c", "d9540765", "a898523d", "2f24001b", "8f84f031"].join(""),
});
const WORKSPACE_STATE_CENSUS_SHA256 = [
  "56176c5f",
  "014e0070",
  "fbffade8",
  "94bf09e7",
  "eed2a2f8",
  "3f20984e",
  "5ed6ceb1",
  "51e5e331",
].join("");
const WORKSPACE_STATE_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: ["frontend/src/host/workspaceStateClient.ts"],
  },
]);

// IMPORTANT: only the retained-media client extends this fixed existing seam. Current
// census bytes must not authorize an unrelated host reader or broaden the accepted contract.
const RETAINED_MEDIA_BASE_SOURCE = Object.freeze({
  commit: ["1f0525b5", "ed51d113", "21399355", "b2afbce5", "cc8fb0f0"].join(""),
  tree: ["b59ec169", "3ec58c6f", "c00b60cb", "8ab433eb", "66f5a24b"].join(""),
});
const RETAINED_MEDIA_CENSUS_SHA256 = [
  "ae93e492",
  "d3b5255b",
  "f06eb122",
  "6c6f5c20",
  "e85f3612",
  "04cdebfe",
  "658243be",
  "904d1e9c",
].join("");
const RETAINED_MEDIA_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: ["frontend/src/host/retainedAssetsClient.ts"],
  },
]);

// IMPORTANT: pin the base and both project clients independently of current census bytes;
// deriving these expectations from the candidate would authorize unrelated host access.
const PROJECT_PERSISTENCE_BASE_SOURCE = Object.freeze({
  commit: ["5c139ea5", "3877dcc1", "dec225a3", "1317d678", "c8eb4532"].join(""),
  tree: ["120fe938", "125036a8", "9b2d990b", "4e999c5c", "dbed0cde"].join(""),
});
const PROJECT_PERSISTENCE_CENSUS_SHA256 = [
  "5eb83e32",
  "2e9b2907",
  "0220c994",
  "699601ea",
  "5cdb0c4d",
  "b0fc234e",
  "5035b133",
  "d68b8062",
].join("");
const PROJECT_PERSISTENCE_SOURCE_PATH_ADDITIONS = Object.freeze([
  {
    seam_id: "frontend.api.fetch_api",
    added_source_paths: [
      "frontend/src/host/projectDocumentClient.ts",
      "frontend/src/host/projectRecoveryClient.ts",
    ],
  },
]);

type HostSeamCensusTransitionWire = Readonly<{
  schema:
    | "h3.context.host_seam_census_transition.v1"
    | "h3.context.host_seam_census_transition_m23_18.v1"
    | "h3.context.host_seam_census_transition_m23_19.v1"
    | "h3.context.host_seam_census_transition_m23_25.v1"
    | "h3.context.host_seam_census_transition_m25_02.v1"
    | "h3.context.host_seam_census_transition_m23_24.v1"
    | "h3.context.host_seam_census_transition_m23_47.v1"
    | "h3.context.host_seam_census_transition_m23_28.v1"
    | "h3.context.host_seam_census_transition_m26_04.v1"
    | "h3.context.host_seam_census_transition_m26_03.v1"
    | "h3.context.host_seam_census_transition_m25_13.v1"
    | "h3.context.host_seam_census_transition_managed_qualification.v1"
    | "h3.context.host_seam_census_transition_m25_16.v1"
    | "h3.context.host_seam_census_transition_m16_05.v1"
    | "h3.context.host_seam_census_transition_m25_33.v1"
    | "h3.context.host_seam_census_transition_m16_05_release.v1"
    | "h3.context.host_seam_census_transition_m25_38.v1"
    | "h3.context.host_seam_census_transition_modal_keyboard.v1"
    | "h3.context.host_seam_census_transition_workspace_state.v1"
    | "h3.context.host_seam_census_transition_retained_media.v1"
    | "h3.context.host_seam_census_transition_project_persistence.v1";
  item:
    | "M23-15"
    | "M23-18"
    | "M23-19"
    | "M23-25"
    | "M25-02"
    | "M23-24"
    | "M23-47"
    | "M23-28"
    | "M26-04"
    | "M26-03"
    | "M25-13"
    | "MANAGED-QUALIFICATION"
    | "M25-16"
    | "M16-05"
    | "M25-33"
    | "M25-38"
    | "MODAL-KEYBOARD"
    | "WORKSPACE-STATE"
    | "RETAINED-MEDIA"
    | "PROJECT-PERSISTENCE";
  source: Readonly<{ commit: string; tree: string }>;
  artifacts: Readonly<{
    from_census_sha256: string;
    to_census_sha256: string;
    fixture_sha256: string;
    seam_contract_sha256: string;
  }>;
  changes: readonly (
    | Readonly<{
        seam_id: string;
        added_source_paths: readonly string[];
      }>
    | Readonly<{
        seam_id: string;
        removed_source_paths: readonly string[];
        from_owner: string;
        to_owner: string;
      }>
  )[];
}>;

export type HostSeamProbeRow = Readonly<{
  seam_id: string;
  availability: "OBSERVED" | "UNAVAILABLE";
  observation: HostSeamObservationWire | null;
}>;

export type HostSeamDriftReport = Readonly<{
  result: "PASS" | "DRIFTED" | "NOT_RUN";
  reason: "MATCH" | "HOST_DRIFT" | "PARTIAL_UNAVAILABLE" | "HOST_UNAVAILABLE";
  summary: Readonly<{ passed: number; drifted: number; unavailable: number }>;
  seams: readonly Readonly<{
    seam_id: string;
    status: "PASS" | "DRIFTED" | "UNAVAILABLE";
  }>[];
}>;

function record(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error(`${field} must be an object`);
  return value as Record<string, unknown>;
}

function closed(
  value: unknown,
  field: string,
  expectedKeys: readonly string[],
): Record<string, unknown> {
  const candidate = record(value, field);
  const actual = Object.keys(candidate).sort();
  const expected = [...expectedKeys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    throw new Error(`${field} does not have the required closed shape`);
  return candidate;
}

function boundedInt(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error(`${field} must be a bounded nonnegative integer`);
  return value as number;
}

function sha256(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function exactGitSource(
  value: unknown,
  field: string,
): Readonly<{ commit: string; tree: string }> {
  const source = closed(value, field, SOURCE_KEYS);
  if (
    typeof source.commit !== "string" ||
    !HEX_40.test(source.commit) ||
    typeof source.tree !== "string" ||
    !HEX_40.test(source.tree)
  )
    throw new Error(`${field} must contain full Git identities`);
  return Object.freeze({ commit: source.commit, tree: source.tree });
}

export function bindExactCandidateIdentity(
  supplied: unknown,
  actual: unknown,
): Readonly<{ commit: string; tree: string }> {
  const suppliedSource = exactGitSource(supplied, "supplied candidate");
  const actualSource = exactGitSource(actual, "actual Git candidate");
  if (
    suppliedSource.commit !== actualSource.commit ||
    suppliedSource.tree !== actualSource.tree
  )
    throw new Error(
      "supplied identity does not match the actual Git candidate",
    );
  return suppliedSource;
}

export type HostProbeAuditSummary = Readonly<{
  blocked_cross_origin_attempts: number;
  blocked_non_get_attempts: number;
  blocked_credential_attempts: number;
  blocked_prompt_attempts: number;
  blocked_workflow_attempts: number;
  blocked_media_attempts: number;
}>;

export function summarizeHostProbeAudits(
  audits: readonly BrowserRequestAudit[],
): HostProbeAuditSummary {
  const blocked = audits.filter((audit) => !audit.allow);
  const sum = (field: Exclude<keyof BrowserRequestAudit, "allow">): number =>
    blocked.reduce((count, audit) => count + audit[field], 0);
  return Object.freeze({
    blocked_cross_origin_attempts: sum("crossOrigin"),
    blocked_non_get_attempts: sum("nonGet"),
    blocked_credential_attempts: sum("credentialBearing"),
    blocked_prompt_attempts: sum("promptOperation"),
    blocked_workflow_attempts: sum("workflowOperation"),
    blocked_media_attempts: sum("mediaOperation"),
  });
}

export function assertTransportFailurePolicyClean(
  audits: readonly BrowserRequestAudit[],
): void {
  const summary = summarizeHostProbeAudits(audits);
  if (Object.values(summary).some((count) => count !== 0))
    throw new Error("host probe policy violation caused transport failure");
}

function joinedHexParts(
  value: unknown,
  field: string,
  expectedParts: number,
): string {
  if (
    !Array.isArray(value) ||
    value.length !== expectedParts ||
    value.some(
      (part) => typeof part !== "string" || !/^[0-9a-f]{8}$/.test(part),
    )
  )
    throw new Error(`${field} must contain exact eight-hex chunks`);
  return value.join("");
}

export function parseHostSeamBaseline(value: unknown): HostSeamBaselineWire {
  const root = closed(value, "baseline", BASELINE_KEYS);
  if (
    root.schema !== "h3.context.host_seam_drift_baseline.v1" ||
    root.fixture_version !== 1
  )
    throw new Error("baseline has an unsupported schema or fixture version");
  const source = closed(root.source, "baseline.source", BASELINE_SOURCE_KEYS);
  const artifacts = closed(
    root.artifacts,
    "baseline.artifacts",
    BASELINE_ARTIFACT_KEYS,
  );
  const commit = joinedHexParts(source.commit_parts, "source.commit_parts", 5);
  const tree = joinedHexParts(source.tree_parts, "source.tree_parts", 5);
  const censusSha256 = joinedHexParts(
    artifacts.census_sha256_parts,
    "artifacts.census_sha256_parts",
    8,
  );
  const fixtureSha256 = joinedHexParts(
    artifacts.fixture_sha256_parts,
    "artifacts.fixture_sha256_parts",
    8,
  );
  return {
    schema: root.schema,
    fixture_version: root.fixture_version,
    source: {
      commit,
      tree,
    },
    artifacts: {
      census_sha256: censusSha256,
      fixture_sha256: fixtureSha256,
    },
  } as HostSeamBaselineWire;
}

const REMOVAL_TRANSITION_SCHEMAS: ReadonlySet<string> = new Set([
  "h3.context.host_seam_census_transition_m23_25.v1",
  "h3.context.host_seam_census_transition_m23_47.v1",
  "h3.context.host_seam_census_transition_m23_28.v1",
  "h3.context.host_seam_census_transition_m25_38.v1",
]);

function parseHostSeamCensusTransition(
  value: unknown,
): HostSeamCensusTransitionWire {
  const root = closed(value, "census transition", TRANSITION_KEYS);
  if (!(
    (root.schema === "h3.context.host_seam_census_transition.v1" &&
      root.item === "M23-15") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_18.v1" &&
      root.item === "M23-18") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_19.v1" &&
      root.item === "M23-19") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_25.v1" &&
      root.item === "M23-25") ||
    (root.schema === "h3.context.host_seam_census_transition_m25_02.v1" &&
      root.item === "M25-02") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_24.v1" &&
      root.item === "M23-24") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_47.v1" &&
      root.item === "M23-47") ||
    (root.schema === "h3.context.host_seam_census_transition_m23_28.v1" &&
      root.item === "M23-28") ||
    (root.schema === "h3.context.host_seam_census_transition_m26_04.v1" &&
      root.item === "M26-04") ||
    (root.schema === "h3.context.host_seam_census_transition_m26_03.v1" &&
      root.item === "M26-03") ||
    (root.schema === "h3.context.host_seam_census_transition_m25_13.v1" &&
      root.item === "M25-13") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_managed_qualification.v1" &&
      root.item === "MANAGED-QUALIFICATION") ||
    (root.schema === "h3.context.host_seam_census_transition_m25_16.v1" &&
      root.item === "M25-16") ||
    (root.schema === "h3.context.host_seam_census_transition_m16_05.v1" &&
      root.item === "M16-05") ||
    (root.schema === "h3.context.host_seam_census_transition_m25_33.v1" &&
      root.item === "M25-33") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_m16_05_release.v1" &&
      root.item === "M16-05") ||
    (root.schema === "h3.context.host_seam_census_transition_m25_38.v1" &&
      root.item === "M25-38") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_modal_keyboard.v1" &&
      root.item === "MODAL-KEYBOARD") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_workspace_state.v1" &&
      root.item === "WORKSPACE-STATE") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_retained_media.v1" &&
      root.item === "RETAINED-MEDIA") ||
    (root.schema ===
      "h3.context.host_seam_census_transition_project_persistence.v1" &&
      root.item === "PROJECT-PERSISTENCE")
  ))
    throw new Error("census transition has an unsupported identity");
  const source = closed(
    root.source,
    "census transition.source",
    BASELINE_SOURCE_KEYS,
  );
  const artifacts = closed(
    root.artifacts,
    "census transition.artifacts",
    TRANSITION_ARTIFACT_KEYS,
  );
  if (
    !Array.isArray(root.changes) ||
    root.changes.length === 0 ||
    root.changes.length > 64
  )
    throw new Error("census transition changes must be bounded");
  // A transition document is either all additions or all removals; a removal also has to say who
  // the seam's owner becomes, because removing the current owner's path without naming the next one
  // would leave the census unable to say which source is authoritative.
  const isRemovalTransition = REMOVAL_TRANSITION_SCHEMAS.has(
    String(root.schema),
  );
  const validSourcePath = (path: unknown): path is string =>
    typeof path === "string" &&
    path.length <= 260 &&
    /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+$/.test(path) &&
    !path.split("/").includes("..") &&
    /\.(?:py|ts|tsx)$/.test(path);
  const changes = root.changes.map((value) => {
    const change = closed(
      value,
      "census transition change",
      isRemovalTransition
        ? TRANSITION_REMOVAL_CHANGE_KEYS
        : TRANSITION_CHANGE_KEYS,
    );
    if (typeof change.seam_id !== "string" || !SEAM_ID.test(change.seam_id))
      throw new Error("census transition change is invalid");
    const sourcePaths = isRemovalTransition
      ? change.removed_source_paths
      : change.added_source_paths;
    if (
      !Array.isArray(sourcePaths) ||
      sourcePaths.length === 0 ||
      sourcePaths.length > 64 ||
      sourcePaths.some((path) => !validSourcePath(path))
    )
      throw new Error("census transition change is invalid");
    const paths = [...sourcePaths] as string[];
    if (paths.some((path, index) => path !== [...new Set(paths)].sort()[index]))
      throw new Error("census transition paths must be sorted and unique");
    if (isRemovalTransition) {
      // IMPORTANT: M23-28 centralizes host-member ownership while retaining some former owners as
      // typed-probe callers. Those callers remain valid census sources, so only older removal
      // schemas require the former owner itself to be one of the removed paths.
      const formerOwnerMustLeave =
        root.schema !== "h3.context.host_seam_census_transition_m23_28.v1";
      if (
        !validSourcePath(change.from_owner) ||
        !validSourcePath(change.to_owner) ||
        change.from_owner === change.to_owner ||
        (formerOwnerMustLeave && !paths.includes(change.from_owner)) ||
        paths.includes(change.to_owner)
      )
        throw new Error("census transition owner change is invalid");
      return Object.freeze({
        seam_id: change.seam_id,
        removed_source_paths: Object.freeze(paths),
        from_owner: change.from_owner,
        to_owner: change.to_owner,
      });
    }
    return Object.freeze({
      seam_id: change.seam_id,
      added_source_paths: Object.freeze(paths),
    });
  });
  if (
    changes.some(
      (change, index) =>
        change.seam_id !==
        [...new Set(changes.map((candidate) => candidate.seam_id))].sort()[
          index
        ],
    )
  )
    throw new Error("census transition seams must be sorted and unique");
  return Object.freeze({
    schema: root.schema,
    item: root.item,
    source: Object.freeze({
      commit: joinedHexParts(source.commit_parts, "source.commit_parts", 5),
      tree: joinedHexParts(source.tree_parts, "source.tree_parts", 5),
    }),
    artifacts: Object.freeze({
      from_census_sha256: joinedHexParts(
        artifacts.from_census_sha256_parts,
        "artifacts.from_census_sha256_parts",
        8,
      ),
      to_census_sha256: joinedHexParts(
        artifacts.to_census_sha256_parts,
        "artifacts.to_census_sha256_parts",
        8,
      ),
      fixture_sha256: joinedHexParts(
        artifacts.fixture_sha256_parts,
        "artifacts.fixture_sha256_parts",
        8,
      ),
      seam_contract_sha256: joinedHexParts(
        artifacts.seam_contract_sha256_parts,
        "artifacts.seam_contract_sha256_parts",
        8,
      ),
    }),
    changes: Object.freeze(changes),
  }) as HostSeamCensusTransitionWire;
}

function parsedJsonBytes(value: Uint8Array): unknown {
  if (value.length === 0 || value.length > 1_048_576)
    throw new Error("repository contract bytes are not bounded");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(value));
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string")
    return JSON.stringify(value);
  if (typeof value === "number" && Number.isFinite(value))
    return JSON.stringify(value);
  if (Array.isArray(value))
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  const object = record(value, "canonical JSON value");
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
    .join(",")}}`;
}

function seamContractProjection(census: unknown): unknown {
  const root = closed(census, "census", ["schema", "profile", "seams"]);
  if (!Array.isArray(root.seams))
    throw new Error("census seams must be a list");
  return {
    schema: root.schema,
    profile: root.profile,
    seams: root.seams.map((value) => {
      const row = record(value, "census seam");
      const { source_paths: sourcePaths, ...contract } = row;
      if (!Array.isArray(sourcePaths))
        throw new Error("census source paths must be a list");
      return contract;
    }),
  };
}

function validateEvidenceSource(
  value: unknown,
): Readonly<{ commit: string; tree: string }> {
  return exactGitSource(value, "evidence.source");
}

function validateEvidenceArtifacts(
  value: unknown,
): Readonly<{ census_sha256: string; fixture_sha256: string }> {
  const artifacts = closed(value, "evidence.artifacts", ARTIFACT_KEYS);
  if (
    typeof artifacts.census_sha256 !== "string" ||
    !HEX_64.test(artifacts.census_sha256) ||
    typeof artifacts.fixture_sha256 !== "string" ||
    !HEX_64.test(artifacts.fixture_sha256)
  )
    throw new Error("evidence artifact fingerprints are invalid");
  return Object.freeze({
    census_sha256: artifacts.census_sha256,
    fixture_sha256: artifacts.fixture_sha256,
  });
}

export function verifyHostSeamBaseline(
  value: unknown,
  censusBytes: Uint8Array,
  fixtureBytes: Uint8Array,
  transitionValue?: unknown,
): Readonly<{
  result: "PASS" | "DRIFTED";
  reason:
    "BASELINE_MATCH" | "COMPATIBILITY_TRANSITION_MATCH" | "REPOSITORY_BREAKAGE";
}> {
  const baseline = parseHostSeamBaseline(value);
  const fixedBaselineSourceMatches =
    baseline.source.commit === ACCEPTED_HC09_SOURCE.commit &&
    baseline.source.tree === ACCEPTED_HC09_SOURCE.tree;
  if (!fixedBaselineSourceMatches)
    return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };
  const censusSha256 = sha256(censusBytes);
  const fixtureSha256 = sha256(fixtureBytes);
  if (censusSha256 === baseline.artifacts.census_sha256)
    return fixtureSha256 === baseline.artifacts.fixture_sha256
      ? { result: "PASS", reason: "BASELINE_MATCH" }
      : { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };
  if (transitionValue === undefined)
    return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };

  const transitionValues = Array.isArray(transitionValue)
    ? transitionValue
    : [transitionValue];
  const expectedTransitions = [
    {
      item: "M23-15",
      source: ACCEPTED_M23_14_SOURCE,
      from_census_sha256: baseline.artifacts.census_sha256,
      to_census_sha256: ACCEPTED_M23_15_CENSUS_SHA256,
      fixture_sha256: baseline.artifacts.fixture_sha256,
      seam_contract_sha256: ACCEPTED_M23_18_SEAM_CONTRACT_SHA256,
      changes: M23_15_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M23-18",
      source: ACCEPTED_M23_17_SOURCE,
      from_census_sha256: ACCEPTED_M23_15_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_18_CENSUS_SHA256,
      fixture_sha256: baseline.artifacts.fixture_sha256,
      seam_contract_sha256: ACCEPTED_M23_18_SEAM_CONTRACT_SHA256,
      changes: M23_18_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M23-19",
      source: ACCEPTED_M23_18_SOURCE,
      from_census_sha256: ACCEPTED_M23_18_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_19_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_19_SEAM_CONTRACT_SHA256,
      changes: M23_19_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M23-25",
      source: ACCEPTED_M23_21_SOURCE,
      from_census_sha256: ACCEPTED_M23_19_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_25_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_25_SEAM_CONTRACT_SHA256,
      changes: M23_25_SOURCE_PATH_REMOVALS,
    },
    {
      item: "M25-02",
      source: ACCEPTED_M25_01_SOURCE,
      from_census_sha256: ACCEPTED_M23_25_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M25_02_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_25_SEAM_CONTRACT_SHA256,
      changes: M25_02_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M23-24",
      source: ACCEPTED_M23_24_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M25_02_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_24_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_25_SEAM_CONTRACT_SHA256,
      changes: M23_24_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M23-47",
      source: ACCEPTED_M23_47_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M23_24_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_47_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_47_SEAM_CONTRACT_SHA256,
      changes: M23_47_SOURCE_PATH_CHANGES,
    },
    {
      item: "M23-28",
      source: ACCEPTED_M23_28_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M23_47_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M23_28_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M23_28_SOURCE_PATH_CHANGES,
    },
    {
      item: "M26-04",
      source: ACCEPTED_M26_04_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M23_28_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M26_04_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M26_04_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M26-03",
      source: ACCEPTED_M26_03_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M26_04_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M26_03_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M26_03_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M25-13",
      source: ACCEPTED_M25_13_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M26_03_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M25_13_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M25_13_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "MANAGED-QUALIFICATION",
      source: MANAGED_QUALIFICATION_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M25_13_CENSUS_SHA256,
      to_census_sha256: MANAGED_QUALIFICATION_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: MANAGED_QUALIFICATION_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M25-16",
      source: M25_16_BASE_SOURCE,
      from_census_sha256: MANAGED_QUALIFICATION_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M25_16_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M25_16_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M16-05",
      source: M16_05_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M25_16_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M16_05_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M16_05_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M25-33",
      source: M25_33_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M16_05_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M25_33_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M25_33_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M16-05",
      source: M16_05_RELEASE_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M25_33_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M16_05_RELEASE_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M16_05_RELEASE_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "M25-38",
      source: M25_38_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M16_05_RELEASE_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_M25_38_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_M23_19_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_M23_28_SEAM_CONTRACT_SHA256,
      changes: M25_38_SOURCE_PATH_REMOVALS,
    },
    {
      item: "MODAL-KEYBOARD",
      source: MODAL_KEYBOARD_BASE_SOURCE,
      from_census_sha256: ACCEPTED_M25_38_CENSUS_SHA256,
      to_census_sha256: ACCEPTED_MODAL_KEYBOARD_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_MODAL_KEYBOARD_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_MODAL_KEYBOARD_SEAM_CONTRACT_SHA256,
      changes: MODAL_KEYBOARD_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "WORKSPACE-STATE",
      source: WORKSPACE_STATE_BASE_SOURCE,
      from_census_sha256: ACCEPTED_MODAL_KEYBOARD_CENSUS_SHA256,
      to_census_sha256: WORKSPACE_STATE_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_MODAL_KEYBOARD_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_MODAL_KEYBOARD_SEAM_CONTRACT_SHA256,
      changes: WORKSPACE_STATE_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "RETAINED-MEDIA",
      source: RETAINED_MEDIA_BASE_SOURCE,
      from_census_sha256: WORKSPACE_STATE_CENSUS_SHA256,
      to_census_sha256: RETAINED_MEDIA_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_MODAL_KEYBOARD_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_MODAL_KEYBOARD_SEAM_CONTRACT_SHA256,
      changes: RETAINED_MEDIA_SOURCE_PATH_ADDITIONS,
    },
    {
      item: "PROJECT-PERSISTENCE",
      source: PROJECT_PERSISTENCE_BASE_SOURCE,
      from_census_sha256: RETAINED_MEDIA_CENSUS_SHA256,
      to_census_sha256: PROJECT_PERSISTENCE_CENSUS_SHA256,
      fixture_sha256: ACCEPTED_MODAL_KEYBOARD_FIXTURE_SHA256,
      seam_contract_sha256: ACCEPTED_MODAL_KEYBOARD_SEAM_CONTRACT_SHA256,
      changes: PROJECT_PERSISTENCE_SOURCE_PATH_ADDITIONS,
    },
  ] as const;
  const targetIndex = expectedTransitions.findIndex(
    (expected) => expected.to_census_sha256 === censusSha256,
  );
  if (targetIndex < 0 || transitionValues.length !== targetIndex + 1)
    return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };
  const transitions = transitionValues.map(parseHostSeamCensusTransition);
  const exactTransitionsMatch = transitions.every((transition, index) => {
    const expected = expectedTransitions[index];
    return (
      expected !== undefined &&
      transition.item === expected.item &&
      transition.source.commit === expected.source.commit &&
      transition.source.tree === expected.source.tree &&
      transition.artifacts.from_census_sha256 === expected.from_census_sha256 &&
      transition.artifacts.to_census_sha256 === expected.to_census_sha256 &&
      transition.artifacts.fixture_sha256 === expected.fixture_sha256 &&
      transition.artifacts.seam_contract_sha256 ===
        expected.seam_contract_sha256 &&
      JSON.stringify(transition.changes) === JSON.stringify(expected.changes)
    );
  });
  if (!exactTransitionsMatch)
    return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };

  try {
    const censusWire = parsedJsonBytes(censusBytes);
    const fixtureWire = parsedJsonBytes(fixtureBytes);
    const contract = parseHostSeamContract(censusWire, fixtureWire);
    const contractDigest = sha256(
      new TextEncoder().encode(
        canonicalJson(seamContractProjection(censusWire)),
      ),
    );
    // IMPORTANT: validate the last authorized state for each seam/path in transition order. A
    // global removal set rejects a legitimate later re-add; unordered membership would let stale
    // paths survive after a final removal.
    const finalPathStates = new Map<string, "added" | "removed">();
    for (const transition of transitions)
      for (const change of transition.changes) {
        const state =
          "added_source_paths" in change
            ? ("added" as const)
            : ("removed" as const);
        const paths =
          "added_source_paths" in change
            ? change.added_source_paths
            : change.removed_source_paths;
        for (const path of paths)
          finalPathStates.set(`${change.seam_id}\0${path}`, state);
      }
    // IMPORTANT: owner moves are ordered. Validate a seam against its last authorized owner;
    // checking every historical intermediate owner rejects a legitimate later relocation.
    const finalOwners = new Map<string, string>();
    for (const transition of transitions)
      for (const change of transition.changes)
        if ("to_owner" in change)
          finalOwners.set(change.seam_id, change.to_owner);
    if (
      fixtureSha256 !== expectedTransitions[targetIndex]?.fixture_sha256 ||
      contractDigest !==
        expectedTransitions[targetIndex]?.seam_contract_sha256 ||
      [...finalPathStates].some(([identity, state]) => {
        const [seamId, path] = identity.split("\0");
        const row = seamId === undefined ? undefined : contract.byId[seamId];
        if (row === undefined || path === undefined) return true;
        const present = row.sourcePaths.includes(path);
        return state === "added" ? !present : present;
      }) ||
      [...finalOwners].some(
        ([seamId, owner]) => contract.byId[seamId]?.owner !== owner,
      )
    )
      return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };
  } catch {
    return { result: "DRIFTED", reason: "REPOSITORY_BREAKAGE" };
  }
  return { result: "PASS", reason: "COMPATIBILITY_TRANSITION_MATCH" };
}

function observationCensus(census: unknown): unknown {
  const root = record(census, "census");
  if (!Array.isArray(root.seams))
    throw new Error("census seams must be a list");
  return {
    ...root,
    seams: root.seams.map((value) => ({
      ...record(value, "census seam"),
      readiness_states: ALL_READINESS_STATES,
    })),
  };
}

export function hostProbeAvailability(value: string | undefined): Readonly<{
  result: "READY" | "NOT_RUN";
  reason: "HOST_SUPPLIED" | "HOST_NOT_SUPPLIED";
}> {
  return value === undefined
    ? { result: "NOT_RUN", reason: "HOST_NOT_SUPPLIED" }
    : { result: "READY", reason: "HOST_SUPPLIED" };
}

function normalizedObservation(
  value: HostSeamObservationWire,
): HostSeamObservationWire {
  const row = closed(value, "probe.observation", OBSERVATION_KEYS);
  return {
    seam_id: row.seam_id as HostSeamObservationWire["seam_id"],
    presence: row.presence as HostSeamObservationWire["presence"],
    kind: row.kind as HostSeamObservationWire["kind"],
    key_shape: row.key_shape as HostSeamObservationWire["key_shape"],
    element_kind: row.element_kind as HostSeamObservationWire["element_kind"],
    readiness_state:
      row.readiness_state as HostSeamObservationWire["readiness_state"],
    count_bucket: row.count_bucket as HostSeamObservationWire["count_bucket"],
    byte_bucket: row.byte_bucket as HostSeamObservationWire["byte_bucket"],
    latency_bucket:
      row.latency_bucket as HostSeamObservationWire["latency_bucket"],
  };
}

export function classifyHostSeamDrift(
  census: unknown,
  fixture: unknown,
  probeRows: readonly HostSeamProbeRow[],
): HostSeamDriftReport {
  parseHostSeamContract(census, fixture);
  const expectedFixture = fixture as HostSeamFixtureWire;
  const expectedIds = expectedFixture.observations.map((row) => row.seam_id);
  const actualIds = probeRows.map((row) => row.seam_id);
  if (
    actualIds.length !== expectedIds.length ||
    actualIds.some((id) => !expectedIds.includes(id))
  )
    throw new Error("probe rows must be complete for the recorded seam set");
  if (actualIds.some((id, index) => index > 0 && id <= actualIds[index - 1]!))
    throw new Error("probe seam IDs must be sorted and unique");
  if (JSON.stringify(actualIds) !== JSON.stringify(expectedIds))
    throw new Error("probe rows must be complete for the recorded seam set");

  const validationObservations = probeRows.map((row, index) => {
    const envelope = closed(row, "probe row", [
      "seam_id",
      "availability",
      "observation",
    ]);
    if (envelope.seam_id !== expectedIds[index])
      throw new Error("probe rows must be complete for the recorded seam set");
    if (envelope.availability === "UNAVAILABLE") {
      if (envelope.observation !== null)
        throw new Error("an unavailable probe row cannot carry an observation");
      return expectedFixture.observations[index]!;
    }
    if (envelope.availability !== "OBSERVED" || envelope.observation === null)
      throw new Error("a probe row has an invalid availability state");
    const observation = normalizedObservation(
      envelope.observation as HostSeamObservationWire,
    );
    if (observation.seam_id !== envelope.seam_id)
      throw new Error("probe row and observation seam IDs disagree");
    return observation;
  });
  // The accepted census readiness set describes the expected host, not the
  // structural domain of an observation. Widen only readiness while parsing a
  // live row so an absent seam becomes HOST_DRIFT instead of an exception.
  parseHostSeamContract(observationCensus(census), {
    ...expectedFixture,
    observations: validationObservations,
  });

  const seams = probeRows.map((row, index) => {
    let status: "PASS" | "DRIFTED" | "UNAVAILABLE";
    if (row.availability === "UNAVAILABLE") status = "UNAVAILABLE";
    else
      status =
        JSON.stringify(normalizedObservation(row.observation!)) ===
        JSON.stringify(
          normalizedObservation(expectedFixture.observations[index]!),
        )
          ? "PASS"
          : "DRIFTED";
    return Object.freeze({ seam_id: row.seam_id, status });
  });
  const summary = {
    passed: seams.filter((row) => row.status === "PASS").length,
    drifted: seams.filter((row) => row.status === "DRIFTED").length,
    unavailable: seams.filter((row) => row.status === "UNAVAILABLE").length,
  };
  if (summary.drifted > 0)
    return Object.freeze({
      result: "DRIFTED",
      reason: "HOST_DRIFT",
      summary: Object.freeze(summary),
      seams: Object.freeze(seams),
    });
  if (summary.unavailable > 0)
    return Object.freeze({
      result: "NOT_RUN",
      reason: "PARTIAL_UNAVAILABLE",
      summary: Object.freeze(summary),
      seams: Object.freeze(seams),
    });
  return Object.freeze({
    result: "PASS",
    reason: "MATCH",
    summary: Object.freeze(summary),
    seams: Object.freeze(seams),
  });
}

export function unavailableHostSeamProbe(
  fixture: unknown,
  reason: "HOST_UNAVAILABLE",
): HostSeamDriftReport {
  const root = record(fixture, "fixture");
  if (!Array.isArray(root.observations) || root.observations.length === 0)
    throw new Error("fixture observations must be a non-empty list");
  const seams = root.observations.map((value) => {
    const row = record(value, "fixture observation");
    if (typeof row.seam_id !== "string" || !SEAM_ID.test(row.seam_id))
      throw new Error("fixture observation has an invalid seam ID");
    return Object.freeze({
      seam_id: row.seam_id,
      status: "UNAVAILABLE" as const,
    });
  });
  const ids = seams.map((row) => row.seam_id);
  if (ids.some((id, index) => index > 0 && id <= ids[index - 1]!))
    throw new Error("fixture seam IDs must be sorted and unique");
  return Object.freeze({
    result: "NOT_RUN",
    reason,
    summary: Object.freeze({
      passed: 0,
      drifted: 0,
      unavailable: seams.length,
    }),
    seams: Object.freeze(seams),
  });
}

function parseSummary(value: unknown): {
  passed: number;
  drifted: number;
  unavailable: number;
} {
  const summary = closed(value, "evidence.summary", SUMMARY_KEYS);
  return {
    passed: boundedInt(summary.passed, "evidence.summary.passed"),
    drifted: boundedInt(summary.drifted, "evidence.summary.drifted"),
    unavailable: boundedInt(
      summary.unavailable,
      "evidence.summary.unavailable",
    ),
  };
}

function canonicalSeamIds(fixture: unknown): readonly string[] {
  const root = record(fixture, "expected fixture");
  if (!Array.isArray(root.observations) || root.observations.length !== 29)
    throw new Error("expected fixture must contain exactly 29 observations");
  const ids = root.observations.map((value) => {
    const row = record(value, "expected fixture observation");
    if (typeof row.seam_id !== "string" || !SEAM_ID.test(row.seam_id))
      throw new Error("expected fixture contains an invalid seam ID");
    return row.seam_id;
  });
  if (ids.some((id, index) => index > 0 && id <= ids[index - 1]!))
    throw new Error("expected fixture seam IDs must be sorted and unique");
  return ids;
}

export function validateHostSeamDriftEvidence(
  value: unknown,
  expectedFixture: unknown,
  expectedBaseline: HostSeamBaselineWire,
  expectedProbe: Readonly<{ commit: string; tree: string }>,
): void {
  validateContentFreeEvidence(value);
  const root = closed(value, "evidence", EVIDENCE_KEYS);
  if (
    root.schema !== "h3.context.host_seam_drift_evidence.v1" ||
    root.item !== "HC-10" ||
    !["PASS", "DRIFTED", "NOT_RUN"].includes(root.result as string) ||
    ![
      "MATCH",
      "HOST_DRIFT",
      "REPOSITORY_BREAKAGE",
      "HOST_NOT_SUPPLIED",
      "HOST_UNAVAILABLE",
      "PARTIAL_UNAVAILABLE",
    ].includes(root.reason as string)
  )
    throw new Error("evidence has an unsupported identity or result");
  const source = validateEvidenceSource(root.source);
  const artifacts = validateEvidenceArtifacts(root.artifacts);
  const probe = exactGitSource(root.probe, "evidence.probe");
  if (
    JSON.stringify(source) !== JSON.stringify(expectedBaseline.source) ||
    JSON.stringify(artifacts) !== JSON.stringify(expectedBaseline.artifacts) ||
    JSON.stringify(probe) !== JSON.stringify(expectedProbe)
  )
    throw new Error("evidence identities do not match their exact authorities");
  if (root.subject !== null) {
    const subject = closed(root.subject, "evidence.subject", SUBJECT_KEYS);
    if (
      typeof subject.comfyui_version !== "string" ||
      !VERSION.test(subject.comfyui_version) ||
      typeof subject.frontend_version !== "string" ||
      !VERSION.test(subject.frontend_version)
    )
      throw new Error("evidence subject contains a non-version identity");
  }
  const summary = parseSummary(root.summary);
  if (!Array.isArray(root.seams) || root.seams.length !== 29)
    throw new Error("evidence must contain exactly 29 seam results");
  const seamResults = root.seams.map((value) => {
    const row = closed(value, "evidence.seam", SEAM_RESULT_KEYS);
    if (
      typeof row.seam_id !== "string" ||
      !SEAM_ID.test(row.seam_id) ||
      !["PASS", "DRIFTED", "UNAVAILABLE"].includes(row.status as string)
    )
      throw new Error("evidence contains an invalid seam result");
    return { seam_id: row.seam_id, status: row.status as string };
  });
  const ids = seamResults.map((row) => row.seam_id);
  if (ids.some((id, index) => index > 0 && id <= ids[index - 1]!))
    throw new Error("evidence seam results must be sorted and unique");
  if (JSON.stringify(ids) !== JSON.stringify(canonicalSeamIds(expectedFixture)))
    throw new Error("evidence seam IDs do not match the canonical fixture");
  const observedSummary = {
    passed: seamResults.filter((row) => row.status === "PASS").length,
    drifted: seamResults.filter((row) => row.status === "DRIFTED").length,
    unavailable: seamResults.filter((row) => row.status === "UNAVAILABLE")
      .length,
  };
  if (JSON.stringify(summary) !== JSON.stringify(observedSummary))
    throw new Error("evidence seam summary is inconsistent");
  const requestPolicy = closed(
    root.request_policy,
    "evidence.request_policy",
    REQUEST_POLICY_KEYS,
  );
  for (const key of REQUEST_POLICY_KEYS)
    boundedInt(requestPolicy[key], `evidence.request_policy.${key}`);
  if (
    requestPolicy.api_get_requests !== 6 ||
    requestPolicy.redirects !== 0 ||
    requestPolicy.browser_cookie_records_before !== 0 ||
    requestPolicy.browser_cookie_records_after !== 0 ||
    requestPolicy.model_or_queue_operations !== 0 ||
    requestPolicy.queue_before_running !== 0 ||
    requestPolicy.queue_before_pending !== 0 ||
    requestPolicy.queue_after_running !== 0 ||
    requestPolicy.queue_after_pending !== 0 ||
    requestPolicy.blocked_credential_attempts !== 0 ||
    requestPolicy.blocked_prompt_attempts !== 0 ||
    requestPolicy.blocked_workflow_attempts !== 0
  )
    throw new Error("evidence request policy does not prove a safe probe");
  const privacy = closed(root.privacy, "evidence.privacy", PRIVACY_KEYS);
  for (const key of PRIVACY_KEYS)
    if (boundedInt(privacy[key], `evidence.privacy.${key}`) !== 0)
      throw new Error("evidence privacy counters must all be zero");
  if (root.result === "PASS") {
    if (
      root.reason !== "MATCH" ||
      summary.passed !== 29 ||
      summary.drifted !== 0 ||
      summary.unavailable !== 0 ||
      root.subject === null
    )
      throw new Error("PASS evidence is incomplete or inconsistent");
    return;
  }
  if (root.result === "DRIFTED") {
    if (
      !["HOST_DRIFT", "REPOSITORY_BREAKAGE"].includes(root.reason as string) ||
      (root.reason === "HOST_DRIFT" &&
        (summary.drifted === 0 || root.subject === null)) ||
      (root.reason === "REPOSITORY_BREAKAGE" && root.subject !== null)
    )
      throw new Error("DRIFTED evidence is incomplete or inconsistent");
    return;
  }
  if (
    !["HOST_NOT_SUPPLIED", "HOST_UNAVAILABLE", "PARTIAL_UNAVAILABLE"].includes(
      root.reason as string,
    ) ||
    summary.drifted !== 0 ||
    summary.unavailable === 0 ||
    (root.reason === "PARTIAL_UNAVAILABLE") !== (root.subject !== null) ||
    (root.reason !== "PARTIAL_UNAVAILABLE" && summary.unavailable !== 29)
  )
    throw new Error("NOT_RUN evidence is incomplete or inconsistent");
}
