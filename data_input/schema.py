from dataclasses import dataclass, field


@dataclass
class TestCase:
    id: str
    category: str             # "ALLOW" / "REWRITE" / "REFUSE"
    description: str
    payload: str              # 送进 Safety Fence 的原始内容
    is_outbound: bool         # True = 数据即将出本机
    expected_decision: str
    original_goal: str        # 用户原始任务目标（e2e 记录用）
    sensitive_tokens: list = field(default_factory=list)  # REWRITE 残留检查用
