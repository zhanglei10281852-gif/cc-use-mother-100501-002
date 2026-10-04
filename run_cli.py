"""新能源接入承诺清算命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from grid_commitment import CapacityCommitment


def main() -> None:
    item = CapacityCommitment(commitment_code='commitment-code-001', resource_code='resource-code-001', period_code='period-code-001', state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
