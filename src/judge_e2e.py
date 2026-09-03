"""
LLM-as-Judge（维度 ③：端到端任务成功率）。

读取 runner 产出的 results.json，对每条带非空 e2e 的 case，
用 spec/eval_rule.md 作为评判提示词，调用云端模型（DeepSeek）打分，
补全 judge_score / judge_reason / success，并输出 e2e_results.json。

运行方式：
    python judge_e2e.py \
        --results results/round3/results.json \
        --output  results/round3/e2e_results.json
"""

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openai
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))   # src/

import config

EVAL_RULE_PATH = ROOT / "spec" / "eval_rule.md"

# 输出 case 中保留的字段（与第二批 e2e_results.json 对齐）
_CASE_FIELDS = (
    "id", "category", "description", "is_outbound", "original_goal",
    "expected_decision", "actual_decision", "effective_decision",
)


def _strip_json(raw: str) -> str:
    """从模型回复里抽出 JSON 主体（容忍 ```json 围栏与前后噪声）。"""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fenced:
        return fenced.group(1).strip()
    brace = re.search(r"\{[\s\S]*\}", raw)
    return brace.group(0).strip() if brace else raw.strip()


def judge_one(case: dict, system_prompt: str, client: OpenAI) -> dict:
    """对单条 case 的 e2e 结果打分，返回 {judge_score, judge_reason, success}。"""
    e2e = case["e2e"]
    cloud_response = e2e.get("cloud_response") or ""

    # 云端错误/超时 → 直接 1 分，不浪费一次评判调用
    if cloud_response.startswith("[CLOUD_ERROR]") or cloud_response.startswith("[CLOUD_TIMEOUT]"):
        return {
            "judge_score": 1,
            "judge_reason": "云端返回错误或超时，未产出任何结果。",
            "success": False,
        }

    user_message = (
        "请对以下一条 case 的端到端结果按评分标准（1–5）打分，"
        "只输出一个 JSON 对象，字段为 id / judge_score / judge_reason / success。\n\n"
        f"id: {case['id']}\n"
        f"original_goal: {case['original_goal']}\n"
        f"actual_decision: {case['actual_decision']}\n"
        f"effective_decision: {case['effective_decision']}\n\n"
        f"sent_payload:\n{e2e.get('sent_payload', '')}\n\n"
        f"cloud_response:\n{cloud_response}"
    )

    for _ in range(2):
        try:
            resp = client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,
                timeout=config.CLOUD_TIMEOUT,
            )
            raw = resp.choices[0].message.content or ""
            obj = json.loads(_strip_json(raw))
            score = int(obj["judge_score"])
            score = max(1, min(5, score))
            reason = str(obj.get("judge_reason", "")).strip()
            return {
                "judge_score": score,
                "judge_reason": reason,
                "success": score >= 3,
            }
        except Exception as exc:  # noqa: BLE001 — 评判失败兜底，进入下一次重试
            last_exc = exc

    return {
        "judge_score": None,
        "judge_reason": f"[JUDGE_ERROR] {last_exc}",
        "success": None,
    }


def build_output_case(case: dict, prior_reviews: dict) -> dict:
    out = {k: case[k] for k in _CASE_FIELDS}
    e2e = case["e2e"]
    out["sent_payload"] = e2e.get("sent_payload")
    out["cloud_response"] = e2e.get("cloud_response")
    out["judge_score"] = case["judge_score"]
    out["judge_reason"] = case["judge_reason"]
    out["success"] = case["success"]
    # 保留上一轮人工复核（重跑 judge 时不丢人工填写的 human_review）
    out["human_review"] = prior_reviews.get(case["id"]) or {
        "agree_with_judge": None,
        "corrected_score": None,
        "note": "",
    }
    return out


def load_prior_human_reviews(output_path: Path) -> dict:
    """从已存在的 e2e_results.json 读出每条 case 的 human_review，用于重跑时合并保留。"""
    if not output_path.exists():
        return {}
    try:
        prev = json.loads(output_path.read_text(encoding="utf-8"))
        return {c["id"]: c["human_review"] for c in prev.get("cases", []) if c.get("human_review")}
    except Exception:
        return {}


