"""Generate the M18-03 cross-language structural authority and the browser module derived from it.

Browser codecs used to declare the key set of a Python wire by hand.  This generator reads the
Python declaration -- the runtime producer, and the only declaration that exists for every
cross-language wire -- and emits two things from it: a committed record of each shape, and the
TypeScript module the codecs import instead of restating the shape.

The direction was forced by measurement, not chosen by preference.  Only five of the fourteen
cross-language wires have a JSON Schema; all fourteen have a `to_wire`.  Making the schema the
authority would have meant authoring nine new ones, growing the contract surface in the middle of a
chain whose next item retires part of it.

Two rules run through everything.

The extractor **reads modules as data**.  It parses with `ast`, imports nothing and executes
nothing.  A tool that must run product code in order to describe it can be defeated by the code it
is describing.

Every export names **exactly one** authority.  A shape with no authority, or two, fails generation.
And each export's key set is checked against the handwritten list it replaces at the moment the
handwritten list is removed, so the de-duplication cannot quietly change what a codec accepts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.governance.cross_language_surface import (  # noqa: E402
    CrossLanguageSurface,
    CrossLanguageSurfaceError,
    WireShape,
    build_cross_language_surface,
    extract_shape,
)

ARTIFACT_PATH = Path("comfyui_h3_context/contracts/cross_language_surface_v1.json")
GENERATED_TS_PATH = Path("frontend/src/contracts/generatedSurface.ts")
CODEC_DIR = Path("frontend/src/contracts")


class SurfaceGenerationError(RuntimeError):
    """Raised when a declared authority cannot be resolved, or a codec still duplicates a shape."""


@dataclass(frozen=True)
class _Declared:
    """One frozen mapping from a Python authority to the browser export derived from it."""

    module_path: str
    class_name: str
    #: The codec files whose handwritten key list this export replaces.  Recorded so the
    #: duplicate-detection test knows where to look, and so a reader can see the blast radius.
    codecs: tuple[str, ...]


#: The frozen export table. Existing workspace, provider-settings, and composition shapes all
#: derive from their Python `to_wire`; a frontend decoder may refine values but may not restate a
#: shape.
DECLARED: tuple[_Declared, ...] = (
    _Declared(
        "comfyui_h3_context/core/assisted_authoring_scope.py",
        "AssistedAuthoringState",
        ("projectionCodecs.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/generation_profile.py",
        "FamilyProfile",
        ("generationProfileCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/generation_profile.py",
        "GenerationProfile",
        ("generationProfileCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/generation_sequence.py",
        "GenerationSequenceProgress",
        ("generationSequenceCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/generation_sequence.py",
        "GenerationSequenceProjection",
        ("generationSequenceCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/product_shell.py",
        "ProductShellBinding",
        ("projectionCodecs.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/prompt_model_session.py",
        "DiscoveryCandidate",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/prompt_model_provider.py",
        "ModelMetadata",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/prompt_model_provider.py",
        "ModelChoice",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "ProviderConsentView",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "ProviderDiagnostic",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "ProviderIntentResult",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "ProviderProfileView",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "ProviderSettingsProjection",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/provider_settings.py",
        "TransmissionDisclosure",
        ("providerSettingsCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/product_shell.py",
        "ProductShellHostProfile",
        ("projectionCodecs.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/product_shell.py",
        "ProductShellProjection",
        ("projectionCodecs.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/semantic_proposal_review.py",
        "SemanticProposalClarification",
        ("semanticProposalReviewCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/semantic_proposal_review.py",
        "SemanticProposalReviewGroup",
        ("semanticProposalReviewCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/semantic_proposal_review.py",
        "SemanticProposalReviewHandle",
        ("semanticProposalReviewCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/semantic_proposal_review.py",
        "SemanticProposalReviewItem",
        ("semanticProposalReviewCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/semantic_proposal_review.py",
        "SemanticProposalReviewProjection",
        ("semanticProposalReviewCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/segment_workspace.py",
        "SegmentDuration",
        ("generationSequenceCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/sidebar_workspace.py",
        "SidebarWorkspaceProjection",
        ("sidebarWorkspaceCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/transaction_transparency.py",
        "TransactionTransparencyProjection",
        ("transactionTransparencyCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/transaction_transparency.py",
        "TransactionTransparencySnapshot",
        ("transactionTransparencyCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/ui_projection.py",
        "ExecutionCorrelation",
        ("projectionCodecs.ts", "semanticProposalReviewCodec.ts"),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "Rational",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "TimingLandmark",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "PublicAsset",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "CompositionTrack",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "Transform2D",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "Crop",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "TextStyle",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "Transition",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "Effect",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "ClipAudio",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "CompositionClip",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "OutputProfile",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "CapabilityProfile",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "CompositionBlocker",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "AudioExtension",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "RenderVocabulary",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "PublicCompositionSnapshot",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "ResolvedLayer",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "EmbeddedAudioSpan",
        ("compositionCodec.ts",),
    ),
    _Declared(
        "comfyui_h3_context/core/composition_contract.py",
        "ResolvedScene",
        ("compositionCodec.ts",),
    ),
)

#: Key lists that are NOT whole-wire shapes and must stay handwritten, with the reason each one is
#: exempt.  Written down so "why was this one left behind" has an answer that is not "we missed it",
#: and so the duplicate-detection test can distinguish a refinement from a regression.
#: Keyed by `<codec>::<name>`, not by name alone.  Two codecs both declare an `actionKeys`, and
#: they are unrelated -- exempting one by bare name would silently exempt the other, which is
#: exactly the kind of accidental hole this item exists to close.
HANDWRITTEN_REFINEMENTS: dict[str, str] = {
    "authoringOutputCodec.ts::bindingKeys": (
        "a shared sub-selection of output request and status fields identifying the workspace "
        "revision, timeline revision and snapshot fingerprint, not a whole wire shape"
    ),
    "generationSequenceCodec.ts::commandFingerprintKeys": (
        "a sub-selection of one wire, naming which fields take fingerprint-pattern validation"
    ),
    "generationSequenceCodec.ts::commandIdentifierListKeys": (
        "a sub-selection of one wire, naming which fields are identifier lists"
    ),
    "generationSequenceCodec.ts::commandKeys": (
        "a composed shape: GenerationSequenceJob plus the attempt and transaction_id the "
        "carrying projection adds, so no single to_wire declares it"
    ),
    "semanticProposalReviewCodec.ts::resultKeys": (
        "a browser-to-backend request shape; Python parses it and never emits it"
    ),
    "semanticProposalReviewCodec.ts::actionKeys": (
        "a browser-to-backend request shape; Python parses it and never emits it"
    ),
    "semanticProposalReviewCodec.ts::actionReasonKeys": (
        "a browser-to-backend request shape; Python parses it and never emits it"
    ),
    "transactionTransparencyCodec.ts::actionKeys": (
        "an enum of TransactionIntent values, not a wire key set; it shares the array idiom "
        "and nothing else, so it is recorded here rather than narrowing the detector"
    ),
}

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_TS_KEY_LIST = re.compile(
    r"^const (\w*[Kk]eys)(?:\s*:\s*[^=]+?)?\s*=\s*"
    r"(?:\[(?P<array>.*?)\](?:\s*as const)?|\n?\s*\"(?P<joined>[a-z0-9_,]+)\");",
    re.S | re.M,
)


def export_name(class_name: str) -> str:
    """`ProductShellProjection` -> `productShellProjectionKeys`, deterministically."""

    parts = _CAMEL.sub(" ", class_name).split()
    head, *tail = parts
    return head.lower() + "".join(part[0].upper() + part[1:] for part in tail) + "Keys"


def build_surface() -> CrossLanguageSurface:
    """Extract every declared authority, refusing anything the parser could not decide."""

    seen: set[str] = set()
    shapes: list[WireShape] = []
    for declared in DECLARED:
        name = export_name(declared.class_name)
        if name in seen:
            raise SurfaceGenerationError(f"two authorities claim the export {name!r}")
        seen.add(name)
        path = ROOT / declared.module_path
        if not path.is_file():
            raise SurfaceGenerationError(f"authority {declared.module_path} does not exist")
        extracted = extract_shape(path, declared.module_path, declared.class_name)
        shapes.append(
            WireShape(
                export_name=name,
                module_path=declared.module_path,
                class_name=declared.class_name,
                required_keys=extracted.required_keys,
                optional_keys=extracted.optional_keys,
            )
        )
    return build_cross_language_surface(tuple(shapes))


def codec_key_lists() -> dict[str, dict[str, frozenset[str]]]:
    """Read back what each codec still declares by hand, in either idiom it uses."""

    found: dict[str, dict[str, frozenset[str]]] = {}
    for path in sorted((ROOT / CODEC_DIR).glob("*.ts")):
        if path.name == GENERATED_TS_PATH.name:
            continue
        text = path.read_text(encoding="utf-8")
        declared: dict[str, frozenset[str]] = {}
        for match in _TS_KEY_LIST.finditer(text):
            name = match.group(1)
            if match.group("array") is not None:
                declared[name] = frozenset(re.findall(r'"([^"]+)"', match.group("array")))
            else:
                declared[name] = frozenset(match.group("joined").split(","))
        if declared:
            found[path.name] = declared
    return found


#: A comma-joined key set written straight into a call rather than named first.  Two of these were
#: found only after the named constants were removed and the compiler complained about a type -- one
#: of them a THIRD copy of `ExecutionCorrelation` -- so the detector looks for them explicitly. A
#: duplicate that is never given a name is still a duplicate.
_TS_INLINE_KEY_SET = re.compile(r'"([a-z][a-z0-9_]*(?:,[a-z][a-z0-9_]*)+)"')
# IMPORTANT: separators must consume a comma. Repeating optional separators around whitespace
# backtracks exponentially on an unfinished array and stalls every surface/artifact check.
_TS_INLINE_ARRAY = re.compile(
    r'\[\s*("[a-z][a-z0-9_]*"(?:\s*,\s*"[a-z][a-z0-9_]*")*\s*,?)\s*\]', re.S
)
_TS_STRING = re.compile(r'"([a-z][a-z0-9_]*)"')


def _unexempted_source(codec: str, text: str) -> str:
    """The codec's text with its recorded refinements removed, so only unowned sets remain.

    A refinement is exempt wherever it is written, including inside its own array literal, and the
    scan below reads array literals anywhere in the file.  Cutting the exempted declarations out
    first is what keeps the two facts from contradicting each other.
    """

    def drop(match: re.Match[str]) -> str:
        return "" if f"{codec}::{match.group(1)}" in HANDWRITTEN_REFINEMENTS else match.group(0)

    return _TS_KEY_LIST.sub(drop, text)


def duplicate_shapes(surface: CrossLanguageSurface) -> list[str]:
    """Codec key sets that restate a shape the generated module owns -- named, inline or joined.

    All three forms are looked for, because all three were found.  The first pass over named
    ``const ...Keys`` declarations is the obvious one; it missed a key set spelled as a comma-joined
    string, and then missed four more passed straight into ``object(...)`` as an argument literal,
    which the browser-side suite caught. A duplicate is a duplicate whether or not it was given a
    name, so the detector stopped caring how it was written.
    """

    owned = {shape.all_keys: shape.export_name for shape in surface.shapes}
    duplicates: list[str] = []
    for codec, declared in codec_key_lists().items():
        for name, keys in declared.items():
            if f"{codec}::{name}" in HANDWRITTEN_REFINEMENTS:
                continue
            owner = owned.get(tuple(sorted(keys)))
            if owner is not None:
                duplicates.append(f"{codec}::{name} restates {owner}")
    for path in sorted((ROOT / CODEC_DIR).glob("*.ts")):
        if path.name == GENERATED_TS_PATH.name:
            continue
        text = _unexempted_source(path.name, path.read_text(encoding="utf-8"))
        for literal in _TS_INLINE_KEY_SET.findall(text):
            owner = owned.get(tuple(sorted(literal.split(","))))
            if owner is not None:
                duplicates.append(f"{path.name} inlines {owner}")
        for body in _TS_INLINE_ARRAY.findall(text):
            owner = owned.get(tuple(sorted(_TS_STRING.findall(body))))
            if owner is not None:
                duplicates.append(f"{path.name} inlines {owner}")
    return sorted(set(duplicates))


def typescript_module(surface: CrossLanguageSurface) -> str:
    """Render the generated browser module: key sets only, no logic, no imports."""

    lines = [
        "/**",
        " * GENERATED FILE -- DO NOT EDIT.",
        " *",
        " * Regenerate with `python scripts/cross_language_surface.py --write`, which derives",
        " * every key set below from the Python `to_wire` that produces it. The committed",
        " * record is `comfyui_h3_context/contracts/cross_language_surface_v1.json`; a frontend",
        " * test asserts this module reproduces it exactly, so an edit here fails the gate",
        " * rather than shipping.",
        " *",
        " * Key sets only. Every semantic, privacy, lifecycle and identity guard stays",
        " * handwritten in the codec that owns it -- those are refinements this file has no",
        " * authority over.",
        " *",
        f" * Surface fingerprint: {surface.fingerprint}",
        " */",
        "",
    ]
    for shape in surface.shapes:
        lines.append(f"/** {shape.module_path} :: {shape.class_name} */")
        prefix = f"export const {shape.export_name}: readonly string[] = "
        single_line = prefix + "[" + ", ".join(f'"{key}"' for key in shape.all_keys) + "];"
        # IMPORTANT: keep generated bytes at Prettier's default 80-column fixed point. Always
        # expanding short arrays makes generator --check and the frontend static gate rewrite each
        # other's output, so neither can pass on the same candidate.
        if len(single_line) <= 80:
            lines.append(single_line)
        else:
            lines.append(prefix + "[")
            for key in shape.all_keys:
                lines.append(f'  "{key}",')
            lines.append("];")
        lines.append("")
    return "\n".join(lines)


def artifact_bytes(surface: CrossLanguageSurface) -> bytes:
    return (
        json.dumps(surface.to_wire(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate both outputs")
    parser.add_argument("--check", action="store_true", help="fail if either output is stale")
    args = parser.parse_args(argv)
    try:
        surface = build_surface()
    except (CrossLanguageSurfaceError, SurfaceGenerationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1

    expected_artifact = artifact_bytes(surface)
    expected_module = typescript_module(surface)
    artifact = ROOT / ARTIFACT_PATH
    module = ROOT / GENERATED_TS_PATH
    if args.write:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(expected_artifact)
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(expected_module, encoding="utf-8", newline="\n")
    if args.check:
        stale = []
        if (artifact.read_bytes() if artifact.is_file() else b"") != expected_artifact:
            stale.append(ARTIFACT_PATH.as_posix())
        if (module.read_text(encoding="utf-8") if module.is_file() else "") != expected_module:
            stale.append(GENERATED_TS_PATH.as_posix())
        if stale:
            print(json.dumps({"status": "FAIL", "stale": stale}, ensure_ascii=False))
            return 1

    duplicates = duplicate_shapes(surface)
    if duplicates:
        print(json.dumps({"status": "FAIL", "duplicates": duplicates}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                **surface.to_public_dict(),
                "handwritten_refinements": len(HANDWRITTEN_REFINEMENTS),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
