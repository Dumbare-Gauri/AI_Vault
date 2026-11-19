import json

import pytest

from vault_shared.ai_gateway.recommendation import (
    MAX_ACTIONS,
    AIResponseRejected,
    RecommendationKind,
    find_disallowed_content,
    neutralize_free_text,
    validate_component_name,
    validate_recommendation,
)

REFS = {"f1", "f2"}


def _validate(recommendations: list[dict], **extra: object):
    return validate_recommendation(
        {"intent": "organize", "recommendations": recommendations, **extra}, allowed_refs=REFS
    )


def _rename(**overrides: object) -> dict:
    return {
        "kind": "rename",
        "target_ref": "f1",
        "proposed_name": "Nike_Spring_Banner_v01.psd",
        "reason": "Matches naming convention",
        "confidence": 0.8,
        **overrides,
    }


class TestWholeResponse:
    def test_a_valid_recommendation_is_accepted(self) -> None:
        result = _validate([_rename()], confidence=0.7, reasoning_summary="Looks like a banner.")

        assert len(result.actions) == 1
        assert result.actions[0].kind is RecommendationKind.RENAME
        assert result.actions[0].proposed_name == "Nike_Spring_Banner_v01.psd"
        assert result.rejected == ()
        assert result.confidence == 0.7

    def test_accepts_a_json_string_wrapped_in_a_markdown_fence(self) -> None:
        raw = "```json\n" + json.dumps({"recommendations": [_rename()]}) + "\n```"

        result = validate_recommendation(raw, allowed_refs=REFS)

        assert len(result.actions) == 1

    @pytest.mark.parametrize("raw", ["not json at all", "[1, 2, 3]", '"a string"', "", "null"])
    def test_unusable_responses_are_rejected_outright(self, raw: str) -> None:
        with pytest.raises(AIResponseRejected):
            validate_recommendation(raw, allowed_refs=REFS)

    def test_recommendations_that_is_not_a_list_is_rejected(self) -> None:
        with pytest.raises(AIResponseRejected):
            validate_recommendation({"recommendations": "rename everything"}, allowed_refs=REFS)

    def test_no_recommendations_is_a_valid_empty_result(self) -> None:
        assert _validate([]).actions == ()


class TestNonAuthoritative:
    def test_confirmation_is_always_required_regardless_of_what_the_model_says(self) -> None:
        result = _validate(
            [_rename(requires_confirmation=False, auto_apply=True)],
            requires_confirmation=False,
            authoritative=True,
        )

        assert result.requires_confirmation is True
        assert result.authoritative is False

    def test_unknown_fields_are_discarded_not_passed_through(self) -> None:
        result = _validate([_rename(shell="rm -rf /", api_key="sk-secret", execute=True)])

        action_fields = set(vars(result.actions[0]))
        assert action_fields.isdisjoint({"shell", "api_key", "execute"})

    @pytest.mark.parametrize(
        "kind",
        [
            "delete",
            "permanent_delete",
            "trash",
            "restore",
            "upload",
            "download",
            "share",
            "change_permissions",
            "run_command",
            "execute",
            "",
            None,
            7,
        ],
    )
    def test_destructive_and_unknown_action_kinds_cannot_be_recommended(self, kind: object) -> None:
        result = _validate([{"kind": kind, "target_ref": "f1", "confidence": 0.9}])

        assert result.actions == ()
        assert len(result.rejected) == 1

    def test_the_recommendation_vocabulary_contains_no_destructive_or_privileged_kind(self) -> None:
        kinds = {kind.value for kind in RecommendationKind}

        assert kinds.isdisjoint(
            {"delete", "permanent_delete", "trash", "restore", "upload", "download", "share"}
        )


class TestRefsAreTheOnlyHandles:
    @pytest.mark.parametrize(
        "target",
        [
            "f3",
            "0b0e1c1e-6a8f-4c1c-9b57-1f1c3f3a0d11",
            "1A2b3C4d5E6f7G8h9I0j",
            "../../etc/passwd",
            "https://drive.google.com/file/d/abc",
            "",
            None,
            {"id": "f1"},
        ],
    )
    def test_a_target_that_was_not_supplied_is_rejected(self, target: object) -> None:
        result = _validate([_rename(target_ref=target)])

        assert result.actions == ()
        assert "target_ref" in result.rejected[0].reason

    def test_a_missing_target_is_rejected_for_targeted_kinds(self) -> None:
        bare = {"kind": "archive", "confidence": 0.5}

        assert _validate([bare]).actions == ()


