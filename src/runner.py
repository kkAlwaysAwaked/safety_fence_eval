"""
主测试编排器。

运行方式：
    python runner.py                                  # 默认：test_cases -> results.json
    python runner.py --test-cases test_cases1 \
        --output results/round3/results.json          # 指定用例集与输出路径

输出：
    results.json（或 --output 指定的路径）
"""

import argparse
import importlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import openai
from openai import OpenAI

# 让脚本无论从哪个工作目录运行，都能找到 src/ 与 data_input/ 下的模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))   # src/
sys.path.insert(0, str(ROOT / "data_input"))               # data_input/

import config
from judge import check_rewrite_quality
from safety_fence import SafetyFence


# ─────────────────────────────────────────────────────────────────────────────
# 云端调用
# ─────────────────────────────────────────────────────────────────────────────

def call_deepseek(payload: str, client: OpenAI) -> str:
    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": payload}],
            temperature=0.7,
            timeout=config.CLOUD_TIMEOUT,
        )
        return response.choices[0].message.content or ""
    except openai.APITimeoutError as exc:
        return f"[CLOUD_TIMEOUT] Request timed out after {config.CLOUD_TIMEOUT}s: {exc}"
    except Exception as exc:
        return f"[CLOUD_ERROR] {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# 单条 case 执行
# ─────────────────────────────────────────────────────────────────────────────

def run_case(tc, fence: SafetyFence, deepseek_client: OpenAI) -> dict:
    result = {
        "id": tc.id,
        "category": tc.category,
        "description": tc.description,
        "is_outbound": tc.is_outbound,
        "original_goal": tc.original_goal,
        "expected_decision": tc.expected_decision,
        # --- 围栏输出 ---
        "actual_decision": None,
        "fence_reason": None,
        "fence_raw_response": None,
        "decision_correct": None,
        # --- REWRITE 质量 ---
        "rewrite_check": None,
        # --- 门控后的实际执行决策 ---
        "effective_decision": None,
        # --- 端到端 ---
        "e2e": None,
    }

    # Step 1：Safety Fence 判决
    sf = fence.evaluate(tc.payload, tc.is_outbound)
    result["actual_decision"] = sf.decision
    result["fence_reason"] = sf.reason
    result["fence_raw_response"] = sf.raw_response
    # PARSE_ERROR 不计入准确率（视为跳过）
    result["decision_correct"] = (
        None if sf.parse_error else (sf.decision == tc.expected_decision)
    )

    if sf.parse_error:
        result["effective_decision"] = "PARSE_ERROR"
        return result

    # Step 2：REWRITE 质量检查门控  
    # 如果 REWRITE 决策，则进行 REWRITE 质量检查
    effective_decision = sf.decision

    if sf.decision == "REWRITE":
        rq = check_rewrite_quality(sf.rewritten_payload, tc.sensitive_tokens)
        result["rewrite_check"] = asdict(rq)
        if rq.rewrite_safety_failure:
            effective_decision = "REFUSE"   # 脱敏不干净，强制覆盖

    result["effective_decision"] = effective_decision

    # Step 3：端到端（仅限外发且门控通过的 case）
    if tc.is_outbound and effective_decision in ("ALLOW", "REWRITE"):
        sent_payload = (
            sf.rewritten_payload
            if sf.decision == "REWRITE"
            else tc.payload
        )
        cloud_response = call_deepseek(sent_payload, deepseek_client)
        result["e2e"] = {
            "sent_payload": sent_payload,
            "cloud_response": cloud_response,
            # 留空，供人工或外部 Judge 事后填写
            "judge_score": None,
            "judge_reason": None,
            "success": None,
        }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 汇总统计
# ─────────────────────────────────────────────────────────────────────────────

