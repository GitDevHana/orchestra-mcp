from __future__ import annotations

import asyncio
import html
import json
import shutil
from datetime import datetime
from pathlib import Path
from .process import spawn
from .store import Store
from .redaction import redact


def atomic_text(path: Path, text: str):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def export_run(store: Store, run: str) -> dict:
    # Finalization and dashboard exports can run in different worker threads.
    # Serialize generation and registration so hashes always match a complete export.
    with store.export_lock:
        return _export_run(store, run)


def _export_run(store: Store, run: str) -> dict:
    info = redact(store.get(run))
    events = store.all_events(run)
    root = store.root / "runs" / run
    root.mkdir(parents=True, exist_ok=True)
    atomic_text(root / "run.json", json.dumps(info, ensure_ascii=False, indent=2))
    atomic_text(root / "events.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    data = json.dumps({"run": info, "events": events}, ensure_ascii=False).replace("<", "\\u003c")
    template = (Path(__file__).parent / "static" / "transcript.html").read_text(encoding="utf-8")
    atomic_text(root / "transcript.html", template.replace("/*RUN_DATA*/null", data))
    pending = [item for item in info["approvals"] if item["state"] == "pending"]
    summary = [f"# 작업 {run}", f"상태: {info['status']}", f"사유: {info['reason']}",
               "", "## 추가 승인 대기", "초기 범위 밖의 작업만 모았습니다. 승인되지 않은 항목은 실행하지 않습니다."]
    for item in pending:
        summary.extend([f"\n- ID: {item['id']}", f"  작업: {item['description']}", f"  이유: {item['reason']}",
                        f"  필요 권한/예상 변경: {item['scope']}", f"  요청 근거: {item['source']}",
                        f"  명시 실행 명령(argv): {json.dumps(item['argv'],ensure_ascii=False)}"])
    if not pending:
        summary.append("없음")
    comparisons = [e["payload"] for e in events if e["kind"] == "comparison_ready"]
    if comparisons:
        comparison = comparisons[-1]
        summary.extend(["", "## 독립 구현 비교 · 사용자 검토 대기", "자동 승자 선정과 병합은 수행하지 않습니다.",
            f"상호 피드백 완료: {comparison['feedback_complete']}",
            "수치는 고정된 검증 명령의 성공 여부와 관측 시간입니다. 종합 품질 점수나 반복 성능 벤치마크가 아닙니다.",
            "", "| 대상 | 검증 성공 | 관측 시간(초) | 변경 파일 수 |", "|---|---|---:|---:|"])
        for key, metrics in [("기준", comparison["baseline_metrics"])] + [(k.upper(), c["metrics"]) for k,c in comparison["candidates"].items()]:
            passed = metrics["validation_passed"]
            elapsed = f"{metrics['validation_seconds']:.4f}" if metrics['validation_seconds'] is not None else "미측정"
            summary.append(f"| {key} | {'미측정' if passed is None else '성공' if passed else '실패'} | {elapsed} | {metrics.get('changed_files', '—')} |")
        for key, candidate in comparison["candidates"].items():
            feedback = next((e["payload"]["text"] for e in reversed(events) if e["kind"] == "peer_feedback" and e["agent"] == key), None)
            summary.extend(["", f"### 구현 {key.upper()}", f"보존 폴더: {candidate['preserved']['path']}",
                candidate["implementation"], "", "상대에게 전달한 피드백:", feedback or "미완료"])
        summary.extend(["", "다음 구현은 기준 A/B, 가져올 장점/파일, 사용자 지시, 새 에이전트별 토큰과 새 시간을 입력해 계획합니다.",
                        "검토 대기 중에는 에이전트를 실행하지 않습니다. 원본과 1차 결과는 다음 단계에 덮어쓰지 않습니다."])
    atomic_text(root / "approval-report.md", "\n".join(summary))
    handoff = [f"# 이어하기 인수인계 · {run}", f"상태: {info['status']}", f"중단/종료 사유: {info['reason']}",
        f"작업 폴더: {info['config']['workspace']}", f"기록 폴더: {root}", "", "## 원래 과제", info['config']['task'],
        "", "## 현재 사용량과 한도", json.dumps(info['usage'], ensure_ascii=False, indent=2),
        "", "## 이어갈 때", "자동 재실행하지 마세요. 아래 기록과 실제 파일을 확인하고 남은 작업을 판단하세요.",
        "완료 상태는 completed인 경우에만 기록상 완료입니다. 미완료·중단 상태를 성공으로 취급하지 마세요.",
        "새 사용률/시간 예산으로 계획을 승인한 뒤 미완료 작업만 이어가세요. 토큰과 구독 비율은 환산하지 마세요.",
        "다른 Codex 사용도 계정 한도를 공유하며 조회 지연 때문에 정확한 10% 잔량은 보장되지 않습니다.",
        "원본/후보/보존 경로를 구분하고 아래 마지막 체크포인트와 실제 파일을 먼저 확인하세요.",
        "", "## 마지막 답변 · 남은 작업의 단서"]
    cleanup_start = next((e['seq'] for e in events if e['kind'] == 'cleanup_started'), None)
    for agent in ('a', 'b'):
        answers = [e for e in events if e['agent'] == agent
                   and e['kind'] in {'answer', 'partial_answer', 'handoff_answer'}]
        # Older answer events have no phase. The cleanup boundary also identifies
        # those records, including a partial cleanup interrupted before completion.
        def is_handoff(event):
            return (event['kind'] == 'handoff_answer'
                    or event['payload'].get('phase') == 'handoff'
                    or (cleanup_start is not None and event['seq'] > cleanup_start))
        work = next((e for e in reversed(answers) if not is_handoff(e)), None)
        cleanup = next((e for e in reversed(answers) if is_handoff(e)), None)
        handoff += [f"### {agent.upper()} · 마지막 작업 답변",
                    json.dumps(work['payload'], ensure_ascii=False, indent=2) if work
                    else "최종 답변 없음. 중단된 턴의 파일/원시 로그를 확인하세요.",
                    f"### {agent.upper()} · 정리 답변",
                    json.dumps(cleanup['payload'], ensure_ascii=False, indent=2) if cleanup
                    else "정리 답변 없음. 위 작업 기록과 실제 파일부터 확인하세요."]
    handoff += ["", "## 검증 · 변경 · 체크포인트 · 오류"]
    for kind in ('completion_gate', 'source_selected', 'implementation_preserved', 'source_preservation_error', 'rework_requested', 'rework_scheduled', 'cleanup_started', 'cleanup_finished', 'cleanup_budget_stop', 'cleanup_error', 'approved_execution', 'validation', 'file_changes', 'candidate_workspaces', 'candidate_checkpoint', 'candidate_preserved', 'quota_limit_reached', 'error'):
        selected = [e for e in events if e['kind'] == kind]
        handoff += [f"### {kind}", json.dumps(selected[-8:],ensure_ascii=False,indent=2) if selected else "기록 없음"]
    handoff += ["", "## 보류 권한", json.dumps(pending,ensure_ascii=False,indent=2),
        "", "## 전체 기록", "events.jsonl과 transcript.html이 전체 기록입니다. 위 항목은 종류별 최근 8개 요약이며 검증하지 않은 기능의 완성을 보장하지 않습니다."]
    atomic_text(root / "HANDOFF.md", "\n".join(handoff))
    for name in ("run.json", "events.jsonl", "transcript.html", "approval-report.md", "HANDOFF.md"):
        store.register_artifact(run, root / name)
    return {"directory": str(root), "transcript": str(root / "transcript.html"),
            "events": str(root / "events.jsonl"), "approval_report": str(root / "approval-report.md"), "handoff": str(root / "HANDOFF.md")}


def candidates(store: Store, run: str) -> list[Path]:
    """Measured event sequence, not a claim of reverse engineered repository architecture."""
    info = store.get(run)
    meaningful = [e for e in store.all_events(run) if e["kind"] in
                  {"prompt", "answer", "approval_deferred", "validation", "approved_execution", "file_changes", "state"}]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = store.root / ".archify" / f"sequence-{run[:8]}-{stamp}"
    folder.mkdir(parents=True)
    participants = [{"id": "control", "type": "backend", "label": "실행 관리자"}]
    for index, spec in enumerate(info["config"]["agents"]):
        participants.append({"id": chr(97+index), "type": "external", "label": f"{chr(65+index)} · {spec['provider']}"})
    participants.append({"id": "record", "type": "database", "label": "실행 기록"})
    result = []
    for page, start in enumerate(range(0, max(1, len(meaningful)), 10), 1):
        chunk = meaningful[start:start+10]
        messages = []
        for i, event in enumerate(chunk):
            kind, agent, payload = event["kind"], event["agent"], event["payload"]
            if kind == "prompt":
                source, target, label, variant = "control", agent, "작업 지시", "emphasis"
            elif kind == "answer":
                source, target, label, variant = agent, "control", "답변과 검토", "return"
            elif kind == "approval_deferred":
                source, target, label, variant = agent or "control", "record", "추가 승인 보류", "security"
            elif kind == "file_changes":
                source, target, label, variant = agent or "control", "record", f"파일 변경 {len(payload['files'])}개", "dashed"
            elif kind in {"validation", "approved_execution"}:
                source, target, label, variant = "control", "record", "승인 명령 결과" if kind == "approved_execution" else "검증 결과", "default"
            else:
                source, target, label, variant = "control", "record", payload.get("status", "상태 변경"), "default"
            messages.append({"id": f"event-{event['seq']}", "from": source, "to": target, "y": 160 + i*50,
                             "label": f"#{event['seq']} {label}", "variant": variant})
        if not messages:
            messages = [{"id": "planned", "from": "control", "to": "record", "y": 160, "label": "초기 승인 대기"}]
        name = f"trace-{page}.html"
        candidate = {"schema_version": 1, "diagram_type": "sequence", "meta": {
            "title": f"작업 진행 기록 · {run[:8]} · {page}", "locale": "ko", "quality_profile": "showcase",
            "output": (folder.relative_to(store.root) / name).as_posix()},
            "participants": participants, "messages": messages,
            "cards": [{"dot": "cyan", "title": "기록 범위", "items": [
                "실제로 저장된 대화와 단계의 순서를 표시합니다.",
                "원문, 도구 이벤트, 표준 출력과 오류는 transcript.html과 events.jsonl에서 확인합니다.",
                "화살표 간격은 읽기용 배치이며 실행 시간을 뜻하지 않습니다."]}]}
        path = folder / f"candidate-{page}.json"
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        result.append(path)
    return result


async def render_archify(store: Store, run: str, cli: Path | None = None) -> dict:
    cli = cli or Path.home() / ".agents" / "skills" / "archify" / "bin" / "archify.mjs"
    paths = await asyncio.to_thread(candidates, store, run)
    if not cli.is_file() or not shutil.which("node"):
        return {"status": "renderer_unavailable", "candidates": [str(p) for p in paths], "message": "Set ORCH_ARCHIFY_CLI or install the Archify skill; no HTML validation was claimed"}
    receipts = []
    for path in paths:
        candidate = json.loads(path.read_text(encoding="utf-8"))
        output = store.root / candidate["meta"]["output"]
        argv = [shutil.which("node"), str(cli), "finalize", "sequence", str(path), str(output), "--quality", "showcase", "--json"]
        process, tree = await spawn(argv, str(store.root))
        try:
            async with asyncio.timeout(180):
                stdout, stderr = await process.communicate()
            record = {"path": str(output), "returncode": process.returncode,
                      "stdout": stdout.decode("utf-8", "replace"), "stderr": stderr.decode("utf-8", "replace")}
            receipts.append(record)
            path.with_suffix(".receipt.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if process.returncode:
                return {"status": "validation_failed", "receipts": receipts}
            store.register_artifact(run, output)
        finally:
            tree.close()
            await process.wait()
    # A passing receipt is required for every linked diagram.
    links = "".join(f'<li><a href="{html.escape(Path(r["path"]).relative_to(store.root).as_posix(),quote=True)}">진행 기록 {i+1}</a></li>' for i,r in enumerate(receipts))
    index = store.root / f"archify-{run}.html"
    atomic_text(index, f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>Archify 기록</title><h1>작업 진행 다이어그램</h1><ul>{links}</ul></html>')
    store.register_artifact(run, index)
    return {"status": "passed", "index": str(index), "receipts": receipts}
