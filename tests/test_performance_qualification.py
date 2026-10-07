from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core.performance_qualification import (
    MAX_PERFORMANCE_PROFILE_WIRE_BYTES,
    CacheObservation,
    MeasurementOwner,
    MetricSource,
    PerformanceObservation,
    PerformanceProfileId,
    PerformanceQualificationError,
    QualificationStatus,
    decode_performance_profiles_json,
    decode_performance_qualification_json,
    default_performance_profiles,
    qualify_performance_profile,
)

FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64
FP_C = "sha256:" + "c" * 64
COMMIT = "1" * 40
CONTRACTS = Path(__file__).parents[1] / "governance" / "contracts"


def _observation(
    latency_ms: float,
    *,
    cache: CacheObservation = CacheObservation.DISABLED,
    python_peak: int | None = 2048,
    rss_peak: int | None = 4096,
    vram_peak: int | None = None,
    vram_owner: MeasurementOwner | None = None,
    output_fingerprint: str = FP_C,
    cancelled: bool = False,
    cancellation_latency_ms: float | None = None,
    cleanup: bool = True,
) -> PerformanceObservation:
    return PerformanceObservation(
        latency_ms=latency_ms,
        output_bytes=512,
        output_items=4,
        python_peak_bytes=python_peak,
        process_rss_peak_bytes=rss_peak,
        vram_peak_bytes=vram_peak,
        vram_owner=vram_owner,
        cache=cache,
        output_fingerprint=output_fingerprint,
        cancelled=cancelled,
        cancellation_latency_ms=cancellation_latency_ms,
        cleanup_verified=cleanup,
    )


def test_default_profiles_are_closed_ordered_and_owner_truthful() -> None:
    profiles = default_performance_profiles()
    assert tuple(profile.profile_id for profile in profiles) == tuple(PerformanceProfileId)
    assert profiles[0].required_sources == (
        MetricSource.PACKAGE_CLOCK,
        MetricSource.PYTHON_TRACED,
        MetricSource.PROCESS_RSS,
    )
    assert profiles[1].budget.max_process_rss_bytes < profiles[0].budget.max_process_rss_bytes
    assert MetricSource.COMFYUI_HOST in profiles[2].required_sources
    assert MetricSource.COMFYUI_HOST in profiles[3].required_sources
    assert profiles[2].vram_owner is MeasurementOwner.COMFYUI_HOST
    assert profiles[3].vram_owner is MeasurementOwner.COMFYUI_HOST
    assert all(profile.budget.repetitions >= 3 for profile in profiles)
    assert all(profile.budget.warmups >= 1 for profile in profiles)
    assert all(
        profile.budget.max_wall_time_ms >= profile.budget.max_latency_ms for profile in profiles
    )
    assert all(profile.budget.max_concurrency == 1 for profile in profiles)
    assert all(profile.budget.max_cache_ttl_seconds == 60 for profile in profiles)


