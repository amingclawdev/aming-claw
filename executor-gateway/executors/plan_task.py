import json
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    text = (payload.get("command_text") or "").strip()

    steps = [
        "理解需求并识别影响模块",
        "设计最小改动方案",
        "实现代码并补充必要测试",
        "运行测试并输出结果摘要",
    ]

    result = {
        "ok": True,
        "summary": f"generated plan for: {text[:80]}",
        "details": {"steps": steps},
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
