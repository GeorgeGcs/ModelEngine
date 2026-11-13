"""一条命令跑完离线回放层评测：正常回放入口 -> 结果文件 -> 独立比对。

流程刻意分成两个独立进程：
1. subprocess 运行 `python -m portfolio_risk data/eval_20.json`（正常回放入口，未做任何改动），
   将 stdout 统一为 LF 换行后写入 eval/results/replay_actual.json；
2. subprocess 运行 eval/compare.py，由它读取冻结标注与结果文件逐格比对。

比对进程不导入被测包，因此「生成信号的」和「判定对错的」是两套相互独立的代码。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from compare import FrozenAnnotationError, load_expected, read_lock, sha256_bytes

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
RESULT_PATH = EVAL_DIR / "results" / "replay_actual.json"
REPORT_PATH = EVAL_DIR / "results" / "compare_report.json"
EXPECTED_PATH = EVAL_DIR / "expected_20.json"


def run() -> int:
    REPORT_PATH.unlink(missing_ok=True)
    RESULT_PATH.unlink(missing_ok=True)
    try:
        expected, _ = load_expected(EXPECTED_PATH)
        replay_input = ROOT / expected["source"]["replay_input"]
        input_digest = sha256_bytes(replay_input.read_bytes())
        if input_digest != read_lock(replay_input):
            raise FrozenAnnotationError("行情输入与冻结锁不符，拒绝运行回放。")
    except (OSError, ValueError, FrozenAnnotationError) as exc:
        print(f"评测中止：{exc}", file=sys.stderr)
        return 2
    source = expected["source"]
    print(f"行情输入 SHA256：{input_digest}")

    print(f"[1/2] 正常回放入口：python -m portfolio_risk {source['replay_input']}")
    completed = subprocess.run(
        [sys.executable, "-m", "portfolio_risk", source["replay_input"]],
        cwd=ROOT, capture_output=True,
    )
    if completed.returncode != 0:
        print("回放失败：", completed.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return 2

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_bytes(completed.stdout.replace(b"\r\n", b"\n"))
    result = json.loads(completed.stdout.decode("utf-8-sig"))
    print(
        f"      结果文件：{RESULT_PATH.relative_to(ROOT).as_posix()}  "
        f"portfolio_count={result['portfolio_count']} "
        f"snapshot_count={result['snapshot_count']} "
        f"signal_count={result['signal_count']}"
    )
    if (result["portfolio_count"] != source["portfolio_count"]
            or result["snapshot_count"] != source["snapshot_count"]):
        print("回放输入与冻结标注声明的组合数/快照数不一致，拒绝比对。", file=sys.stderr)
        return 2

    print("[2/2] 独立比对：eval/compare.py")
    compared = subprocess.run(
        [sys.executable, str(EVAL_DIR / "compare.py"),
         "--expected", str(EXPECTED_PATH),
         "--actual", str(RESULT_PATH),
         "--report", str(REPORT_PATH),
         "--scope", "offline-replay-layer"],
        cwd=ROOT,
    )
    return compared.returncode


def main() -> int:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
