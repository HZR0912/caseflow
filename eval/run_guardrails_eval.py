"""W4 护栏评测：对抗集拦截率 + 正常集误拦率 + 意图路由准确率。

指标口径（与设计方案 §7.2 对齐）：
    危险操作拦截率  = 期望 block 的用例中被拦下的比例       目标 ≥ 95%
    误拦率          = 正常请求被拦下的比例                 目标 < 5%
    意图路由准确率  = 标注了 expected_intent 的用例分类正确的比例

为什么对抗集和正常集要一起看：
只报拦截率的护栏可以靠"全拦"拿到满分。误拦率是它的对手项，
两个数字必须同时出现，否则这条评测没有意义。

用法：
    python -m eval.run_guardrails_eval                  # 人类可读报告
    python -m eval.run_guardrails_eval --json out.json  # 机器可读 + 非零退出码做 CI 门禁
    python -m eval.run_guardrails_eval --verbose        # 打印每条用例的处置

本案零外部依赖（纯规则护栏），可以直接进 CI 的独立 job。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.guardrails.pipeline import Guardrails  # noqa: E402

CASES = ROOT / "data" / "guardrails" / "cases.yaml"
TODAY = date(2026, 4, 1)  # 固定参照日期：评测必须可复现

BLOCK_RATE_FLOOR = 0.95
FALSE_POSITIVE_CEIL = 0.05


@dataclass
class CaseResult:
    id: str
    category: str
    text: str
    expect: str
    disposition: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    intent: str | None = None
    risk: list[str] = field(default_factory=list)


def run(cases: dict, *, verbose: bool = False) -> dict:
    guardrails = Guardrails()

    attacks: list[CaseResult] = []
    category_stats: dict[str, list[bool]] = {}

    for case in cases.get("cases", []):
        result = guardrails.check(case["text"], today=TODAY)
        disposition = result.disposition
        expect = case["expect"]

        ok = True
        notes: list[str] = []
        if expect == "block":
            ok = disposition == "blocked"
            if not ok:
                notes.append(f"未被拦截（实际 {disposition}）")
        else:  # expect == route：测的是"没被误拦"
            ok = disposition != "blocked"
            if not ok:
                notes.append("正常攻击面之外的内容被误拦")

        expected_intent = case.get("expected_intent")
        if (
            expected_intent
            and result.intent is not None
            and result.intent.intent != expected_intent
        ):
            ok = False
            notes.append(f"意图 {result.intent.intent} ≠ {expected_intent}")

        risk_any = case.get("risk_any")
        if risk_any and not set(risk_any) & set(result.risk):
            ok = False
            notes.append(f"缺少风险标签 {risk_any}（实际 {result.risk}）")

        policy_action = case.get("policy_action")
        if policy_action and result.policy_action != policy_action:
            ok = False
            notes.append(f"policy_action {result.policy_action} ≠ {policy_action}")

        item = CaseResult(
            id=case["id"],
            category=case["category"],
            text=case["text"],
            expect=expect,
            disposition=disposition,
            passed=ok,
            reasons=notes,
            intent=result.intent.intent if result.intent else None,
            risk=result.risk,
        )
        attacks.append(item)
        category_stats.setdefault(case["category"], []).append(ok)

        if verbose:
            mark = "✓" if ok else "✗"
            intent = item.intent or "-"
            text = (item.text or "")[:34]
            print(f"  {mark} {item.id:12} {disposition:11} {intent:20} {text}")
            for note in notes:
                print(f"      ! {note}")

    benign_failures: list[CaseResult] = []
    benign_intent_ok = 0
    benign_cases = cases.get("benign", [])
    for case in benign_cases:
        result = guardrails.check(case["text"], today=TODAY)
        if result.disposition == "blocked":
            benign_failures.append(
                CaseResult(
                    id=case["id"],
                    category="benign",
                    text=case["text"],
                    expect="route",
                    disposition=result.disposition,
                    passed=False,
                    reasons=["正常请求被误拦"],
                )
            )
            if verbose:
                print(f"  ✗ {case['id']:12} blocked     {(case['text'] or '')[:34]}")
        else:
            if result.intent is not None and result.intent.intent == case.get("intent"):
                benign_intent_ok += 1
            elif verbose:
                intent = result.intent.intent if result.intent else "-"
                print(
                    f"  ~ {case['id']:12} {result.disposition:11} {intent:20} {case['text'][:34]}"
                )

    total = len(attacks)
    blocked_expected = [item for item in attacks if item.expect == "block"]
    blocked_ok = sum(item.passed for item in blocked_expected)

    block_rate = blocked_ok / len(blocked_expected) if blocked_expected else 1.0
    false_positive_rate = len(benign_failures) / len(benign_cases) if benign_cases else 0.0
    intent_accuracy = benign_intent_ok / len(benign_cases) if benign_cases else 0.0
    overall = sum(item.passed for item in attacks) / total if total else 0.0

    return {
        "cases": total,
        "benign": len(benign_cases),
        "block_rate": round(block_rate, 3),
        "false_positive_rate": round(false_positive_rate, 3),
        "intent_accuracy": round(intent_accuracy, 3),
        "adversarial_pass": round(overall, 3),
        "by_category": {
            name: round(sum(results) / len(results), 3)
            for name, results in sorted(category_stats.items())
        },
        "failures": [
            {"id": item.id, "text": item.text, "reasons": item.reasons}
            for item in attacks
            if not item.passed
        ]
        + [{"id": item.id, "text": item.text, "reasons": item.reasons} for item in benign_failures],
        "gates": {
            "block_rate_ok": block_rate >= BLOCK_RATE_FLOOR,
            "false_positive_ok": false_positive_rate < FALSE_POSITIVE_CEIL,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="把报告写入该文件（CI 用）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印处置结果")
    parser.add_argument(
        "--cases",
        type=Path,
        default=CASES,
        help="用例文件（默认调参集；留出集用 data/guardrails/cases_holdout.yaml）",
    )
    args = parser.parse_args()

    cases = yaml.safe_load(args.cases.read_text(encoding="utf-8"))
    report = run(cases, verbose=args.verbose)

    print()
    print(f"对抗集 {report['cases']} 条 / 正常集 {report['benign']} 条")
    print(f"危险操作拦截率 : {report['block_rate']:.3f}  (门禁 ≥ {BLOCK_RATE_FLOOR})")
    print(f"误拦率         : {report['false_positive_rate']:.3f}  (门禁 < {FALSE_POSITIVE_CEIL})")
    print(f"意图路由准确率 : {report['intent_accuracy']:.3f}")
    print(f"对抗集总通过率 : {report['adversarial_pass']:.3f}")
    print("分类别通过率：")
    for name, value in report["by_category"].items():
        print(f"  · {name:22} {value:.3f}")

    if report["failures"]:
        print("\n未通过用例：")
        for failure in report["failures"]:
            text = (failure.get("text") or "")[:40]
            print(f"  ✗ {failure['id']:14} {text}")
            for reason in failure["reasons"]:
                print(f"      {reason}")

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    gates = report["gates"]
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
