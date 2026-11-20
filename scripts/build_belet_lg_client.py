"""Build the unsigned LG webOS Belet client (IPK) without touching other apps."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import belet_lg_client  # noqa: E402  (path bootstrap above)

OUT = ROOT / 'lg-belet' / belet_lg_client.PACKAGE_NAME


def build(output=OUT):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = belet_lg_client.build_ipk()
    output.write_bytes(data)
    return output


if __name__ == '__main__':
    print(build())
