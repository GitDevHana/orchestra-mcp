# Agent Orchestra — Python MCP 오케스트레이터

현재 완료 판정·재작업 변경은 [COMPLETION-POLICY.md](COMPLETION-POLICY.md)를 먼저 읽으세요. 이전 상태는 [STATUS-20261019.md](STATUS-20261019.md), [HANDOFF.md](HANDOFF.md), [RESUME-20261019.md](RESUME-20261019.md)에 남아 있습니다.

Codex, Cursor, Claude 중 하나 또는 두 개를 선택합니다. 같은 CLI 두 개도 A/B의 서로 다른 대화 세션으로 실행합니다. MCP가 실행을 제어하고, 로컬 브라우저 화면에서 두 창의 대화와 진행 기록을 봅니다.

## 두 작업 방식과 단계별 예산

- **옵션 1 (`implement_review`, 기본값)**: A 구현 + B 읽기 전용 검토. 하나의 코드에서 피드백을 반영하고 종료합니다. 에이전트 하나도 사용할 수 있습니다.
- **옵션 2 (`dual_implementers`)**: 두 에이전트가 같은 기준 코드의 **서로 다른 복사본**을 구현합니다. 첫 구현 중에는 상대 답변을 전달하지 않습니다. 각 구현을 보존한 뒤 새 읽기 전용 세션에서 상대 코드·테스트 전체의 복사본을 검토합니다. 기본 `dual_feedback_action: "revise"`는 남은 라운드 안에서 차단 결함이 있는 후보만 수정·재검토합니다. `"wait"`는 첫 피드백 뒤 사용자 선택을 기다립니다. 자동 승자 선정·병합·새 예산 실행은 없습니다.

옵션 2는 `awaiting_user_review`로 저장됩니다. 실행 프로세스와 단계 타이머는 끝나고, 검토 대기에는 모델을 호출하지 않습니다. 며칠 뒤 같은 데이터 디렉터리로 다시 실행해도 코드 A/B, 상호 피드백, 측정값, 사용량을 복원합니다. 서버 재시작에 따른 자동 재실행은 없습니다.

브라우저의 **A/B 비교와 사용자 결정**에서 기준 코드 A/B, 각각 가져올 장점, 구현 방향을 기록하세요. 선택적으로 `{"src/ui.py":"b"}`처럼 가져올 파일을 명시할 수 있습니다. 파일 전체만 새 입력 폴더로 복사하며, 함수나 설계의 혼합은 사용자 지시에 따라 다음 단계에서 구현·검증합니다. 충돌 해결을 완료한 것처럼 자동 병합하지 않습니다. 선택한 장점의 구현 보고·코드 자료도 제한된 크기로 다음 단계에 전달하고 생략된 자료를 표시합니다.

**2차 계획에는 에이전트별 토큰과 최대 시간을 반드시 다시 입력합니다.** 이전 단계의 잔여 시간·사용량을 이월하거나 예산 값을 자동 재사용하지 않습니다. `plan_next_stage`는 연결된 계획만 만들고, 범위 확인 후 `approve_and_start`로 실행합니다. 다음 단계에서도 옵션 1/2를 고를 수 있습니다. 이번 단계의 시간에는 복사·구현·검증·피드백이 포함되고, 사용자 검토 대기는 포함되지 않습니다.

보존 위치는 `데이터폴더/runs/<id>/preserved/a`, `preserved/b`입니다. 각 구현 턴 후 체크포인트도 남깁니다. 복사에는 하드링크를 쓰지 않으며 링크/정션과 Git 메타데이터·가상환경·의존성 디렉터리는 제외하고 누락 목록을 기록합니다. 의존성이 필요하면 새 폴더에서 준비해야 합니다. 다음 단계 입력은 별도 `stage-inputs/<id>`에 만듭니다. 보존 결과의 해시가 달라지면 다음 단계 사용을 거절합니다.

폴더 분리는 OS 보안 경계는 아닙니다. 각 CLI의 작업 폴더 권한을 적용하고 상대 작업 폴더 변경을 감지하면 중단합니다. 악의적인 프로세스까지 차단하려면 별도 OS 사용자나 컨테이너가 필요합니다.

