"""Tests for provisioning orchestration (TD-04-T3).

Covers role-spec capability validation and prompt construction.
"""

import pytest

from app.orchestration.capability_catalog import CATALOG
from app.orchestration.provisioning import (
    RoleSpecDraft,
    build_role_spec_prompt,
    draft_role_spec,
)


class TestDraftRoleSpec:
    def test_user_keys_only(self):
        """User selects keys, no LLM output."""
        draft = draft_role_spec(
            role_name="前端工程师",
            source_request="我要一个能写React的前端",
            user_capability_keys=["write_code", "run_tests"],
        )
        assert draft.role_name == "前端工程师"
        assert draft.capability_keys == ["run_tests", "write_code"]  # sorted
        assert draft.invalid_keys_stripped == []
        assert draft.responsibilities == []

    def test_llm_output_with_valid_keys(self):
        """LLM suggests keys, no user keys."""
        llm_output = {
            "responsibilities": ["写前端代码", "运行测试", "发PR"],
            "suggested_capability_keys": ["write_code", "run_tests", "git_push"],
        }
        draft = draft_role_spec(
            role_name="前端工程师",
            source_request="我要一个前端",
            llm_output=llm_output,
        )
        assert set(draft.capability_keys) == {"write_code", "run_tests", "git_push"}
        assert draft.responsibilities == ["写前端代码", "运行测试", "发PR"]

    def test_unknown_keys_stripped(self):
        """Unknown capability keys are stripped, not an error."""
        draft = draft_role_spec(
            role_name="测试员",
            source_request="测试",
            user_capability_keys=["write_code", "bogus_key", "nonexistent"],
        )
        assert draft.capability_keys == ["write_code"]
        assert draft.invalid_keys_stripped == ["bogus_key", "nonexistent"]

    def test_llm_unknown_keys_stripped(self):
        """LLM-suggested unknown keys are also stripped."""
        llm_output = {
            "responsibilities": ["写代码"],
            "suggested_capability_keys": ["write_code", "made_up_key"],
        }
        draft = draft_role_spec(
            role_name="工程师",
            source_request="工程师",
            llm_output=llm_output,
        )
        assert draft.capability_keys == ["write_code"]
        assert draft.invalid_keys_stripped == ["made_up_key"]

    def test_user_and_llm_keys_unioned(self):
        """User + LLM keys are unioned."""
        llm_output = {
            "responsibilities": ["写代码"],
            "suggested_capability_keys": ["run_tests", "git_push"],
        }
        draft = draft_role_spec(
            role_name="工程师",
            source_request="工程师",
            user_capability_keys=["write_code", "run_tests"],  # overlap with LLM
            llm_output=llm_output,
        )
        assert set(draft.capability_keys) == {"write_code", "run_tests", "git_push"}

    def test_risk_gate_always_from_catalog(self):
        """risk_gate is from catalog, not LLM — verified via resolve_bundle."""
        # domain_register should always be prohibited_auto regardless
        draft = draft_role_spec(
            role_name="运维",
            source_request="运维",
            user_capability_keys=["domain_register"],
        )
        assert "domain_register" in draft.capability_keys
        # Verify via catalog that risk_gate is prohibited_auto
        from app.orchestration.capability_catalog import get_capability
        cap = get_capability("domain_register")
        assert cap.risk_gate == "prohibited_auto"

    def test_responsibilities_limited_to_12(self):
        """More than 12 responsibilities are truncated."""
        llm_output = {
            "responsibilities": [f"职责{i}" for i in range(20)],
            "suggested_capability_keys": ["write_code"],
        }
        draft = draft_role_spec(
            role_name="工程师",
            source_request="工程师",
            llm_output=llm_output,
        )
        assert len(draft.responsibilities) == 12

    def test_responsibility_chars_capped_at_200(self):
        """Each responsibility is capped at 200 chars."""
        llm_output = {
            "responsibilities": ["x" * 300],
            "suggested_capability_keys": [],
        }
        draft = draft_role_spec(
            role_name="工程师",
            source_request="工程师",
            llm_output=llm_output,
        )
        assert len(draft.responsibilities[0]) == 200

    def test_no_keys_no_llm(self):
        """No user keys, no LLM output → empty keys."""
        draft = draft_role_spec(
            role_name="员工",
            source_request="普通员工",
        )
        assert draft.capability_keys == []
        assert draft.invalid_keys_stripped == []


class TestBuildRoleSpecPrompt:
    def test_prompt_contains_all_keys(self):
        prompt = build_role_spec_prompt("我要一个前端工程师")
        for key in CATALOG:
            assert key in prompt

    def test_prompt_contains_descriptions(self):
        prompt = build_role_spec_prompt("测试")
        assert "编写、修改代码文件" in prompt  # write_code description


class TestRealWorldScenarios:
    """3 real-world scenarios from TD-04-T3 acceptance criteria."""

    def test_frontend_engineer(self):
        """前端工程师: write_code + run_tests + git_push + deploy_preview."""
        llm_output = {
            "responsibilities": [
                "使用 React 编写前端组件",
                "编写并运行单元测试",
                "提交代码并发 PR",
                "部署到预览环境验证",
            ],
            "suggested_capability_keys": [
                "write_code", "run_tests", "git_push", "deploy_preview",
            ],
        }
        draft = draft_role_spec(
            role_name="前端工程师",
            source_request="我要一个会 React 的前端工程师，能写代码发PR部署预览",
            llm_output=llm_output,
        )
        assert set(draft.capability_keys) == {
            "write_code", "run_tests", "git_push", "deploy_preview",
        }
        # resolve_bundle should give approval (git_push is approval)
        from app.orchestration.capability_catalog import resolve_bundle
        bundle = resolve_bundle(draft.capability_keys)
        assert bundle["risk_gate"] == "approval"

    def test_xiaohongshu_operator(self):
        """小红书运营: social_content + seo_audit."""
        llm_output = {
            "responsibilities": [
                "撰写小红书笔记内容",
                "执行 SEO 审计优化笔记",
                "生成配图",
            ],
            "suggested_capability_keys": ["social_content", "seo_audit", "nonexistent_skill"],
        }
        draft = draft_role_spec(
            role_name="小红书运营",
            source_request="我要一个小红书运营，能写笔记和做SEO",
            llm_output=llm_output,
        )
        assert set(draft.capability_keys) == {"social_content", "seo_audit"}
        assert draft.invalid_keys_stripped == ["nonexistent_skill"]
        # social_content is approval
        from app.orchestration.capability_catalog import resolve_bundle
        bundle = resolve_bundle(draft.capability_keys)
        assert bundle["risk_gate"] == "approval"

    def test_finance_assistant(self):
        """财务助理: write_code (for spreadsheet work), no risky capabilities."""
        draft = draft_role_spec(
            role_name="财务助理",
            source_request="帮我管账和做报表",
            user_capability_keys=["write_code"],
        )
        assert draft.capability_keys == ["write_code"]
        from app.orchestration.capability_catalog import resolve_bundle
        bundle = resolve_bundle(draft.capability_keys)
        assert bundle["risk_gate"] == "auto"
