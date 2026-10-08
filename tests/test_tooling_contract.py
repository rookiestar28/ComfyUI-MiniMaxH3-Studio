"""Standard development and acceptance tooling contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path

import tomli
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ToolingContractTests(unittest.TestCase):
    def test_host_runtime_is_not_a_mandatory_dependency_and_dev_tools_are_bounded(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        metadata = tomli.loads(text)
        # CRITICAL: ComfyUI owns its frontend runtime; a Requires-Dist entry lets pip replace it.
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertEqual(
            metadata["project"]["optional-dependencies"]["host-tests"], ["aiohttp>=3.11,<4"]
        )
        self.assertIn("[project.optional-dependencies]", text)
        for package, bound in {
            "numpy": ">=2.2,<3",
            "ruff": ">=0.16,<0.17",
            "mypy": ">=2.3,<2.4",
            "pytest": ">=9.1,<9.2",
            "pytest-cov": ">=7.1,<7.2",
            "pre-commit": ">=4.6,<4.7",
            "detect-secrets": ">=1.5,<1.6",
            "build": ">=1.5,<1.6",
            "wheel": ">=0.46.2,<0.47",
            "tomli": ">=2.4,<2.5",
        }.items():
            self.assertRegex(text, rf"['\"]{re.escape(package)}{re.escape(bound)}['\"]")

    def test_tool_configuration_excludes_internal_and_reference_trees(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        pre_commit = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        for excluded in (".planning", "reference", ".venv-wsl", "dist"):
            self.assertIn(excluded, pyproject)
            self.assertIn(excluded, pre_commit)

    def test_pre_commit_hooks_are_immutable_and_complete(self) -> None:
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        revisions = re.findall(r"(?m)^\s+rev:\s*([0-9a-f]{40})\s*(?:#.*)?$", text)
        self.assertGreaterEqual(len(revisions), 4)
        self.assertEqual(len(revisions), len(set(revisions)))
        for repository in (
            "astral-sh/ruff-pre-commit",
            "pre-commit/mirrors-mypy",
            "Yelp/detect-secrets",
            "pre-commit/pre-commit-hooks",
        ):
            self.assertIn(repository, text)

    def test_standard_runners_share_one_bounded_stage_set(self) -> None:
        windows = (ROOT / "scripts/run_full_tests_windows.ps1").read_text(encoding="utf-8")
        linux = (ROOT / "scripts/run_full_tests_linux.sh").read_text(encoding="utf-8")
        for stage in (
            "workspace link guard",
            "shipped artifact integrity",
            "pre-commit once (includes secret scan, lint, format, and typing)",
            "package import",
            "backend product tests",
            "security audit",
            "frontend static contract",
            "frontend unit tests",
            "frontend hermetic smoke",
            "FULL GATE: PASS",
        ):
            self.assertIn(stage, windows)
            self.assertIn(stage, linux)
        for removed_mechanism in (
            "acceptance_integrity.py",
            "change_impact.py",
            "governance_contract_audit.py",
            "full_gate.py",
            "checkpoint-record",
            "I0",
            "R0",
        ):
            self.assertNotIn(removed_mechanism, windows)
            self.assertNotIn(removed_mechanism, linux)
        self.assertNotIn("git status --porcelain", windows)
        self.assertNotIn("git status --porcelain", linux)
        self.assertNotIn("H3_CONTEXT_HOST_ROOT", windows)
        self.assertNotIn("H3_CONTEXT_HOST_ROOT", linux)
        self.assertIn("browser-smoke", windows)
        self.assertIn("browser-smoke", linux)

    def test_mypy_handles_the_custom_node_loader_root_entrypoint(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("explicit_package_bases = true", pyproject)
        self.assertIn('mypy_path = ["tests"]', pyproject)

    def test_secret_baseline_is_valid_and_closed_to_reviewed_public_literals(self) -> None:
        baseline = json.loads(
            (ROOT / "requirements/secret-scan-baseline.json").read_text(encoding="utf-8")
        )
        self.assertEqual(baseline.get("version"), "1.5.0")
        self.assertEqual(
            baseline.get("plugins_used"),
            [
                {"name": "ArtifactoryDetector"},
                {"name": "AWSKeyDetector"},
                {"name": "AzureStorageKeyDetector"},
                {"name": "Base64HighEntropyString", "limit": 4.5},
                {"name": "BasicAuthDetector"},
                {"name": "CloudantDetector"},
                {"name": "DiscordBotTokenDetector"},
                {"name": "GitHubTokenDetector"},
                {"name": "GitLabTokenDetector"},
                {"name": "HexHighEntropyString", "limit": 3.0},
                {"name": "IbmCloudIamDetector"},
                {"name": "IbmCosHmacDetector"},
                {"name": "IPPublicDetector"},
                {"name": "JwtTokenDetector"},
                {"name": "KeywordDetector", "keyword_exclude": ""},
                {"name": "MailchimpDetector"},
                {"name": "NpmDetector"},
                {"name": "OpenAIDetector"},
                {"name": "PrivateKeyDetector"},
                {"name": "PypiTokenDetector"},
                {"name": "SendGridDetector"},
                {"name": "SlackDetector"},
                {"name": "SoftlayerDetector"},
                {"name": "SquareOAuthDetector"},
                {"name": "StripeDetector"},
                {"name": "TelegramBotTokenDetector"},
                {"name": "TwilioKeyDetector"},
            ],
        )
        historical_blob = "22bc91cd45-70a071c664-ec4848a5be-748909e1e7".replace("-", "")
        historical_backend = "c44dea1880-9e3ca0e12e-25cbe6938c-2c45a29c9d".replace("-", "")
        historical_frontend = "f339c6b2ec-0cc90e1c4f-a717d9f7d6-d565a8f6da".replace("-", "")
        self.assertEqual(
            baseline.get("filters_used"),
            [
                {"path": "detect_secrets.filters.allowlist.is_line_allowlisted"},
                {
                    "path": "detect_secrets.filters.common.is_baseline_file",
                    "filename": "requirements/secret-scan-baseline.json",
                },
                {
                    "path": (
                        "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
                    ),
                    "min_level": 2,
                },
                {"path": "detect_secrets.filters.heuristic.is_indirect_reference"},
                {"path": "detect_secrets.filters.heuristic.is_likely_id_string"},
                {"path": "detect_secrets.filters.heuristic.is_lock_file"},
                {"path": "detect_secrets.filters.heuristic.is_not_alphanumeric_string"},
                {"path": "detect_secrets.filters.heuristic.is_potential_uuid"},
                {"path": "detect_secrets.filters.heuristic.is_prefixed_with_dollar_sign"},
                {"path": "detect_secrets.filters.heuristic.is_sequential_string"},
                {"path": "detect_secrets.filters.heuristic.is_swagger_file"},
                {"path": "detect_secrets.filters.heuristic.is_templated_secret"},
                {
                    "path": "detect_secrets.filters.regex.should_exclude_file",
                    "pattern": [
                        r"(?:(^|[\\/])(\.planning|reference|\.venv-wsl|build|dist)"
                        r"([\\/]|$)|^frontend[\\/]pnpm-lock\.yaml$)"
                    ],
                },
                {
                    "path": "detect_secrets.filters.regex.should_exclude_secret",
                    "pattern": [
                        f"^(?:{historical_blob}|{historical_backend}|{historical_frontend})$"
                    ],
                },
            ],
        )
        backend_revision = "b323a345bb-bfb2f3a95b-5b73b68eb7-919a26515e".replace("-", "")
        current_backend_revision = "8cfe5e1ecb-97512dea8d-eaac15e122-8d7e6feeb1".replace("-", "")
        frontend_revision = "6d6af63c00-f132cd25dc-29307fc56b-d2c094fa22".replace("-", "")
        native_blob = "0b1840e851-c248f89e99-20159c3c82-37fa2e7186".replace("-", "")
        # M18-05's rollback target: the accepted M18-04 checkpoint, whose tree still contains every
        # schema that item retired. A 40-character object name reads as high-entropy hex, but it is
        # the opposite of a secret -- it is a public commit in this repository's own history, and
        # the retirement record is useless without it. Written split for the same reason the three
        # revisions above are: so this file holds no bare high-entropy literal of its own.
        rollback_commit = "aea047c471-e3cc87654f-c67e64c6b9-db6f216cfd".replace("-", "")
        # M18-06's closeout matrix names the chain's first and last accepted checkpoints as the
        # commits its baseline and final numbers were read from. Same reasoning as the rollback
        # target above: public object names in this repository's own history, useless to redact,
        # and split here so this file still holds no bare high-entropy literal.
        chain_first = "add8a535bc-a3769a9129-1fb31012b7-447e1682b8".replace("-", "")
        chain_last = "48c0bcb232-65827f77be-59b334d27e-ffe9199844".replace("-", "")
        # M19-06's closeout matrix reads each of its nineteen rows out of the artifact the
        # checkpoint itself committed, so it has to name all five M19 checkpoints rather than only
        # the first and last. Same reasoning as every commit id above -- public object names in this
        # repository's own history, useless to redact, and split so this file holds no bare literal.
        m19_01 = "2fea0df902-5e639e4b2c-6bf66ba65d-081eee0d4e".replace("-", "")
        m19_02 = "f3742ae66c-9c64b27a8f-8d466c8a18-09c90353af".replace("-", "")
        m19_03 = "e0d897915f-a6c4f9dadd-18854f204d-2031c2afbd".replace("-", "")
        m19_04 = "9da959fc6d-e7d27ba1d2-89c74b64a4-f45832fa5a".replace("-", "")
        m19_05 = "ab4c3cc0c1-e149509ac2-d821fa4533-664273a2b7".replace("-", "")
        # M22-15's reduced live qualification evidence binds the exact pre-promotion Git commit
        # and tree. They are public repository object names, not credentials; the JSON catalog
        # cannot carry an inline scanner allowlist, so these are its only two approved findings.
        m22_15_commit = "bf67ff1ad1-e454452ec1-da912a3b20-026cd14c72".replace("-", "")
        m22_15_tree = "90d9bea824-934221df1a-0ece97d2e1-5d670df56f".replace("-", "")
        # M22-18 promoted the OpenAI row on a reachability basis, and its evidence binds the same
        # two object names for its own candidate. Same reasoning, same file, one entry each.
        m22_18_commit = "cc88820a89-68b34e074d-1b6e9f27e5-746834cc38".replace("-", "")
        m22_18_tree = "1f938ca4d1-ecd39711af-226bede26a-6b7f1cabb9".replace("-", "")
        # M22-21 promoted the Gemini row on a completion basis and binds its own candidate.
        m22_21_commit = "32bfa79a77-ed43fc5bce-fa0fce664e-cc2ced2284".replace("-", "")
        m22_21_tree = "8c2f9a9a2a-ebcdda83bc-f2a44f5336-98bb5c975c".replace("-", "")
        # M23-24's generated provenance record binds the public source commit and tree used for
        # the deterministic frontend build. The generated JSON cannot carry inline allowlisting.
        # CRITICAL: read these from the record rather than pinning the literals. Pinned literals
        # made a correct revision refresh turn this test red, so the record's base could only stay
        # correct by never being updated -- the pin defended the drift it was supposed to catch.
        # The count assertion below is what still guards; this pair only has to name the two
        # objects the record currently carries.
        provenance_identity = json.loads(
            (ROOT / "comfyui_h3_context" / "contracts" / "build_provenance_v1.json").read_text(
                encoding="utf-8"
            )
        )["external_parameters"]
        m23_24_commit = provenance_identity["source_commit"]
        m23_24_tree = provenance_identity["source_tree"]
        # Keep the six audited public font object names closed and paired with the baseline;
        # accepting every high-entropy manifest value would also approve unrelated new findings.
        font_revisions = {
            "font_build": "c4a321e123-e4d4ff315f-57f4e0adf2-94fe3a95be".replace("-", ""),
            "font_distribution": "7eb462dbfc-5fe79b18ea-35e7f1773f-9495b8a1c6".replace("-", ""),
            "font_regular": "f27f4ff595-62d58480f1-cb94194393-484b8da9e9".replace("-", ""),
            "font_italic": "7f53133343-8b401f18ca-96db911899-54356beb65".replace("-", ""),
            "font_bold": "aae7546dc1-905b228aff-70cde8c818-b82f3a2bc4".replace("-", ""),
            "font_bold_italic": "6f685b2e5e-0df0077ea3-96ba6384d4-6c8e96d9e9".replace("-", ""),
        }
        # CRITICAL: pin reviewed public model/asset digests independently of the source and
        # baseline. Reading either to generate approvals would silently approve new credentials.
        # Qwen uses the public model tag digest; Whisper uses the published weight and processor
        # asset SHA256 values. They bind integrity/identity, never authentication.
        perception_digests = {
            "qwen_model": (
                "25b843619e-944cd0ae60-69f94ff4e5-e26a16e109-ccbc0a66a0-f05979ed70-098e".replace(
                    "-", ""
                )
            ),
            "whisper_weight": (
                "a8e94b8597-6e5864ba3e-9525c7e6c8-3b2a1eca42-d4b797a0c7-c24d778e40-fd95".replace(
                    "-", ""
                )
            ),
            "whisper_config": (
                "ad0e8d1e46-f4d01f7861-a21509e5d0-f977d6cc1f-367a370603-c92541d819-807b".replace(
                    "-", ""
                )
            ),
            "whisper_generation_config": (
                "fbdfa70135-de9b1d3155-3393f14e80-aaeb1936ea-36576b2ba8-64055943c0-9d23".replace(
                    "-", ""
                )
            ),
            "whisper_preprocessor_config": (
                "7ccc62c6f2-765af1f3b4-6c00c9b589-4426835a05-021c8b9c01-eecb6dfb54-2711".replace(
                    "-", ""
                )
            ),
            "whisper_tokenizer": (
                "6d8cbd7cd0-d8d5815e47-8dac67b85a-26bbe77c1f-5e0c6d76d1-ce2abc0e5f-21ca".replace(
                    "-", ""
                )
            ),
            "whisper_tokenizer_config": (
                "844b642c73-a91359722f-47b35705f7-174686df33-d252695d85-72cf9ac03a-6389".replace(
                    "-", ""
                )
            ),
            "whisper_special_tokens": (
                "1c70773c07-8cb2ca96e0-fcff113102-f1d3e2b150-4272c3bb63-b035d4a670-0d87".replace(
                    "-", ""
                )
            ),
            "whisper_vocab": (
                "e2aa043ef0-15641d363d-8288e7c241-c85e36a5c7-61fb303598-e071023334-4387".replace(
                    "-", ""
                )
            ),
            "whisper_merges": (
                "2df2990a39-5e35e8dfbc-7511e08c12-d56018d8d0-4691e0133e-5d63b21e15-4dc6".replace(
                    "-", ""
                )
            ),
            "whisper_normalizer": (
                "bf1c507dc8-724ca9cf99-03640dacfb-69dae2f00e-dee4f21ceb-a106a7392f-26dd".replace(
                    "-", ""
                )
            ),
            "whisper_added_tokens": (
                "3c51f66c4c-21f9e12697-0078f11ae7-7a78c74aee-8df606ee9d-aba86e4671-08e0".replace(
                    "-", ""
                )
            ),
        }
        # CRITICAL: one place that knows what each approved row is *for*. Before M23-49 these
        # literals were named once, hashed, and never referred to again, so nothing in this test --
        # or anywhere else -- could tell that a row had gone inert. Seven had, across four files,
        # from `5978836` onward. Keep `public_hashes` derived from this mapping rather than written
        # out beside it; two hand-kept lists of the same eighteen things is how the omission
        # happened.
        approved_literals = {
            **font_revisions,
            **perception_digests,
            "backend": backend_revision,
            "current_backend": current_backend_revision,
            "chain_first": chain_first,
            "chain_last": chain_last,
            "frontend": frontend_revision,
            "m19_01": m19_01,
            "m19_02": m19_02,
            "m19_03": m19_03,
            "m19_04": m19_04,
            "m19_05": m19_05,
            "m22_15_commit": m22_15_commit,
            "m22_15_tree": m22_15_tree,
            "m22_18_commit": m22_18_commit,
            "m22_18_tree": m22_18_tree,
            "m22_21_commit": m22_21_commit,
            "m22_21_tree": m22_21_tree,
            "m23_24_commit": m23_24_commit,
            "m23_24_tree": m23_24_tree,
            "native_blob": native_blob,
            "rollback_commit": rollback_commit,
        }
        public_hashes = {
            identity: hashlib.sha1(literal.encode(), usedforsecurity=False).hexdigest()
            for identity, literal in approved_literals.items()
        }
        approved_paths = {
            **{
                identity: (
                    (
                        "comfyui_h3_context/adapters/perception_worker.py",
                        "comfyui_h3_context/core/perception_execution.py",
                    )
                    if identity in {"qwen_model", "whisper_weight"}
                    else ("comfyui_h3_context/adapters/perception_worker.py",)
                )
                for identity in perception_digests
            },
            **{
                identity: ("comfyui_h3_context/fonts/font_manifest_v1.json",)
                for identity in font_revisions
            },
            # CRITICAL: this is an approved *register*, not a scanner cache. A row may be
            # removed only when the literal it approves has genuinely left the tree, and the
            # removal belongs in the same commit as the baseline edit -- that pairing is the whole
            # audit trail. Never regenerate the baseline to make this census pass: a regenerated
            # baseline is a different document that nobody approved, and M23-51 tried exactly that
            # and was caught here.
            #
            # M23-49 removed seven rows across four files. `5978836` made hosts capability-admitted
            # instead of revision-pinned, so those four stopped carrying any 40-character hex
            # literal at all and their rows could never match again. Verified by scanning each file
            # for the three approved literals rather than by trusting the scanner's own output.
            "backend": (
                "comfyui_h3_context/core/product_shell.py",
                "frontend/tests/projectionCodecs.test.ts",
                "frontend/tests/sidebarHost.test.ts",
                "frontend/tests/sidebarWorkspaceFixture.ts",
                "scripts/m16_03_release_audit.py",
                "tests/fixtures/m15_05_native_mode_matrix.json",
                "tests/test_m16_03_release_audit.py",
            ),
            # IMPORTANT: active host fixtures use the current public revision. Keep the
            # historical golden separate or a correct host refresh fails this closed register.
            "current_backend": (
                "governance/contracts/assisted_authoring_compatibility_v1.json",
                "governance/contracts/assisted_authoring_compatibility_v2.json",
                "comfyui_h3_context/contracts/host_seam_shape_fixture_v1.json",
                "scripts/m22_16_compatibility_matrix.py",
                "tests/test_m22_16_compatibility_closeout.py",
            ),
            "frontend": (
                "comfyui_h3_context/core/product_shell.py",
                "frontend/tests/projectionCodecs.test.ts",
                "frontend/tests/sidebarHost.test.ts",
                "frontend/tests/sidebarWorkspaceFixture.ts",
            ),
            "native_blob": ("tests/fixtures/m15_05_native_mode_matrix.json",),
            # Only the generated record needs the baseline. `scripts/retirement_eligibility.py`
            # carries the same value behind an inline `pragma: allowlist secret`, the way
            # `.pre-commit-config.yaml` already does for the scanner's own pinned revision; a
            # generated JSON contract has nowhere to put a comment.
            "rollback_commit": (
                "governance/contracts/retirement_eligibility_v1.json",
                # M18-06's matrix reads this checkpoint's manifest metrics.
                "governance/contracts/closeout_matrix_v1.json",
            ),
            "chain_first": ("governance/contracts/closeout_matrix_v1.json",),
            "chain_last": ("governance/contracts/closeout_matrix_v1.json",),
            "m19_01": ("governance/contracts/m19_closeout_v1.json",),
            "m19_02": ("governance/contracts/m19_closeout_v1.json",),
            "m19_03": ("governance/contracts/m19_closeout_v1.json",),
            "m19_04": ("governance/contracts/m19_closeout_v1.json",),
            "m19_05": ("governance/contracts/m19_closeout_v1.json",),
            "m22_15_commit": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m22_15_tree": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m22_18_commit": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m22_18_tree": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m22_21_commit": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m22_21_tree": ("comfyui_h3_context/contracts/prompt_model_profiles_v5.json",),
            "m23_24_commit": ("comfyui_h3_context/contracts/build_provenance_v1.json",),
            "m23_24_tree": ("comfyui_h3_context/contracts/build_provenance_v1.json",),
        }
        # CRITICAL: every approved row must still describe something that exists. A row whose
        # literal has left the tree is inert -- it can never match again -- and both sides of the
        # census above would go on agreeing with each other about it forever. This is the assertion
        # that makes the next one fail loudly rather than wait for someone to go looking. When it
        # fires, the fix is to delete the row from `.secrets.baseline` and its entry here **in the
        # same commit**, never to regenerate the baseline: this is an approved register, and a
        # regenerated one is a different document nobody approved.
        for identity, paths in sorted(approved_paths.items()):
            literal = approved_literals[identity]
            for path in paths:
                with self.subTest(identity=identity, path=path):
                    body = (ROOT / path).read_text(encoding="utf-8", errors="replace")
                    self.assertIn(
                        literal,
                        body,
                        f"approved row {identity} names {path}, which no longer contains it; "
                        "remove the row and this entry together",
                    )

        expected = Counter(
            (path, "Hex High Entropy String", public_hashes[identity])
            for identity, paths in approved_paths.items()
            for path in paths
        )
        self.assertEqual(sum(expected.values()), 54)
        # SECURITY: these four matches are the exact public repository owner/name, not
        # credentials. Bind literal, detector and paths; never allow arbitrary Base64 rows.
        repository_name = "rookiestar28/" + "ComfyUI-" + "MiniMaxH3-Studio"
        repository_hash = hashlib.sha1(repository_name.encode(), usedforsecurity=False).hexdigest()
        for path in (
            ".github/workflows/publish.yml",
            "scripts/security_audit.py",
            "tests/test_comfy_registry_metadata.py",
            "tests/test_security_audit.py",
        ):
            with self.subTest(public_repository_path=path):
                self.assertIn(repository_name, (ROOT / path).read_text(encoding="utf-8"))
            expected[(path, "Base64 High Entropy String", repository_hash)] = 1
        # IMPORTANT: this one key-shaped match is encoded MP4 structure, not a credential.
        # Pin the whole normalized fixture, detector and match; never exempt its path broadly.
        fixture_path = "scripts/fixtures/m25_48_192_frame_mp4.b64"
        fixture_text = (ROOT / fixture_path).read_text(encoding="ascii")
        self.assertEqual(
            hashlib.sha256(fixture_text.encode("ascii")).hexdigest(),
            "8178b8bba9-3454f7f4ce-a5b026ec7d-1f8b642aba-650fe72d4c-5752a2d421-494f".replace(
                "-", ""
            ),
        )
        fixture_match = "ad8649af30-91cf8af0f2-c679b92a47-b82287b425".replace("-", "")
        expected[(fixture_path, "AWS Access Key", fixture_match)] = 1
        observed: Counter[tuple[str, str, str]] = Counter()
        for path, findings in baseline.get("results", {}).items():
            normalized_path = path.replace("\\", "/")
            for finding in findings:
                self.assertFalse(finding.get("is_verified"))
                self.assertGreater(finding.get("line_number", 0), 0)
                self.assertEqual(finding.get("filename", "").replace("\\", "/"), normalized_path)
                observed[
                    (normalized_path, finding.get("type", ""), finding.get("hashed_secret", ""))
                ] += 1
        self.assertEqual(sum(observed.values()), 59)
        self.assertEqual(observed, expected)

    def test_hosted_ci_uses_separate_public_lanes(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for lane in ("import-smoke:", "quality:", "backend:", "frontend:", "browser:"):
            self.assertIn(lane, workflow)
        for command in (
            'python -c "import comfyui_h3_context"',
            "python scripts/acceptance_baseline.py validate",
            "python scripts/supply_chain_manifest.py --emit validate",
            "python -m pytest --cov=comfyui_h3_context",
            "python scripts/security_audit.py",
            "pnpm --dir frontend run test",
            "pnpm --dir frontend run build",
            "${{ matrix.python }} scripts/browser_ci.py",
        ):
            self.assertIn(command, workflow)
        self.assertNotIn("run_full_tests_windows", workflow)
        self.assertNotIn("run_full_tests_linux", workflow)
        self.assertNotIn("H3_CONTEXT_HOST", workflow)
        self.assertNotIn(".planning/", workflow)
        self.assertNotIn("ROADMAP.md", workflow)
        self.assertNotIn("Classify change impact", workflow)
        self.assertNotIn("D0 governance contract", workflow)
        self.assertNotIn("full_gate.py", workflow)
        self.assertNotIn("acceptance_integrity.py", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@(v\d+|main|master)\b")
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("scripts/acceptance_baseline.py compare", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("Reject pull-request policy relaxation", workflow)
        self.assertIn("Reject policy relaxation on direct push", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("GITHUB_TOKEN", workflow)

    def test_internal_roadmap_is_not_a_public_test_dependency(self) -> None:
        roadmap_tests = (ROOT / "tests/test_roadmap_registry.py").read_text(encoding="utf-8")
        self.assertNotIn("test_repository_index_is_materially_smaller", roadmap_tests)
        self.assertNotIn("test_repository_preserves_pre_cutover_identity", roadmap_tests)

    def test_hermetic_and_supported_host_e2e_are_distinct(self) -> None:
        package = (ROOT / "frontend/package.json").read_text(encoding="utf-8")
        windows = (ROOT / "scripts/run_full_tests_windows.ps1").read_text(encoding="utf-8")
        linux = (ROOT / "scripts/run_full_tests_linux.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("test:e2e:ci", package)
        browser = yaml.safe_load(workflow)["jobs"]["browser"]
        self.assertEqual(
            {row["python"] for row in browser["strategy"]["matrix"]["include"]},
            {".venv/bin/python", ".venv/Scripts/python.exe"},
        )
        commands = [step.get("run", "") for step in browser["steps"]]
        self.assertIn("${{ matrix.python }} scripts/browser_ci.py", commands)
        self.assertFalse(any("playwright.host.config.ts" in command for command in commands))
        for subject in (windows, linux):
            self.assertIn("browser-smoke", subject)
        self.assertIn("playwright.host.config.ts", package)
        self.assertNotIn("H3_CONTEXT_RUN_HERMETIC_E2E", linux)
        self.assertNotIn("RunHermeticE2E", windows)

    def test_every_hermetic_journey_is_collected_by_a_playwright_config(self) -> None:
        # M25-63: the hermetic configs list their journeys one by one, so a new spec that no config
        # names is silently never run, even when passed on the command line. Every top-level
        # journey must be named by a config, except the pinned explicit-opt-in qualifications.
        frontend = ROOT / "frontend"
        configs = "\n".join(
            path.read_text(encoding="utf-8") for path in frontend.glob("playwright*.config.ts")
        )
        explicit_opt_in = {
            # Real-artifact loopback qualification, run only against an explicit fixture target.
            "authoringOutputNative.spec.ts",
        }
        journeys = sorted(path.name for path in (frontend / "tests/e2e/journeys").glob("*.spec.ts"))
        uncollected = [
            name
            for name in journeys
            if f'"journeys/{name}"' not in configs and name not in explicit_opt_in
        ]
        self.assertEqual(uncollected, [])
        self.assertTrue(explicit_opt_in.issubset(journeys))


if __name__ == "__main__":
    unittest.main()