측정은 원본 복사본과 A/B에 같은 사전 승인 명령을 실행한 결과, 관측 시간, 변경 파일 수를 기록합니다. 검증 명령이 없으면 성공으로 꾸미지 않고 미측정으로 표시합니다. 실패 후보도 보존하며 최종 판단은 사용자가 합니다. 에이전트가 수정한 테스트 자체의 적절성도 검토해야 합니다. 단일 실행 시간을 성능 개선율이나 종합 품질 점수로 해석하지 않습니다.

MCP의 다음 단계 입력 예시(예산은 예시):

```json
{
  "run_id": "1차 실행 ID",
  "decision": {
    "base_candidate": "a",
    "take_from_a": "모듈 구조",
    "take_from_b": "오류 처리",
    "feedback": "A 구조를 유지하며 B의 오류 처리를 통합하고 회귀 검증",
    "selected_files": {},
    "agents": [{"max_tokens": 40000}, {"max_tokens": 20000}],
    "max_seconds": 1800,
    "mode": "implement_review"
  }
}
```

## 실행

Windows PowerShell, 프로젝트 폴더에서:

```powershell
.\.venv\Scripts\python.exe -m orchestrator
```

MCP는 표준 입출력을 사용합니다. 로컬 화면 주소는 **stderr**와 `%USERPROFILE%\.agent-orchestrator\server.json`에 기록됩니다. 일반 사용을 위한 화면만 실행하려면:

```powershell
.\.venv\Scripts\python.exe -m orchestrator --dashboard-only --port 8765
```

브라우저에서는 `server.json`의 `dashboard` 주소를 엽니다. 주소의 토큰이 있어야 로컬 API를 사용할 수 있습니다. `127.0.0.1`에만 바인딩합니다. 화면의 **오프라인 데모 → 범위 승인 후 시작**으로 AI 호출 없이 전체 흐름을 확인할 수 있습니다.

작업 폴더는 서버 시작 시 지정한 `--workspace-root` 아래로 제한합니다. 옵션은 여러 번 지정할 수 있고, 생략하면 **서버를 시작한 작업 디렉터리**가 기준입니다. 데이터 폴더의 부모를 기준으로 삼지 않습니다. 데모 폴더와 서버가 만드는 다음 단계 입력 폴더는 내부 예외입니다. 계획할 때와 시작할 때 모두 경로를 검사합니다.

```powershell
.\.venv\Scripts\python.exe -m orchestrator --workspace-root 'C:\projects\my-project' --data-dir '.\records'
```

검증·후속 명령의 기본 실행 방식은 `command_backend: "container"`입니다. 명령을 지정하면 설치된 Docker와 `command_image`가 필요합니다. 이미지는 미리 준비해야 하며 자동 다운로드나 호스트 실행 대체는 하지 않습니다. 예를 들어 `python:3.14-slim` 이미지에서는 명령을 `["python", "-m", "unittest"]`처럼 컨테이너 내부 경로로 지정합니다. Windows Python 절대 경로는 Linux 이미지에서 사용할 수 없습니다. 작업 폴더는 `/workspace`에 쓰기 가능하게 연결되고 네트워크는 차단됩니다. 에이전트 CLI 자체가 컨테이너로 이동하는 설정은 아닙니다.

이 PC에서 명령을 실행하려면 대시보드의 **검증 명령 실행 위치 → 이 PC에서 실행** 또는 `command_backend: "trusted_host"`를 명시합니다. 표시된 명령과 계정 권한을 검토한 뒤 시작하세요. 새 정책 이전의 저장 계획에는 이 선택이 없을 수 있으므로 명령이 포함된 계획은 새로 작성해야 합니다. 다음 단계와 승인 항목 후속 실행은 원래 계획의 실행 방식을 유지합니다. 서버가 사람 승인 주체를 독립적으로 검증하는 기능은 아직 없으며 MCP 클라이언트와 사용자의 승인 절차에 의존합니다.