def test_shipped_profile_manifest_and_schema_match_the_pure_contract() -> None:
    raw = (CONTRACTS / "performance_profiles_v1.json").read_bytes()
    schema = json.loads(
        (CONTRACTS / "performance_profiles_v1.schema.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(raw)
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []
    assert decode_performance_profiles_json(raw) == default_performance_profiles()


def test_cpu_qualification_aggregates_real_samples_cache_gain_and_quality() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=5),
    )
    observations = (
        _observation(20, cache=CacheObservation.MISS),
        _observation(18, cache=CacheObservation.MISS),
        _observation(10, cache=CacheObservation.HIT),
        _observation(8, cache=CacheObservation.HIT),
        _observation(9, cache=CacheObservation.HIT),
    )
    result = qualify_performance_profile(
        profile,
        observations,
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert result.status is QualificationStatus.QUALIFIED
    assert result.sample_count == 5
    assert result.median_latency_ms == 10
    assert result.p95_latency_ms == 20
    assert result.worst_latency_ms == 20
    assert result.throughput_per_second == pytest.approx(100)
    assert result.peak_python_bytes == 2048
    assert result.peak_process_rss_bytes == 4096
    assert result.peak_output_bytes == 512
    assert result.peak_output_items == 4
    assert result.cache_misses == 2
    assert result.cache_hits == 3
    assert result.cache_gain_percent == pytest.approx(50)
    assert result.quality_preserved is True
    assert result.cleanup_verified is True
    assert result.to_wire()["candidate_commit"] == COMMIT


def test_gpu_profile_cannot_qualify_without_owner_scoped_vram_observation() -> None:
    profile = replace(
        default_performance_profiles()[2],
        budget=replace(default_performance_profiles()[2].budget, repetitions=3),
    )
    observations = tuple(_observation(value) for value in (10, 11, 12))
    result = qualify_performance_profile(
        profile,
        observations,
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=(MetricSource.PACKAGE_CLOCK, MetricSource.COMFYUI_HOST),
    )
    assert result.status is QualificationStatus.UNAVAILABLE
    assert "vram_observation_missing" in result.diagnostic_codes
    assert result.peak_vram_bytes is None


def test_required_metric_values_and_exact_sample_inventory_fail_closed() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=3),
    )
    missing = qualify_performance_profile(
        profile,
        tuple(_observation(10, python_peak=None) for _ in range(3)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert missing.status is QualificationStatus.UNAVAILABLE
    assert "python_observation_missing" in missing.diagnostic_codes

    extra = qualify_performance_profile(
        profile,
        tuple(_observation(10) for _ in range(4)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert extra.status is QualificationStatus.FAILED
    assert "sample_count_mismatch" in extra.diagnostic_codes


def test_qualification_value_rejects_internally_inconsistent_wire_authority() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=3),
    )
    qualified = qualify_performance_profile(
        profile,
        tuple(_observation(10) for _ in range(3)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    with pytest.raises(PerformanceQualificationError, match="qualified receipt"):
        replace(qualified, sample_count=0)
    with pytest.raises(PerformanceQualificationError, match="cache inventory"):
        replace(qualified, cache_hits=4)


def test_gpu_profile_rejects_wrong_owner_and_accepts_matching_host_observation() -> None:
    profile = replace(
        default_performance_profiles()[2],
        budget=replace(default_performance_profiles()[2].budget, repetitions=3),
    )
    wrong = tuple(
        _observation(
            value,
            vram_peak=1024,
            vram_owner=MeasurementOwner.OLLAMA_EXTERNAL,
        )
        for value in (10, 11, 12)
    )
    with pytest.raises(PerformanceQualificationError, match="VRAM owner"):
        qualify_performance_profile(
            profile,
            wrong,
            candidate_commit=COMMIT,
            workload_fingerprint=FP_A,
            config_fingerprint=FP_B,
            observed_sources=profile.required_sources,
        )
    correct = tuple(
        _observation(value, vram_peak=1024, vram_owner=MeasurementOwner.COMFYUI_HOST)
        for value in (10, 11, 12)
    )
    result = qualify_performance_profile(
        profile,
        correct,
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert result.status is QualificationStatus.QUALIFIED
    assert result.peak_vram_bytes == 1024


def test_quality_drift_budget_overflow_and_cleanup_failure_are_not_qualified() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=3),
    )
    quality = qualify_performance_profile(
        profile,
        (
            _observation(10),
            _observation(10),
            _observation(10, output_fingerprint=FP_B),
        ),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert quality.status is QualificationStatus.FAILED
    assert "quality_fingerprint_drift" in quality.diagnostic_codes

    overflow = qualify_performance_profile(
        replace(profile, budget=replace(profile.budget, max_output_bytes=256)),
        tuple(_observation(10) for _ in range(3)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert overflow.status is QualificationStatus.FAILED
    assert "output_bytes_exceeded" in overflow.diagnostic_codes

    cleanup = qualify_performance_profile(
        profile,
        (_observation(10), _observation(10), _observation(10, cleanup=False)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert cleanup.status is QualificationStatus.FAILED
    assert "cleanup_unverified" in cleanup.diagnostic_codes


def test_cancelled_sample_requires_latency_and_cleanup_and_stays_distinct() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=3),
    )
    with pytest.raises(PerformanceQualificationError, match="cancellation latency"):
        _observation(1, cancelled=True)
    result = qualify_performance_profile(
        profile,
        (
            _observation(10),
            _observation(10),
            _observation(1, cancelled=True, cancellation_latency_ms=1),
        ),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    assert result.status is QualificationStatus.FAILED
    assert "cancelled_sample" in result.diagnostic_codes
    assert result.cancelled_samples == 1


def test_profile_json_decoder_is_exact_strict_utf8_duplicate_and_resource_bounded() -> None:
    profiles = default_performance_profiles()
    wire = {
        "schema": "h3.performance.profiles.v1",
        "profiles": [profile.to_wire() for profile in profiles],
    }
    encoded = json.dumps(wire, separators=(",", ":")).encode()
    assert decode_performance_profiles_json(encoded) == profiles
    assert decode_performance_profiles_json(bytearray(encoded)) == profiles
    assert decode_performance_profiles_json(encoded.decode()) == profiles

    hostile = encoded.decode().replace(
        '"schema":"h3.performance.profiles.v1"',
        '"schema":"h3.performance.profiles.v1","schema":"h3.performance.profiles.v1"',
        1,
    )
    with pytest.raises(PerformanceQualificationError, match="duplicate"):
        decode_performance_profiles_json(hostile)
    with pytest.raises(PerformanceQualificationError, match="UTF-8"):
        decode_performance_profiles_json(b"\xff")
    with pytest.raises(PerformanceQualificationError, match="maximum"):
        decode_performance_profiles_json(b" " * (MAX_PERFORMANCE_PROFILE_WIRE_BYTES + 1))
    with pytest.raises(PerformanceQualificationError, match="exact built-in"):
        decode_performance_profiles_json(type("Spoof", (str,), {})(encoded.decode()))
    unknown = dict(wire, unknown=True)
    with pytest.raises(PerformanceQualificationError, match="members"):
        decode_performance_profiles_json(json.dumps(unknown))
    nonfinite = encoded.decode().replace('"max_latency_ms":', '"extra":NaN,"max_latency_ms":', 1)
    with pytest.raises(PerformanceQualificationError):
        decode_performance_profiles_json(nonfinite)


def test_qualification_json_decoder_round_trips_and_rejects_hostile_wire() -> None:
    profile = replace(
        default_performance_profiles()[0],
        budget=replace(default_performance_profiles()[0].budget, repetitions=3),
    )
    result = qualify_performance_profile(
        profile,
        tuple(_observation(10) for _ in range(3)),
        candidate_commit=COMMIT,
        workload_fingerprint=FP_A,
        config_fingerprint=FP_B,
        observed_sources=profile.required_sources,
    )
    encoded = json.dumps(result.to_wire(), separators=(",", ":")).encode()
    assert decode_performance_qualification_json(encoded) == result
    assert decode_performance_qualification_json(bytearray(encoded)) == result
    assert decode_performance_qualification_json(encoded.decode()) == result
    with pytest.raises(PerformanceQualificationError, match="duplicate"):
        decode_performance_qualification_json(
            encoded.decode().replace(
                '"schema":"h3.performance.qualification.v1"',
                '"schema":"h3.performance.qualification.v1","schema":"h3.performance.qualification.v1"',
            )
        )
    with pytest.raises(PerformanceQualificationError, match="members"):
        decode_performance_qualification_json(
            json.dumps({**result.to_wire(), "raw_prompt": "must not be retained"})
        )
    with pytest.raises(PerformanceQualificationError, match="exact built-in"):
        decode_performance_qualification_json(type("Spoof", (bytes,), {})(encoded))

    schema = json.loads(
        (CONTRACTS / "performance_qualification_v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(result.to_wire())) == []
