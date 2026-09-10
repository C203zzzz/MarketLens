from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    plt = None

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "runs.db"
# 这些列是每一行的标识，不是可比较的维度
IDENTITY_COLUMN_WORDS = ("序号", "编号", "id", "index", "品牌", "厂商", "公司", "供应商", "产品名称", "商品名称")
# 备注类列常混放各类信息，专属列都没命中时才用它兜底
FALLBACK_COLUMNS = ("备注", "说明", "注释", "其他", "详情", "remark", "note")
# 既没有表格也没有 AI 时用的通用维度。刻意不含任何行业假设，任何品类都适用。
GENERIC_DIMENSIONS = ("价格", "核心功能", "规格与参数", "用户评价", "认证与合规", "更新活跃度")


def dimensions_from_table(tables: list[pd.DataFrame]) -> list[str]:
    """用表格的列名作为比较维度。

    用户表格的列名就是他定义的比较框架（"单价""质保期""控温方式"），直接采用比让模型猜测
    更准，也天然与后续的事实提取对齐 —— 因此不再需要任何"维度名到列名"的映射表。
    """
    dimensions: list[str] = []
    for table in tables:
        for column in table.columns:
            name = str(column).strip()
            if not name or len(name) > 24:
                continue
            if any(word in name.lower() for word in IDENTITY_COLUMN_WORDS):
                continue
            # 整列取值都相同的列（如"仪器设备"列全是"生化培养箱"）不构成比较，跳过
            values = table[column].astype(str).str.strip()
            values = values[values != ""]
            if len(values) > 1 and values.nunique() == 1:
                continue
            if name not in dimensions:
                dimensions.append(name)
    return dimensions[:6]


def columns_for_dimension(columns: list[Any], dimension: str) -> list[Any]:
    """找出该维度对应的表格列。

    维度通常直接来自列名（精确命中）；也可能来自 AI 补充的维度，此时按包含关系匹配，
    两者都不中才退到备注类列。
    """
    key = str(dimension or "").strip().lower()
    if not key:
        return []
    lowered = {column: str(column).strip().lower() for column in columns}
    exact = [column for column, low in lowered.items() if low == key]
    if exact:
        return exact
    contains = [column for column, low in lowered.items() if key in low or low in key]
    if contains:
        return contains
    return [column for column, low in lowered.items() if any(word in low for word in FALLBACK_COLUMNS)]


DOCUMENT_TYPE_WORDS = ("宣传册", "规格书", "说明书", "手册", "白皮书", "彩页", "样本册", "brochure", "datasheet", "manual", "catalog", "leaflet")


def is_document_type(category: str) -> bool:
    """判断 AI 给出的“品类”其实是资料形式，而不是产品。

    上传产品手册时，模型很容易把“产品宣传册”“技术规格书”当成品类填进来，
    这会让报告标题、检索词和竞品方向全部跑偏。
    """
    key = str(category or "").lower()
    return bool(key) and any(word in key for word in DOCUMENT_TYPE_WORDS)


# ── 以下两张表只服务「未配置 AI Key」的演示降级路径 ──
# 有 AI Key 时，品类、实体与维度全部由模型按实际产品判定，这两张表完全不参与。
# 所以它们覆盖的行业有限并不影响正常使用，新增品类也不需要改动这里。
CATEGORY_HINTS = {
    "实验室仪器": ("实验室仪器", "实验室设备", "仪器", "设备", "型号", "规格", "参数"),
    "分析仪器": ("分析仪器", "色谱", "光谱", "质谱", "分析检测"),
    "测量仪器": ("测量仪器", "量程", "精度", "传感器", "测量设备"),
    "医疗器械": ("医疗器械", "诊断设备", "临床设备", "注册证"),
    "项目管理": ("项目管理", "任务管理", "看板", "甘特"),
    "团队协作": ("团队协作", "协作", "工作空间", "即时通讯"),
    "在线笔记": ("笔记", "知识库", "文档", "wiki"),
    "数据分析": ("数据分析", "BI", "报表", "仪表盘"),
    "客户关系管理": ("CRM", "客户关系", "销售管理", "线索"),
    "设计协作": ("设计", "原型", "白板", "UI"),
}
CANDIDATE_LIBRARY = {
    "实验室仪器": ["赛默飞世尔", "安捷伦", "岛津", "沃特世", "梅特勒托利多"],
    "分析仪器": ["安捷伦", "岛津", "沃特世", "赛默飞世尔", "布鲁克"],
    "测量仪器": ["福禄克", "横河", "罗德与施瓦茨", "Keysight", "泰克"],
    "医疗器械": ["迈瑞", "西门子医疗", "GE 医疗", "飞利浦医疗", "罗氏诊断"],
    "项目管理": ["Jira", "ClickUp", "monday.com", "Linear", "Wrike"],
    "团队协作": ["Slack", "Microsoft Teams", "飞书", "钉钉", "企业微信"],
    "在线笔记": ["Evernote", "Obsidian", "语雀", "Confluence", "Craft"],
    "数据分析": ["Tableau", "Power BI", "Looker", "Qlik", "FineBI"],
    "客户关系管理": ["Salesforce", "HubSpot", "Zoho CRM", "纷享销客", "销帮帮"],
    "设计协作": ["Figma", "Sketch", "Miro", "Framer", "Adobe XD"],
}
VERIFIED_STATUSES = ("来自用户文件", "来自公开原文", "已核验")
AI_PROMPT_VERSION = "file-intake-v3"
PRODUCT_NAME_PROMPT_VERSION = "product-name-intake-v1"
MARKET_PROFILES: dict[str, dict[str, Any]] = {
    "国内电商": {
        "region": "中国大陆",
        "currency": "CNY",
        "lang_hint": "以中文检索",
        "sources": "品牌官网与产品页、行业媒体和专业评测、百科与公开资料、行业研报、电商详情页（京东/天猫/淘宝只作为价格与在售状态的佐证之一，不是唯一来源）",
        "discovery_term": "竞品 主流品牌 对比",
    },
    "海外电商": {
        "region": "海外",
        "currency": "USD",
        "lang_hint": "以英文或目标市场当地语言检索",
        "sources": "品牌官网与产品页、专业评测媒体、行业报告与白皮书、权威零售渠道页面",
        "discovery_term": "competitors leading brands comparison",
    },
}


def market_profile(market: str) -> dict[str, Any]:
    return MARKET_PROFILES.get(str(market or "").strip(), MARKET_PROFILES["国内电商"])


def query_subject(parsed: dict[str, Any]) -> str:
    """检索词主体。

    用户直接输入的产品名最具体（搜"蛋白粉"比搜"运动营养/膳食补充剂"精准得多），优先使用；
    海外档取其英文名；资料上传场景再用推断出的品类，最后退化为品牌名。
    """
    brands = [str(item).strip() for item in parsed.get("brands", []) if str(item).strip()]
    if str(parsed.get("input_mode") or "") == "输入产品名称" and brands:
        if str(parsed.get("market") or "") == "海外电商":
            return str(parsed.get("product_name_en") or "").strip() or brands[0]
        return brands[0]
    # 上传资料模式：品类名常常过宽（"实验室仪器"连纯水机都会搜进来），
    # 优先用 AI 识别出的具体产品范围，它比品类名精确得多。
    profile = (parsed.get("ai_analysis") or {}).get("document_profile") if isinstance(parsed.get("ai_analysis"), dict) else None
    scope = str((profile or {}).get("product_scope") or "").strip() if isinstance(profile, dict) else ""
    if scope and 1 < len(scope) <= 40:
        return scope
    category = str(parsed.get("category") or "").strip()
    if category and category not in {"待确认产品类别", "未识别"}:
        return category
    return brands[0] if brands else ""
AI_SYSTEM_PROMPT = """你是 MarketLens 的产品资料结构化分析模块，不是开放式聊天助手。
你的任务是把用户上传的 Excel、CSV、PDF 或其他产品资料转换为可审计的结构化输入。

硬性规则：
1. 只使用资料中可以定位的内容；资料没有写明时使用 null、空数组或“未提及”，严禁猜测。
2. 品牌、公司、产品、型号、系列、版本和参数必须逐字符原样保留（如 SPX-70BIII 不得写成 SPX-70BII），不要合并不同型号。
3. 每个重要判断都必须引用 source_refs；没有来源位置不能标记为 covered。
4. 数值必须保留原始值、单位、适用条件和时间/地区/版本范围。
5. 同一字段出现多个值时全部保留并标记 conflict，不得平均、覆盖或擅自选择。
6. 实体关系只能基于资料明示或强证据判断；资料未明示任何“自家产品”时，严禁把任何品牌或型号标为 own_product，一律按 listed_competitor 或 unknown 处理，不得按出现顺序、篇幅或排位推断；不能把供应商、客户或上下游自动判定为竞品。
7. 比较维度必须由资料实际出现的字段、用户说明和实体类型共同生成，不得套用固定行业模板，最多 6 个。
8. 搜索计划必须针对具体缺失字段、实体关系或官方来源核验，避免泛化词。
9. 用户资料已经覆盖且有 source_refs 的字段必须标记 covered，并跳过重复搜索。
10. 输出只能是符合指定结构的 JSON，不要 Markdown、解释或额外字段。"""
AI_USER_TEMPLATE = """请分析以下产品资料，并严格返回约定 JSON 结构。

识别全部实体并标注关系：own_product、listed_competitor、possible_competitor、substitute、adjacent_product、supplier_or_upstream、customer_or_downstream、unknown。
提取实际出现的字段和值，保留单位、条件、时间、地区、版本和来源位置。
document_profile.category.name 必须是**产品品类**（如“多功能酶标仪”“生化培养箱”），严禁填写文档类型——
“产品宣传册”“技术规格书”“说明书”“操作手册”“白皮书”描述的是资料形式，不是产品，填了会让整份报告的标题和检索方向全错。
根据资料生成最多 6 个比较维度（**维度名必须用中文**，它直接展示在中文界面上，不要输出 unit_price 这类英文键名）；为每个维度说明生成理由、需要的证据和哪些实体已覆盖。
为缺失信息生成搜索计划，每个搜索词必须包含实体、类别、目标字段和搜索意图；文件中已有且有来源位置的内容不要重复搜索。

返回结构必须包含：document_profile、entities、observed_fields、comparison_dimensions、search_plan、missing_information、conflicts、quality。
各字段内部键名必须与下列示例完全一致，不得改名、替换或增删：
- document_profile: category（含 name、path、confidence、source_refs）、product_scope（**必填**：资料中的具体产品类型，如“多功能酶标仪”“生化培养箱”，不要只写“实验室仪器”这类宽泛大类）、market_scope、time_scope
- entities[]: name、entity_type、relation、confidence、source_refs
- observed_fields[]: name、value_type、unit、values（数组，每项含 entity、raw_value、source_ref）、conditions、source_refs、completeness（covered/partial/missing）
- comparison_dimensions[]: name（**必须是中文**，它直接展示在中文界面上）、reason、source_fields、covered_entities、priority
- search_plan[]: query、intent
- missing_information[]: reason、dimension
- conflicts[]: description、affected_entities、source_refs
- quality: overall_confidence、source_coverage、needs_user_confirmation

资料片段：
{text}"""
SCHEMA_SPEC = """返回结构必须包含：document_profile、entities、observed_fields、comparison_dimensions、search_plan、missing_information、conflicts、quality。
各字段内部键名必须与下列示例完全一致，不得改名、替换或增删（不要用 dimension_name、rationale、primary_category 之类的别名）：
- document_profile: category（含 name、path、confidence、source_refs）、product_scope（**必填**：资料中的具体产品类型，如“多功能酶标仪”“生化培养箱”，不要只写“实验室仪器”这类宽泛大类）、market_scope、time_scope
- entities[]: name、entity_type、relation、confidence、source_refs
- observed_fields[]: name、value_type、unit、values（数组，每项含 entity、raw_value、source_ref）、conditions、source_refs、completeness（covered/partial/missing）
- comparison_dimensions[]: name（**必须是中文**，它直接展示在中文界面上）、reason、source_fields、covered_entities、priority
- search_plan[]: query、intent
- missing_information[]: reason、dimension
- conflicts[]: description、affected_entities、source_refs
- quality: overall_confidence、source_coverage、needs_user_confirmation"""

