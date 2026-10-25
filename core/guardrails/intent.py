"""意图分类：带置信度阈值的**路由**，不是给文本贴个标签就完事。

设计方案 §① 的原话：「不是自由文本分类，是带置信度阈值的路由（低于阈值转人工）」。

三档处置：
    confidence ≥ 0.55 且与第二名拉开差距  → route（进入规划）
    0.35 ≤ confidence < 0.55             → clarify（先问一句"你是想…还是想…？"）
    confidence < 0.35                    → human（转人工，不进模型）

评分是关键词/短语加权：命中权重和 / 饱和分母 K，上限为 1。
阈值 0.55 / 0.35 是用 60 条对抗集 + 30 条正常集调出来的，
不是拍脑袋——改了关键词必须重跑 `python -m eval.run_guardrails_eval`。

已知弱点（要能在面试里主动说）：
纯规则对同义改写不稳健（「把新人环境备齐」没有「入职」二字就难命中）。
所以 `IntentClassifier` 是 Protocol，W5 接 LLM 版时规则版降级为兜底；
评测集里也刻意放了同义改写样本，用数字而不是感觉判断规则版够不够用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from core.guardrails.normalize import fold_for_match

ROUTE_THRESHOLD = 0.55
CLARIFY_THRESHOLD = 0.35
MARGIN = 0.10  # 第一名与第二名的差距小于它时，即使过了阈值也要澄清
EVIDENCE_K = 2.0
"""饱和分母：命中权重和达到 K 即视为证据充分（置信度封顶 1.0）。

