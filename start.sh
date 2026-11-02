#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ -x /opt/homebrew/bin/python3 ]]; then
  kb22_python=/opt/homebrew/bin/python3
elif [[ -x /usr/local/bin/python3 ]]; then
  kb22_python=/usr/local/bin/python3
else
  kb22_python=$(command -v python3 || true)
fi
if [[ -z "$kb22_python" ]]; then
  print '需要 Python 3.10 或更新版本。安装后重新运行。'
  exit 1
fi
"$kb22_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "需要 Python 3.10 或更新版本")'
exec "$kb22_python" -u kb22_remote.py "$@"
