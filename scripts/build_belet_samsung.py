"""Build the unsigned Samsung Belet Tizen client without changing existing apps."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'belet_samsung_native'
OUT = ROOT / 'samsung-belet' / 'BeletSamsungClient-unsigned.wgt'
FILES = ('config.xml', 'index.html', 'style.css', 'api.js', 'hls.min.js', 'hls.LICENSE', 'app.js',
         'wrt-service.js', 'service/clock-proxy.js',
         'icon.png', 'belet-logo.png', 'login-background.png', 'belet-regular.ttf', 'belet-bold.ttf')


def build(output=OUT):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(output), 'w', zipfile.ZIP_DEFLATED) as package:
        for name in FILES:
            package.write(str(SRC / name), name)
    return output


if __name__ == '__main__':
    print(build())
