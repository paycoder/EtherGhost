#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive
export WINEARCH=win64
export WINEDEBUG=fixme-all
export WINEPREFIX=/wine
export DISPLAY=:99

PYTHON_VERSION="3.10.11"
PYTHON_VER_NO_DOT="310"

for i in $(seq 1 5); do
    apt-get update -qy && apt-get install -y --no-install-recommends --fix-missing \
        wine winbind cabextract wget xvfb && break
    echo "apt-get attempt $i failed, retrying..."
    sleep 10
done

if ! command -v Xvfb &>/dev/null; then
    echo "ERROR: apt-get failed after 5 retries"
    exit 1
fi

Xvfb :99 -screen 0 1024x768x24 &
sleep 2

wineboot --init
wineserver -w

wget -nv -O /tmp/python.zip "https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip"
mkdir -p /wine/drive_c/Python${PYTHON_VER_NO_DOT}
cd /wine/drive_c/Python${PYTHON_VER_NO_DOT}
unzip /tmp/python.zip
rm /tmp/python.zip

PY_DIR="C:\\Python${PYTHON_VER_NO_DOT}"

sed -i 's/#import site/import site/' python${PYTHON_VER_NO_DOT}._pth

wget -nv -O /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
wine python.exe /tmp/get-pip.py
rm /tmp/get-pip.py

wineserver -w

cat > /usr/local/bin/wine-python << EOF
#!/bin/bash
export WINEPREFIX=/wine DISPLAY=:99
wine "${PY_DIR}\\python.exe" "\$@"
EOF
chmod +x /usr/local/bin/wine-python

for cmd in pip pyinstaller; do
    cat > "/usr/local/bin/wine-${cmd}" << EOF
#!/bin/bash
export WINEPREFIX=/wine DISPLAY=:99
wine "${PY_DIR}\\Scripts\\${cmd}.exe" "\$@"
EOF
    chmod +x "/usr/local/bin/wine-${cmd}"
done

wine-pip install pyinstaller
