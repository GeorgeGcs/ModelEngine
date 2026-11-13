"""从冻结行情输入生成 24 个逻辑任务清单（3 组合 × 8 快照）并冻结。

清单里只有**输入**，没有任何预期触发结果；预期标注只会交给最后的独立
比对进程。本模块不调用被测规则引擎，不读取冻结标注文件。
"""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "eval_20.json"
MANIFEST = Path(__file__).resolve().parent / "task_manifest.json"
MANIFEST_LOCK = MANIFEST.with_name(MANIFEST.name + ".sha256")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify_manifest_lock(manifest_path: Path | None = None,
                         lock_path: Path | None = None) -> str:
    """校验**清单自身**的 SHA256 锁，供实跑入口与对账调用。

    只校验输入文件的锁是不够的：清单被改动而保留旧锁时，实跑仍会按
    被改过的任务集执行。这里把清单字节与其锁绑定，改动即拒绝运行。
    允许传入副本路径，便于反向测试验证「改清单保留旧锁」会被拒绝。
    """
    manifest_path = manifest_path or MANIFEST
    lock_path = lock_path or MANIFEST_LOCK
    if not manifest_path.exists():
        raise ValueError(f"缺少任务清单：{manifest_path}")
    if not lock_path.exists():
        raise ValueError(f"缺少任务清单冻结锁：{lock_path}")
    raw = manifest_path.read_bytes()
    recorded = lock_path.read_text(encoding="utf-8").split()[0]
    current = sha256_bytes(raw)
    if recorded != current:
        raise ValueError(
            f"任务清单被改动而锁未更新：锁={recorded} 当前={current}。"
            "清单变更必须重新生成并留下评审记录。"
        )
    return current


def verify_lock(path: Path) -> str:
    raw = path.read_bytes()
    lock = path.with_name(path.name + ".sha256")
    if not lock.exists():
        raise ValueError(f"缺少冻结锁：{lock}")
    recorded = lock.read_text(encoding="utf-8").split()[0]
    current = sha256_bytes(raw)
    if recorded != current:
        raise ValueError(
            f"冻结输入被改动：锁={recorded} 当前={current}。"
            "行情输入变更必须重新生成任务清单并留下评审记录。"
        )
    return current


def build(document: dict, source_sha256: str) -> dict:
    portfolios = document["portfolios"]
    snapshots = document["snapshots"]
    tasks = []
    cell_keys = []
    for snapshot_index, snapshot in enumerate(snapshots, start=1):
        timestamp = snapshot["timestamp"]
        prices = snapshot["prices"]
        for portfolio in portfolios:
            holdings = portfolio["holdings"]
            symbols = [holding["symbol"] for holding in holdings]
            task_key = f"{portfolio['id']}|{timestamp}"
            cells = [f"{portfolio['id']}|{timestamp}|{symbol}" for symbol in symbols]
            cell_keys.extend(cells)
            tasks.append({
                "task_key": task_key,
                "task_index": len(tasks) + 1,
                "portfolio_id": portfolio["id"],
                "snapshot_index": snapshot_index,
                "timestamp": timestamp,
                "input_symbols": symbols,
                "holdings": holdings,
                "prices": prices,
                "cell_keys": cells,
                "tool_arguments": {
                    "portfolio": {
                        "id": portfolio["id"],
                        "holdings": holdings,
                    },
                    "snapshot": {"timestamp": timestamp, "prices": prices},
                },
            })
    if len(set(cell_keys)) != len(cell_keys):
        raise ValueError("出现重复的（组合, 时间, 标的）格，任务清单不完整")
    return {
        "schema": "topic16-task-manifest-v1",
        "source_input": "data/eval_20.json",
        "source_sha256": source_sha256,
        "portfolio_count": len(portfolios),
        "snapshot_count": len(snapshots),
        "task_count": len(tasks),
        "cell_count": len(cell_keys),
        "note": "只含输入，不含任何预期触发结果；任务键 = 组合 | 快照原始时间。",
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="只校验现有清单与冻结输入一致")
    args = parser.parse_args()

    digest = verify_lock(INPUT)
    document = json.loads(INPUT.read_bytes().decode("utf-8-sig"))
    expected = build(document, digest)

    if args.verify:
        try:
            locked = verify_manifest_lock()
        except ValueError as exc:
            print(f"任务清单校验：FAIL\n  {exc}")
            return 1
        actual_raw = MANIFEST.read_bytes()
        actual = json.loads(actual_raw.decode("utf-8-sig"))
        manifest_digest = sha256_bytes(actual_raw)
        ok = actual == expected
        print(f"任务清单校验：{'PASS' if ok else 'FAIL'}")
        print(f"  输入 SHA256：{digest}")
        print(f"  清单 SHA256：{manifest_digest}（锁 {locked}）")
        print(f"  {expected['task_count']} 个任务 / {expected['cell_count']} 个持仓格")
        return 0 if ok else 1

    raw = (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    MANIFEST.write_bytes(raw)
    # 锁文件同样用字节写入固定 LF，避免 Windows 文本模式写出 CRLF。
    MANIFEST_LOCK.write_bytes(
        f"{sha256_bytes(raw)}  {MANIFEST.name}\n".encode("utf-8")
    )
    print(f"已生成 {MANIFEST.relative_to(ROOT).as_posix()}")
    print(f"  输入 SHA256：{digest}")
    print(f"  任务 {expected['task_count']} 个 / 持仓格 {expected['cell_count']} 个")
    print(f"  清单 SHA256：{sha256_bytes(raw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