자식 환경은 필요한 경로·언어·로그인 설정 폴더와 에이전트용 프록시 변수만 전달합니다. 임의 환경변수와 API 키는 기본 전달하지 않습니다. 환경변수 API 키에 의존하던 인증 방식은 별도 지원이 필요하고, 기존 CLI 로그인 폴더를 사용하는 방식과 구분해야 합니다.

새 환경 설치:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
```

## 처음 한 번 승인하고, 추가 작업은 나중에 모아 승인

1. 과제, 기존 작업 폴더, 에이전트 1~2개, 각각의 토큰 예산, 전체 시간, 최대 라운드, 파일 접근 모드와 검증 명령을 입력합니다.
2. `plan_task` / **계획 확인**은 기록만 만듭니다. CLI는 시작하지 않습니다.
3. 사용자가 표시된 범위를 승인하면 `approve_and_start`로 실행합니다.
4. 옵션 1은 A 구현 → B 검토 → A 수정 순서로 진행합니다. 옵션 2는 별도 폴더에서 각각 구현·검증·상호 피드백 후, 설정에 따라 남은 라운드에서 결함을 수정합니다. 각 세션 ID를 별도로 유지합니다.
5. 추가 권한이 필요한 요청은 `approvals`에 `pending`으로 저장합니다. 다른 독립 작업은 계속합니다. 자동으로 `y`를 보내지 않습니다.
6. 종료 후 **보류 목록** / `approval-report.md`에서 작업·이유·필요 권한·예상 변경을 확인합니다. 필요하면 정확한 실행 명령을 JSON argv로 수정합니다.
7. `decide_deferred`에서 선택 항목만 승인/거절합니다. 이 단계도 실행하지 않습니다.
8. `run_approved_items`가 승인됐고 아직 실행하지 않은 항목만 새 후속 실행으로 보냅니다. 명시적으로 승인한 argv가 있으면 정확히 그 명령을 먼저 실행합니다. 거절하거나 선택하지 않은 항목은 제외합니다.

후속 실행에는 새 전체 시간과 원래의 **에이전트별 토큰 예산을 새로** 적용하며, 한 라운드로 제한합니다. 이는 OS 관리자 권한이나 CLI 권한을 우회하는 승인이 아닙니다. 실제 로그인/관리자 권한이 여전히 없으면 실패나 새 보류 항목으로 기록됩니다. 원래 실행은 다시 돌리지 않습니다.

이 보류 항목 후속 실행은 옵션 1용입니다. 옵션 2는 사용자 선택으로 다음 단계를 계획해야 하며 원본 폴더에 보류 작업을 실행하지 않습니다. **보류 작업 추가 실행과 A/B 선택 후 2차 구현은 별도 기능**입니다.

CLI가 제공한 구조적 `permission_denials`는 `provider_reported`, 에이전트가 제시한 추가 작업은 `agent_reported`로 구분합니다. 출력의 단순 `Permission denied` 문구는 `permission_hint` 참고 이벤트이며 승인 목록에 자동 등록하지 않습니다. 오류 이벤트와 비정상 종료는 승인 거부 여부와 관계없이 실패로 처리합니다. CLI가 승인 이벤트를 내보내지 않으면 모든 요청을 자동 감지할 수는 없습니다. 에이전트에도 최종 JSON의 `deferred` 목록에 명시하도록 지시합니다.

## MCP 연결

`mcp-config.example.json`은 Cursor/Claude 스타일 예시입니다. 실행 파일 절대 경로를 실제 설치 위치에 맞춥니다. Codex 예시:

```toml
[mcp_servers.orchestra]
command = 'C:\tools\agent-orchestrator\.venv\Scripts\python.exe'
args = ['-m', 'orchestrator', '--workspace-root', 'C:\projects\my-project']
startup_timeout_sec = 30
tool_timeout_sec = 600
```

같은 기록 폴더의 서버는 한 프로세스만 소유합니다. 여러 클라이언트가 별도로 stdio 서버를 실행한다면 각각 `--data-dir`을 달리 지정하세요. 기본 데이터는 `%USERPROFILE%\.agent-orchestrator`입니다. 서버가 닫히면 진행 중인 작업도 중단되며, 재시작 시 `interrupted`로 남기고 자동 재실행하지 않습니다. 로그인 정보는 각 CLI가 기존 방식으로 관리합니다. 이 프로그램은 키를 저장하거나 로그인 UI를 대신 조작하지 않습니다.

| MCP 도구 | 동작 |
|---|---|
| `orchestrator_doctor` | CLI 설치 상태·화면 주소 확인; 로그인 성공 여부는 별도 |
| `get_codex_quota` | 기존 Codex 로그인으로 구독 사용률 창만 읽기; 모델 호출 없음 |
| `confirm_quota_unknown` | 사용률 조회 누락 시 이 실행을 계속할지 사용자가 명시적으로 결정 |
| `plan_task` | 초기 승인용 계획 작성 |
| `plan_next_stage` | 사용자 A/B 선택·장점·피드백과 새 예산으로 다음 구현 계획 작성; 자동 시작 없음 |
| `approve_and_start` | 표시된 계획을 사용자 승인 후 실행 |
| `list_tasks`, `get_task` | 상태·예산·보류 항목 확인 |
| `get_events` | seq 기반 전체 기록 페이징 |
| `send_message` | 지정 에이전트의 다음 턴에 지시 전달 |
| `stop_task` | 실행과 소유한 하위 프로세스 트리 중단 |
| `decide_deferred` | 항목별 사용자 승인/거절과 명시 argv 기록 |
| `run_approved_items` | 승인 항목만 연결된 후속 실행으로 시작 |
| `export_task` | JSONL·독립 HTML·승인 보고서, 선택적으로 Archify 생성 |
| `plan_offline_demo` | 모델을 호출하지 않는 명시적 데모 계획 |

`examples/run.json`에서 `agents`에 하나만 남기면 단일 실행입니다. 두 항목의 `provider`를 모두 `claude`로 지정하면 Claude A/B입니다. `name`, `role`, `model`, `max_tokens`를 슬롯별로 정할 수 있습니다. 화면의 **별도 창**은 해당 에이전트 기록을 보여주는 독립 브라우저 창입니다. 실제 Codex/Cursor/Claude GUI를 자동 조작하는 프로그램은 아닙니다.

## 예산과 실행 경계

- 전체 시간은 `asyncio.timeout`으로 감시합니다. Windows에서는 Job Object, POSIX에서는 프로세스 그룹을 사용해 실행 중인 하위 프로세스도 정리합니다. 종료·기록 내보내기에는 짧은 추가 시간이 필요할 수 있습니다.
- 토큰은 CLI가 보고한 input/output/cache 사용량을 기록합니다. 보고가 없으면 UTF-8 길이 기반 **추정**을 사용하며, 추정·실측 값을 따로 표시합니다.
- **사용량 또는 스트리밍 추정 감시값이 설정값 이상(`>=`)이면 작업 전체를 자동 중단합니다.** 진행 중인 CLI와 소유 하위 프로세스를 먼저 닫고, 다음 에이전트·다음 턴·검증 명령은 시작하지 않습니다. 중단 이유와 한도를 `token_limit_reached` 이벤트로 기록하고 화면에 `토큰 한도 · 자동 중단`을 표시합니다. 프롬프트 추정치만으로 잔여 예산에 도달해도 CLI를 시작하지 않습니다.
- Claude는 부분 스트리밍을 켜서 입력·캐시·출력 사용량 업데이트와 텍스트 조각을 감시합니다. 같은 메시지의 누적 사용량이나 최종 응답을 중복 합산하지 않으며, 이미 감지한 감시 최대값을 나중의 짧은 응답으로 낮추지 않습니다. 화면의 감시값에는 추정이 포함되므로 제공자 과금 보고값과 다를 수 있습니다.
- Cursor도 부분 출력 스트리밍을 켜고, 도구 호출 전·턴 종료 시 중복 전송되는 텍스트는 토큰 추정에 다시 더하지 않습니다. 실행 전 프롬프트 추정 때문에 시작을 막은 경우 실제 사용량으로 청구한 것처럼 기록하지 않고 `not_started`와 예상 토큰을 남깁니다.
- CLI가 사용량을 요청 종료 시에만 보고하면 그 요청 내부에서 예산을 초과할 수 있습니다. 보이지 않는 추론/도구 입력 토큰도 추정에 포함되지 않습니다. **정확한 과금 하드캡이 아닙니다.** 엄밀한 과금 상한이 필요하면 각 제공자의 API/계정 예산 기능을 추가해야 합니다.
- Codex는 `approval=never`와 지정 sandbox를 사용합니다. Windows sandbox 등 사용자 플랫폼 설정은 유지하되, 연결된 MCP 서버와 플러그인을 자식 실행에서 비활성화해 재귀 실행을 피합니다. 이 환경의 Codex 0.163.0에서 `exec --ignore-user-config`를 사용하면 `workspace-write` 요청에도 실제 모델 권한이 읽기 전용이었던 문제를 확인해 해당 옵션을 제거했습니다. 관리 정책이나 샌드박스를 우회하지 않습니다. Claude는 `dontAsk`와 사전 허용 파일 도구를 사용하고 자동 명령 승인을 하지 않습니다. Cursor는 설치된 권한 설정을 존중하며 전면 허용 `--force`를 추가하지 않습니다.
- 세션은 대화상 독립입니다. 옵션 1은 하나의 코드, 옵션 2는 별도 복사본을 사용합니다. 각 CLI/OS가 제공하는 것 이상의 샌드박스 격리는 구현하지 않았습니다. 같은 원본 폴더의 두 실행은 동시에 시작할 수 없습니다.
- 검증과 승인 명령은 문자열 셸 평가 없이 argv 배열로 실행합니다. `.cmd/.bat/.ps1` 파일은 직접 실행 대상으로 받지 않습니다. 필요하면 승인 화면에 `python.exe`, `node.exe` 등 실제 실행 파일과 인수를 표시합니다.
- 옵션 1은 모든 에이전트의 구조화된 통과 판정과 사전 설정한 검증 명령의 성공이 있어야 `completed`입니다. 차단 결함은 `done`보다 우선해 다음 라운드로 반환합니다. 검증 명령이나 근거가 없으면 `unverified`, 미해결 상태로 라운드를 소진하면 `round_limit`입니다. 옵션 2는 후보별 완료 판정을 표시하고 `awaiting_user_review`로 전환합니다. 자세한 규칙은 [COMPLETION-POLICY.md](COMPLETION-POLICY.md)를 참고하세요.

## 기록과 Archify

### Codex 구독 5시간·주간 한도

작업 토큰 예산과 구독 소진율은 별개입니다. 토큰 80,000개가 구독의 몇 %라고 추정하거나 미공개 최대 토큰 수를 만들어내지 않습니다.

`get_codex_quota` / 화면의 **현재 비율 조회**는 공식 `codex app-server`의 `account/rateLimits/read`를 사용합니다. `rateLimitsByLimitId`를 우선 읽고 구버전 `rateLimits`도 지원합니다. `primary`를 무조건 5시간으로 간주하지 않고 `windowDurationMins=300`과 `10080`으로 분류합니다. 버킷별 `usedPercent`, `resetsAt`을 표시하고, 필요한 창이 없으면 조회 불가로 남깁니다. 기본 버킷은 `codex`이며 필요하면 `codex_quota_limit_ids`로 명시합니다. 모델별 버킷 매핑을 임의로 추정하지 않습니다.

두 백분율 제한은 기본값 **미설정**이며 사용자가 정합니다. `codex_five_hour_stop_at_used_percent` / `codex_weekly_stop_at_used_percent`를 지정하면 실행 전·각 턴 전·검증 전에 확인하고, 에이전트 실행 중에도 `codex_quota_poll_seconds` 간격(기본 15초)으로 조회합니다. 설정값 이상이면 현재 에이전트의 프로세스를 닫고 작업 전체를 `quota_limit`으로 중단합니다. 계정의 다른 Codex 창/프로그램 사용량도 영향을 줍니다. 조회 주기와 제공자의 반영 지연 때문에 정확히 그 비율에서 멈추는 계정 하드캡은 아닙니다.

`missing_quota_policy`는 다음을 지원합니다.

- `pause_for_confirmation`(기본): 진행 중인 에이전트 호출을 닫고 `awaiting_quota_confirmation`에서 사용자 결정을 기다립니다. 명시 승인 시 해당 실행의 누락 창만 예외로 인정하고 실제 실행 직전에 다시 조회합니다. 알려진 창의 중단 기준은 우회하지 않습니다.
- `stop`: `quota_unavailable`로 종료합니다.
- `continue_with_warning`: 누락 위험을 수용하도록 사전에 설정한 경우에만 경고를 기록하고 계속합니다.

확인 대기 시간도 전체 최대 시간에 포함됩니다. 중단 후 승인받으면 중단된 에이전트 턴만 다시 진행하며 이전 승인 명령/완료 턴은 반복하지 않습니다. 이미 적용된 파일 변경은 남고, 중단된 턴의 사용량도 기록합니다. CLI/원격 서비스 특성상 진행 중 원격 요청의 과금 취소를 보장하지 않습니다.

입력 편의를 위해 `agents[].max_total_tokens`를 `max_tokens`의 별칭으로 지원하며, `limits` 객체 안의 백분율 필드와 `max_runtime_minutes`, `max_rounds`도 받습니다. 예시의 숫자는 기본 정책으로 고정하지 않습니다. `task`와 기존 `workspace`는 별도로 지정해야 합니다.

조회용 app-server 연결에서는 `initialize`, `initialized`, `account/rateLimits/read`만 사용합니다. 에이전트 스레드/모델 턴, 로그인, 크레딧 초기화를 요청하거나 인증 토큰을 내보내지 않습니다. 이 기능은 [공식 app-server 규격](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)을 따릅니다.

SQLite가 원본입니다. 대화 지시·답변, CLI가 공개한 원시 JSON 도구 이벤트, stdout/stderr, 사용량, 상태 변경, 검증 결과, 승인 결정과 후속 실행 연결을 저장합니다. 제공자가 노출하지 않은 내부 추론이나 외부 UI 활동은 저장할 수 없습니다.

각 턴/승인 명령/검증 전후에 폴더를 비교해 파일 해시와 가능한 텍스트 diff를 남깁니다. 파일 개수와 크기 제한 없이 바이너리도 해시하며 텍스트 diff는 128KB까지입니다. 일반 감사는 `__pycache__`, `.pytest_cache`, `.archify`와 Orchestra 기록 폴더를 제외합니다. 읽기 전용 검토는 캐시 폴더까지 검사하며 읽기 실패나 파일 변경 시 실패로 처리합니다. 링크·정션은 대상 경로를 기록하고 따라가지 않습니다. 이는 쓰기 자체의 차단이나 작업 폴더 밖 감시를 보장하지 않습니다. 후보 복사에서 Git/의존성 폴더를 제외하는 정책과 감사 범위는 다릅니다. 기록에 실제 소스와 도구 출력이 포함되므로 로컬 데이터 폴더를 공유할 때는 내용을 확인하세요.

HTML 기록은 등록된 내보내기 파일의 해시가 일치할 때만 `/artifact`로 제공하며 sandbox CSP를 적용합니다. 오래된 파일이 거절되면 **전체 기록 내보내기**를 다시 실행하세요. 내용을 수정한 파일은 다시 내보내야 합니다. 동일 서버에서 동시에 실행한 내보내기는 순서대로 처리합니다. 마스킹은 일부 비밀 문자열에 대한 보조 조치이며 원시 DB 전체의 비밀 제거를 보장하지 않습니다.

`export_task(archify=true)` 또는 화면의 **Archify 진행도 생성**은 저장된 실제 대화/단계로 Archify Sequence 후보를 만들고 `finalize`로 검증·렌더링합니다. 긴 실행은 10개 메시지 단위의 여러 페이지로 나눕니다. 그림에는 단계 요약, 전체 원문은 `transcript.html`과 `events.jsonl`에 있습니다. 그림의 세로 간격은 시간 측정치가 아닙니다.

기본 Archify 실행기: `%USERPROFILE%\.agents\skills\archify\bin\archify.mjs`. 다른 위치는 `ORCH_ARCHIFY_CLI` 환경변수로 지정합니다. 실행기/브라우저 검증이 없으면 후보와 오류를 반환하며 검증 성공으로 표시하지 않습니다. 생성된 HTML은 기존 파일과 충돌하지 않는 `.archify/sequence-...` 폴더에 보관합니다.

## 검증과 현재 범위

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\smoke_mcp.py
.\.venv\Scripts\python.exe scripts\make_demo.py
```