class TestNamesAreNamesNotPaths:
    @pytest.mark.parametrize(
        "name",
        [
            "../../etc/passwd",
            "a/b.pdf",
            "a\\b.pdf",
            "..",
            ".",
            "C:\\Windows\\system32",
            "name\x00.pdf",
            "line\nbreak.pdf",
            " leading.pdf",
            "trailing.pdf ",
            "trailing.",
            "invoice\u202egpj.exe",
            "zero\u200bwidth.pdf",
            "",
            "   ",
            "x" * 256,
            None,
            123,
            ["a"],
        ],
    )
    def test_unsafe_proposed_names_are_rejected(self, name: object) -> None:
        result = _validate([_rename(proposed_name=name)])

        assert result.actions == ()
        assert len(result.rejected) == 1

    @pytest.mark.parametrize(
        "name",
        [
            "https://evil.example/x.pdf",
            "file://etc/passwd",
            "www.evil.example",
            "a$(curl evil).pdf",
            "a`id`.pdf",
            "x && rm -rf ~",
            "x | sh",
            "x; rm -rf /",
            "'; DROP TABLE files;--",
            "a UNION SELECT password FROM users",
            "x; DELETE FROM users WHERE 1=1",
        ],
    )
    def test_urls_shell_and_sql_are_rejected_in_names(self, name: str) -> None:
        assert find_disallowed_content(name) is not None
        assert _validate([_rename(proposed_name=name)]).actions == ()

    @pytest.mark.parametrize(
        "name",
        ["Nike_Spring_Banner_v01.psd", "Q3 Report (final).pdf", "Résumé — Ada.pdf", "logo@2x.png"],
    )
    def test_ordinary_names_are_accepted(self, name: str) -> None:
        assert validate_component_name(name, what="name") == name

    def test_destination_folder_components_are_validated_individually(self) -> None:
        move = {
            "kind": "move",
            "target_ref": "f1",
            "destination_folder": ["Clients", "Nike", "../../secret"],
            "confidence": 0.7,
        }

        assert _validate([move]).actions == ()

    def test_destination_folder_depth_is_bounded(self) -> None:
        move = {
            "kind": "move",
            "target_ref": "f1",
            "destination_folder": [f"level{i}" for i in range(20)],
            "confidence": 0.7,
        }

        assert _validate([move]).actions == ()

    def test_a_valid_move_keeps_the_folder_path_as_separate_components(self) -> None:
        move = {
            "kind": "move",
            "target_ref": "f1",
            "destination_folder": ["Clients", "Nike"],
            "confidence": 0.7,
        }

        assert _validate([move]).actions[0].destination_folder == ("Clients", "Nike")

    def test_create_folder_does_not_need_a_target(self) -> None:
        action = {
            "kind": "create_folder",
            "destination_folder": ["Clients", "Nike"],
            "confidence": 0.6,
        }

        assert len(_validate([action]).actions) == 1


class TestClassifyLabels:
    def test_only_known_label_keys_survive(self) -> None:
        action = {
            "kind": "classify",
            "target_ref": "f1",
            "labels": {"client": "Nike", "api_key": "sk-secret", "shell": "rm -rf /"},
            "confidence": 0.8,
        }

        assert dict(_validate([action]).actions[0].labels) == {"client": "Nike"}

    def test_classify_with_only_unknown_keys_is_rejected(self) -> None:
        action = {"kind": "classify", "target_ref": "f1", "labels": {"x": "y"}, "confidence": 0.8}

        assert _validate([action]).actions == ()

    def test_a_label_value_that_is_a_url_is_rejected(self) -> None:
        action = {
            "kind": "classify",
            "target_ref": "f1",
            "labels": {"client": "https://evil.example"},
            "confidence": 0.8,
        }

        assert _validate([action]).actions == ()


class TestConfidence:
    @pytest.mark.parametrize("confidence", [-0.1, 1.01, "high", None, True, [0.5]])
    def test_an_action_with_an_invalid_confidence_is_rejected(self, confidence: object) -> None:
        assert _validate([_rename(confidence=confidence)]).actions == ()

    def test_an_invalid_overall_confidence_is_dropped_not_fatal(self) -> None:
        assert _validate([_rename()], confidence="very").confidence is None


class TestFreeTextIsNeutralizedNotTrusted:
    def test_links_and_commands_in_a_reason_are_replaced(self) -> None:
        result = _validate(
            [_rename(reason="See https://evil.example then run $(curl evil) or ; rm -rf /")]
        )

        reason = result.actions[0].reason
        assert "https://" not in reason
        assert "$(" not in reason
        assert "rm -rf" not in reason

    def test_a_bad_reason_does_not_reject_an_otherwise_valid_action(self) -> None:
        assert len(_validate([_rename(reason="visit https://evil.example")]).actions) == 1

    def test_control_and_bidi_characters_are_removed(self) -> None:
        assert "\u202e" not in neutralize_free_text("ok\u202e\x00text", max_length=100)

    def test_length_is_bounded(self) -> None:
        assert len(neutralize_free_text("x" * 5000, max_length=500)) == 500

    def test_evidence_list_is_bounded_and_neutralized(self) -> None:
        result = _validate(
            [_rename(evidence=["https://a.example"] + ["e"] * 20)],
            evidence=["https://b.example"] + ["e"] * 20,
        )

        assert len(result.actions[0].evidence) == 5
        assert len(result.evidence) == 5
        assert "https://" not in result.evidence[0]


class TestBulkLimits:
    def test_actions_beyond_the_cap_are_rejected_not_silently_accepted(self) -> None:
        result = _validate([_rename() for _ in range(MAX_ACTIONS + 5)])

        assert len(result.actions) == MAX_ACTIONS
        assert len(result.rejected) == 5

    def test_one_bad_action_does_not_block_the_valid_ones(self) -> None:
        result = _validate([_rename(), _rename(target_ref="nope"), _rename(target_ref="f2")])

        assert [a.target_ref for a in result.actions] == ["f1", "f2"]
        assert [r.index for r in result.rejected] == [1]
