from tests.vision_comparison_plan import build_blind_comparison_plan


def test_canonical_blind_plan_excludes_reclassified_case04():
    plan = build_blind_comparison_plan()

    assert len(plan) == 10
    assert {item.fixture_id for item in plan} == {"case_03", "case_05"}
    assert sum(item.layout_variant == "legacy" for item in plan) == 4
    assert sum(item.layout_variant == "v2" and item.repetition == 1 for item in plan) == 4
    assert sum(item.layout_variant == "v2" and item.repetition == 2 for item in plan) == 2

    for case_id in ("case_03", "case_05"):
        for agent in ("A", "B"):
            old = next(
                item
                for item in plan
                if item.layout_variant == "legacy"
                and item.fixture_id == case_id
                and item.agent == agent
            )
            new = next(
                item
                for item in plan
                if item.layout_variant == "v2"
                and item.fixture_id == case_id
                and item.agent == agent
                and item.repetition == 1
            )
            assert old.provider_input.md_sha256 == new.provider_input.md_sha256
            assert case_id not in old.provider_input.prompt
            assert case_id not in new.provider_input.prompt
            assert "score_key" not in old.provider_input.__dataclass_fields__

    for agent in ("A", "B"):
        first = next(
            item
            for item in plan
            if item.layout_variant == "v2"
            and item.fixture_id == "case_05"
            and item.agent == agent
            and item.repetition == 1
        )
        repeat = next(
            item
            for item in plan
            if item.layout_variant == "v2"
            and item.fixture_id == "case_05"
            and item.agent == agent
            and item.repetition == 2
        )
        assert first.provider_input.image_sha256 == repeat.provider_input.image_sha256
        assert first.provider_input.prompt == repeat.provider_input.prompt
        assert first.provider_input.md_sha256 == repeat.provider_input.md_sha256
