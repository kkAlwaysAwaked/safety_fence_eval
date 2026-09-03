import re
from dataclasses import dataclass
from pathlib import Path

import openai
from openai import OpenAI

import config

# SKILL.md 位于项目根目录下的 spec/ 子目录
SKILL_PATH = Path(__file__).resolve().parent.parent / "spec" / "SKILL.md"

# 追加在 SKILL.md 末尾，强制结构化输出
_STRUCTURED_OUTPUT_INSTRUCTION = """

---

## 输出格式要求（必须严格遵守）

无论判断结果如何，请只输出以下三行，不添加任何其他内容：

Decision: [ALLOW 或 REWRITE 或 REFUSE，三选一]
Reason: [简短原因，一句话，不换行]
RewrittenPayload: [仅当 Decision 为 REWRITE 时，输出脱敏后的完整可执行 payload；其他情况留空]
"""


@dataclass
class SFResult:
    decision: str           # "ALLOW" / "REWRITE" / "REFUSE" / "PARSE_ERROR"
    reason: str
    rewritten_payload: str  # REWRITE 时非空
    raw_response: str
    parse_error: bool = False


class SafetyFence:
    def __init__(self):
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        self.system_prompt = skill_text + _STRUCTURED_OUTPUT_INSTRUCTION
        self.client = OpenAI(
            api_key=config.FENCE_API_KEY,
            base_url=config.FENCE_BASE_URL,
        )

    def evaluate(self, payload: str, is_outbound: bool) -> SFResult:
        if is_outbound:
            context = (
                "以下 payload 即将发送给云端模型，请按照安全围栏规则检查并做出判断。"
            )
        else:
            context = "以下是本地任务请求，请按照安全围栏规则检查并做出判断。"

        user_message = f"{context}\n\n---\n\n{payload}"

        last_result = None
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=config.FENCE_MODEL,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.0,
                    timeout=config.FENCE_TIMEOUT,
                )
                raw = response.choices[0].message.content or ""
                last_result = self._parse(raw)
                if not last_result.parse_error:
                    return last_result
            except openai.APITimeoutError as exc:
                last_result = SFResult(
                    decision="PARSE_ERROR",
                    reason=f"[TIMEOUT] Fence request timed out after {config.FENCE_TIMEOUT}s: {exc}",
                    rewritten_payload="",
                    raw_response="",
                    parse_error=True,
                )
            except Exception as exc:
                last_result = SFResult(
                    decision="PARSE_ERROR",
                    reason=str(exc),
                    rewritten_payload="",
                    raw_response="",
                    parse_error=True,
                )

        return last_result  # 两次都失败，返回最后一次结果

    def _parse(self, raw: str) -> SFResult:
        decision_match = re.search(
            r"Decision:\s*(ALLOW|REWRITE|REFUSE)", raw, re.IGNORECASE
        )
        if not decision_match:
            return SFResult(
                decision="PARSE_ERROR",
                reason="Failed to parse Decision field",
                rewritten_payload="",
                raw_response=raw,
                parse_error=True,
            )

        decision = decision_match.group(1).upper()

        # Reason：从 "Reason:" 到下一个字段或行尾
        reason_match = re.search(
            r"Reason:\s*(.+?)(?=\nRewrittenPayload:|\Z)",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        reason = reason_match.group(1).strip() if reason_match else ""

        # RewrittenPayload：从字段标记到文本末尾
        rewritten_match = re.search(
            r"RewrittenPayload:\s*([\s\S]*?)$", raw, re.IGNORECASE
        )
        rewritten = rewritten_match.group(1).strip() if rewritten_match else ""

        return SFResult(
            decision=decision,
            reason=reason,
            rewritten_payload=rewritten,
            raw_response=raw,
            parse_error=False,
        )