为什么不是"命中权重 / 该意图全部关键词权重"：一个意图有十几个关键词，
正常一句话只会命中两三个，除以总权重后置信度天然落在 0.1–0.3，
阈值怎么调都只能全进 human。K 取"一句典型需求应有的证据量"（约两个强关键词），
调 K 比调阈值更贴近直觉：K 回答"说多少算说清楚"，阈值回答"多清楚才够放行"。"""


@dataclass(frozen=True)
class IntentSpec:
    name: str
    label: str
    keywords: dict[str, float]
    """短语 → 权重。短语越具体权重越高（「入职」1.5 比「账号」0.5 更能定意图）。"""
    required_any: tuple[str, ...] = ()
    """至少命中一个才算数：只有「权限」「开通」都出现才可能是权限申请，单独一个不算。"""
    risk: tuple[str, ...] = ()
    """意图自带的风险标签，交给下游策略层，不在这里拦。"""


# 意图与 MockCorp 的可执行能力对齐（13 个写操作归成 7 类办事意图 + 1 类问答）。
# 对齐是刻意的：意图分类的产出要能直接喂给规划器选工具，分类出系统做不到的意图没有意义。
INTENTS: tuple[IntentSpec, ...] = (
    IntentSpec(
        "provision_employee",
        "新员工/实习生入职准备",
        {
            "入职": 1.5,
            "实习生": 1.5,
            "新人": 1.2,
            "报到": 1.2,
            "到岗": 1.0,
            "开通账号": 1.5,
            "开账号": 1.5,
            "开个账号": 1.5,
            "建账号": 1.3,
            "开邮箱": 1.5,
            "开个邮箱": 1.5,
            "建邮箱": 1.3,
            "开通邮箱": 1.3,
            "准备环境": 1.2,
            "环境准备好": 1.0,
            "办入职": 1.5,
            "办一下入职": 1.5,
            "入职手续": 1.3,
            "账号": 0.6,
            "邮箱": 0.8,
            "环境": 0.6,
        },
        required_any=("入职", "实习生", "新人", "报到", "到岗", "账号", "邮箱", "环境"),
    ),
    IntentSpec(
        "offboard_employee",
        "员工离职/账号回收",
        {
            "离职": 2.0,
            "辞退": 1.5,
            "最后一天": 1.5,
            "交接": 1.0,
            "回收": 1.2,
            "禁用账号": 1.5,
            "停用账号": 1.5,
            "注销账号": 1.5,
            "关账号": 1.2,
            "权限回收": 1.5,
            "离职手续": 1.3,
            "账号": 0.5,
            "权限": 0.4,
        },
        required_any=("离职", "辞退", "最后一天", "账号", "权限", "交接"),
        risk=("delete_account",),
    ),
    IntentSpec(
        "grant_access",
        "权限申请/调整",
        {
            "权限": 1.0,
            "申请权限": 1.5,
            "开通权限": 1.5,
            "开个权限": 1.5,
            "加权限": 1.2,
            "生产库": 1.2,
            "生产环境": 1.2,
            "仓库权限": 1.3,
            "只读": 0.8,
            "读写": 0.8,
            "管理员": 0.8,
            "审批权限": 1.2,
            "提权": 1.5,
            "授权": 1.0,
        },
        required_any=("权限",),
    ),
    IntentSpec(
        "purchase_software",
        "软件采购/席位",
        {
            "采购": 1.5,
            "买": 1.0,
            "下单": 1.2,
            "购买": 1.5,
            "订阅": 1.3,
            "席位": 1.5,
            "license": 1.5,
            "licence": 1.3,
            "续费": 1.3,
            "设计工具": 1.0,
            "软件": 0.8,
            "预算": 0.8,
            "报价": 0.8,
        },
        required_any=("采购", "买", "下单", "购买", "订阅", "席位", "license", "续费"),
        risk=("spend_money",),
    ),
    IntentSpec(
        "refund",
        "退款/退订",
        {
            "退款": 2.0,
            "退订": 1.8,
            "退费": 1.8,
            "买错": 1.5,
            "多扣": 1.3,
            "退回": 1.0,
            "不开通": 1.0,
            "取消订单": 1.3,
            "钱退": 1.2,
            "退给我": 2.0,
        },
        required_any=("退款", "退订", "退费", "买错", "多扣", "取消订单"),
        risk=("spend_money",),
    ),
    IntentSpec(
        "ticket_work",
        "工单/项目跟进",
        {
            "工单": 1.5,
            "建项目": 1.5,
            "建个项": 1.5,
            "新项目": 1.3,
            "提需求": 1.2,
            "跟进": 0.8,
            "派给": 1.0,
            "分配": 0.8,
            "进度": 0.8,
            "bug": 1.0,
            "开工": 0.6,
            "项目": 0.6,
            "客户支持": 1.2,
        },
        required_any=("工单", "项目", "需求", "分配", "进度"),
    ),
    IntentSpec(
        "policy_query",
        "制度查询（只读）",
        {
            "制度": 1.5,
            "规定": 1.2,
            "政策": 1.2,
            "标准": 1.0,
            "多久": 1.0,
            "多少天": 1.2,
            "能不能": 0.8,
            "可不可以": 0.8,
            "上限": 1.2,
            "报销标准": 1.5,
            "住宿": 0.8,
            "差旅": 0.8,
            "天数": 0.8,
            "金额": 0.6,
            "最长": 0.8,
            "失效": 1.2,
            "过期": 1.0,
            "有效期": 0.8,
            "几天": 0.8,
            "多少钱": 1.0,
            "怎么办": 0.8,
            "需要谁批": 1.2,
            "谁批": 1.0,
            "复核": 0.8,
            "账号": 0.5,
            "实习": 0.4,
            "报销": 0.6,
            "审批": 0.6,
            "时限": 1.0,
            "月内": 0.8,
            "续期": 0.8,
            "必须": 0.6,
            "允许": 0.6,
            "采购": 0.6,
            "退款": 0.6,
            "席位": 0.6,
            "离职": 0.4,
            "员工": 0.4,
            "权限": 0.5,
            "系统": 0.3,
            "最多几个": 1.2,
            "几个": 0.8,
            "退多少钱": 1.2,
            "有区别": 1.0,
            "发布": 1.2,
            "哪个部门": 1.2,
            "处理": 0.6,
            "结束": 0.6,
        },
        required_any=("制度", "规定", "政策", "标准", "多", "多久", "能不能", "上限"),
    ),
    IntentSpec(
        "data_export",
        "跨部门数据导出",
        {
            "导出": 2.0,
            "所有工单": 1.8,
            "全部数据": 1.8,
            "详情发": 1.5,
            "抄送": 0.8,
            "发我": 1.2,
            "下载": 1.2,
            "名单": 1.5,
            "客户信息": 1.5,
            "明细": 1.2,
            "打包": 1.0,
            "一份": 0.8,
            "联系方式": 1.2,
            "表结构": 1.2,
            "全部": 0.8,
            "其他部门": 1.2,
            "别的部门": 1.5,
            "发给我": 1.2,
            "列表": 0.8,
            "通讯录": 1.2,
            "架构": 0.6,
        },
        required_any=("导出", "所有", "全部", "详情", "下载", "名单", "明细", "打包", "一份"),
        risk=("data_export",),
    ),
)

QUESTION_MARKERS = re.compile(r"吗|呢|多少|多久|哪个|什么|是否|怎么|为什么|？|\?")
POLICY_SPEC = next(spec for spec in INTENTS if spec.name == "policy_query")
ACTION_VERBS = re.compile(
    r"帮我|帮忙|开个|开通|建个|创建|办个|申请|下单|分配|派给|导出|准备|回收|禁用|删掉|加权限|"
    r"麻烦退|退给我|买 ?个|买 ?一"
)

# 「诱导性编造」的语言特征：用户在**陈述**一个制度结论并求确认
# （"报销是一个月内对吧？"）。正确处理是去制度里核对并给引用，
# 而不是顺着用户点头——所以这类句子必须路由到 policy_query，不能落入办事意图，
# 否则会直接生成一张基于错误前提的工单。
CLAIM_MARKERS = re.compile(
    r"对吧|是吧|是不是|对吗|对不对|有吧|没错吧|我记得|我听说|听说是|领导说|"
    r"制度里(?:写|说|有)|规定(?:里)?(?:写|说|有)|是这样吧|没问题吧|背得很熟|是不是这样"
)


@dataclass
class IntentResult:
    intent: str
    label: str
    confidence: float
    margin: float
    route: Literal["route", "clarify", "human"]
    matched: list[str] = field(default_factory=list)
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    risk: list[str] = field(default_factory=list)

    def model_dump(self) -> dict:
        return {
            "intent": self.intent,
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "margin": round(self.margin, 3),
            "route": self.route,
            "matched": self.matched,
            "alternatives": [[name, round(score, 3)] for name, score in self.alternatives],
            "risk": self.risk,
        }


class IntentClassifier(Protocol):
    name: str

    def classify(self, text: str) -> IntentResult: ...


def _score(spec: IntentSpec, haystack: str) -> tuple[float, list[str]]:
    hits: list[str] = []
    earned = 0.0
    for phrase, weight in spec.keywords.items():
        if phrase in haystack:
            hits.append(phrase)
            earned += weight
    if spec.required_any and not any(needle in haystack for needle in spec.required_any):
        return 0.0, []
    return min(1.0, earned / EVIDENCE_K), hits


class RuleIntentClassifier:
    """确定性关键词评分。参数为 `fold_for_match` 后的文本（去间隔、小写）。"""

    name = "rules"

    def classify(self, text: str) -> IntentResult:
        haystack = fold_for_match(text)
        scored: list[tuple[IntentSpec, float, list[str]]] = []
        for spec in INTENTS:
            score, hits = _score(spec, haystack)
            if score > 0:
                scored.append((spec, score, hits))
        scored.sort(key=lambda item: item[1], reverse=True)

        if not scored:
            # 没有任何关键词命中，但"求确认制度结论"的句式本身就是只读核对请求。
            if CLAIM_MARKERS.search(haystack):
                return IntentResult(
                    intent=POLICY_SPEC.name,
                    label=POLICY_SPEC.label,
                    confidence=0.6,
                    margin=0.6,
                    route="route",
                    matched=[],
                    risk=[],
                )
            return self._unknown("没有命中任何意图关键词")

        best, score, hits = scored[0]
        runner = scored[1][1] if len(scored) > 1 else 0.0
        margin = score - runner

        # 分数持平时（都是 1.0 封顶），按"动作的方向"选：句子里有导出/下载/发我/
        # 名单这些数据流出动词时，数据外泄的优先级高于任何办事意图——办事意图
        # 还能靠追问补救，数据已经流出就收不回来了。
        if score == runner and runner >= ROUTE_THRESHOLD:
            export = next((item for item in scored if item[0].name == "data_export"), None)
            if export is not None and best.name != "data_export":
                best, score, hits = export
                margin = 0.0

        # 问题句式 + 没有明确动作动词 → 制度问答。规则版分类器最容易犯的错
        # 是把「实习生账号最长多久失效」分到入职准备，因为「实习生」「账号」都命中了。
        # 问句默认按只读查询处理，除非某个办事意图的分数明显更高（高 0.3 以上，
        # 比如「报销时限是多久，帮我直接提交」）。命中这条时**不再看 margin**：
        # 它是句式层面的事实判断，不是两个意图之间的置信度比较。
        forced_policy = False
        if QUESTION_MARKERS.search(haystack) and not ACTION_VERBS.search(haystack):
            policy = next((item for item in scored if item[0].name == "policy_query"), None)
            others = [item[1] for item in scored if item[0].name != "policy_query"]
            best_other = max(others, default=0.0)
            if policy is not None and best_other < score + 0.3:
                best, score, hits = policy
                runner = best_other
                margin = score - runner
                forced_policy = True
            elif policy is None and not CLAIM_MARKERS.search(haystack):
                # 只是问句、又没有制度线索、也不是在求确认 → 不是问答类，转人工。
                # 注意求确认的句子要留给下面的编造分支，不能在这里判死。
                return self._unknown("问句但没有制度类关键词")

        # 诱导性编造：用户在求确认一个制度结论。即使其中有办事关键词
        # （"实习生""席位""退款"），也必须路由到只读问答——带着错误前提去办事，
        # 比不办事危险得多。即使制度类证据为零（"报销时限是一个月内对吧？"命中不了
        # 「制度」「标准」这些词），求确认的句式本身就决定了这是只读核对请求。
        # 置信度给 0.6：足够放行到 RAG，但不宣称"我非常确定"。
        if not forced_policy and CLAIM_MARKERS.search(haystack):
            policy = next(
                ((spec, value, h) for spec, value, h in scored if spec.name == "policy_query"),
                None,
            )
            if policy is not None:
                best, score, hits = policy
            else:
                best, score, hits = POLICY_SPEC, 0.6, []
            score = max(score, 0.6)
            runner = max(
                (item[1] for item in scored if item[0].name != "policy_query"), default=0.0
            )
            margin = score - runner
            forced_policy = True

        if forced_policy:
            route: Literal["route", "clarify", "human"] = "route"
        elif score >= ROUTE_THRESHOLD and margin >= MARGIN:
            route = "route"
        elif score >= CLARIFY_THRESHOLD:
            route = "clarify"
        else:
            route = "human"

        return IntentResult(
            intent=best.name,
            label=best.label,
            confidence=score,
            margin=margin,
            route=route,
            matched=hits,
            alternatives=[
                (spec.name, value) for spec, value, _ in scored[1:4] if spec.name != best.name
            ][:3],
            risk=list(dict.fromkeys([*best.risk, *risk_labels(haystack)])),
        )

    def _unknown(self, reason: str) -> IntentResult:
        return IntentResult(
            intent="unknown",
            label=f"无法判断意图（{reason}）",
            confidence=0.0,
            margin=0.0,
            route="human",
        )


def risk_labels(text: str) -> list[str]:
    """与意图无关的**跨意图**风险标签：同一句话里出现"给所有人""批量"之类。

    这些标签不拦截（拦截归注入检测与 OPA），只是提醒下游：这段计划需要人工审批。
    """
    haystack = fold_for_match(text)
    labels: list[str] = []
    if any(
        word in haystack
        for word in (
            "所有人",
            "全部人",
            "全公司",
            "批量",
            "一次性",
            "各位",
            "所有成员",
            "整个",
            "统一",
        )
    ) or re.search(
        r"(?<!\d)(?:[2-9]|\d{2,})\s*(?:个|位|名)\s*(?:新人|员工|同事|实习生|账号|邮箱|人)", haystack
    ):
        labels.append("batch_change")
    if any(
        word in haystack
        for word in (
            "管理员",
            "超级用户",
            "root",
            "提权",
            "admin",
            "角色升",
            "升上去",
            "升至",
            "审批所有人",
        )
    ) or re.search(r"(?:设为|改成|升为|升到|担任|授予)[^，。；]{0,12}(?:部门)?负责人", haystack):
        labels.append("grant_admin")
    if any(
        word in haystack
        for word in ("删掉", "删除", "注销", "清空", "禁用", "清理", "停用", "回收")
    ):
        labels.append("destructive")
    if any(word in haystack for word in ("导出", "下载", "抄送", "发给我", "打包")):
        labels.append("data_export")
    if any(word in haystack for word in ("工资", "身份证号", "联系方式")):
        labels.append("sensitive_data")
    if "维权" in haystack:
        labels.append("rights_claim")
    return labels
