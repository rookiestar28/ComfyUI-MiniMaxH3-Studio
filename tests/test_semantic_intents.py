"""Typed semantic boundaries preserve renderer structure and caller-owned dialogue."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from test_semantic_proposal_execution import model_result
from test_semantic_proposal_producer import _provider_bundle, _semantic_document

from comfyui_h3_context.core import (
    AudioIntent,
    AudioLayer,
    HardConstraintSet,
    IntentScene,
    TaskMode,
    TimelineSegment,
    TimePoint,
    build_intent_graph,
    render_base_prompt,
)
from comfyui_h3_context.core.constrained_semantic_planning import (
    TYPED_SEMANTIC_PLANNING_SCHEMA,
    SemanticPlanningStatus,
    parse_semantic_proposal,
)
from comfyui_h3_context.core.constraints import DialogueSpeaker, ExactTextConstraint, ExactTextKind
from comfyui_h3_context.core.semantic_intents import (
    DialogueSemanticBinding,
    SemanticIntentError,
    SemanticIntentKind,
    TypedSemanticIntent,
    decode_typed_semantic_intent,
    validate_semantic_dialogue_bindings,
)
from comfyui_h3_context.core.semantic_proposal_producer import (
    SemanticSourceBundle,
    _assemble_semantic_proposal_product,
    _build_enrichment_proposals,
    _build_semantic_ollama_generation_request,
    build_semantic_source_bundle,
)
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextNativeH3AdapterNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
)


def dialogue_source() -> SemanticSourceBundle:
    constraints = HardConstraintSet(
        (
            ExactTextConstraint(
                "line.zh",
                ExactTextKind.DIALOGUE,
                "你好！",
                "Chinese",
                speakers=(DialogueSpeaker("speaker.one"),),
            ),
            ExactTextConstraint(
                "line.en",
                ExactTextKind.DIALOGUE,
                "Hello!",
                "English",
                speakers=(DialogueSpeaker("speaker.two"),),
            ),
        )
    )
    raw = replace(
        H3ContextRequestNode().build_request(
            TaskMode.T2VA, "A quiet dialogue.", duration_seconds=5.0
        )[0],
        hard_constraints=constraints,
    )
    duration = TimePoint.from_text("5.166666666666667")
    graph = build_intent_graph(
        effective_duration=duration,
        registry=raw.reference_registry,
        scenes=(IntentScene("scene_1", "A quiet studio."),),
        audios=(AudioIntent("audio.dialogue", AudioLayer.DIALOGUE, "A natural conversation."),),
        segments=(
            TimelineSegment(
                "segment_1",
                TimePoint.from_text("0"),
                duration,
                scene_id="scene_1",
                audio_ids=("audio.dialogue",),
            ),
        ),
    ).graph
    assert graph is not None
    plan = H3ContextPlanNode().build_plan(raw, intent_graph=graph)[0]
    document = H3ContextCompilerNode().compile(plan)[2]
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = H3ContextNativeH3AdapterNode().adapt(report)[1]
    return build_semantic_source_bundle(report, wiring, _provider_bundle().profile)


class SemanticIntentTests(unittest.TestCase):
    def test_source_snapshots_and_renderer_preserve_approved_multilingual_dialogue(self) -> None:
        source = dialogue_source()
        accepted = source.baseline_plan.hard_constraints.exact_texts
        self.assertEqual(
            tuple(item.text for item in source.planning_request.protected_exact_text),
            tuple(item.text for item in accepted),
        )
        payload = json.loads(_semantic_document(source, typed_intents=True))
        payload["proposals"][0].update(
            proposal_id="proposal.audio", target_kind="audio", target_id="audio.dialogue"
        )
        payload["proposals"][0]["intent"] = {
            "kind": "audio",
            "description": "Gentle, unhurried speech with natural pauses.",
            "dialogue_bindings": [
                DialogueSemanticBinding(
                    item.constraint_id, item.language, (item.speakers[0].key,)
                ).to_wire()
                for item in accepted
            ],
        }
        result = _assemble_semantic_proposal_product(
            source,
            model_result(source, json.dumps(payload)),
            "sha256:" + "9" * 64,
            typed_intents=True,
        )
        contract = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "governance/contracts/constrained_semantic_planning_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(contract).validate(result.planning_result.to_wire())
        after = result.enrichment_result.after_plan
        assert after is not None
        self.assertEqual(after.hard_constraints, source.baseline_plan.hard_constraints)
        self.assertEqual(after.intent_graph.segments, source.baseline_plan.intent_graph.segments)
        before_text = render_base_prompt(source.baseline_plan).text
        after_text = render_base_prompt(after).text
        for item in accepted:
            self.assertIn(item.text, before_text)
            self.assertIn(item.text, after_text)
            self.assertIn("[" + cast(str, item.language) + "]", after_text)
        self.assertIn("Gentle, unhurried speech", after_text)
        self.assertIn("(S1)", after_text)
        self.assertIn("(S2)", after_text)
        # The provider-free parser cannot authorize binding fields from output alone.
        standalone = parse_semantic_proposal(source.planning_request, json.dumps(payload))
        self.assertIsNot(standalone.status, SemanticPlanningStatus.COMPLETE)
        self.assertIn("dialogue_binding_mutation", standalone.receipt.diagnostics)
        mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda value: value["proposals"][0]["intent"]["dialogue_bindings"][0].update(
                language="English"
            ),
            lambda value: value["proposals"][0]["intent"]["dialogue_bindings"][0].update(
                speaker_keys=["invented.speaker"]
            ),
            lambda value: value["preserved_exact_text"][0].update(text="Changed dialogue"),
        )
        for mutate in mutations:
            changed = json.loads(json.dumps(payload))
            mutate(changed)
            refused = parse_semantic_proposal(
                source.planning_request, json.dumps(changed), accepted_exact_text=accepted
            )
            self.assertIsNot(refused.status, SemanticPlanningStatus.COMPLETE)
            self.assertIsNone(refused.document)

    def test_v2_uses_source_guards_and_refuses_mixed_versions_or_renderer_control_text(
        self,
    ) -> None:
        source = _provider_bundle()
        raw = json.loads(_semantic_document(source))
        raw["schema"] = TYPED_SEMANTIC_PLANNING_SCHEMA
        proposal = raw["proposals"][0]
        proposal["schema"] = TYPED_SEMANTIC_PLANNING_SCHEMA
        proposal["intent"] = {
            "kind": "scene",
            "description": proposal.pop("claim"),
            "dialogue_bindings": [],
        }
        result = parse_semantic_proposal(source.planning_request, json.dumps(raw))
        self.assertIs(result.status, SemanticPlanningStatus.COMPLETE)
        self.assertEqual(
            _build_enrichment_proposals(source, result)[0].text,
            "Soft daylight clarifies the quiet studio.",
        )
        self.assertIsNotNone(result.document)
        mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda value: value["proposals"][0]["intent"].update(kind="audio"),
            lambda value: value["proposals"][0]["intent"].update(description="<d>injected</d>"),
            lambda value: value["proposals"][0].update(claim="smuggled v1 field"),
            lambda value: value.update(schema="h3.constrained_semantic_planning.v1"),
            lambda value: value.update(effective_duration="1"),
            lambda value: value.update(complete=False),
        )
        for mutate in mutations:
            changed = json.loads(json.dumps(raw))
            mutate(changed)
            refused = parse_semantic_proposal(source.planning_request, json.dumps(changed))
            self.assertIsNot(refused.status, SemanticPlanningStatus.COMPLETE)
            self.assertIsNone(refused.document)

    def test_v2_refuses_duplicate_targets_and_temporal_or_keep_fields(self) -> None:
        source = _provider_bundle()
        raw = json.loads(_semantic_document(source, typed_intents=True))
        duplicated = json.loads(json.dumps(raw))
        duplicated["proposals"].append(
            {**duplicated["proposals"][0], "proposal_id": "proposal.other"}
        )
        result = parse_semantic_proposal(source.planning_request, json.dumps(duplicated))
        self.assertIsNot(result.status, SemanticPlanningStatus.COMPLETE)
        self.assertIsNone(result.document)
        for field, value in (("start", "0"), ("end", "1"), ("keep", False), ("event_ids", [])):
            changed = json.loads(json.dumps(raw))
            changed["proposals"][0][field] = value
            refused = parse_semantic_proposal(source.planning_request, json.dumps(changed))
            self.assertIsNot(refused.status, SemanticPlanningStatus.COMPLETE)
            self.assertIsNone(refused.document)

    def test_provider_v2_hint_has_closed_intent_fields_and_keeps_immutable_source_facts(
        self,
    ) -> None:
        source = _provider_bundle()
        request = _build_semantic_ollama_generation_request(source, typed_intents=True)
        schema = cast(dict[str, Any], request.structured_schema)
        properties = schema["properties"]
        self.assertEqual(properties["schema"]["const"], TYPED_SEMANTIC_PLANNING_SCHEMA)
        self.assertEqual(
            properties["effective_duration"]["const"],
            source.planning_request.timeline_plan.effective_duration.raw,
        )
        proposal = properties["proposals"]["items"]
        self.assertNotIn("claim", proposal["properties"])
        intent = proposal["properties"]["intent"]
        self.assertFalse(intent["additionalProperties"])
        self.assertEqual(set(intent["required"]), {"kind", "description", "dialogue_bindings"})
        self.assertEqual(intent["properties"]["dialogue_bindings"]["maxItems"], 0)

    def test_every_supported_kind_round_trips_through_a_closed_shape(self) -> None:
        for kind in SemanticIntentKind:
            intent = TypedSemanticIntent(kind, "柔和的自然光")
            self.assertEqual(decode_typed_semantic_intent(intent.to_wire()), intent)
        intent = TypedSemanticIntent(SemanticIntentKind.CAMERA, "A static medium shot")
        for payload in (
            {**intent.to_wire(), "prompt_text": "extra"},
            {**intent.to_wire(), "kind": []},
            {**intent.to_wire(), "dialogue_bindings": "not an array"},
            {"kind": "camera", "description": "text"},
        ):
            with self.assertRaises(SemanticIntentError):
                decode_typed_semantic_intent(payload)

    def test_descriptions_cannot_author_renderer_markup_or_unsafe_wire_text(self) -> None:
        for text in (
            "<d>invented line</d>",
            "[Audio] invented block",
            "line\nnew block",
            "line\u2028new block",
            "line\u0085new block",
            "unsafe\0value",
            "unsafe\ud800value",
            " ",
        ):
            with self.subTest(text=repr(text)), self.assertRaises(SemanticIntentError):
                TypedSemanticIntent(SemanticIntentKind.AUDIO, text)

    def test_dialogue_references_exact_approved_language_and_ordered_speakers(self) -> None:
        constraint = ExactTextConstraint(
            "line.one",
            ExactTextKind.DIALOGUE,
            "你好",
            "Mandarin",
            speakers=(DialogueSpeaker("speaker.one"), DialogueSpeaker("speaker.two")),
        )
        binding = DialogueSemanticBinding(
            constraint.constraint_id, constraint.language, ("speaker.one", "speaker.two")
        )
        intent = TypedSemanticIntent(
            SemanticIntentKind.AUDIO, "Natural spoken dialogue", (binding,)
        )
        validate_semantic_dialogue_bindings(intent, (constraint,))
        self.assertEqual(decode_typed_semantic_intent(intent.to_wire()), intent)
        for mutation in (
            replace(binding, constraint_id="other.line"),
            replace(binding, language="English"),
            replace(binding, speaker_keys=("speaker.two", "speaker.one")),
            replace(binding, speaker_keys=("invented.speaker",)),
        ):
            with self.assertRaises(SemanticIntentError):
                validate_semantic_dialogue_bindings(
                    replace(intent, dialogue_bindings=(mutation,)), (constraint,)
                )
        with self.assertRaises(SemanticIntentError):
            replace(intent, kind=SemanticIntentKind.ACTION)
        with self.assertRaises(SemanticIntentError):
            replace(intent, dialogue_bindings=(binding, binding))
        with self.assertRaises(SemanticIntentError):
            decode_typed_semantic_intent(
                {
                    **intent.to_wire(),
                    "dialogue_bindings": [{**binding.to_wire(), "text": "changed caller text"}],
                }
            )

    def test_anonymous_lines_remain_distinct_and_cannot_borrow_a_named_speaker(self) -> None:
        first = ExactTextConstraint("line.one", ExactTextKind.DIALOGUE, "First")
        second = ExactTextConstraint("line.two", ExactTextKind.DIALOGUE, "Second")
        binding = DialogueSemanticBinding("line.one", None, ("@anonymous:line.one",))
        intent = TypedSemanticIntent(SemanticIntentKind.AUDIO, "Quiet conversation", (binding,))
        validate_semantic_dialogue_bindings(intent, (first, second))
        with self.assertRaises(SemanticIntentError):
            validate_semantic_dialogue_bindings(
                replace(intent, dialogue_bindings=(replace(binding, constraint_id="line.two"),)),
                (first, second),
            )