실제 자식 프로세스·MCP 프로토콜·SQLite·승인 후속 실행을 검증합니다. 데모의 답변과 토큰은 모의 데이터라고 명시합니다. 설치된 CLI/로그인/구독에 따른 실제 유료 응답은 이 테스트로 검증하지 않습니다. 이 PC에서는 구현 시 Codex만 PATH에서 발견됐습니다. Cursor와 Claude를 사용하려면 각각 CLI 설치와 로그인이 필요합니다.

공식 연동 계약: [Codex CLI](https://learn.chatgpt.com/docs/cli/reference), [Claude Code CLI](https://code.claude.com/docs/en/cli-reference), [Cursor headless](https://cursor.com/docs/cli/headless), [Cursor 출력](https://cursor.com/docs/cli/reference/output-format).


## 2026-10-18 추가: 구독 비율 예산과 이어하기

- 새 대시보드 계획은 Codex 구독 사용률 기준이 기본이며 토큰 상한 없이 사용량만 기록할 수 있습니다 (`max_tokens: null`). 다른 공급자는 기존 토큰 예산을 사용합니다.
- 새 화면에서 만든 계획은 `codex_reserve_percent: 10`을 지정합니다. 5시간·주간 중단 기준을 모두 최대 90%로 제한하며 더 낮게 지정할 수 있습니다. 사용률은 계정 누적값이며 다른 창의 사용도 포함합니다. 작업별 토큰과 비율을 환산하지 않습니다.
- 필요한 사용량을 사전에 알 수 없고 조회/반영 지연이 있으므로 정확한 10% 잔량을 보장하지 않는다고 시작 화면에서 안내합니다. 구독 비율만 사용하는 화면 계획은 조회 누락 시 전체 종료합니다.
- 완료 조건을 먼저 충족하면 예산을 소진하지 않고 종료합니다. 한도 도달 시 전체 실행과 소유 프로세스를 중단하고 로컬 기록만 정리합니다.
- 실행 종료 내보내기에 `runs/<id>/HANDOFF.md`가 자동 생성됩니다. 과제, 상태/사유, 사용량, 에이전트 마지막 답변, 최근 검증/변경/체크포인트/오류, 보류 권한, 전체 로그 위치가 들어갑니다. 추가 AI 호출은 없습니다.
- 다음 A/B 단계도 새 비율과 시간을 입력할 수 있습니다. 기존 저장 계획과 API 호출의 기본 토큰 정책은 호환을 위해 유지됩니다. API/MCP로 같은 여유분 정책을 쓰려면 `codex_reserve_percent: 10`을 명시합니다.
- 로컬 회귀 테스트 62개 및 대시보드 JS 구문 검사를 통과했습니다. 유료 모델 재시험은 하지 않았습니다. 실행 중 서버는 이전 Python 코드를 유지하므로 새 대시보드 프로세스 또는 MCP 재연결이 필요합니다.


## 2026-10-18: Windows source-copy lock fix
A/B baseline copy now omits Orchestra runtime directories identified by runs.sqlite3 + server.lock, and records the omitted path. Inventory and audit use the same exclusion. Real source copy failures name the file. Actual Windows byte-lock regression and all 64 tests passed. Run 97c283d6 failed before any model turn; it is preserved and never automatically retried.


## 2026-10-18: Windows source-copy lock fix
A/B baseline copy now omits Orchestra runtime directories identified by runs.sqlite3 + server.lock, and records the omitted path. Inventory and audit use the same exclusion. Real source copy failures name the file. Actual Windows byte-lock regression and all 64 tests passed. Run 97c283d6 failed before any model turn; it is preserved and never automatically retried.
