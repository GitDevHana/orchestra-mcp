"""Real Codex A/B trial on the running dashboard. Explicitly uses subscription."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--max-seconds", type=int, required=True)
    args = parser.parse_args()
    metadata = json.loads((ROOT / "demo-data" / "records" / "server.json").read_text(encoding="utf-8"))
    dashboard = metadata["dashboard"]
    url = urlsplit(dashboard)
    base = url.scheme + "://" + url.netloc

    def api(path, body=None):
        request = Request(base + path + ("&" if "?" in path else "?") + url.query,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    workspace = ROOT / "real-runs" / ("codex-dual-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("Independent clamp implementation trial. Source input must remain unchanged.\n", encoding="utf-8")
    checks = """from clamp import clamp
for value, low, high, expected in [(5,0,10,5),(-1,0,10,0),(11,0,10,10),(3,3,3,3)]:
    assert clamp(value,low,high)==expected
try:
    clamp(5,10,0)
except ValueError:
    pass
else:
    raise AssertionError('invalid bounds must raise ValueError')
print('5 fixed acceptance checks passed')
"""
    spec = {"task": "현재 독립 작업 폴더에 clamp.py 하나만 작성하세요. clamp(value, minimum, maximum)는 경계를 포함하여 값을 제한하며 minimum > maximum이면 ValueError를 발생시킵니다. 역할에 지정된 구현 방식을 사용하세요. 외부 패키지, 네트워크, 다른 폴더 접근과 변경은 필요하지 않습니다. 테스트는 오케스트레이터가 고정한 5개 검사로 수행합니다. 실제 작성 결과를 JSON summary/verdict/deferred로 보고하세요.",
        "workspace": str(workspace), "mode": "dual_implementers", "agents": [
            {"provider": "codex", "name": "Codex 구현 A", "role": "명시적인 if 분기를 사용하여 독립 구현하고 피드백한다", "max_tokens": args.max_tokens},
            {"provider": "codex", "name": "Codex 구현 B", "role": "경계 검사 후 min/max 함수를 사용하여 독립 구현하고 피드백한다", "max_tokens": args.max_tokens}],
        "max_seconds": args.max_seconds, "max_rounds": 1, "validation_commands": [[sys.executable, "-c", checks]], "validation_timeout": 30,
        "command_backend": "trusted_host"}
    planned = api("/api/plan", {"spec": spec})
    run = planned["id"]
    report = {"run_id": run, "dashboard": dashboard + "&run=" + run, "workspace": str(workspace),
        "spec": spec, "real_provider": True, "status": planned["status"]}
    report_file = ROOT / "real-dual-example.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    api("/api/start", {"run": run})
    print(json.dumps({"stage": "real_pair_started", "run": run, "dashboard": report["dashboard"]}), flush=True)
    seq = 0
    deadline = time.monotonic() + args.max_seconds + 60
    while time.monotonic() < deadline:
        info = api("/api/run?run=" + run)
        events = api(f"/api/events?run={run}&after={seq}")
        for event in events:
            seq = event["seq"]
            if event["kind"] in {"state", "turn_started", "candidate_metrics", "peer_feedback", "error", "token_limit_reached"}:
                print(json.dumps({"stage": event["kind"], "agent": event["agent"], "payload": event["payload"]}, ensure_ascii=True), flush=True)
        if info["status"] not in {"running", "awaiting_quota_confirmation"}:
            break
        time.sleep(2)
    else:
        api("/api/stop", {"run": run})
        raise RuntimeError("Trial monitoring timeout; task stopped")
    report.update({"status": info["status"], "reason": info["reason"], "usage": info["usage"], "comparison": info["comparison"]})
    comparison = info["comparison"]
    if comparison:
        for candidate in comparison["candidates"].values():
            candidate["generated_code"] = Path(candidate["preserved"]["path"], "clamp.py").read_text(encoding="utf-8")
    report["original_untouched"] = sorted(p.name for p in workspace.iterdir()) == ["README.md"]
    report["export"] = api("/api/export", {"run": run})
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "pair_result", "status": info["status"], "usage": info["usage"],
        "original_untouched": report["original_untouched"], "feedback_complete": comparison and comparison["feedback_complete"],
        "report": str(report_file)}, ensure_ascii=True), flush=True)
    if info["status"] != "awaiting_user_review":
        raise SystemExit(1)
    assert comparison["feedback_complete"]
    assert all(c["metrics"]["validation_passed"] for c in comparison["candidates"].values())
    assert report["original_untouched"]


if __name__ == "__main__":
    main()
