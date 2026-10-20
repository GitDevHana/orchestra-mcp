# Agent Orchestra MCP

여러 AI 코딩 CLI의 구현·검토·재작업을 연결하는 로컬 MCP 오케스트레이터입니다.
Python 서버가 실행 순서와 예산을 관리하고, 웹 대시보드에서 진행 상황과 검증 결과를 확인합니다.
아직 Codex 외의 기능은 제대로 검증하지 않았습니다.
GPT, Claude, Cursor, Gemini를 지원할 예정입니다. 

## 주요 기능

- **구현·검토:** 구현자 A와 읽기 전용 검토자 B가 하나의 코드를 개선합니다.
- **독립 A/B 비교:** 같은 코드에서 별도로 구현한 두 결과를 검토하고 사용자가 선택합니다.
- **피드백 반영:** 차단 결함과 검증 실패를 남은 라운드에서 구현자에게 돌려보냅니다.
- **완료 판정:** 완료 기준·검토 근거·등록된 검증 명령을 확인하고, 미검증 결과는 구분합니다.
- **예산 관리:** 실행 시간, 라운드, 에이전트별 토큰 한도와 Codex 구독 사용률을 관리합니다.
- **기록·이어하기:** 대화, 변경 사항, 검증 결과와 코드 보존본을 저장하고 새 예산으로 이어갑니다.

Codex · Claude Code · Gemini CLI · Cursor CLI 어댑터를 제공합니다.
실제 사용에는 각 CLI의 설치·인증과 이용 가능한 모델이 필요합니다.

## 빠른 시작

Python 3.11 이상이 필요합니다. 아래 명령은 Windows PowerShell 기준이며 프로젝트 폴더에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m orchestrator --dashboard-only --workspace-root .
```

터미널에 출력된 대시보드 주소를 브라우저에서 엽니다.
주소는 기본 데이터 폴더인 `%USERPROFILE%\.agent-orchestrator\server.json`에도 저장됩니다.
다른 프로젝트에서 작업하려면 `--workspace-root`에 해당 폴더를 지정하세요.

1. 과제, 작업 방식, 모델, 완료 기준과 검증 명령을 입력합니다.
2. 계획의 작업 범위와 예산을 확인하고 시작합니다.
3. 종료 후 결과를 검토하거나 보존된 코드에서 이어갑니다.

**오프라인 데모**로 AI 호출 없이 실행 흐름을 확인할 수 있습니다.
Docker 없이 검증하려면 화면에서 **검증 명령 실행 위치 → 이 PC에서 실행**을 선택하세요.

## 현재 범위

현재는 최대 2개 에이전트를 지원합니다. 실행은 순차적으로 진행하며, 최종 A/B 선택은 사용자가 합니다.
등록된 검증 명령이 없거나 확인 근거가 부족하면 **검증 미완료**로 표시합니다.
폴더 분리와 CLI 권한을 사용하지만, 별도 OS 보안 격리를 제공하지는 않습니다.

총괄·조사·구현·다중 검증 역할을 구성하는 **프로젝트 팀 모드**는 향후 확장 방향입니다.

## 개발·문서

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

- 기술: Python · MCP · SQLite · HTML/CSS/JavaScript
- [상세 사용법과 MCP 연결](USAGE.md)
- [완료 판정과 재작업 설계](COMPLETION-POLICY.md)
- [공유 MCP 서버 연결](SHARED-MCP.md)