def parse_args():
    p = argparse.ArgumentParser(description="LLM-as-Judge for e2e task success")
    p.add_argument("--results", required=True,
                   help="runner 产出的 results.json 路径（相对路径基于项目根）")
    p.add_argument("--output", required=True,
                   help="e2e_results.json 输出路径（相对路径基于项目根）")
    return p.parse_args()


def main():
    args = parse_args()

    results_path = Path(args.results)
    results_path = results_path if results_path.is_absolute() else (ROOT / results_path)
    output_path = Path(args.output)
    output_path = output_path if output_path.is_absolute() else (ROOT / output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = json.loads(results_path.read_text(encoding="utf-8"))
    all_cases = data["cases"]
    e2e_cases = [c for c in all_cases if c.get("e2e") is not None]

    prior_reviews = load_prior_human_reviews(output_path)
    if prior_reviews:
        filled = sum(
            1 for hr in prior_reviews.values()
            if hr.get("agree_with_judge") is not None
            or (hr.get("note") or "").strip()
            or hr.get("corrected_score") is not None
        )
        print(f"  [merge] 保留上一轮 human_review：{filled} 条已填写、共 {len(prior_reviews)} 条")

    system_prompt = EVAL_RULE_PATH.read_text(encoding="utf-8")
    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)

    total = len(e2e_cases)
    print("=" * 60)
    print("LLM-as-Judge (e2e task success)")
    print("=" * 60)
    print(f"  input  : {results_path}")
    print(f"  output : {output_path}")
    print(f"  judging {total} e2e cases  (concurrency={config.CONCURRENCY})...\n")

    print_lock = threading.Lock()
    done = 0

    with ThreadPoolExecutor(max_workers=config.CONCURRENCY) as ex:
        fut_to_case = {
            ex.submit(judge_one, c, system_prompt, client): c
            for c in e2e_cases
        }
        for fut in as_completed(fut_to_case):
            c = fut_to_case[fut]
            verdict = fut.result()
            c["judge_score"] = verdict["judge_score"]
            c["judge_reason"] = verdict["judge_reason"]
            c["success"] = verdict["success"]
            with print_lock:
                done += 1
                print(f"  [{done:03d}/{total}] {c['id']:14s} "
                      f"score={verdict['judge_score']}  success={verdict['success']}")

    # 汇总
    scored = [c for c in e2e_cases if c["judge_score"] is not None]
    success_n = sum(1 for c in scored if c["success"])
    dist = {str(s): sum(1 for c in scored if c["judge_score"] == s) for s in range(1, 6)}
    avg = round(sum(c["judge_score"] for c in scored) / len(scored), 3) if scored else None

    # 输出 case：按 judge_score 升序（None 排最后），低分在前
    ordered = sorted(
        e2e_cases,
        key=lambda c: (c["judge_score"] is None, c["judge_score"] if c["judge_score"] is not None else 99),
    )
    out_cases = [build_output_case(c, prior_reviews) for c in ordered]

    output = {
        "source": str(results_path.name),
        "e2e_summary": {
            "ran": total,
            "judged": len(scored),
            "success": success_n,
            "success_rate": round(success_n / len(scored), 4) if scored else None,
            "avg_judge_score": avg,
            "score_distribution": dist,
            "note": ("judge_score/judge_reason/success filled by LLM-as-Judge "
                     "per spec/eval_rule.md (dimension 3: end-to-end task success)."),
        },
        "review_instructions": (
            "按 judge_score 升序排列，低分在前。请在每条 human_review 中填写 "
            "agree_with_judge(true/false)、corrected_score(1-5)、note。"
        ),
        "total_extracted": total,
        "cases": out_cases,
    }

    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print("Judge Results")
    print("=" * 60)
    print(f"  judged        : {len(scored)}/{total}")
    print(f"  success (>=3) : {success_n}/{len(scored)}  "
          f"({(success_n/len(scored)*100) if scored else 0:.1f}%)")
    print(f"  avg score     : {avg}")
    print(f"  distribution  : {dist}")
    print(f"\n  → {output_path} written.\n")


if __name__ == "__main__":
    main()
