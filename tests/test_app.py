from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

import pandas as pd

import app


class UploadedFile:
    def __init__(self, name: str, content: bytes) -> None:
        self.name = name
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class CoreBehaviorTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_and_fragment(self) -> None:
        url = "HTTPS://Example.COM/item/?utm_source=a&sku=12#reviews"
        self.assertEqual(app.canonical_url(url), "https://example.com/item?sku=12")

    def test_merge_source_keeps_all_discovery_providers(self) -> None:
        sources = []
        first = app.make_source("1", "A", "https://example.com/p?utm_source=qwen", "发现", "仅搜索摘要", "short", "外部搜索", provider="qwen")
        second = app.make_source("2", "A2", "https://example.com/p", "发现", "仅搜索摘要", "a longer excerpt", "外部搜索", provider="tavily")
        self.assertTrue(app.merge_source(sources, first))
        self.assertFalse(app.merge_source(sources, second))
        self.assertEqual(sources[0]["discovered_by"], ["qwen", "tavily"])
        self.assertEqual(sources[0]["excerpt"], "a longer excerpt")

    def test_csv_file_evidence_includes_product_fields(self) -> None:
        content = "品牌,产品名称,型号,SKU,价格,评分,评论数\n品牌A,产品甲,X1,S-01,199,4.8,250\n".encode("utf-8-sig")
        parsed = app.parse_files([UploadedFile("products.csv", content)])
        sources, facts, products = app.extract_file_evidence(parsed)
        self.assertTrue(sources)
        self.assertTrue(facts)
        self.assertEqual(products[0]["brand"], "品牌A")
        self.assertEqual(products[0]["model"], "X1")
        self.assertEqual(products[0]["sku"], "S-01")
        self.assertEqual(products[0]["rating"], "4.8")

    def test_name_input_and_demo_report_are_complete(self) -> None:
        parsed = app.make_name_input("Thermo Scientific Sorvall ST 8", "海外电商")
        self.assertEqual(parsed["input_mode"], "输入产品名称")
        self.assertEqual(parsed["market"], "海外电商")
        demo = app.make_demo_result(app.make_demo_input())
        self.assertTrue(app.metric_rows(demo).shape[0] >= 3)
        self.assertEqual(app.score_matrix(demo).shape[1], 6)
        self.assertIn("关键差异与资料缺口", app.report_markdown(demo))

    def test_name_input_ai_fallback_is_explicit(self) -> None:
        parsed = app.make_name_input("未知型号 ZX-9", "国内电商")
        result = app.ai_identify_product_name(parsed, "")
        self.assertEqual(result["prompt_version"], app.PRODUCT_NAME_PROMPT_VERSION)
        self.assertEqual(result["source"], "本地规则降级")
        self.assertTrue(result["quality"]["needs_user_confirmation"])

    def test_verified_fact_wins_over_earlier_missing_placeholder(self) -> None:
        result = app.make_demo_result(app.make_demo_input())
        brand = result["task"]["competitors"][0]
        dim = result["task"]["criteria"][0]
        result["facts"].insert(0, {"competitor": brand, "dimension": dim, "value": "待补", "status": "缺失", "confidence": 0})
        rows = app.metric_rows(result)
        self.assertNotIn("待补", rows.loc[0, dim])
        self.assertGreater(app.score_matrix(result).loc[brand, dim], 0)

    def test_category_words_never_enter_competitor_list(self) -> None:
        parsed = app.make_name_input("蛋白粉", "国内电商")
        # 分析对象自身、它的父串都不是竞品
        self.assertTrue(app.is_category_like("蛋白粉", parsed))
        self.assertTrue(app.is_category_like("乳清蛋白粉", parsed))
        self.assertFalse(app.is_category_like("汤臣倍健", parsed))
        self.assertFalse(app.is_category_like("Optimum Nutrition", parsed))
        result = app.build_result(parsed, parsed["brands"], ["蛋白粉", "汤臣倍健"], [], [], "本地演示", 0.0, "")
        self.assertEqual(result["task"]["competitors"], ["汤臣倍健"])

    def test_entities_marked_non_brand_are_excluded(self) -> None:
        # 品类词靠 AI 的 entity_type 判定，不依赖任何行业的品类词后缀表
        parsed = app.make_name_input("蛋白粉", "国内电商")
        parsed["entities"] = [
            {"name": "乳清蛋白粉", "entity_type": "category", "relation": "unknown"},
            {"name": "增肌粉", "entity_type": "product", "relation": "unknown"},
            {"name": "康比特", "entity_type": "brand", "relation": "listed_competitor"},
        ]
        self.assertTrue(app.is_category_like("乳清蛋白粉", parsed))
        self.assertTrue(app.is_category_like("增肌粉", parsed))
        self.assertFalse(app.is_category_like("康比特", parsed))

    def test_own_product_entities_never_become_competitors(self) -> None:
        parsed = app.make_name_input("汤臣倍健蛋白粉", "国内电商")
        parsed["entities"] = [
            {"name": "汤臣倍健", "entity_type": "brand", "relation": "own_product"},
            {"name": "汤臣倍健蛋白粉", "entity_type": "product", "relation": "own_product"},
            {"name": "Swisse", "entity_type": "brand", "relation": "possible_competitor"},
        ]
        self.assertTrue(app.is_category_like("汤臣倍健", parsed))
        self.assertFalse(app.is_category_like("Swisse", parsed))
        result = app.build_result(parsed, parsed["brands"], ["汤臣倍健", "Swisse"], [], [], "本地演示", 0.0, "")
        self.assertEqual(result["task"]["competitors"], ["Swisse"])

    def test_document_type_is_not_a_product_category(self) -> None:
        for wrong in ("产品宣传册/技术规格书", "产品手册", "仪器说明书", "brochure"):
            self.assertTrue(app.is_document_type(wrong), wrong)
        for right in ("多功能酶标仪", "实验室仪器", "生化培养箱", "运动营养/膳食补充剂"):
            self.assertFalse(app.is_document_type(right), right)

    def test_prompt_templates_render_without_error(self) -> None:
        # 模板里出现未转义的花括号会让 .format() 抛 KeyError，所有资料分析都会直接崩掉
        rendered = app.AI_USER_TEMPLATE.format(text="示例资料")
        self.assertIn("示例资料", rendered)
        self.assertIn("product_scope", rendered)

    def test_ai_context_keeps_every_page(self) -> None:
        # 整体截断会让靠后的页面整批消失，产品手册的关键参数可能在任何一页
        pages = [{"page": index, "text": "参数" * 1500, "quality": "有正文"} for index in range(1, 31)]
        parsed = {"documents": [{"id": "d1", "name": "big.pdf", "type": "PDF", "status": "已读取", "sheets": [], "pages": pages}], "text": ""}
        context = app.ai_context(parsed)
        self.assertEqual(context.count("[PDF页面]"), 30)
        self.assertLessEqual(len(context), 28000)

    def test_ai_context_leaves_small_documents_intact(self) -> None:
        pages = [{"page": index, "text": f"第{index}页内容", "quality": "有正文"} for index in range(1, 6)]
        parsed = {"documents": [{"id": "d1", "name": "small.pdf", "type": "PDF", "status": "已读取", "sheets": [], "pages": pages}], "text": ""}
        context = app.ai_context(parsed)
        for index in range(1, 6):
            self.assertIn(f"第{index}页内容", context)

    def test_dimensions_come_from_user_table_columns(self) -> None:
        table = pd.DataFrame([
            {"序号": 1, "设备类型": "生化培养箱", "品牌": "一恒", "型号": "LRH-150", "单价": 6860, "质保期": "1年"},
            {"序号": 2, "设备类型": "生化培养箱", "品牌": "菲福斯", "型号": "SPX-150BIII", "单价": 5400, "质保期": "2年"},
        ])
        dimensions = app.dimensions_from_table([table])
        # 标识列不是可比较的维度
        self.assertNotIn("序号", dimensions)
        self.assertNotIn("品牌", dimensions)
        # 整列取值都相同，不构成比较
        self.assertNotIn("设备类型", dimensions)
        self.assertIn("单价", dimensions)
        self.assertIn("质保期", dimensions)

    def test_dimension_maps_to_user_table_columns(self) -> None:
        columns = ["序号", "品牌", "型号", "单价", "总价", "备注"]
        # 维度直接来自列名时精确命中
        self.assertEqual(app.columns_for_dimension(columns, "单价"), ["单价"])
        # AI 补充的维度按包含关系匹配
        self.assertEqual(app.columns_for_dimension(columns, "型号规格"), ["型号"])
        # 专属列都没命中时用“备注”兜底，否则用户资料里的信息会整批丢失
        self.assertEqual(app.columns_for_dimension(columns, "控温功能"), ["备注"])

    def test_model_numbers_are_not_treated_as_brands(self) -> None:
        for model in ("LRH-150", "SPX-150BIII", "YKS-150"):
            self.assertTrue(app.looks_like_model(model), model)
        for brand in ("一恒", "菲福斯", "3M", "Optimum Nutrition"):
            self.assertFalse(app.looks_like_model(brand), brand)
        parsed = app.make_name_input("生化培养箱", "国内电商")
        self.assertTrue(app.is_category_like("LRH-150", parsed))
        self.assertFalse(app.is_category_like("一恒", parsed))

    def test_comparison_table_includes_analysis_subject(self) -> None:
        parsed = app.make_name_input("汤臣倍健蛋白粉", "国内电商")
        parsed["entities"] = [{"name": "汤臣倍健", "entity_type": "brand", "relation": "own_product"}]
        facts = [{"competitor": "汤臣倍健", "dimension": "蛋白质含量及来源", "value": "90%", "status": "来自公开原文", "source_id": "s1"}]
        result = app.build_result(parsed, parsed["brands"], ["Swisse"], [], facts, "本地演示", 0.0, "")
        self.assertEqual(result["task"]["competitors"], ["Swisse"])
        self.assertEqual(result["task"]["subject"], "汤臣倍健")
        self.assertEqual(app.comparison_subjects(result), ["汤臣倍健", "Swisse"])

    def test_subject_without_evidence_is_not_listed(self) -> None:
        # 用户输入的是品类名（如"冲锋衣"）时不会有它的资料，比较表不该多出一行空对象
        parsed = app.make_name_input("冲锋衣", "国内电商")
        result = app.build_result(parsed, parsed["brands"], ["凯乐石"], [], [], "本地演示", 0.0, "")
        self.assertEqual(app.comparison_subjects(result), ["凯乐石"])


class SearchFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = app.make_name_input("测试产品 X1", "国内电商")
        self.parsed["dimensions"] = ["价格与套餐", "核心功能"]

    @staticmethod
    def fake_search(provider: str, query: str, api_key: str, market: str = ""):
        common = "https://example.com/item?utm_source=" + provider
        return [
            {"title": "共同来源", "url": common, "excerpt": provider + " common"},
            {"title": provider + " 独有", "url": f"https://{provider}.example.com/{abs(hash(query))}", "excerpt": provider},
        ], False

    def run_live(self, candidates: list[str], min_sources: str, min_candidates: str):
        env = {"MIN_UNIQUE_SOURCES": min_sources, "MIN_CANDIDATES": min_candidates, "MAX_SEARCH_CALLS": "16", "MAX_PAGES_TO_READ": "0"}
        providers = [("qwen", "q"), ("tavily", "t"), ("brave", "b")]
        with patch.dict(os.environ, env, clear=False), \
             patch.object(app, "configured_search_providers", return_value=providers), \
             patch.object(app, "cached_search", side_effect=self.fake_search), \
             patch.object(app, "url_is_dead", return_value=False), \
             patch.object(app, "candidate_entities_from_sources", return_value=candidates), \
             patch.object(app, "read_external_sources", return_value={"pages_attempted": 0, "pages_read": 0, "page_cache_hits": 0}), \
             patch.object(app, "extract_web_facts", return_value=([], [])):
            return app.make_live_result(self.parsed)

    def test_qwen_and_tavily_results_merge_without_fallback(self) -> None:
        result = self.run_live(["候选A"], "1", "1")
        self.assertEqual(result["search_stats"]["providers"], ["qwen", "tavily"])
        common = next(item for item in result["sources"] if item.get("canonical_url") == "https://example.com/item")
        self.assertEqual(common["discovered_by"], ["qwen", "tavily"])
        self.assertGreater(result["search_stats"]["duplicates_removed"], 0)

    def test_fallback_runs_when_coverage_is_insufficient(self) -> None:
        result = self.run_live([], "99", "99")
        self.assertIn("brave", result["search_stats"]["providers"])

    def test_qwen_search_returns_only_native_search_info_urls(self) -> None:
        payload = {"output": {"search_info": {"search_results": [
            {"title": "京东商品页", "url": "https://item.jd.com/10001234567.html", "site_name": "京东"},
        ]}}}
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            hits = app.qwen_search("乳清蛋白粉", "secret")
        self.assertEqual([hit["url"] for hit in hits], ["https://item.jd.com/10001234567.html"])

    def test_qwen_search_ignores_urls_found_only_in_model_text(self) -> None:
        # compatible-mode 的正文 URL 是模型凭记忆编的，没有 search_info 就必须视为零结果。
        payload = {"output": {"text": "参考来源：https://www.amazon.com/dp/B000QSNYGI"}}
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            self.assertEqual(app.qwen_search("测试", "secret"), [])


if __name__ == "__main__":
    unittest.main()