st.set_page_config(page_title="MarketLens | 竞品情报工作台", page_icon="M", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
:root { --ink:#15181d; --muted:#6b7280; --line:#e4e7eb; --rule:#c8ced6; --brand:#0b6b5f; --warn:#9a5b00; }
.stApp { background:#fff; color:var(--ink); }
.block-container { max-width:1180px; padding:2.3rem 2rem 3.5rem; }
h1, h2, h3 { font-family:Georgia, 'Noto Serif SC', serif !important; font-weight:600 !important; }
h1 { font-size:2rem !important; line-height:1.16 !important; margin:.15rem 0 .45rem !important; letter-spacing:-.01em; }
h2 { font-size:1.26rem !important; margin:1.5rem 0 .3rem !important; }
h3 { font-size:1.02rem !important; margin:1.1rem 0 .2rem !important; }
p, label, .stMarkdown, .stCaption, small { font-family:'Segoe UI', 'Noto Sans SC', system-ui, sans-serif; }
code, .mono { font-family:'Cascadia Mono', Consolas, monospace; font-size:.79rem; }
/* 数字对齐，便于横向比较 */
table, .stDataFrame { font-variant-numeric:tabular-nums; }
/* 功能区顶栏：起始页视觉语言的缩微版（同色系色块 + 衬线英文 + 中文小字） */
[data-testid="stHeader"] { background:transparent; }
/* 演示时隐藏 Streamlit 自带的部署/主菜单入口，避免开发态 UI 穿帮 */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], #MainMenu, .stDeployButton { display:none !important; }
/* 侧边栏开关叠在深色页头上，用浅色保证可见 */
[data-testid="stSidebarCollapsedControl"] button, [data-testid="stSidebarCollapsedControl"] svg { color:#f4f8f7 !important; fill:#f4f8f7 !important; }
.app-header { margin:-2.3rem -2rem 1.4rem; padding:3.4rem 2rem 2rem; background:linear-gradient(158deg, #0f3733 0%, #164a43 62%, #1b544b 100%); color:#f4f8f7; text-align:center; animation: heroIn .7s cubic-bezier(.2,.7,.3,1) both; }
.app-mark { font-family:Georgia,'Noto Serif SC',serif; font-weight:600; font-size:1.95rem; line-height:1.1; letter-spacing:-.02em; }
.app-sub { color:rgba(244,248,247,.74); font-size:.88rem; margin-top:.4rem; letter-spacing:.06em; }
.brand { border-bottom:1px solid var(--ink); padding-bottom:.7rem; margin-bottom:1.1rem; }
.brand-name { color:var(--brand); font-weight:700; letter-spacing:.18em; font-size:.72rem; text-transform:uppercase; }
.lead { color:var(--muted); font-size:.94rem; margin:.15rem 0 0; }
.eyebrow { color:var(--muted); font-size:.72rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; margin-bottom:.35rem; }
/* 行内项用中点分隔——靠间距分组，不用药丸和浅色底 */
.tag { color:var(--ink); font-size:.86rem; }
.tag + .tag::before { content:"·"; color:var(--rule); margin:0 .5rem; }
/* 提示用左侧竖线 + 留白，不用圆角卡片 */
.summary { border-left:2px solid var(--rule); padding:.4rem 0 .4rem .9rem; color:var(--muted); font-size:.9rem; }
.summary strong { color:var(--ink); }
.fact { border-bottom:1px solid var(--line); padding:.6rem 0; }
.fact:last-child { border-bottom:0; }
.muted { color:var(--muted); } .good { color:#186b46; } .warn { color:var(--warn); }
.report-section { border-top:1px solid var(--ink); padding-top:.9rem; margin-top:1.75rem; }
.stMarkdown p:has(> code) { display:none; }
/* 控件统一为方正的工具栏语汇，去掉 Streamlit 默认大圆角 */
.stButton > button, .stDownloadButton > button { border-radius:4px; border:1px solid var(--rule); font-weight:600; }
.stTextInput input, .stTextArea textarea, .stSelectbox [data-baseweb="select"] > div, .stNumberInput input { border-radius:4px; }
.stTabs [data-baseweb="tab-list"] { gap:1.75rem; border-bottom:1px solid var(--line); }
.stTabs [data-baseweb="tab"] { border-radius:0; padding:0 0 .5rem; font-weight:600; }
[data-testid="stAlert"] { border-radius:3px; box-shadow:none; }
/* 指标卡：数据工具里不需要营销页那种巨型数字 */
[data-testid="stMetricValue"] { font-size:1.45rem !important; font-variant-numeric:tabular-nums; }
[data-testid="stMetricLabel"] { font-size:.8rem !important; }
[data-testid="stMetric"] { padding:.15rem 0; }
[data-testid="stSidebar"] { border-right:1px solid var(--line); }
@media (max-width:700px) { .block-container { padding:2.4rem 1rem 3rem; } h1 { font-size:2rem; } }
</style>
""", unsafe_allow_html=True)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def format_time(value: Any) -> str:
    """统一时间显示。

    早期版本把 created_at 存成了 ISO 串（2026-09-11T00:27:51+08:00），
    历史报告回放时直接显示会很难读，这里统一成 YYYY-MM-DD HH:MM。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def verified_facts(result: dict[str, Any]) -> list[dict[str, Any]]:
    """有依据的事实：来自用户文件或已读取的网页原文，可直接溯源。"""
    return [item for item in result.get("facts", []) if item.get("status") in VERIFIED_STATUSES and str(item.get("value", "")).strip()]


def configured_key(name: str) -> str:
    value = os.getenv(name, "")
    if value:
        return value
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


def configured_value(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def file_analysis_config(depth: str = "标准") -> tuple[str, str, str]:
    """Return the server-side file-analysis endpoint, key, and model."""
    api_key = configured_key("FILE_ANALYSIS_API_KEY") or configured_key("QWEN_API_KEY")
    base_url = (configured_value("FILE_ANALYSIS_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = configured_value("FILE_ANALYSIS_MODEL_DEEP") if depth == "深度" else configured_value("FILE_ANALYSIS_MODEL")
    model = model or ("qwen3.8-max" if depth == "深度" else "qwen3.7-plus")
    return base_url, api_key, model


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS analyses (
            run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, category TEXT NOT NULL,
            competitors TEXT NOT NULL, search_mode TEXT NOT NULL, source_count INTEGER NOT NULL,
            page_count INTEGER NOT NULL, status TEXT NOT NULL, estimated_cost REAL NOT NULL)""")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(analyses)")}
        additions = {
            "input_mode": "TEXT NOT NULL DEFAULT ''",
            "market": "TEXT NOT NULL DEFAULT ''",
            "search_calls": "INTEGER NOT NULL DEFAULT 0",
            "unique_hits": "INTEGER NOT NULL DEFAULT 0",
            "review_count": "INTEGER NOT NULL DEFAULT 0",
            "result_json": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE analyses ADD COLUMN {column} {definition}")


def _result_payload(result: dict[str, Any]) -> str:
    """历史记录里保存完整报告。

    DataFrame 无法 JSON 序列化，网页/文件原文体积大且可在来源面板单独查看，
    所以这两类剔除，其余（来源、事实、商品、复核队列、统计）全部保留。
    """
    payload = dict(result)
    parsed = dict(payload.get("parsed") or {})
    parsed.pop("tables", None)
    parsed["text"] = ""
    for document in parsed.get("documents", []) or []:
        if isinstance(document, dict):
            document.pop("sheets", None)
            for page in document.get("pages", []) or []:
                if isinstance(page, dict):
                    page["text"] = ""
    payload["parsed"] = parsed
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def save_history(result: dict[str, Any]) -> None:
    task = result["task"]
    parsed = result.get("parsed", {})
    stats = result.get("search_stats", {})
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT OR REPLACE INTO analyses (
            run_id, created_at, category, competitors, search_mode, source_count,
            page_count, status, estimated_cost, input_mode, market, search_calls,
            unique_hits, review_count, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            result["run_id"], result["created_at"], task["category"], ", ".join(task["competitors"]),
            result["search_mode"], len(result["sources"]), result["page_count"], result["status"], result["estimated_cost"],
            parsed.get("input_mode", "上传产品资料"), parsed.get("market", ""),
            int(stats.get("search_calls", 0)), int(stats.get("unique_hits", 0)),
            len(result.get("review_queue", [])), _result_payload(result),
        ))


def summarize_names(value: Any, keep: int = 3) -> str:
    """长列表只展示前几项，避免把表格列撑到无法阅读。"""
    items = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if len(items) <= keep:
        return "、".join(items)
    return "、".join(items[:keep]) + f" 等 {len(items)} 个"


def load_history() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        frame = pd.read_sql_query("""SELECT created_at AS 时间, input_mode AS 入口,
            market AS 市场, category AS 产品类别, competitors AS 比较对象,
            page_count AS 可用来源, status AS 状态
            FROM analyses ORDER BY REPLACE(created_at, 'T', ' ') DESC LIMIT 30""", conn)
    if not frame.empty:
        # 兼容早期写入的 ISO 时间戳（2026-09-11T00:27:51+08:00）
        frame["时间"] = frame["时间"].astype(str).str.replace("T", " ", regex=False).str.slice(0, 16)
        frame["比较对象"] = frame["比较对象"].map(summarize_names)
    return frame


def load_history_entries() -> list[tuple[str, str]]:
    """返回 [(run_id, 展示标签)]，供历史报告回看选择。"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT run_id, created_at, category, market FROM analyses WHERE result_json != '' ORDER BY REPLACE(created_at, 'T', ' ') DESC LIMIT 30").fetchall()
    return [(str(row[0]), f"{str(row[1])[:16].replace('T', ' ')} · {row[2]}" + (f" · {row[3]}" if row[3] else "")) for row in rows]


def load_stored_result(run_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT result_json FROM analyses WHERE run_id = ?", (run_id,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        stored = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    task = stored.get("task")
    parsed = stored.get("parsed", {}) if isinstance(stored.get("parsed"), dict) else {}
    if isinstance(task, dict):
        # 早期版本把品类词（如"蛋白粉"）也当成竞品存了下来，回放时按当前规则清理一次
        if isinstance(task.get("competitors"), list):
            task["competitors"] = [name for name in task["competitors"] if not is_category_like(name, parsed)]
        if not task.get("subject"):
            task["subject"] = analysis_subject(parsed)
    return stored


def norm(text: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "", str(text)).lower()


def looks_like_model(name: str) -> bool:
    """判断一个名字是产品型号而不是品牌。

    AI 会把「LRH-150」「SPX-150BIII」这类型号标成 listed_competitor，直接并入品牌列表后
    比较表就变成了零件清单。品牌名里带数字的（如 3M）不含分隔符，不受影响。
    """
    text = str(name or "").strip()
    if len(text) < 3 or len(text) > 30:
        return False
    if not any(char.isdigit() for char in text):
        return False
    return bool(re.search(r"[-_/—－–]", text)) or len(text) > 12


NON_BRAND_ENTITY_TYPES = {"product", "model", "category", "technology", "feature", "component", "industry"}


def is_category_like(name: str, parsed: dict[str, Any]) -> bool:
    """判断候选名不是可比较的品牌。

    只用跨品类通用的信号，不依赖任何行业品类词表：型号（LRH-150）、与品类名相同、
    分析对象自身及其父串，以及 AI 在资料实体识别里已经标为非品牌的名字。
    """
    key = norm(name)
    if not key:
        return True
    if looks_like_model(name):
        return True
    if key == norm(parsed.get("category", "")):
        return True
    for item in parsed.get("entities", []):
        if not isinstance(item, dict):
            continue
        item_key = norm(item.get("name", ""))
        if not item_key:
            continue
        # 分析对象自身（“汤臣倍健”）及其父串（“汤臣倍健蛋白粉”）都不是竞品
        if item.get("relation") == "own_product" and (item_key == key or (len(item_key) >= 2 and item_key in key)):
            return True
        # AI 已把它标成产品/型号/品类，即使混进候选也不该进比较表
        if item_key == key and str(item.get("entity_type") or "").lower() in NON_BRAND_ENTITY_TYPES:
            return True
    # “输入产品名称”模式下 brands 装的就是用户给的分析对象
    if parsed.get("input_mode") == "输入产品名称":
        for item in parsed.get("brands", []):
            target = norm(item)
            if target and len(target) >= 2 and (key == target or target in key):
                return True
    return False


def analysis_subject(parsed: dict[str, Any]) -> str:
    """被分析对象的名字。

    "输入产品名称"模式下用户给的是"汤臣倍健蛋白粉"这样的品牌+产品串，AI 会把它拆成
    品牌（"汤臣倍健"）与产品两条实体；比较表用品牌名更干净，回退时用原始输入。
    """
    if parsed.get("input_mode") == "输入产品名称":
        for item in parsed.get("entities", []):
            if isinstance(item, dict) and item.get("relation") == "own_product" and item.get("entity_type") == "brand":
                name = str(item.get("name") or "").strip()
                if name:
                    return name
        brands = parsed.get("brands", [])
        return str(brands[0]).strip() if brands else ""
    # 上传资料模式：只有 AI 标为“自家产品”的品牌才是分析对象；
    # 资料里的其余品牌仍是竞品，所以不能拿 brands[0] 顶替。
    for item in parsed.get("entities", []):
        if isinstance(item, dict) and item.get("relation") == "own_product" and str(item.get("entity_type") or "") in {"brand", "company"}:
            name = str(item.get("name") or "").strip()
            if name:
                return name
    return ""


def align_subject(name: str, subjects: list[str]) -> str:
    """把抽取出来的对象名归一到已知的比较对象。

    AI 从网页抽取时会混用"汤臣倍健"与"汤臣倍健蛋白粉"这类名称；不归一就匹配不上
    比较表，结果是明明抓到了事实，界面却显示"暂无可比较的数据"。
    """
    key = norm(name)
    if not key:
        return name
    for candidate in subjects:
        target = norm(candidate)
        if not target:
            continue
        if key == target or (len(target) >= 2 and target in key) or (len(key) >= 2 and key in target):
            return candidate
    return name


def canonical_url(url: str) -> str:
    """Remove tracking noise so the same page found by two providers is merged."""
    raw_url = str(url or "").strip()
    if not raw_url or not raw_url.lower().startswith(("http://", "https://")):
        return raw_url
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if not key.lower().startswith(("utm_", "fbclid", "gclid", "ref"))]
        path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), ""))
    except ValueError:
        return str(url).strip()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def infer_category(text: str) -> tuple[str, float]:
    scores = [(name, sum(1 for hint in hints if hint.lower() in text.lower())) for name, hints in CATEGORY_HINTS.items()]
    name, score = max(scores, key=lambda pair: pair[1])
    return (name, min(.95, .55 + score * .12)) if score else ("待确认产品类别", .35)


def extract_brands(text: str, tables: list[pd.DataFrame]) -> list[str]:
    found: list[str] = []
    # 优先只看“品牌/厂商/公司”列；只有在完全没有这类列时才退而用“产品”列。
    # 否则型号（如 LRH-150）会被当成品牌，比较表里会混进一串型号。
    for keywords in (("品牌", "厂商", "公司", "竞品", "供应商"), ("产品",)):
        for table in tables:
            for col in table.columns:
                if any(word in str(col).lower() for word in keywords):
                    for value in table[col].dropna().astype(str).tolist():
                        value = value.strip()
                        if 1 < len(value) < 45 and value not in found and not value.isnumeric():
                            found.append(value)
        if found:
            break
    known = re.findall(r"(?<![\w])(?:Notion|Trello|Asana|Slack|Figma|Jira|ClickUp|monday\.com|Linear|飞书|钉钉|企业微信|Tableau|Power BI|Salesforce|HubSpot|语雀|Confluence|Evernote|Obsidian)(?![\w])", text, flags=re.I)
    for value in known:
        if value not in found:
            found.append(value)
    labeled = re.findall(r"(?:品牌|厂商|公司|产品名称|竞品)\s*[:：]\s*([^\n,，;；|]{2,40})", text)
    for value in labeled:
        value = value.strip()
        if value and value not in found and not value.isnumeric():
            found.append(value)
    return found[:12]


def infer_dimensions(text: str, tables: list[pd.DataFrame] | None = None) -> list[str]:
    """推断比较维度：表格列名优先，没有表格时用通用维度兜底。

    表格场景下列名就是用户自己的比较框架；PDF/纯文本没有结构可依，先用一组不含
    行业假设的通用维度，随后由 AI 按实际产品覆盖。
    """
    from_table = dimensions_from_table(tables or [])
    if len(from_table) >= 3:
        return from_table
    return list(dict.fromkeys(from_table + list(GENERIC_DIMENSIONS)))[:6]


def parse_files(uploaded_files: list[Any]) -> dict[str, Any]:
    documents, tables, all_text = [], [], []
    for uploaded in uploaded_files:
        raw, suffix = uploaded.getvalue(), Path(uploaded.name).suffix.lower()
        digest = sha256(raw)
        all_text.append(Path(uploaded.name).stem)
        if suffix in (".xlsx", ".xls", ".csv"):
            try:
                if suffix == ".csv":
                    sheets = {"CSV": pd.read_csv(io.BytesIO(raw))}
                else:
                    book = pd.ExcelFile(io.BytesIO(raw))
                    sheets = {sheet: pd.read_excel(io.BytesIO(raw), sheet_name=sheet) for sheet in book.sheet_names}
                summaries = []
                for sheet_name, table in sheets.items():
                    table = table.dropna(how="all").fillna("")
                    tables.append(table)
                    text = table.astype(str).to_csv(index=False)
                    all_text.append(text)
                    summaries.append({"name": sheet_name, "rows": len(table), "columns": [str(c) for c in table.columns], "text": text[:5000]})
                documents.append({"id": f"file-{len(documents)+1}", "name": uploaded.name, "type": "Excel/CSV", "hash": digest, "status": "已读取", "sheets": summaries, "pages": len(summaries)})
            except Exception as exc:
                documents.append({"id": f"file-{len(documents)+1}", "name": uploaded.name, "type": "Excel/CSV", "hash": digest, "status": f"读取失败：{exc}", "sheets": [], "pages": 0})
        elif suffix == ".pdf":
            pages, texts = [], []
            try:
                if fitz:
                    pdf = fitz.open(stream=raw, filetype="pdf")
                    for page_no, page in enumerate(pdf, 1):
                        text = page.get_text("text").strip()
                        texts.append(text)
                        pages.append({"page": page_no, "text": text[:5000], "quality": "有正文" if text else "空页"})
                else:
                    pages = [{"page": 1, "text": "未安装 PDF 解析器", "quality": "不可读取"}]
                all_text.extend(texts)
                documents.append({"id": f"file-{len(documents)+1}", "name": uploaded.name, "type": "PDF", "hash": digest, "status": "已读取" if any(texts) else "正文为空", "pages": pages, "page_count": len(pages)})
            except Exception as exc:
                documents.append({"id": f"file-{len(documents)+1}", "name": uploaded.name, "type": "PDF", "hash": digest, "status": f"读取失败：{exc}", "pages": [], "page_count": 0})
    combined = "\n".join(all_text)
    brands = extract_brands(combined, tables)
    category, confidence = infer_category(combined + " " + " ".join(brands))
    return {"documents": documents, "tables": tables, "text": combined, "brands": brands, "category": category, "category_confidence": confidence, "dimensions": infer_dimensions(combined, tables), "file_count": len(uploaded_files), "file_hash": sha256(combined.encode("utf-8", errors="ignore"))}


ENTITY_RELATIONS = {"own_product", "listed_competitor", "possible_competitor", "substitute", "adjacent_product", "supplier_or_upstream", "customer_or_downstream", "unknown"}


def ai_context(parsed: dict[str, Any], budget: int = 26000) -> str:
    """把资料拼成送给模型的上下文。

    整体截断（[:N]）会让靠后的文件和页面整批消失 —— 一份 28 页的手册只传前几页，
    后面的参数模型根本看不到。改为按内容块均分预算：短资料毫无损失，长资料每页也都有代表。
    """
    blocks: list[tuple[str, str]] = []
    for document in parsed.get("documents", []):
        blocks.append((f"[文件] id={document['id']} name={document['name']} type={document['type']} status={document['status']}", ""))
        for sheet in document.get("sheets", []):
            blocks.append((f"[工作表] source_ref={document['id']}:{sheet['name']} rows={sheet['rows']} columns={sheet['columns']}", str(sheet.get("text", ""))))
        for page in document.get("pages", []) if isinstance(document.get("pages", []), list) else []:
            blocks.append((f"[PDF页面] source_ref={document['id']}:page={page['page']} quality={page['quality']}", str(page.get("text", ""))))
    if not blocks:
        return str(parsed.get("text", ""))[:budget]
    header_len = sum(len(header) for header, _ in blocks)
    contents = [content for _, content in blocks if content]
    available = max(1200, budget - header_len)
    total = sum(len(content) for content in contents)
    # 没超预算就一字不删；超了才按块均分，保证每一页都留下代表
    chunk = available if total <= available else max(600, available // max(1, len(contents)))
    return "\n\n".join(f"{header}\n{content[:chunk]}" if content else header for header, content in blocks)


def _recover_category(raw: dict[str, Any]) -> str:
    """AI 偶尔不按 schema 返回，从常见的漂移路径里尽力捞回类别名。"""
    input_analysis = raw.get("input_analysis")
    if isinstance(input_analysis, dict):
        inference = input_analysis.get("category_inference")
        if isinstance(inference, dict):
            value = str(inference.get("primary_category") or inference.get("category") or "").strip()
            if value:
                return value
    return str(raw.get("category") or "").strip()


def normalize_ai_payload(raw: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    profile = raw.get("document_profile") if isinstance(raw.get("document_profile"), dict) else {}
    category_obj = profile.get("category") if isinstance(profile.get("category"), dict) else {}
    category = str(category_obj.get("name") or "").strip()
    # AI 会把资料形式（“产品宣传册”“技术规格书”）当成品类，那描述的是文件而不是产品，必须换掉
    if not category or category in {"待确认产品类别", "未识别"} or is_document_type(category):
        category = _recover_category(raw) or parsed["category"]
    if not category or category in {"待确认产品类别", "未识别"} or is_document_type(category):
        # 品类实在识别不出时用产品名兜底，避免占位串进入报告标题和检索词
        category = next((str(item).strip() for item in parsed.get("brands", []) if str(item).strip()), category)
    entities = []
    for item in raw.get("entities", []) if isinstance(raw.get("entities"), list) else []:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        confidence = max(0, min(1, float(item.get("confidence", 0)))) if str(item.get("confidence", "")).strip() else 0
        relation = item.get("relation") if item.get("relation") in ENTITY_RELATIONS else "unknown"
        source_refs = [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()]
        entities.append({"name": str(item["name"]).strip(), "normalized_name": str(item.get("normalized_name") or norm(item["name"])), "entity_type": str(item.get("entity_type") or "unknown"), "relation": relation, "confidence": confidence if source_refs else min(confidence, .5), "source_refs": source_refs})
    fields = []
    for item in raw.get("observed_fields", []) if isinstance(raw.get("observed_fields"), list) else []:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            source_refs = [str(ref) for ref in item.get("source_refs", []) if str(ref).strip()]
            completeness = str(item.get("completeness") or "missing")
            if completeness == "covered" and not source_refs:
                completeness = "partial"
            fields.append({"name": str(item["name"]).strip(), "normalized_name": str(item.get("normalized_name") or norm(item["name"])), "value_type": str(item.get("value_type") or "unknown"), "unit": str(item.get("unit") or ""), "values": item.get("values", []) if isinstance(item.get("values", []), list) else [], "conditions": item.get("conditions", []) if isinstance(item.get("conditions", []), list) else [], "source_refs": source_refs, "completeness": completeness})
    dimensions = []
    for item in raw.get("comparison_dimensions", []) if isinstance(raw.get("comparison_dimensions"), list) else []:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            dimensions.append({"id": str(item.get("id") or f"dim-{len(dimensions)+1}"), "name": str(item["name"]).strip(), "reason": str(item.get("reason") or "由资料字段生成"), "source_fields": item.get("source_fields", []) if isinstance(item.get("source_fields", []), list) else [], "required_evidence": item.get("required_evidence", []) if isinstance(item.get("required_evidence", []), list) else [], "covered_entities": item.get("covered_entities", []) if isinstance(item.get("covered_entities", []), list) else [], "missing_entities": item.get("missing_entities", []) if isinstance(item.get("missing_entities", []), list) else [], "priority": str(item.get("priority") or "medium")})
    dimensions = dimensions[:6] or [{"id": f"dim-{i+1}", "name": name, "reason": "规则降级生成", "source_fields": [], "required_evidence": [], "covered_entities": [], "missing_entities": [], "priority": "medium"} for i, name in enumerate(parsed["dimensions"][:6])]
    search_plan = [item for item in raw.get("search_plan", []) if isinstance(item, dict) and str(item.get("query", "")).strip()][:24] if isinstance(raw.get("search_plan"), list) else []
    missing = [item for item in raw.get("missing_information", []) if isinstance(item, dict)] if isinstance(raw.get("missing_information"), list) else []
    conflicts = [item for item in raw.get("conflicts", []) if isinstance(item, dict)] if isinstance(raw.get("conflicts"), list) else []
    quality = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
    confidence = max(0, min(1, safe_float(quality.get("overall_confidence"), -1))) if str(quality.get("overall_confidence", "")).strip() else parsed["category_confidence"]
    if confidence < 0:
        confidence = parsed["category_confidence"]
    keywords = [str(item["query"]).strip() for item in search_plan if str(item.get("query", "")).strip()][:12]
    names = list(dict.fromkeys([item["name"] for item in entities if not looks_like_model(item["name"])] + parsed["brands"]))[:12]
    analysis_brands = list(dict.fromkeys([item["name"] for item in entities if item["relation"] in {"own_product", "listed_competitor"} and not looks_like_model(item["name"])] + parsed["brands"]))[:12]
    return {"prompt_version": AI_PROMPT_VERSION, "product_name_en": str(profile.get("product_name_en") or "").strip(), "document_profile": {"category": {"name": category, "path": category_obj.get("path", []) if isinstance(category_obj.get("path", []), list) else [], "confidence": confidence, "source_refs": category_obj.get("source_refs", []) if isinstance(category_obj.get("source_refs", []), list) else []}, "product_scope": str(profile.get("product_scope") or ""), "market_scope": str(profile.get("market_scope") or ""), "time_scope": str(profile.get("time_scope") or "")}, "entities": entities[:24], "observed_fields": fields[:32], "comparison_dimensions": dimensions, "search_plan": search_plan, "missing_information": missing[:24], "conflicts": conflicts[:16], "quality": {"overall_confidence": confidence, "source_coverage": safe_float(quality.get("source_coverage"), 0.5 if quality.get("source_coverage") else 0), "needs_user_confirmation": quality.get("needs_user_confirmation", []) if isinstance(quality.get("needs_user_confirmation", []), list) else []}, "category": category, "brands": names, "analysis_brands": analysis_brands, "keywords": keywords, "dimensions": [item["name"] for item in dimensions], "focus_data": [item["name"] for item in fields if item.get("completeness") != "covered"][:12], "missing_info": [str(item.get("reason") or item.get("dimension") or "待补充") for item in missing[:12]], "confidence": confidence}


def ai_analyze_documents(parsed: dict[str, Any], api_key: str, depth: str = "标准") -> dict[str, Any]:
    fallback = normalize_ai_payload({"document_profile": {"category": {"name": parsed["category"], "confidence": parsed["category_confidence"]}}, "entities": [{"name": brand, "relation": "listed_competitor", "confidence": .5, "source_refs": []} for brand in parsed["brands"]], "comparison_dimensions": [{"name": name} for name in parsed["dimensions"]], "quality": {"overall_confidence": parsed["category_confidence"]}}, parsed)
    fallback["source"] = "本地规则降级"
    if not api_key:
        return fallback
    prompt = AI_USER_TEMPLATE.format(text=ai_context(parsed))
    base_url, _, model = file_analysis_config(depth)
    payload = json.dumps({"model": model, "temperature": 0, "enable_thinking": False, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": AI_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "Jingyantai/0.5"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
        result = normalize_ai_payload(json.loads(body["choices"][0]["message"]["content"]), parsed)
        result["source"] = "AI API"
        return result
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        fallback["source"] = "AI API 失败，使用本地规则降级"
        return fallback


def apply_ai_analysis(parsed: dict[str, Any], ai_result: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(parsed)
    parsed["ai_analysis"] = ai_result
    parsed["category"] = ai_result.get("category") or parsed["category"]
    parsed["category_confidence"] = float(ai_result.get("confidence", parsed["category_confidence"]))
    parsed["entities"] = ai_result.get("entities", [])
    parsed["all_entities"] = ai_result.get("brands", [])
    parsed["brands"] = list(dict.fromkeys(parsed["brands"] + ai_result.get("analysis_brands", ai_result.get("brands", []))))[:12]
    # 表格列名是用户自己定义的比较框架，比任何生成的维度都贴切，也保证后续事实提取
    # 能对上列；只有没有表格（PDF/纯文本）时才采用 AI 生成的维度。
    table_dimensions = dimensions_from_table(parsed.get("tables", []))
    parsed["dimensions"] = table_dimensions if len(table_dimensions) >= 3 else (ai_result.get("dimensions") or parsed["dimensions"])
    parsed["product_name_en"] = ai_result.get("product_name_en") or ""
    return parsed


def make_demo_input() -> dict[str, Any]:
    table = pd.DataFrame([
        {"品牌": "飞书", "产品": "飞书多维表格", "价格": "免费版；专业版价格需核对", "功能": "协作表格、审批、自动化", "目标用户": "中国团队", "部署": "云端，支持开放接口"},
        {"品牌": "Notion", "产品": "Notion", "价格": "免费版；付费按席位", "功能": "文档、数据库、知识库", "目标用户": "个人与小团队", "部署": "云端，第三方集成"},
        {"品牌": "Airtable", "产品": "Airtable", "价格": "免费版；按用户和功能分级", "功能": "数据库、视图、自动化", "目标用户": "运营与产品团队", "部署": "云端，API 与集成"},
    ])
    text = table.astype(str).to_csv(index=False)
    return {"documents": [{"id": "demo-file", "name": "协作工具竞品资料_演示.xlsx", "type": "Excel/CSV", "hash": sha256(text.encode()), "status": "本地演示", "sheets": [{"name": "竞品概览", "rows": 3, "columns": list(table.columns), "text": text}], "pages": 1}], "tables": [table], "text": text, "brands": ["飞书", "Notion", "Airtable"], "category": "团队协作", "category_confidence": .92, "dimensions": ["价格与套餐", "核心功能", "目标用户", "部署与集成", "用户反馈", "更新活跃度"], "file_count": 1, "demo": True}


def make_name_input(product_name: str, market: str) -> dict[str, Any]:
    product_name = product_name.strip()
    category, confidence = infer_category(product_name)
    dimensions = ["价格与套餐", "核心功能", "规格与配置", "用户反馈", "售后与合规", "更新活跃度"]
    text = f"用户输入产品：{product_name}\n目标市场：{market}"
    return {"documents": [{"id": "user-input", "name": "用户输入", "type": "产品名称", "hash": sha256(text.encode()), "status": "待联网确认", "pages": 0}], "tables": [], "text": text, "brands": [product_name], "category": category, "category_confidence": confidence, "dimensions": dimensions, "file_count": 0, "file_hash": sha256(text.encode()), "input_mode": "输入产品名称", "market": market}


def ai_identify_product_name(parsed: dict[str, Any], api_key: str, depth: str = "标准") -> dict[str, Any]:
    """Identify a typed product name before building the web-search plan."""
    product_name = parsed["brands"][0]
    fallback = normalize_ai_payload({
        "document_profile": {"category": {"name": parsed["category"], "confidence": parsed["category_confidence"], "source_refs": ["user-input"]}, "market_scope": parsed.get("market", "")},
        "entities": [{"name": product_name, "entity_type": "product", "relation": "own_product", "confidence": .5, "source_refs": ["user-input"]}],
        "comparison_dimensions": [{"name": name, "reason": "本地规则生成", "priority": "medium"} for name in parsed["dimensions"]],
        "quality": {"overall_confidence": parsed["category_confidence"], "needs_user_confirmation": ["产品类别和具体型号需要通过公开来源确认"]},
    }, parsed)
    fallback["prompt_version"] = PRODUCT_NAME_PROMPT_VERSION
    fallback["source"] = "本地规则降级"
    if not api_key:
        return fallback
    market = parsed.get("market", "未指定市场")
    language_rule = (
        "目标市场为海外：document_profile.product_name_en 必须给出英文产品名，search_plan 的 query 一律用英文。"
        if market == "海外电商"
        else "search_plan 的 query 用中文。"
    )
    prompt = f"""用户只提供了一个产品名称，请先把它转换为竞品检索计划。
产品名称：{product_name}
目标市场：{market}

{SCHEMA_SPEC}

额外要求：
1. 将用户输入标为 own_product，保留完整品牌、产品、系列和型号，不确定时标为 unknown。
2. document_profile.category.name 必须给出具体可检索的品类名称（例如“运动营养/膳食补充剂”“实验室移液器”），
   严禁输出“待确认产品类别”“未知品类”这类占位表述；确实不确定时给出最接近的品类并降低 confidence。
3. 生成 4-6 个与该产品实际购买决策相关的比较维度（**维度名必须用中文**，它直接展示在中文界面上，不要输出 unit_price 这类英文键名），不套用固定行业模板。
4. 生成搜索计划，先核验产品身份和官网，再发现直接竞品，之后补价格、规格、评价等缺失字段。
5. {language_rule}
6. 不要虚构竞品、参数、价格或市场地位。此阶段只理解输入并制定检索计划，不输出事实结论。
7. source_refs 只能使用 user-input。最多 12 个搜索任务。"""
    base_url, _, model = file_analysis_config(depth)
    payload = json.dumps({"model": model, "temperature": 0, "enable_thinking": False, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": AI_SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "Jingyantai/0.7"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        result = normalize_ai_payload(json.loads(body["choices"][0]["message"]["content"]), parsed)
        result["prompt_version"] = PRODUCT_NAME_PROMPT_VERSION
        result["source"] = "AI API"
        return result
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        fallback["source"] = "AI API 失败，使用本地规则降级"
        return fallback


def make_source(sid: str, title: str, url: str, kind: str, status: str, excerpt: str, origin: str, location: str = "", provider: str = "") -> dict[str, Any]:
    clean_url = canonical_url(url)
    return {"id": sid, "title": title, "url": url, "canonical_url": clean_url, "type": kind, "status": status, "excerpt": excerpt, "origin": origin, "provider": provider or origin, "discovered_by": [provider] if provider else [], "location": location, "retrieved_at": now(), "content_hash": sha256(excerpt.strip().encode()) if excerpt.strip() else ""}


def merge_source(sources: list[dict[str, Any]], source: dict[str, Any]) -> bool:
    """Merge duplicate URLs or identical excerpts while preserving discovery providers."""
    duplicate = next((item for item in sources if source.get("canonical_url") and item.get("canonical_url") == source["canonical_url"]), None)
    if duplicate is None and source.get("content_hash"):
        duplicate = next((item for item in sources if item.get("content_hash") == source["content_hash"]), None)
    if duplicate is None:
        sources.append(source)
        return True
    duplicate["discovered_by"] = list(dict.fromkeys(duplicate.get("discovered_by", []) + source.get("discovered_by", [])))
    if len(source.get("excerpt", "")) > len(duplicate.get("excerpt", "")):
        duplicate["excerpt"] = source["excerpt"]
        duplicate["content_hash"] = source.get("content_hash", "")
    return False


def mobile_variant(url: str) -> str | None:
    """把电商 PC 页换成移动端地址。

    京东/天猫的 PC 商品页返回的是 SPA 空壳（实测 2.6KB，无正文），
    移动端同一个商品返回完整 HTML（实测 251KB，含 price 字段）。
    这是公开页面的另一种地址形式，不涉及绕过任何访问控制。
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    if host.endswith("jd.com") and "item.jd.com" in host:
        match = re.search(r"/(?:product/)?([A-Za-z0-9]+)\.html", parsed.path)
        if match:
            return f"https://item.m.jd.com/product/{match.group(1)}.html"
    if "detail.tmall.com" in host:
        return urllib.parse.urlunsplit(("https", "detail.m.tmall.com", parsed.path, parsed.query, ""))
    if "amazon." in host:
        # 亚马逊的 PC 商品页（/dp/ASIN）不返回价格，移动端 /gp/aw/d/ASIN 才有
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path)
        if match and "/gp/aw/" not in parsed.path:
            return f"https://{host}/gp/aw/d/{match.group(1)}"
    return None


def decode_html(raw: bytes, content_type: str = "") -> str:
    """按页面真实编码解码。

    中文站点（尤其政府站与老论坛）常用 GBK/GB18030，一律按 UTF-8 解会产生乱码，
    而乱码文本会直接污染品牌名与事实值。优先用响应头里的 charset，再依次尝试常见编码。
    """
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    candidates: list[str] = []
    if match:
        candidates.append(match.group(1).lower())
    # 从 meta 标签里再找一次 charset
    head = raw[:2048].decode("ascii", errors="ignore")
    meta = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if meta and meta.group(1).lower() not in candidates:
        candidates.append(meta.group(1).lower())
    candidates += ["utf-8", "gb18030", "big5"]
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if "�" not in text:
            return text
    return raw.decode("utf-8", errors="ignore")


_PC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
_PAGE_CACHE: dict[str, tuple[str, str]] = {}


def fetch_public_text(url: str) -> tuple[str, str]:
    """抓取网页正文。

    缓存放在模块级 dict：线程安全（GIL 下读写原子），并发抓取时可共享，
    且比 st.session_state 少了会话绑定，同一 URL 不会重复抓。
    """
    key = canonical_url(url)
    cached = _PAGE_CACHE.get(key)
    if cached:
        return cached[0], "缓存"
    targets = [url]
    mobile = mobile_variant(url)
    if mobile:
        targets.insert(0, mobile)
    targets.append(f"https://r.jina.ai/{url}")
    for target in targets:
        netloc = urllib.parse.urlsplit(target).netloc
        user_agent = _MOBILE_UA if netloc.startswith("m.") or ".m." in netloc else _PC_UA
        try:
            request = urllib.request.Request(target, headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(request, timeout=12) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(1_500_000)
            text = decode_html(raw, content_type)
            if "html" in content_type.lower() or "<html" in text[:500].lower():
                parser = TextExtractor()
                parser.feed(text)
                text = "\n".join(parser.parts)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            lower = text[:4000].lower()
            if any(marker in lower for marker in ("captcha", "verify you are human", "访问验证", "请登录后", "安全验证")):
                continue
            if len(text) >= 300:
                _PAGE_CACHE[key] = (text[:30000], now())
                return _PAGE_CACHE[key][0], "新读取"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            continue
    return "", "失败"


def pick_read_targets(candidates: list[dict[str, Any]], limit: int, subjects: list[str] | None = None) -> list[dict[str, Any]]:
    """挑出要读取的页面：先保证每个比较对象各有一条来源，再按优先级补满。

    candidates 已按读取优先级排好序。若只按优先级截断，某个竞品可能一条来源都进不了
    读取名单，报告里那个对象就永远是空的 —— 这正是横向对比长期只有一两个对象的原因。
    """
    if limit <= 0:
        return []
    names = [str(item).strip() for item in (subjects or []) if str(item).strip()]
    if not names:
        return candidates[:limit]
    picked: list[dict[str, Any]] = []
    used: set[int] = set()
    for name in names:
        key = norm(name)
        if not key:
            continue
        for index, item in enumerate(candidates):
            if index in used:
                continue
            text = norm(f"{item.get('title', '')}{item.get('excerpt', '')}")
            if key in text:
                picked.append(item)
                used.add(index)
                break
    for index, item in enumerate(candidates):
        if len(picked) >= limit:
            break
        if index not in used:
            picked.append(item)
            used.add(index)
    return picked[:limit]


def read_external_sources(sources: list[dict[str, Any]], limit: int = 8, subjects: list[str] | None = None, topic_terms: list[str] | None = None) -> dict[str, int]:
    stats = {"pages_attempted": 0, "pages_read": 0, "page_cache_hits": 0}
    candidates = [item for item in sources if item.get("origin") == "外部搜索" and item.get("url", "").startswith("http")]
    # 电商详情页反爬最重、信息也最少，排最后。域名特征跨品类通用。
    ecommerce_hosts = ("jd.com", "tmall.com", "taobao.com", "1688.com", "pinduoduo", "amazon.", "ebay.", "rakuten", "detail.")
    # 下面三组都是语言层面的通用规律，不含任何行业知识
    method_words = ("怎么", "如何", "教程", "指南", "方法论", "白皮书", "步骤", "技巧", "调研", "how to", "guide", "tutorial")
    comparison_words = ("排行榜", "榜单", "十大", "排行", "top", "对比", "测评", "推荐", "vs", "comparison", "alternative", "review")
    price_words = ("价格", "报价", "售价", "多少钱", "价格表", "price", "pricing", "cost")
    price_symbols = re.compile(r"[¥￥$€£]\s?\d")
    topic_keys = [norm(term) for term in (topic_terms or []) if str(term).strip()]
    def mentions_topic(item: dict[str, Any]) -> bool:
        """标题或摘要里是否出现目标品类/产品词。

        只按"标题含'榜'"来判断，会把别的行业的榜单（如"某工具品牌榜"）也当成横向对比文读进来。
        """
        if not topic_keys:
            return True
        blob = norm(f"{item.get('title', '')}{item.get('excerpt', '')}")
        return any(term in blob for term in topic_keys)
    def looks_like_price_page(item: dict[str, Any]) -> bool:
        """按内容特征判断是不是报价页。

        以前靠硬编码一批比价站域名（smzdm/zol/it168…），换个品类（仪器、软件）就完全失效；
        改成看页面自身有没有价格意图和货币数字，任何品类的报价页都能命中。
        """
        blob = f"{item.get('title', '')}{item.get('excerpt', '')}"
        return bool(price_symbols.search(blob)) or any(word in blob.lower() for word in price_words)
    def read_priority(item: dict[str, Any]) -> tuple[int, int]:
        host = urllib.parse.urlsplit(item.get("url", "")).netloc.lower()
        title = str(item.get("title", "")).lower()
        # 与目标产品无关的页面读进来也是噪音，排在最后
        if not mentions_topic(item):
            return (5, 0)
        # 带明确报价的页面信息密度最高
        if looks_like_price_page(item):
            return (0, 0)
        # "怎么做竞品调研"这类方法论文章与目标产品无关，读它纯属浪费读取额度
        if any(word in title for word in method_words):
            return (4, 0)
        # 榜单与横向对比文天然并列多个品牌，是竞品横向数据的主要来源
        if any(word in title for word in comparison_words):
            return (1, item.get("type") != "竞品发现")
        # 电商详情页反爬最重，排最后
        return (3 if any(word in host for word in ecommerce_hosts) else 2, item.get("type") != "竞品发现")
    candidates.sort(key=read_priority)
    targets = pick_read_targets(candidates, limit, subjects)
    stats["pages_attempted"] = len(targets)
    if not targets:
        return stats
    # 并发抓取：串行时 8 页各带重试最坏要几分钟，界面会长时间卡在 spinner
    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        fetched = list(pool.map(lambda item: fetch_public_text(item["url"]), targets))
    for item, (text, state) in zip(targets, fetched):
        if not text:
            item["status"] = "无法读取原文"
            continue
        stats["pages_read"] += 1
        stats["page_cache_hits"] += int(state == "缓存")
        item["status"] = "已读取"
        item["excerpt"] = text[:6000]
        item["content_hash"] = sha256(text.encode())
        item["location"] = "网页正文"
    return stats


def product_record(brand: str, product: str = "", model: str = "", sku: str = "", platform: str = "用户文件", region: str = "", currency: str = "", price: str = "", specifications: str = "", rating: str = "", review_count: str = "", review_summary: str = "", source_id: str = "", status: str = "待确认") -> dict[str, Any]:
    return {"brand": brand, "product": product, "model": model, "sku": sku, "platform": platform, "region": region, "currency": currency, "price": price, "specifications": specifications, "rating": rating, "review_count": review_count, "review_summary": review_summary, "source_id": source_id, "status": status}


def extract_file_evidence(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    products: list[dict[str, Any]] = []
    for doc in parsed["documents"]:
        excerpt = f"已读取 {doc['name']}，用于识别产品类别、品牌和比较字段。"
        sources.append(make_source(doc["id"], doc["name"], "用户上传文件", "用户文件", doc["status"], excerpt, "用户文件", "整份文件", "file"))
        for page in doc.get("pages", []) if isinstance(doc.get("pages", []), list) else []:
            if isinstance(page, dict) and page.get("text"):
                # 每页留足正文，截得太短（原先 1200）会让规格书这类密集页面在抽取时丢参数
                sources.append(make_source(f"{doc['id']}-page-{page['page']}", f"{doc['name']} · 第 {page['page']} 页", "用户上传文件", "PDF 页面", "已读取", page["text"][:4500], "用户文件", f"page={page['page']}", "file"))
    for table_index, table in enumerate(parsed.get("tables", [])):
        brand_columns = [column for column in table.columns if any(word in str(column).lower() for word in ("品牌", "厂商", "公司", "产品", "竞品", "供应商"))]
        if not brand_columns:
            continue
        brand_column = brand_columns[0]
        sheet_name = "工作表"
        source_id = parsed["documents"][0]["id"] if parsed["documents"] else f"table-{table_index+1}"
        for doc in parsed["documents"]:
            sheets = doc.get("sheets", [])
            if table_index < len(sheets):
                sheet_name = sheets[table_index]["name"]
                source_id = doc["id"]
                break
        for row_index, row in table.iterrows():
            brand = str(row.get(brand_column, "")).strip()
            if not brand or norm(brand) not in {norm(item) for item in parsed["brands"]}:
                continue
            for dimension in parsed["dimensions"]:
                matching = columns_for_dimension(list(table.columns), dimension)
                values = [str(row.get(column, "")).strip() for column in matching if str(row.get(column, "")).strip()]
                if not values:
                    continue
                facts.append({"competitor": brand, "dimension": dimension, "value": "；".join(dict.fromkeys(values)), "status": "来自用户文件", "source_id": source_id, "confidence": .9, "basis": "文件原文", "location": f"{sheet_name}!第 {row_index + 2} 行"})
            def find_value(words: tuple[str, ...]) -> str:
                values = [str(row.get(column, "")).strip() for column in table.columns if any(word in str(column).lower() for word in words) and str(row.get(column, "")).strip()]
                return "；".join(dict.fromkeys(values))
            products.append(product_record(
                brand=brand,
                product=find_value(("产品名称", "商品名称", "产品", "商品")),
                model=find_value(("型号", "model", "系列", "版本")),
                sku=find_value(("sku", "货号", "商品编码", "产品编码")),
                platform=find_value(("平台", "渠道", "店铺")) or "用户文件",
                region=find_value(("地区", "市场", "国家")),
                currency=find_value(("币种", "货币")),
                price=find_value(("价格", "售价", "到手价", "费用")),
                specifications=find_value(("规格", "参数", "配置", "尺寸", "重量", "量程", "精度")),
                rating=find_value(("评分", "星级")),
                review_count=find_value(("评论数", "评价数", "review")),
                review_summary=find_value(("评论摘要", "评价", "反馈", "差评")),
                source_id=source_id,
                status="来自用户文件",
            ))
    return sources, facts, products


def missing_queries(parsed: dict[str, Any], facts: list[dict[str, Any]]) -> list[str]:
    queries: list[str] = []
    profile = market_profile(parsed.get("market", ""))
    subject = query_subject(parsed)
    ai = parsed.get("ai_analysis", {})
    plan = ai.get("search_plan", []) if isinstance(ai, dict) else []
    covered_map = {(norm(fact["competitor"]), fact["dimension"]) for fact in facts if fact["status"] == "来自用户文件"}
    for item in plan:
        entity = str(item.get("target_entity", ""))
        dimension = str(item.get("target_dimension", ""))
        if entity and dimension and (norm(entity), dimension) in covered_map:
            continue
        query = str(item.get("query", "")).strip().replace("待确认产品类别", subject).strip()
        # AI 会把"国内电商"这类内部市场标签写进检索词，搜索引擎不认识，去掉后更接近真实问法
        for label in ("国内电商", "海外电商", "国内", "海外"):
            query = query.replace(label, "").strip()
        query = re.sub(r"\s{2,}", " ", query)
        # AI 有时会输出 [Competitor Brand] 这类未替换的模板占位符，检索词必须含真实实体
        if query and "[" not in query and "]" not in query:
            queries.append(query)
    is_overseas = str(parsed.get("market") or "") == "海外电商"
    price_queries: list[str] = []
    for brand in parsed["brands"][:5]:
        covered = {fact["dimension"] for fact in facts if norm(fact["competitor"]) == norm(brand) and fact["status"] == "来自用户文件"}
        for dimension in parsed["dimensions"]:
            if dimension not in covered:
                if any(word in dimension.lower() for word in ("价格", "售价", "套餐", "性价比", "price")):
                    # 价格主要在比价/电商页；实测 query 不带价格意图时搜索引擎不会去找比价页
                    price_queries.append(f"{subject} {'price quote' if is_overseas else '价格 报价'}")
                    continue
                if is_overseas and subject:
                    # 海外档的品牌名和维度名都是中文，拼进去只会污染英文检索词，只用英文主体+维度
                    queries.append(f"{subject} {dimension}")
                    continue
                parts = [brand]
                if subject and norm(subject) not in norm(brand):
                    parts.append(subject)
                parts.append(dimension)
                queries.append(" ".join(part for part in parts if part))
    if subject:
        # 竞品发现要优先：否则会被 [:6] 的配额截断，导致只盘点单一品牌、没有横向对比
        queries.insert(0, f"{subject} {profile['discovery_term']}")
    # 价格检索词同样要优先，否则会被 [:6] 截掉，报告里价格一栏永远是空的
    for item in reversed(list(dict.fromkeys(price_queries))):
        queries.insert(1, item)
    return list(dict.fromkeys(queries))


def build_result(parsed: dict[str, Any], brands: list[str], candidates: list[str], sources: list[dict[str, Any]], facts: list[dict[str, Any]], mode: str, cost: float, note: str, products: list[dict[str, Any]] | None = None, search_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = market_profile(parsed.get("market", ""))
    # "输入产品名称"模式下 brands 里装的是用户指定的分析对象（如"蛋白粉"），
    # 它是被分析的目标而不是竞品，不能进比较列表。
    listed = [] if parsed.get("input_mode") == "输入产品名称" else brands
    # AI 标为“自家产品”的实体（品牌本身及其产品线）是被分析对象，不是竞品。
    # 不做这一步，上传 Tecan 资料时会得到一份「Tecan vs Tecan 的各个型号」的荒诞比较表。
    own_keys = {norm(item.get("name", "")) for item in parsed.get("entities", []) if isinstance(item, dict) and item.get("relation") == "own_product"}
    competitors = [
        name for name in dict.fromkeys(listed + candidates)
        if not is_category_like(name, parsed) and norm(name) not in own_keys
    ]
    task = {"category": parsed["category"], "subject": analysis_subject(parsed), "competitors": competitors, "criteria": parsed["dimensions"], "region": profile["region"], "currency": profile["currency"], "freshness": "优先近期", "note": note}
    # 只统计联网读取的页面数；用户上传的资料页另有来源台账，
    # 混在一起会让"可用来源"这个指标失去意义（一份 28 页 PDF 会撑到 35）
    usable = sum(1 for item in sources if item["status"] in ("已读取", "本地演示", "可用") and str(item.get("url", "")).startswith("http"))
    covered = sum(1 for fact in facts if fact["status"] in ("来自用户文件", "来自公开原文", "已核验"))
    status = "可形成初步判断" if covered else ("待核验" if sources else "未获得结果")
    review_queue = [{"id": f"review-{index+1}", "competitor": fact["competitor"], "dimension": fact["dimension"], "issue": fact["value"], "source_id": fact.get("source_id", ""), "status": "待确认"} for index, fact in enumerate(facts) if fact["status"] in ("待核验", "待确认", "缺失")]
    ai = parsed.get("ai_analysis", {}) if isinstance(parsed.get("ai_analysis"), dict) else {}
    for conflict in ai.get("conflicts", []) if isinstance(ai.get("conflicts"), list) else []:
        if not isinstance(conflict, dict):
            continue
        values = conflict.get("values", [])
        review_queue.append({"id": f"review-{len(review_queue)+1}", "competitor": str(conflict.get("entity") or "资料内实体"), "dimension": str(conflict.get("field") or "字段冲突"), "issue": "；".join(map(str, values)) or str(conflict.get("reason") or "同一字段出现不同值"), "source_id": "、".join(map(str, conflict.get("source_refs", []))), "status": "存在冲突"})
    for item in ai.get("quality", {}).get("needs_user_confirmation", []) if isinstance(ai.get("quality"), dict) else []:
        review_queue.append({"id": f"review-{len(review_queue)+1}", "competitor": "分析范围", "dimension": "需要确认", "issue": str(item), "source_id": "", "status": "待确认"})
    return {"run_id": ("demo-" if mode == "本地演示" else "live-") + uuid.uuid4().hex[:10], "created_at": now(), "task": task, "search_mode": mode, "sources": sources, "facts": facts, "products": products or [], "review_queue": review_queue, "search_stats": search_stats or {}, "page_count": usable, "status": status, "estimated_cost": cost, "parsed": parsed, "skipped": [], "queries": [], "coverage": round(covered / max(1, len(facts)) * 100)}


def comparison_subjects(result: dict[str, Any]) -> list[str]:
    """横向比较覆盖的对象：分析对象 + 竞品。

    产品名称模式下分析对象不算竞品，但用户要回答的是"我的产品和竞品比怎么样"，
    它必须留在比较表里。不过用户输入的若是品类名（如"冲锋衣"）而非具体品牌，
    就不会有它的资料，这时不要凭空多出一行空对象。
    """
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    competitors = [str(item) for item in task.get("competitors", [])]
    subject = str(task.get("subject") or "").strip()
    if not subject or norm(subject) in {norm(item) for item in competitors}:
        return competitors
    has_evidence = any(
        norm(item.get("competitor", "")) == norm(subject) and item.get("status") in VERIFIED_STATUSES
        for item in result.get("facts", [])
    )
    return [subject] + competitors if has_evidence else competitors


def make_demo_result(parsed: dict[str, Any], note: str = "") -> dict[str, Any]:
    brands, dims = parsed["brands"][:5], parsed["dimensions"]
    sources, facts, products = extract_file_evidence(parsed)
    covered_keys = {(norm(fact["competitor"]), fact["dimension"]) for fact in facts}
    for brand in brands:
        for dim in dims:
            if (norm(brand), dim) not in covered_keys:
                facts.append({"competitor": brand, "dimension": dim, "value": "用户资料中未提及", "status": "缺失", "source_id": "", "confidence": 0, "basis": "待补充", "location": ""})
    ai_candidates = [item["name"] for item in parsed.get("entities", []) if item.get("relation") in {"possible_competitor", "substitute"}]
    library_candidates = CANDIDATE_LIBRARY.get(parsed["category"], ["Slack", "Microsoft Teams", "ClickUp"]) if parsed.get("demo") else []
    candidates = [c for c in list(dict.fromkeys(ai_candidates + library_candidates)) if norm(c) not in {norm(b) for b in brands}][:2]
    for index, candidate in enumerate(candidates):
        sid = f"candidate-{index + 1}"
        sources.append(make_source(sid, f"{parsed['category']}候选：{candidate}", f"https://www.google.com/search?q={candidate}+{parsed['category']}", "竞品发现", "待确认", f"按“{parsed['category']}”类别发现候选，尚未把搜索摘要当作事实。", "系统发现"))
        for dim in dims:
            facts.append({"competitor": candidate, "dimension": dim, "value": "尚未读取原文", "status": "待确认", "source_id": sid, "confidence": .35, "basis": "候选发现"})
    result = build_result(parsed, brands, candidates, sources, facts, "本地演示", 0.0, note, products, {"providers": ["本地演示"], "search_calls": 0, "raw_hits": 0, "unique_hits": len(sources), "duplicates_removed": 0, "cache_hits": 0, "failed_calls": 0})
    result["skipped"] = [{"brand": brand, "reason": "品牌已在用户资料中，作为内部证据使用，不重复搜索品牌概览"} for brand in brands]
    result["queries"] = missing_queries(parsed, facts)
    return result


def tavily_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    payload = json.dumps({"api_key": api_key, "query": query, "search_depth": "basic", "max_results": 5, "include_answer": False}).encode("utf-8")
    request = urllib.request.Request("https://api.tavily.com/search", data=payload, headers={"Content-Type": "application/json", "User-Agent": "Jingyantai/0.2"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [{"title": str(item.get("title", "")), "url": str(item.get("url", "")), "excerpt": str(item.get("content", ""))} for item in data.get("results", [])]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []


def brave_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 5})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": api_key, "User-Agent": "Jingyantai/0.4"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [{"title": str(item.get("title", "")), "url": str(item.get("url", "")), "excerpt": str(item.get("description", ""))} for item in data.get("web", {}).get("results", [])]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []


def exa_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    payload = json.dumps({"query": query, "numResults": 5, "contents": {"text": {"maxCharacters": 1200}}}).encode("utf-8")
    request = urllib.request.Request("https://api.exa.ai/search", data=payload, headers={"Content-Type": "application/json", "x-api-key": api_key, "User-Agent": "Jingyantai/0.4"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [{"title": str(item.get("title", "")), "url": str(item.get("url", "")), "excerpt": str(item.get("text", ""))} for item in data.get("results", [])]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []


def serpapi_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode({"engine": "google", "q": query, "api_key": api_key, "num": 5})
    request = urllib.request.Request(url, headers={"User-Agent": "Jingyantai/0.4"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [{"title": str(item.get("title", "")), "url": str(item.get("link", "")), "excerpt": str(item.get("snippet", ""))} for item in data.get("organic_results", [])]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []


def search_system_prompt(market: str) -> str:
    profile = market_profile(market)
    return f"""你是竞品检索助手，负责联网搜索市场上真实存在的产品信息。{profile['lang_hint']}，目标市场：{profile['region']}。
硬性要求：
1. 来源要覆盖多类公开信息：{profile['sources']}。
2. 只输出本次联网检索真实返回的页面。每条必须带 source_url，且必须是能直接打开的完整 URL（含路径或查询参数）。
3. 严禁凭记忆、常识或推测编写 URL；严禁输出网站首页、泛域名或示例域名。无法确认真实 URL 的产品，宁可不输出这一条。
4. 只提取页面上明确写出的信息，不得虚构参数、价格、型号或市场地位。
5. 输出为 JSON 数组，每条含：product_name、vendor、model、key_specs、source_url。
6. 只输出 JSON 数组本体，不要 Markdown 代码块、解释或多余字段。"""


def _usable_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if not parsed.path.strip("/"):
        return False
    low = url.lower()
    return not any(word in low for word in ("example.com", "example.org", "placeholder", "yourdomain"))


@functools.lru_cache(maxsize=1024)
def url_is_dead(url: str) -> bool:
    """只判定"确定不存在"的链接（404/410）。

    403/405/429/超时多为反爬或站点限制，不能据此判断链接虚假，一律视为存活，
    避免误杀真实的电商/媒体链接。
    """
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 Jingyantai/0.7", "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status >= 400
    except urllib.error.HTTPError as exc:
        return exc.code in (404, 410)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def qwen_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    """百炼原生 API 联网检索。

    compatible-mode 不返回 search_info（已实测），模型只能在正文里凭记忆编 URL；
    原生接口配 search_options.enable_source=true 才会回传本次检索到的真实 URL。
    因此这里只取 search_info.search_results，完全忽略模型正文里的任何 URL。
    """
    endpoint = configured_value("QWEN_SEARCH_ENDPOINT", "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation")
    model = configured_value("QWEN_SEARCH_MODEL", "qwen-turbo")
    payload = json.dumps({
        "model": model,
        "input": {"messages": [{"role": "user", "content": f"{search_system_prompt(market)}\n检索主题：{query}\n请联网检索并列出相关产品的公开来源。"}]},
        "parameters": {
            "result_format": "message",
            "enable_search": True,
            "search_options": {"enable_source": True, "enable_citation": False},
            "max_tokens": 1200,
        },
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "Jingyantai/0.7"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    output = data.get("output", {}) if isinstance(data.get("output"), dict) else {}
    search_info = output.get("search_info") or data.get("search_info") or {}
    results = search_info.get("search_results", []) if isinstance(search_info, dict) else []
    hits: list[dict[str, str]] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append({"title": str(item.get("title") or item.get("site_name") or url).strip(), "url": url, "excerpt": str(item.get("site_name") or "").strip()})
    return [hit for hit in hits if _usable_url(hit["url"])][:8]


def bocha_search(query: str, api_key: str, market: str = "") -> list[dict[str, str]]:
    """博查 BochaAI 全网搜索。

    与纯关键词搜索不同，它的 summary 字段是已经归纳过的正文内容，
    常直接带价格、规格与发布时间；dateLastCrawled 可用于判断时效。
    因此博查来源的 excerpt 直接用 summary，抽取阶段按"搜索摘要"对待。
    """
    payload = json.dumps({
        "query": query,
        "freshness": "oneMonth",
        "summary": True,
        "count": 8,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.bochaai.com/v1/web-search",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    hits: list[dict[str, str]] = []
    for item in pages if isinstance(pages, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append({
            "title": str(item.get("name") or item.get("siteName") or url).strip(),
            "url": url,
            "excerpt": str(item.get("summary") or item.get("snippet") or "").strip(),
        })
    return [hit for hit in hits if _usable_url(hit["url"])][:8]


SEARCH_ADAPTERS = {"qwen": qwen_search, "bocha": bocha_search, "tavily": tavily_search, "brave": brave_search, "exa": exa_search, "serpapi": serpapi_search}
SEARCH_KEY_NAMES = {"qwen": "QWEN_SEARCH_API_KEY", "bocha": "BOCHA_API_KEY", "tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY", "exa": "EXA_API_KEY", "serpapi": "SERPAPI_API_KEY"}
SEARCH_UNIT_COST = {"qwen": 0.0, "bocha": .003, "tavily": .008, "brave": .005, "exa": .007, "serpapi": .025}


def configured_search_providers() -> list[tuple[str, str]]:
    requested = [item.strip().lower() for item in configured_value("SEARCH_PROVIDERS", "qwen,bocha,tavily,exa,serpapi,brave").split(",")]
    result = []
    for provider in requested:
        if provider in SEARCH_ADAPTERS:
            key = configured_key(SEARCH_KEY_NAMES[provider])
            if provider == "qwen":
                key = key or configured_key("QWEN_API_KEY") or configured_key("FILE_ANALYSIS_API_KEY")
            if key:
                result.append((provider, key))
    return result


def selected_search_provider() -> tuple[str, str]:
    providers = configured_search_providers()
    return providers[0] if providers else ("demo", "")


def cached_search(provider: str, query: str, api_key: str, market: str = "") -> tuple[list[dict[str, str]], bool]:
    cache = st.session_state.setdefault("search_result_cache", {})
    cache_key = sha256(f"{provider}\n{market}\n{query}".encode())
    if cache_key in cache:
        return cache[cache_key], True
    hits = SEARCH_ADAPTERS[provider](query, api_key, market)
    if hits:
        cache[cache_key] = hits
    return hits, False


def candidate_entities_from_sources(parsed: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    ai_candidates = [item["name"] for item in parsed.get("entities", []) if item.get("relation") in {"possible_competitor", "substitute"} and safe_float(item.get("confidence")) >= .5]
    ai_candidates = [name for name in ai_candidates if not is_category_like(name, parsed)]
    external = [item for item in sources if item.get("origin") == "外部搜索"][:20]
    if len(ai_candidates) >= 3 or not external:
        return list(dict.fromkeys(ai_candidates))[:5]
    base_url, api_key, model = file_analysis_config()
    if not api_key:
        return list(dict.fromkeys(ai_candidates))[:5]
    evidence = "\n".join(f"- {item['title']} | {item['canonical_url']} | {item['excerpt'][:300]}" for item in external)
    prompt = f"""你是竞品候选识别模块。目标类别是“{parsed['category']}”，已有产品/品牌是 {parsed['brands']}。
只从下列搜索来源中识别可能的直接竞品或替代品，不得补充常识。
candidates[].name 必须是**面向同一使用场景的竞品产品或品牌**（例如“康比特”“汤臣倍健”“Myprotein”）。
严禁输出：产品型号、产品线、品类名称（“增肌配方粉”“乳清蛋白粉”这类都不是品牌），
以及母公司/集团名称——多个产品同属一家公司时只输出产品本身（例如输出“钉钉”而不是“阿里巴巴”）。
返回 JSON：{{"candidates":[{{"name":"品牌名","confidence":0.0,"evidence_url":"URL"}}]}}。
不要返回零售平台、媒体、供应商、配件或已有品牌；最多 5 个候选。\n{evidence}"""
    payload = json.dumps({"model": model, "temperature": 0, "enable_thinking": False, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "Jingyantai/0.6"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = json.loads(data["choices"][0]["message"]["content"])
        found = [str(item.get("name", "")).strip() for item in raw.get("candidates", []) if isinstance(item, dict) and safe_float(item.get("confidence")) >= .5 and str(item.get("evidence_url", "")).startswith("http")]
        return [name for name in dict.fromkeys(ai_candidates + found) if not is_category_like(name, parsed)][:5]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return list(dict.fromkeys(ai_candidates))[:5]


def ai_key_findings(parsed: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把已抽取的事实归纳成可交付的对比结论。

    报告只罗列事实等于把分析工作又推回给用户；这一步补上"所以呢"。
    """
    verified = [item for item in facts if item.get("status") in ("来自用户文件", "来自公开原文", "已核验") and str(item.get("value", "")).strip()]
    if len(verified) < 3:
        return []
    base_url, api_key, model = file_analysis_config()
    if not api_key:
        return []
    lines = "\n".join(f"- {item.get('competitor')}｜{item.get('dimension')}｜{str(item.get('value'))[:180]}" for item in verified[:60])
    prompt = f"""你是资深竞品分析师。下面是从公开来源抽取、且每条都能追溯来源的竞品事实（格式：对象｜维度｜事实）。
目标类别：{parsed['category']}。
请归纳 3-5 条**关键发现**。硬性要求：
1. 每条必须是**对比性结论**——谁在哪个维度领先或落后、差距多大、几家的共性是什么；不要复述单条事实。
2. 只能使用给定事实，不得引入外部知识，不得编造数字。
3. 每条 30-60 字，书面语、客观、可直接放进汇报；不要出现"根据资料""综上所述"这类套话。
4. 事实不足以支撑结论时宁可不写；最多 5 条，可以少于 3 条。
返回 JSON：{{"findings":[{{"text":"结论","basis":"依据的品牌或维度"}}]}}

事实清单：
{lines}"""
    payload = json.dumps({"model": model, "temperature": 0, "enable_thinking": False, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "MarketLens/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        raw = json.loads(body["choices"][0]["message"]["content"])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return []
    findings = []
    for item in raw.get("findings", []) if isinstance(raw.get("findings"), list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            findings.append({"text": text, "basis": str(item.get("basis") or "").strip()})
    return findings[:5]


def _extract_web_batch(parsed: dict[str, Any], batch: list[dict[str, Any]], focus: list[str] | None, base_url: str, api_key: str, model: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_map = {item["id"]: item for item in batch}
    blocks = "\n\n".join(f"[source_id={item['id']}] URL={item['canonical_url']}\n{item['excerpt'][:4500]}" for item in batch)
    focus_entities = [name for name in (focus or parsed["brands"]) if name]
    prompt = f"""你是商品事实抽取模块。只提取下面网页原文中明确出现的信息，不得使用常识或搜索摘要。
目标类别：{parsed['category']}；关注对象：{focus_entities}；比较维度：{parsed['dimensions']}。
关注对象中的每一个都要抽取，不要只保留第一个；原文没提到某个对象时留空即可。
返回 JSON：{{"products":[{{"brand":"","product":"","model":"","sku":"","platform":"","region":"","currency":"","price":"","specifications":"","rating":"","review_count":"","review_summary":"","source_id":""}}],"facts":[{{"competitor":"","dimension":"","value":"","source_id":"","confidence":0.0}}]}}。
source_id 必须逐字使用给定值；同一型号、地区或套餐的不同价格不得合并；未明确出现的字段留空。每条 facts 的 dimension 必须属于给定比较维度。最多返回 20 条商品记录和 40 条事实。
review_summary 必须是**你对原文评价的归纳**（15-30 字、客观、书面语，例如“溶解性好、不结块，巧克力味偏甜”），严禁照抄原文段落或保留口语表达；原文没有评价时留空。
brand 字段必须是品牌或厂商名；product 字段填具体商品名，不要把品类名（如“乳清蛋白粉”）当作 product。
price 是重点字段：页面上出现的售价、活动价、价格区间、单价（元/100g、元/克）都要提取，并保留币种与规格；几个不同规格的价格不要合并。
派生指标：当原文同时给出价格与规格/蛋白含量时，"每克蛋白质单价"这类派生值可以直接计算，但必须能在原文找到计算所需的全部输入，并在 value 里写清依据（例如"404元/2罐÷(2罐×119g)=1.70元/克"）；缺少任一输入值时留空，不得估算或按常识补位。\n\n{blocks}"""
    payload = json.dumps({"model": model, "temperature": 0, "enable_thinking": False, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "User-Agent": "MarketLens/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = json.loads(data["choices"][0]["message"]["content"])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return [], []
    products, facts = [], []
    for item in raw.get("products", []) if isinstance(raw.get("products"), list) else []:
        if not isinstance(item, dict) or item.get("source_id") not in source_map or not str(item.get("brand") or item.get("product") or item.get("model") or item.get("sku") or "").strip():
            continue
        products.append(product_record(**{key: str(item.get(key, "")).strip() for key in ("brand", "product", "model", "sku", "platform", "region", "currency", "price", "specifications", "rating", "review_count", "review_summary", "source_id")}, status="来自公开原文"))
    for item in raw.get("facts", []) if isinstance(raw.get("facts"), list) else []:
        if not isinstance(item, dict) or item.get("source_id") not in source_map or item.get("dimension") not in parsed["dimensions"] or not str(item.get("value", "")).strip():
            continue
        facts.append({"competitor": str(item.get("competitor", "")).strip() or "待确认产品", "dimension": item["dimension"], "value": str(item["value"]).strip(), "status": "来自公开原文", "source_id": item["source_id"], "confidence": min(.9, max(.5, safe_float(item.get("confidence"), .65))), "basis": "网页原文抽取", "location": "网页正文"})
    return products, facts


def extract_web_facts(parsed: dict[str, Any], sources: list[dict[str, Any]], focus: list[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 用户资料和联网结果各占一半抽取配额。PDF 动辄十几页，混在一起排的话
    # 联网来源会被整批挤出配额，反之亦然。
    doc_pages = [item for item in sources if item.get("type") == "PDF 页面" and item.get("excerpt")]
    web_pages = [item for item in sources if item.get("origin") == "外部搜索" and item.get("status") == "已读取" and item.get("excerpt")]
    readable = doc_pages[:6] + web_pages[:6]
    base_url, api_key, model = file_analysis_config()
    if not readable or not api_key:
        return [], []
    # 一次性塞十几个页面会稀释模型注意力，抽出来的事实反而更少；分批抽取再合并。
    batch_size = 6
    products: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for start in range(0, len(readable), batch_size):
        batch_products, batch_facts = _extract_web_batch(parsed, readable[start:start + batch_size], focus, base_url, api_key, model)
        products.extend(batch_products)
        facts.extend(batch_facts)
    return products[:20], facts[:40]


def make_live_result(parsed: dict[str, Any], note: str = "", progress: Any = None) -> dict[str, Any]:
    """progress: 可选回调 (ratio: float, text: str)，用于向界面汇报阶段进度。"""

    def report(ratio: float, text: str = "") -> None:
        if progress:
            progress(ratio, text)

    brands, category, dims = parsed["brands"][:5], parsed["category"], parsed["dimensions"]
    known = {norm(b) for b in brands}
    market = parsed.get("market", "")
    providers = configured_search_providers()
    if not providers:
        report(1.0, "未配置搜索工具，使用本地演示结果")
        return make_demo_result(parsed, note)
    sources, facts, products = extract_file_evidence(parsed)
    report(0.08, "生成检索计划")
    queries = missing_queries(parsed, facts)[:6]
    max_search_calls = max(1, int(configured_value("MAX_SEARCH_CALLS", "16") or 16))
    stats = {"providers": [], "search_calls": 0, "raw_hits": 0, "unique_hits": 0, "duplicates_removed": 0, "cache_hits": 0, "failed_calls": 0, "dead_links": 0, "search_budget": max_search_calls, "remaining_calls": max_search_calls, "budget_reached": False, "cost_note": "成本按默认单价估算；Qwen 费用和各平台实际账单需在控制台核对"}
    cost = 0.0

    def collect(provider: str, api_key: str, selected_queries: list[str], kind_hint: str = "") -> None:
        nonlocal cost
        if provider not in stats["providers"]:
            stats["providers"].append(provider)
        for query in selected_queries:
            if stats["search_calls"] >= max_search_calls:
                stats["budget_reached"] = True
                break
            stats["search_calls"] += 1
            report(0.1 + 0.45 * min(1.0, stats["search_calls"] / max_search_calls), f"检索 {provider}：{query[:30]}")
            hits, cache_hit = cached_search(provider, query, api_key, market)
            if cache_hit:
                stats["cache_hits"] += 1
            else:
                cost += SEARCH_UNIT_COST.get(provider, 0)
            if not hits:
                stats["failed_calls"] += 1
                continue
            stats["raw_hits"] += len(hits)
            selected = [hit for hit in hits[:3] if hit.get("url")]
            if provider == "qwen":
                urls = [str(hit["url"]) for hit in selected]
                with ThreadPoolExecutor(max_workers=6) as pool:
                    dead = dict(zip(urls, pool.map(url_is_dead, urls)))
                kept = [hit for hit in selected if not dead.get(str(hit["url"]), False)]
                stats["dead_links"] += len(selected) - len(kept)
                selected = kept
            for hit in selected:
                kind = kind_hint or ("竞品发现" if "竞品" in query or "替代品" in query or "similar" in query.lower() else "缺失信息搜索")
                source = make_source(f"live-{len(sources)+1}", hit.get("title") or hit["url"], hit["url"], kind, "仅搜索摘要", str(hit.get("excerpt", ""))[:1200], "外部搜索", provider=provider)
                if not merge_source(sources, source):
                    stats["duplicates_removed"] += 1

    primary_names = {"qwen", "bocha", "tavily"}
    primary = [(provider, key) for provider, key in providers if provider in primary_names]
    if not primary:
        primary = providers[:1]
    # 发现型检索词决定能发现哪些竞品，所有引擎都要跑；补齐型检索词按引擎轮流分片。
    # 否则几个引擎重复搜同一批词，只是把预算烧在重叠结果上，留给竞品补充检索的额度就没了。
    head, tail = queries[:2], queries[2:]
    others = [item for item in primary if item[0] != "qwen"]
    for provider, key in primary:
        if provider == "qwen":
            # qwen 原生检索单次 20-40s，只跑最关键的发现与价格检索词
            collect(provider, key, queries[:3])
            continue
        share = tail[others.index((provider, key))::max(1, len(others))] if others else []
        collect(provider, key, head + share)

    candidates = [item for item in candidate_entities_from_sources(parsed, sources) if norm(item) not in known][:5]
    external_count = sum(1 for item in sources if item.get("origin") == "外部搜索")
    min_sources = int(configured_value("MIN_UNIQUE_SOURCES", "8") or 8)
    min_candidates = int(configured_value("MIN_CANDIDATES", "3") or 3)
    fallbacks = [(provider, key) for provider, key in providers if provider not in {item[0] for item in primary}]
    for provider, key in fallbacks:
        if external_count >= min_sources and len(candidates) >= min_candidates:
            break
        collect(provider, key, queries[-2:])
        external_count = sum(1 for item in sources if item.get("origin") == "外部搜索")
        candidates = [item for item in candidate_entities_from_sources(parsed, sources) if norm(item) not in known][:5]

    # 只围绕分析对象检索，抓回来的多是它自己的页面，报告会退化成"自我介绍"而不是"对比"。
    # 候选品牌确认后，针对竞品补一轮快速检索（跳过较慢的 qwen，避免把等待时间翻倍）。
    if candidates:
        subject_name = query_subject(parsed)
        competitor_queries = [f"{name} {subject_name} 参数 价格" for name in candidates[:3] if name]
        fast_providers = [item for item in primary if item[0] != "qwen"] or primary[:1]
        for provider, key in fast_providers[:1]:
            collect(provider, key, competitor_queries, kind_hint="竞品发现")
        external_count = sum(1 for item in sources if item.get("origin") == "外部搜索")

    stats["unique_hits"] = external_count
    report(0.6, f"读取网页正文（{external_count} 个来源）")
    # 品类词用来判断页面是否与目标产品相关，避免别的行业的"品牌榜"混进读取名单
    category_text = str(parsed.get("category", "")).strip()
    topic_terms = [part for part in re.split(r"[/、,，\s]+", category_text) if len(part) >= 2] if category_text not in {"", "待确认产品类别", "未识别"} else []
    page_stats = read_external_sources(sources, int(configured_value("MAX_PAGES_TO_READ", "12") or 12), [analysis_subject(parsed)] + candidates, topic_terms)
    stats.update(page_stats)
    stats["cache_hits"] += page_stats["page_cache_hits"]
    stats["remaining_calls"] = max(0, max_search_calls - stats["search_calls"])
    report(0.82, "从原文抽取产品与事实")
    web_products, web_facts = extract_web_facts(parsed, sources, [analysis_subject(parsed)] + candidates)
    existing_products = {(norm(item.get("brand", "")), norm(item.get("product", "")), norm(item.get("model", "")), norm(item.get("sku", "")), item.get("source_id", "")) for item in products}
    products.extend(item for item in web_products if (norm(item.get("brand", "")), norm(item.get("product", "")), norm(item.get("model", "")), norm(item.get("sku", "")), item.get("source_id", "")) not in existing_products)
    existing_facts = {(norm(item["competitor"]), item["dimension"], item.get("source_id", ""), norm(item["value"])) for item in facts}
    facts.extend(item for item in web_facts if (norm(item["competitor"]), item["dimension"], item.get("source_id", ""), norm(item["value"])) not in existing_facts)
    # 网页原文里出现的品牌并入比较对象，否则横向比较表只显示候选、看不到实际找到的品牌
    known_all = {norm(name) for name in brands + candidates}
    for item in web_products:
        if len(candidates) >= 10:
            break
        name = str(item.get("brand") or "").strip()
        if not name or "�" in name or is_category_like(name, parsed):  # 跳过解码失败的乱码名与品类词
            continue
        if norm(name) not in known_all:
            known_all.add(norm(name))
            candidates.append(name)
    for candidate in candidates:
        products.append(product_record(candidate, status="待确认", platform="公开来源"))
    # 归一抽取出来的对象名，否则事实匹配不上比较对象，横向比较会显示“暂无可比较的数据”
    known_names = [name for name in dict.fromkeys([analysis_subject(parsed)] + brands + candidates) if name]
    for fact in facts:
        fact["competitor"] = align_subject(str(fact.get("competitor", "")), known_names)
    result = build_result(parsed, brands, candidates, sources, facts, " + ".join(stats["providers"]), round(cost, 4), note, products, stats)
    result["queries"] = queries
    result["skipped"] = [{"brand": brand, "reason": "用户资料已覆盖该品牌，联网仅补缺失维度"} for brand in brands]
    report(0.92, "归纳关键发现")
    result["findings"] = ai_key_findings(parsed, facts)
    report(1.0, "分析完成")
    return result


def metric_rows(result: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for competitor in comparison_subjects(result):
        row = {"品牌/产品": competitor}
        for dimension in result["task"]["criteria"]:
            matches = [f for f in result["facts"] if f["competitor"] == competitor and f["dimension"] == dimension]
            verified = [f for f in matches if f["status"] in ("来自用户文件", "来自公开原文", "已核验")]
            values = list(dict.fromkeys(str(f["value"]) for f in verified if str(f.get("value", "")).strip()))
            row[dimension] = "；".join(values[:3]) if values else "—"
        row["证据完整度"] = f"{round(sum(1 for f in result['facts'] if f['competitor'] == competitor and f['status'] in ('来自用户文件', '来自公开原文', '已核验')) / max(1, len(result['task']['criteria'])) * 100)}%"
        rows.append(row)
    return pd.DataFrame(rows)


def render_comparison(result: dict[str, Any]) -> None:
    """按维度分组展示各对象的取值。

    横向比较的本质是"同一指标下不同对象谁高谁低"，所以按维度聚拢；
    没有依据的对象不列进去，避免整段铺满占位符。
    """
    criteria = result["task"]["criteria"]
    facts = result.get("facts", [])
    competitors = comparison_subjects(result)
    shown = False
    for dimension in criteria:
        entries = []
        for name in competitors:
            values = list(dict.fromkeys(
                str(fact.get("value", "")).strip()
                for fact in facts
                if fact.get("competitor") == name and fact.get("dimension") == dimension
                and fact.get("status") in VERIFIED_STATUSES and str(fact.get("value", "")).strip()
            ))
            if values:
                entries.append((name, "；".join(values[:2])))
        if not entries:
            continue
        shown = True
        lines = [f"**{dimension}**"]
        lines += [f"- {name}：{value}" for name, value in entries]
        st.markdown("\n".join(lines))
    if not shown:
        st.info("暂无可比较的数据。")
        return
    missing = [name for name in competitors if not any(
        fact.get("competitor") == name and fact.get("status") in VERIFIED_STATUSES and str(fact.get("value", "")).strip()
        for fact in facts
    )]
    if missing:
        st.markdown(f"*以下对象暂未找到可比对的公开数据：{'、'.join(missing)}*")


def dimension_insights(result: dict[str, Any]) -> pd.DataFrame:
    """Summarize what each comparison dimension can and cannot currently support."""
    rows = []
    competitors = comparison_subjects(result)
    verified_statuses = {"来自用户文件", "来自公开原文", "已核验"}
    for dimension in result["task"]["criteria"]:
        facts = [item for item in result["facts"] if item["dimension"] == dimension]
        covered = list(dict.fromkeys(item["competitor"] for item in facts if item["status"] in verified_statuses))
        missing = [name for name in competitors if name not in covered]
        examples = [f"{item['competitor']}：{item['value']}" for item in facts if item["status"] in verified_statuses and str(item.get("value", "")).strip()]
        rows.append({
            "比较项": dimension,
            "已有依据": "；".join(examples[:3]) or "暂无可核对事实",
            "仍需补充": "、".join(missing) or "已覆盖全部对象",
            "建议动作": "优先核对缺失对象的官方页或商品原文" if missing else "检查地区、版本和时间是否一致",
        })
    return pd.DataFrame(rows)


def evidence_rows(result: dict[str, Any]) -> pd.DataFrame:
    """逐条列出有依据的事实并附原文链接，便于直接回到来源核对。"""
    source_map = {item["id"]: item for item in result.get("sources", [])}
    rows = []
    for fact in result.get("facts", []):
        status = str(fact.get("status") or "")
        if status not in ("来自用户文件", "来自公开原文", "已核验"):
            continue
        source = source_map.get(str(fact.get("source_id") or ""), {})
        url = str(source.get("url") or "")
        rows.append({
            "比较对象": str(fact.get("competitor", "")),
            "比较项": str(fact.get("dimension", "")),
            "事实": str(fact.get("value", ""))[:200],
            "原文": url if url.startswith("http") else None,
        })
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    """Render a small markdown table without requiring the optional tabulate package."""
    if frame.empty:
        return "（暂无数据）"
    columns = [str(column) for column in frame.columns]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in frame.iterrows():
        values = [str(row[column]).replace("|", "\\|").replace("\n", " ") for column in frame.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def score_matrix(result: dict[str, Any]) -> pd.DataFrame:
    brands, dims = comparison_subjects(result), result["task"]["criteria"]
    data = []
    for brand in brands:
        row = []
        for dim in dims:
            verified = [f for f in result["facts"] if f["competitor"] == brand and f["dimension"] == dim and f["status"] not in ("待确认", "待核验", "缺失")]
            if not verified:
                row.append(0)
            else:
                # This is an evidence-availability score, not a product quality claim.
                row.append(round(max(float(fact.get("confidence", .5)) for fact in verified) * 100))
        data.append(row)
    return pd.DataFrame(data, index=brands, columns=dims)


def ensure_python_charts(result: dict[str, Any]) -> tuple[Path | None, Path | None]:
    if plt is None:
        raise RuntimeError("Python 图表依赖 matplotlib 未安装")
    chart_dir = ROOT / "data" / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    run_id = re.sub(r"[^a-zA-Z0-9_-]", "", result["run_id"])
    heatmap_path = chart_dir / f"{run_id}-heatmap-py.png"
    radar_path = chart_dir / f"{run_id}-radar-py.png"
    skip_radar = chart_dir / f"{run_id}-no-radar"
    if heatmap_path.exists() and (radar_path.exists() or skip_radar.exists()):
        return heatmap_path, radar_path if radar_path.exists() else None
    matrix = score_matrix(result)
    # 证据稀疏时先裁掉整行整列都为空的部分，否则整张图铺满占位没有任何信息
    matrix = matrix.loc[(matrix != 0).any(axis=1), (matrix != 0).any(axis=0)]
    if matrix.empty or matrix.shape[1] < 2:
        return None, None
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"], "font.size": 9, "axes.spines.right": False, "axes.spines.top": False, "svg.fonttype": "none", "pdf.fonttype": 42})
    fig, ax = plt.subplots(figsize=(max(6.2, len(matrix.columns) * 1.1), max(2.4, len(matrix.index) * .5 + 1.7)), dpi=300)
    image = ax.imshow(matrix.values, cmap="YlGnBu", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(matrix.columns)), matrix.columns, rotation=20, ha="right")
    ax.set_yticks(range(len(matrix.index)), matrix.index)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            value = matrix.iat[i, j]
            if value:
                ax.text(j, i, f"{value}", ha="center", va="center", color="white" if value > 55 else "#17324d", fontsize=8)
    ax.set_title("指标覆盖热图", loc="left", fontsize=11, fontweight="bold", pad=12)
    fig.colorbar(image, ax=ax, label="证据完整度", fraction=.03, pad=.03)
    fig.tight_layout()
    fig.savefig(heatmap_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    if matrix.shape[1] < 3:
        skip_radar.write_text("insufficient dimensions", encoding="utf-8")
        return heatmap_path, None
    import numpy as np
    angles = np.linspace(0, 2 * np.pi, len(matrix.columns), endpoint=False).tolist(); angles += angles[:1]
    fig, ax = plt.subplots(figsize=(6.2, 5.2), subplot_kw={"polar": True}, dpi=300)
    colors = ["#0b6b5f", "#2364aa", "#b7791f", "#8f4b8b", "#4b5563"]
    for index, brand in enumerate(matrix.index):
        values = matrix.loc[brand].tolist() + [matrix.loc[brand].iloc[0]]
        ax.plot(angles, values, linewidth=1.8, label=brand, color=colors[index % len(colors)])
        ax.fill(angles, values, alpha=.06, color=colors[index % len(colors)])
    ax.set_xticks(angles[:-1], list(matrix.columns)); ax.set_ylim(0, 100); ax.set_yticks([25, 50, 75, 100])
    ax.set_title(f"品牌对比雷达图（{len(matrix.columns)} 项指标）", loc="left", fontsize=11, fontweight="bold", pad=22)
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.12), ncol=3, frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(radar_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return heatmap_path, radar_path


def render_charts(result: dict[str, Any]) -> None:
    try:
        heatmap_path, radar_path = ensure_python_charts(result)
    except (OSError, RuntimeError) as exc:
        st.error(f"图表暂时无法生成：{exc}")
        return
    if not heatmap_path:
        return
    st.markdown("### 指标覆盖热图")
    st.image(str(heatmap_path), caption="", width="stretch")
    st.caption("颜色深浅表示该指标的资料覆盖程度。")
    if radar_path:
        st.markdown("### 品牌对比雷达图")
        st.image(str(radar_path), caption="", width="stretch")


def report_markdown(result: dict[str, Any]) -> str:
    parsed, task = result["parsed"], result["task"]
    stats = result.get("search_stats", {})
    lines = [f"# {task['category']}竞品分析报告", "", f"生成时间：{result['created_at']}", f"目标市场：{parsed.get('market') or '未指定'}", f"资料：{', '.join(d['name'] for d in parsed['documents']) or '本地演示资料'}", f"识别品牌：{', '.join(parsed['brands']) or '未识别'}", "", "## 一页结论", f"- 自动生成比较维度：{'、'.join(task['criteria'])}", f"- 来源覆盖：{result['coverage']}%；可用页面：{result['page_count']}；估算搜索成本：${result['estimated_cost']:.4f}", f"- 搜索 {stats.get('search_calls', 0)} 次，获得 {stats.get('raw_hits', 0)} 条原始结果，去重后保留 {stats.get('unique_hits', 0)} 个来源。", "- 候选竞品仅代表搜索方向，未读取原文前不纳入事实结论。", "", "## 多指标比较", markdown_table(metric_rows(result)), "", "## 关键差异与资料缺口", markdown_table(dimension_insights(result)), "", "## 人工确认", f"- 当前有 {len(result.get('review_queue', []))} 项需要确认，优先处理竞品关系、型号、地区、版本和价格冲突。", "", "## 来源清单"]
    lines.extend(f"- [{item['title']}]({item['url']})｜{item['status']}｜{item['origin']}｜{item['location']}｜{item['excerpt'][:160]}" for item in result["sources"])
    return "\n".join(lines)


def render_upload_summary(parsed: dict[str, Any]) -> None:
    st.markdown('<div class="eyebrow">资料识别</div>', unsafe_allow_html=True)
    st.markdown(f"**识别到的产品类别：** {parsed['category']}　<span class='muted'>置信度 {parsed['category_confidence']:.0%}</span>", unsafe_allow_html=True)
    st.markdown("**文件内品牌：** " + (" ".join(f'<span class=\"tag\">{b}</span>' for b in parsed["brands"]) if parsed["brands"] else "暂未识别到品牌"), unsafe_allow_html=True)
    st.markdown("**自动比较维度：** " + " ".join(f'<span class=\"tag\">{d}</span>' for d in parsed["dimensions"]), unsafe_allow_html=True)
    ai = parsed.get("ai_analysis")
    if ai:
        label = "AI API 已完成识别" if ai.get("source") == "AI API" else ai.get("source", "规则识别")
        with st.expander(f"查看文件分析结果 · {label}", expanded=True):
            entities = ai.get("entities", [])
            if entities:
                entity_rows = [{"实体": item.get("name", ""), "类型": item.get("entity_type", "unknown"), "关系": item.get("relation", "unknown"), "置信度": f"{float(item.get('confidence', 0)):.0%}"} for item in entities]
                st.dataframe(pd.DataFrame(entity_rows), hide_index=True, width="stretch")
            st.markdown("**搜索关键词：** " + ("、".join(ai.get("keywords", [])) or "未生成"))
            st.markdown("**重点关注数据：** " + ("、".join(ai.get("focus_data", [])) or "未识别"))
            st.markdown("**资料缺口：** " + ("、".join(ai.get("missing_info", [])) or "暂未发现"))
            conflicts = ai.get("conflicts", [])
            if conflicts:
                st.warning(f"发现 {len(conflicts)} 个字段冲突，已保留原值并等待核验。")
            st.caption(f"识别置信度 {float(ai.get('confidence', 0)):.0%} · Prompt {ai.get('prompt_version', AI_PROMPT_VERSION)} · {label}")
    for doc in parsed["documents"]:
        count = doc.get("page_count", doc.get("pages", 0))
        st.caption(f"{doc['name']} · {doc['type']} · {count} 个页/工作表 · {doc['status']} · 文件哈希 {doc['hash'][:12]}…")


def render_result(result: dict[str, Any]) -> None:
    headline = " · ".join(part for part in (str(result["task"].get("category", "")), format_time(result.get("created_at"))) if part)
    if headline:
        st.markdown(f"**{headline}**")
    facts = verified_facts(result)
    # 参考资料 = 联网读取的页面 + 用户资料页（PDF 按页计）
    document_pages = sum(1 for item in result.get("sources", []) if item.get("type") == "PDF 页面")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("比较对象", len(result["task"]["competitors"]))
    c2.metric("比较维度", len(result["task"]["criteria"]))
    c3.metric("参考来源", result.get("page_count", 0) + document_pages)
    c4.metric("可溯源事实", len(facts))
    stats = result.get("search_stats", {})
    with st.expander("检索过程与用量"):
        if not stats:
            st.caption("这条报告生成于早期版本，未记录检索用量。")
        else:
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            s1.metric("搜索次数", stats.get("search_calls", 0))
            s2.metric("原始结果", stats.get("raw_hits", 0))
            s3.metric("去重后来源", stats.get("unique_hits", 0))
            s4.metric("合并重复", stats.get("duplicates_removed", 0))
            s5.metric("缓存命中", stats.get("cache_hits", 0))
            remaining = stats.get("remaining_calls", stats.get("search_budget"))
            s6.metric("剩余次数", remaining if remaining is not None else "无需联网")
            st.caption(f"检索工具：{'、'.join(stats.get('providers', [])) or '本地演示'} · 已读 {stats.get('pages_read', 0)}/{stats.get('pages_attempted', 0)} 个页面 · 失败 {stats.get('failed_calls', 0)} 次 · 估算成本 ${safe_float(result.get('estimated_cost')):.4f}")
            if stats.get("budget_reached"):
                st.warning("本次任务已达到搜索次数上限，系统已停止继续调用；现有结果和缓存仍可查看。")
    if result.get("status") == "待核验":
        st.warning("当前只有来源发现或候选信息，搜索摘要不会直接当成事实。请打开来源核对原文。")
    st.markdown('<div class="report-section"><div class="eyebrow">一页结论</div></div>', unsafe_allow_html=True)
    findings = result.get("findings")
    if findings:
        st.markdown("\n".join(f"- {item['text']}" for item in findings))
    elif findings is None:
        st.info("这条报告由早期版本生成，没有结论区块。重新运行一次分析即可获得关键发现。")
    else:
        st.info("当前证据还不足以支撑对比性结论。补齐来源后重新分析，这里会说明谁在哪个维度领先。")
    parsed = result["parsed"]
    competitors = result["task"]["competitors"]
    criteria_count = len(result["task"]["criteria"])
    if parsed.get("input_mode") == "输入产品名称":
        # 用户给的是要分析的品类或产品（如"蛋白粉"），不是已有的资料或品牌，
        # 这里不能套用"上传资料"的框架，否则会把品类误说成"已有产品"。
        subject = result["task"].get("subject") or (parsed["brands"][0] if parsed["brands"] else parsed["category"])
        st.markdown(f"**分析对象：** {subject}。系统据此找到 **{len(competitors)} 个竞品品牌**，围绕 **{criteria_count} 个比较维度**收集公开信息。")
        st.markdown("**竞品品牌**\n\n" + ("、".join(competitors) or "暂未找到可确认的品牌"))
    else:
        own = parsed["brands"]
        external = competitors[len(own):]
        st.markdown(f"**分析对象：** {parsed['category']}。系统从上传资料中识别 **{len(own)} 个品牌**，又补充 **{len(external)} 个外部候选**，围绕 **{criteria_count} 个比较维度**收集公开信息。")
        summary_cols = st.columns(2)
        summary_cols[0].markdown("**资料内品牌**\n\n" + ("、".join(own) or "未识别"))
        summary_cols[1].markdown("**外部补充候选**\n\n" + ("、".join(external) or "暂无"))
    st.markdown("### 多指标横向比较")
    render_comparison(result)
    st.markdown("### 关键差异与资料缺口")
    st.dataframe(dimension_insights(result), width="stretch", hide_index=True)
    evidence = evidence_rows(result)
    if not evidence.empty:
        st.markdown("### 证据明细")
        st.dataframe(evidence, width="stretch", hide_index=True, column_config={
            "事实": st.column_config.TextColumn("事实", width="large"),
            "原文": st.column_config.LinkColumn("原文", display_text="打开"),
        })
    st.markdown("### 商品信息")
    products = result.get("products", [])
    if products:
        product_rows = [{"品牌": item.get("brand", ""), "产品": item.get("product", ""), "型号": item.get("model", ""), "SKU/货号": item.get("sku", ""), "平台": item.get("platform", ""), "地区": item.get("region", ""), "币种": item.get("currency", ""), "价格": item.get("price", ""), "规格": item.get("specifications", ""), "评分": item.get("rating", ""), "评论数": item.get("review_count", ""), "评论摘要": item.get("review_summary", ""), "状态": item.get("status", "")} for item in products]
        st.dataframe(pd.DataFrame(product_rows), width="stretch", hide_index=True)
    else:
        st.info("当前来源还没有形成可定位的商品字段。后续读取商品原文后再补充价格、SKU、规格和评价。")
    render_charts(result)
    st.markdown("### 分析建议")
    weakest = dimension_insights(result)
    weak_names = weakest.loc[weakest["仍需补充"] != "已覆盖全部对象", "比较项"].tolist()[:3]
    if weak_names:
        st.info(f"以下维度还没有覆盖到全部对象：{'、'.join(weak_names)}。补齐后结论会更可靠。")
    else:
        st.success("所有比较维度都已覆盖到全部对象。")
    st.markdown("### 待确认事项")
    queue = result.get("review_queue", [])
    if queue:
        st.caption(f"共 {len(queue)} 条记录需要人工判断。候选关系、来源摘要不会自动写成正式结论，逐条核对后再采信。")
        edited = st.data_editor(pd.DataFrame(queue).rename(columns={"competitor": "产品", "dimension": "比较项", "issue": "问题", "source_id": "来源ID", "status": "处理状态"}), hide_index=True, width="stretch", disabled=["id", "产品", "比较项", "问题", "来源ID"], column_config={"处理状态": st.column_config.SelectboxColumn(options=["待确认", "已确认", "已排除", "存在冲突"], required=True)}, key=f"review-{result['run_id']}")
        result["review_queue"] = edited.rename(columns={"产品": "competitor", "比较项": "dimension", "问题": "issue", "来源ID": "source_id", "处理状态": "status"}).to_dict("records")
    else:
        st.success("当前没有待确认事项。")
    st.markdown("### 下载报告")
    d1, d2, d3 = st.columns(3)
    d1.download_button("下载 Markdown 报告", report_markdown(result), file_name=f"MarketLens_{result['run_id']}.md", mime="text/markdown", width="stretch")
    d2.download_button("下载证据 JSON", json.dumps(result, ensure_ascii=False, indent=2, default=str), file_name=f"MarketLens_{result['run_id']}.json", mime="application/json", width="stretch")
    d3.download_button("下载比较 CSV", metric_rows(result).to_csv(index=False), file_name=f"MarketLens_{result['run_id']}.csv", mime="text/csv", width="stretch")


def render_sources(result: dict[str, Any]) -> None:
    if result.get("skipped"):
        with st.expander("已跳过的重复搜索"):
            for item in result["skipped"]:
                st.write(f"{item['brand']}：{item['reason']}")
    if result.get("queries"):
        with st.expander("本次搜索词"):
            st.write("、".join(result["queries"]))
    for item in result["sources"]:
        icon = "✓" if item["status"] in ("已读取", "本地演示", "可用") else "!"
        with st.expander(f"{icon} {item['title']} · {item['type']} · {item['status']}"):
            providers = "、".join(item.get("discovered_by", [])) or item.get("provider", "")
            st.markdown(f"**来源类型：** {item['origin']}　**发现工具：** {providers or '用户文件'}　**获取时间：** {item['retrieved_at']}　**位置：** {item['location'] or '网页摘要'}")
            st.write(item["excerpt"] or "未获取到正文")
            if item["url"].startswith("http"):
                st.markdown(f"<span class='mono'>{item['url']}</span>", unsafe_allow_html=True)
                st.link_button("打开原文", item["url"])
            else:
                st.markdown(item["url"], unsafe_allow_html=True)
    st.download_button("下载来源记录", json.dumps(result["sources"], ensure_ascii=False, indent=2), file_name=f"sources_{result['run_id']}.json", mime="application/json", width="stretch")


LANDING_CSS = """
<style>
.hero-top { min-height:46vh; margin:-2.3rem -2rem 2.2rem; background:linear-gradient(158deg, #0f3733 0%, #164a43 62%, #1b544b 100%); color:#f4f8f7; display:grid; place-items:center; padding:3.4rem 2rem 3rem; animation: heroIn .9s cubic-bezier(.2,.7,.3,1) both; }
@keyframes heroIn { from { opacity:0; } to { opacity:1; } }
.hero-mark { font-family:Georgia,'Noto Serif SC',serif; font-weight:600; letter-spacing:-.035em; font-size:clamp(2.5rem, 7.5vw, 5.6rem); line-height:1; text-align:center; }
.hero-mark span { display:inline-block; color:rgba(244,248,247,.68); animation: rise .8s cubic-bezier(.2,.7,.3,1) both; }
.hero-mark::after { content:''; display:block; width:52px; height:1px; background:rgba(214,233,228,.4); margin:1.7rem auto 0; }
@keyframes rise { from { opacity:0; transform:translateY(18px); } to { opacity:1; transform:none; } }
/* 定位语与品牌名同处色块内：读者第一眼就要知道这是什么产品 */
.hero-zh { font-family:Georgia,'Noto Serif SC',serif; font-size:1.34rem; letter-spacing:.08em; text-align:center; margin-top:1.5rem; color:rgba(244,248,247,.95); animation: rise .8s .16s cubic-bezier(.2,.7,.3,1) both; }
.hero-note { color:rgba(244,248,247,.66); font-size:.9rem; text-align:center; margin:.6rem auto 0; max-width:34em; line-height:1.65; animation: rise .8s .24s cubic-bezier(.2,.7,.3,1) both; }
.page-head { border-bottom:1px solid var(--ink); padding-bottom:1rem; margin-bottom:1.6rem; }
.page-title { font-family:Georgia,'Noto Serif SC',serif; font-weight:600; font-size:1.6rem; letter-spacing:-.01em; }
.page-note { color:var(--muted); font-size:.92rem; margin:.4rem 0 0; }
.landing-steps { display:grid; grid-template-columns:repeat(3,1fr); margin-top:2.8rem; border-top:1px solid var(--line); animation: rise .9s .32s cubic-bezier(.2,.7,.3,1) both; }
.landing-step { padding:1.4rem 1.5rem; border-right:1px solid var(--line); }
.landing-step:last-child { border-right:0; }
.landing-step b { display:block; font-size:1rem; font-weight:600; margin-bottom:.5rem; color:var(--brand); }
.landing-step span { color:var(--muted); font-size:.9rem; line-height:1.7; }
@media (max-width:760px) { .hero-top { min-height:38vh; padding:2.6rem 1.2rem 2.4rem; } .hero-zh { font-size:1.1rem; } .landing-steps { grid-template-columns:1fr; } .landing-step { border-right:0; border-bottom:1px solid var(--line); } .landing-step:last-child { border-bottom:0; } }
</style>
"""


def render_landing() -> None:
    st.markdown(LANDING_CSS, unsafe_allow_html=True)
    st.markdown("""
<div class="hero-top">
  <div>
    <div class="hero-mark">Market<span>Lens</span></div>
    <div class="hero-zh">竞品情报工作台</div>
    <p class="hero-note">聚合多来源信息，横向对比竞品的关键指标；每一条结论都能回溯到原文。</p>
  </div>
</div>
""", unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1.4])
    if c1.button("使用说明", width="stretch"):
        st.session_state["view"] = "guide"
        st.rerun()
    if c2.button("开始使用", type="primary", width="stretch"):
        st.session_state["view"] = "app"
        st.rerun()
    st.markdown("""
<div class="landing-steps">
  <div class="landing-step"><b>1 提交资料</b><span>上传已有资料，或直接输入一个产品名称。</span></div>
  <div class="landing-step"><b>2 自动检索</b><span>按国内或海外市场，检索品牌官网、专业评测、行业报告与电商页面。</span></div>
  <div class="landing-step"><b>3 对比与溯源</b><span>生成可横向比较的指标，每条事实都标注来源，可逐条回到原文核对。</span></div>
</div>
""", unsafe_allow_html=True)


def render_guide() -> None:
    st.markdown(LANDING_CSS, unsafe_allow_html=True)
    st.markdown("""
<div class="page-head">
  <div class="page-title">使用说明</div>
  <p class="page-note">MarketLens 如何工作，以及它为什么值得信任。</p>
</div>
""", unsafe_allow_html=True)
    st.markdown("""
**它能做什么**

把散落在公开渠道的竞品信息，整理成可横向对比、每条都能追溯的分析结果。适合选品前的竞品摸底、定价参考、销售话术准备，以及定期的竞品监测。

两种输入方式：

- **上传已有资料**（Excel / CSV / PDF）—— 系统先读资料，区分哪些字段已被现有资料覆盖、哪些还缺，再联网只补缺失部分，不做重复劳动。
- **只输入产品名称** —— 系统先确认产品的身份与类别，再从公开来源找出竞品品牌。

**三步完成一次分析**

1. **提交资料** —— 上传文件，或输入一个产品名称。系统用 AI 识别产品类别、品牌、型号，并生成与该产品购买决策相关的比较维度（不使用固定行业模板）。这一步约需 20–30 秒。
2. **选择检索范围** —— 决定检索语言与来源侧重，两者产出完全不同：
   - **国内电商**：中文来源为主，覆盖品牌官网、行业媒体、专业评测、行业研报与比价页。
   - **海外电商**：当地语言来源为主，覆盖品牌官网、评测媒体与零售渠道。
   - 档位可选 **标准**（较快）或 **深度**（字段更全，但耗时更长）。
3. **核对与导出** —— 结果页给出关键发现、横向比较、证据明细与来源清单；每条事实都能点开原文核对。结果可导出为 Markdown、JSON 或 CSV。

**结果页怎么看**

| 区块 | 内容 |
|---|---|
| 关键发现 | 基于已抓取事实归纳的对比性结论，例如谁在哪个维度领先、差距多少 |
| 多指标横向比较 | 按品牌分组列出各指标取值，一个品牌读一段 |
| 证据明细 | 每条事实一行，附「打开」链接直达原文 |
| 来源 | 每个来源的抓取状态；读取失败的会明确标注 |

**关于数据可信度**

- 只有网页原文中明确写出的内容才会成为事实；搜索摘要在读取原文之前不计入结论。
- 表格中出现「—」，表示该指标暂无有依据的数据。系统不会用推测填空。
- 同一字段出现互相矛盾的值时会全部保留，并列入待确认事项，由你判断。
- 来源无法访问或读取失败时会明确标注，不会自动当成已核验。
""")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("返回首页", width="stretch"):
        st.session_state["view"] = "landing"
        st.rerun()
    if c2.button("开始使用", type="primary", width="stretch"):
        st.session_state["view"] = "app"
        st.rerun()


def main() -> None:
    init_db()
    view = st.session_state.get("view", "landing")
    if view == "landing":
        render_landing()
        return
    if view == "guide":
        render_guide()
        return
    st.markdown("""
<div class="app-header">
  <div class="app-mark">MarketLens</div>
  <div class="app-sub">竞品情报工作台 · 聚合多来源信息，横向对比竞品关键指标</div>
</div>
""", unsafe_allow_html=True)
    with st.sidebar:
        st.markdown("### 显示")
        density = st.radio("信息密度", ["标准", "紧凑", "舒适"], index=0, horizontal=True)
        density_scale = {"紧凑": (".93rem", ".78rem"), "标准": ("1rem", ".86rem"), "舒适": ("1.08rem", ".95rem")}[density]
        st.markdown(f"<style>.stApp {{ font-size:{density_scale[0]}; }} .stCaption, small {{ font-size:{density_scale[1]} !important; }}</style>", unsafe_allow_html=True)
        st.divider()
        st.markdown("### 服务状态")
        st.caption("AI Key 和搜索 Key 只保存在服务端，不会写入文件或页面。")
        analysis_base_url, analysis_key, analysis_model = file_analysis_config()
        analysis_host = urllib.parse.urlparse(analysis_base_url).netloc or analysis_base_url
        st.write("文件分析 AI：" + (f"已配置 · {analysis_model} · {analysis_host}" if analysis_key else "规则降级"))
        configured_providers = [provider for provider, _ in configured_search_providers()]
        st.write("搜索工具：" + ("、".join(configured_providers) if configured_providers else "本地演示"))
        st.divider()
        if st.button("返回首页", width="stretch"):
            st.session_state["view"] = "landing"
            st.rerun()
    tabs = st.tabs(["开始分析", "分析结果", "来源", "历史记录"])
    with tabs[0]:
        market = st.segmented_control("分析市场", ["国内电商", "海外电商"], default="国内电商", help="决定检索语言与来源侧重：国内侧重中文行业媒体、评测与研报；海外侧重当地语言来源与零售渠道。电商平台只作为价格与在售佐证之一，不作为唯一来源。") or "国内电商"
        depth = st.segmented_control("AI 分析档位", ["标准", "深度"], default="标准", help="标准：qwen3.7-plus（约 2 分钟）；深度：qwen3.8-max（字段更全，约 3-4 分钟）") or "标准"
        input_mode = st.segmented_control("从哪里开始", ["上传产品资料", "输入产品名称"], default="上传产品资料") or "上传产品资料"
        uploaded_files = []
        product_name = ""
        if input_mode == "上传产品资料":
            uploaded_files = st.file_uploader("上传产品资料", type=["xlsx", "xls", "csv", "pdf"], accept_multiple_files=True, help="支持多个 Excel、CSV 或 PDF；文件内容只用于本次分析。")
        else:
            product_name = st.text_input("产品名称", placeholder="例如：Thermo Scientific Sorvall ST 8", help="尽量填写品牌和完整型号，避免同名产品识别错误。")
        if uploaded_files:
            parsed_preview = parse_files(uploaded_files)
            parsed_preview["input_mode"] = input_mode
            parsed_preview["market"] = market
            cache_key = parsed_preview.get("file_hash", "")
            if st.session_state.get("ai_file_hash") != cache_key:
                with st.spinner("正在用 AI 读取文件并提取类别、关键词和重点数据…"):
                    _, analysis_key, _ = file_analysis_config(depth)
                    ai_result = ai_analyze_documents(parsed_preview, analysis_key, depth)
                st.session_state["ai_file_hash"] = cache_key
                st.session_state["parsed_preview"] = apply_ai_analysis(parsed_preview, ai_result)
            parsed_preview = st.session_state.get("parsed_preview", parsed_preview)
            render_upload_summary(parsed_preview)
        elif input_mode == "上传产品资料":
            st.markdown('<div class="summary"><strong>还没有上传文件</strong><br><span class="muted">上传后会先识别产品、型号、已有字段和资料缺口。</span></div>', unsafe_allow_html=True)
        elif product_name:
            st.markdown(f'<div class="summary"><strong>准备分析：{product_name}</strong><br><span class="muted">系统将先确认产品身份和类别，再发现候选竞品。</span></div>', unsafe_allow_html=True)
        note = st.text_area("补充说明（可选）", placeholder="例如：重点关注中国市场、企业客户和近一年的变化。", height=80)
        submitted = st.button("开始生成分析", type="primary", width="stretch")
        if submitted:
            if input_mode == "输入产品名称":
                parsed = make_name_input(product_name, market) if product_name.strip() else None
                if parsed is not None:
                    identity_key = parsed["file_hash"]
                    if st.session_state.get("ai_product_name_hash") != identity_key:
                        with st.spinner("正在用 AI 确认产品身份、类别和检索重点…"):
                            _, analysis_key, _ = file_analysis_config(depth)
                            identity = ai_identify_product_name(parsed, analysis_key, depth)
                        st.session_state["ai_product_name_hash"] = identity_key
                        st.session_state["parsed_name_input"] = apply_ai_analysis(parsed, identity)
                    parsed = st.session_state.get("parsed_name_input", parsed)
            else:
                parsed = st.session_state.get("parsed_preview") if uploaded_files else None
                parsed = parsed or (parse_files(uploaded_files) if uploaded_files else None)
            if parsed is None:
                st.error("请上传产品资料，或切换到“输入产品名称”并填写产品。")
            else:
                parsed["input_mode"] = input_mode
                parsed["market"] = market
                note_brands = extract_brands(note, []) if note else []
                for brand in note_brands:
                    if brand not in parsed["brands"]:
                        parsed["brands"].append(brand)
                if note and parsed["category_confidence"] < .5:
                    parsed["category"], parsed["category_confidence"] = infer_category(parsed["text"] + " " + note)
                if not parsed["brands"] and not parsed.get("demo"):
                    st.error("暂未识别到品牌。请检查文件是否包含品牌/产品列，或在补充说明中写明品牌名称。")
                else:
                    if selected_search_provider()[0] != "demo" and not parsed.get("demo"):
                        bar = st.progress(0.0)
                        stage = st.empty()
                        stage.caption("读取资料并生成比较报告…")

                        def on_progress(ratio: float, text: str = "") -> None:
                            bar.progress(min(1.0, max(0.0, ratio)))
                            if text:
                                stage.caption(text)

                        result = make_live_result(parsed, note, progress=on_progress)
                        bar.empty()
                        stage.empty()
                    else:
                        result = make_demo_result(parsed, note)
                    save_history(result)
                    st.session_state["result"] = result
                    st.success("分析完成。请先看结果，再打开来源核对关键判断。")
    result = st.session_state.get("result")
    with tabs[1]:
        if result:
            render_result(result)
        else:
            st.info("还没有分析结果。先上传资料，或直接点击“开始生成分析”体验演示。")
    with tabs[2]:
        if result:
            render_sources(result)
        else:
            st.info("运行分析后，这里会显示文件来源、搜索词和访问状态。")
    with tabs[3]:
        history = load_history()
        if history.empty:
            st.info("完成一次分析后，这里会显示产品、搜索范围、来源数和状态等摘要。")
        else:
            st.dataframe(history, width="stretch", hide_index=True)
            entries = load_history_entries()
            if entries:
                st.markdown("### 回看完整报告")
                labels = dict(entries)
                picked = st.selectbox("选择一条记录，展开当时的完整报告", [run_id for run_id, _ in entries], format_func=lambda rid: labels.get(rid, rid), key="history_pick")
                stored = load_stored_result(picked) if picked else None
                if stored:
                    st.divider()
                    render_result(stored)
                else:
                    st.caption("这条记录产生于本次升级之前，只保留了摘要信息。")


if __name__ == "__main__":
    main()
