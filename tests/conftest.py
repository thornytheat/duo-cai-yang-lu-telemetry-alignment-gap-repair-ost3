"""测试共用：把项目根加入 sys.path，并把 SQLite 指向临时目录（不污染源码目录）。"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TSGRID_DATA_DIR", tempfile.mkdtemp(prefix="tsgrid-test-"))