def compute_summary(results: list) -> dict:
    total = len(results)

    # 决策准确率
    def _accuracy(cases):
        # 排除 PARSE_ERROR（decision_correct=None）
        evaluated = [r for r in cases if r["decision_correct"] is not None]
        if not evaluated:
            return {"correct": 0, "total": 0, "skipped": len(cases), "rate": None}
        correct = sum(1 for r in evaluated if r["decision_correct"])
        return {
            "correct": correct,
            "total": len(evaluated),
            "skipped": len(cases) - len(evaluated),
            "rate": round(correct / len(evaluated), 4),
        }

    decision_accuracy = {
        cat: _accuracy([r for r in results if r["category"] == cat])
        for cat in ("ALLOW", "REWRITE", "REFUSE")
    }
    decision_accuracy["overall"] = _accuracy(results)

    # REWRITE 质量
    rewrite_cases = [r for r in results if r["actual_decision"] == "REWRITE"]
    rq_list = [r["rewrite_check"] for r in rewrite_cases if r["rewrite_check"]]

    quality_passed      = sum(1 for rq in rq_list if not rq["rewrite_safety_failure"])
    safety_failures     = sum(1 for rq in rq_list if rq["rewrite_safety_failure"])
    placeholder_viol    = sum(1 for rq in rq_list if not rq["placeholder_compliant"])
    residue_viol        = sum(1 for rq in rq_list if rq["sensitive_residue_found"])

    rewrite_quality = {
        "total_rewrite_cases": len(rewrite_cases),
        "quality_passed": quality_passed,
        "rewrite_safety_failures": safety_failures,
        "placeholder_violations": placeholder_viol,
        "residue_violations": residue_viol,
        "quality_pass_rate": (
            round(quality_passed / len(rewrite_cases), 4)
            if rewrite_cases else None
        ),
    }

    # 端到端
    e2e_cases = [r for r in results if r["e2e"] is not None]
    e2e_summary = {
        "ran": len(e2e_cases),
        "note": (
            "sent_payload and cloud_response recorded per case; "
            "judge_score / judge_reason / success left for external review"
        ),
    }

    return {
        "total_cases": total,
        "decision_accuracy": decision_accuracy,
        "rewrite_quality": rewrite_quality,
        "e2e": e2e_summary,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Safety Fence Evaluation")
    parser.add_argument(
        "--test-cases",
        default="test_cases",
        help="data_input/ 下的用例模块名（不含 .py），默认 test_cases",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="结果输出路径（相对路径以项目根目录为基准），默认 results.json",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 动态加载指定用例集
    test_module = importlib.import_module(args.test_cases)
    test_cases = test_module.TEST_CASES

    # 解析输出路径（相对路径基于项目根目录），并确保目录存在
    if args.output:
        out_arg = Path(args.output)
        results_path = out_arg if out_arg.is_absolute() else (ROOT / out_arg)
    else:
        results_path = ROOT / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Safety Fence Evaluation")
    print("=" * 60)
    print(f"  test cases : {args.test_cases}  ({len(test_cases)} cases)")
    print(f"  output     : {results_path}")

    print("\n[init] Loading Safety Fence (Qwen)...")
    fence = SafetyFence()

    print("[init] Connecting to DeepSeek...")
    deepseek_client = OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
    )

    total = len(test_cases)
    results = [None] * total          # 按原始顺序占位，保证输出顺序稳定
    print_lock = threading.Lock()     # 串行化进度打印
    done = 0

    print(f"\n[run] Starting {total} test cases  (concurrency={config.CONCURRENCY})...\n")

    with ThreadPoolExecutor(max_workers=config.CONCURRENCY) as executor:
        future_to_idx = {
            executor.submit(run_case, tc, fence, deepseek_client): idx
            for idx, tc in enumerate(test_cases)
        }

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            tc = test_cases[idx]
            try:
                result = future.result()
            except Exception as exc:  # run_case 内已兜底，这里只防御未预期异常
                result = {
                    "id": tc.id,
                    "category": tc.category,
                    "description": tc.description,
                    "is_outbound": tc.is_outbound,
                    "original_goal": tc.original_goal,
                    "expected_decision": tc.expected_decision,
                    "actual_decision": "PARSE_ERROR",
                    "fence_reason": f"[RUNNER_ERROR] {exc}",
                    "fence_raw_response": None,
                    "decision_correct": None,
                    "rewrite_check": None,
                    "effective_decision": "PARSE_ERROR",
                    "e2e": None,
                }

            results[idx] = result

            with print_lock:
                done += 1
                direction = "outbound" if tc.is_outbound else "local"
                correct_mark = "✓" if result["decision_correct"] else "✗"
                override_note = (
                    f"  → gate override: {result['effective_decision']}"
                    if result["effective_decision"] != result["actual_decision"]
                    else ""
                )
                print(
                    f"  [{done:03d}/{total}] {tc.id}  ({tc.category} / {direction})  "
                    f"expected={tc.expected_decision}  got={result['actual_decision']}  "
                    f"{correct_mark}{override_note}"
                )

    # 汇总
    print("\n[summary] Computing...")
    summary = compute_summary(results)

    output = {"summary": summary, "cases": results}

    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 打印摘要
    acc = summary["decision_accuracy"]["overall"]
    rq  = summary["rewrite_quality"]
    e2e = summary["e2e"]

    print("\n" + "=" * 60)
    print("Results")
    print("=" * 60)
    print(f"  Decision accuracy  : {acc['correct']}/{acc['total']}  "
          f"({acc['rate']:.1%})")
    for cat in ("ALLOW", "REWRITE", "REFUSE"):
        d = summary["decision_accuracy"][cat]
        print(f"    {cat:8s}  {d['correct']}/{d['total']}  "
              f"({d['rate']:.1%})")
    if rq["total_rewrite_cases"]:
        print(f"  REWRITE quality    : {rq['quality_passed']}/{rq['total_rewrite_cases']}  "
              f"({rq['quality_pass_rate']:.1%})  "
              f"[safety_failures={rq['rewrite_safety_failures']}]")
    print(f"  E2E cases ran      : {e2e['ran']}")
    print(f"\n  → {results_path} written.\n")


if __name__ == "__main__":
    main()
