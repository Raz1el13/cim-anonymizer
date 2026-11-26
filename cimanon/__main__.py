"""python -m cimanon input.xml output.xml   - обезличить
python -m cimanon --check input.xml output.xml   - проверить результат
"""

import argparse
import json
import os
import sqlite3
import sys

from .check import check
from .model import anonymize

parser = argparse.ArgumentParser(prog="cimanon", description="Обезличивание CIM-модели (RDF/XML)")
parser.add_argument("source")
parser.add_argument("target")
parser.add_argument("--state", help="база замен SQLite (по умолчанию рядом с результатом)")
parser.add_argument("--check", action="store_true", help="сравнить target с source")
args = parser.parse_args()

try:
    if args.check:
        print(json.dumps(check(args.source, args.target), ensure_ascii=False, indent=2))
    else:
        secret = os.environ.get("CIMANON_SECRET", "cim-anonymizer-demo-secret")
        state = args.state or os.path.splitext(args.target)[0] + ".sqlite"
        changed = anonymize(args.source, args.target, state, secret.encode())
        print(f"готово: {args.target}, изменено значений: {changed}")
except (OSError, ValueError, sqlite3.Error) as error:
    sys.exit(f"ошибка: {error}")
