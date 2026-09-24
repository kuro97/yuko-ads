"""Поведенческая проверка GET-only reconciliation из реального UI-скрипта."""

import json
import subprocess
from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    async_prefix = "async "
    if source[max(0, start - len(async_prefix)):start] == async_prefix:
        start -= len(async_prefix)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Функция {name} не закрыта")


def test_executing_lost_response_polls_until_confirmed() -> None:
    html = HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _function_source(html, name)
        for name in (
            "pollApprovalAction",
            "pollApprovalActionUntilTerminal",
            "isConfirmedApproval",
            "isTerminalApproval",
            "approvalLaunchOutcome",
        )
    )
    scenario = f"""
const API = '';
const states = [
  {{state: 'EXECUTING', result: null, reconciliation_required: true}},
  {{state: 'CONFIRMED', result: 'CONFIRMED', reconciliation_required: false}}
];
let calls = 0;
function apiErrorText() {{ return 'error'; }}
async function authFetch() {{
  const payload = states[Math.min(calls++, states.length - 1)];
  return {{ok: true, json: async () => payload}};
}}
global.setTimeout = callback => callback();
{functions}
const result = await pollApprovalActionUntilTerminal('operation-1', 3);
if (calls !== 2 || !isConfirmedApproval(result) || approvalLaunchOutcome(result) !== 'succeeded') {{
  throw new Error(JSON.stringify({{calls, result}}));
}}
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "-e", scenario],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, json.dumps(
        {"stdout": completed.stdout, "stderr": completed.stderr},
        ensure_ascii=False,
    )
