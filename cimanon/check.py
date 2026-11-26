"""Проверка результата: файлы читаются параллельно и сравниваются элемент за элементом."""

import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import zip_longest

from .words import dicts, norm, translit

SENSITIVE = {"Substation", "Line", "Customer", "Person", "ServiceLocation", "GeographicalRegion",
             "SubGeographicalRegion", "Plant", "ControlAreaOperator", "HostControlArea", "ACLineSegment"}  # fmt: skip
PROPER_WORD = re.compile(r"\b[A-ZА-ЯЁ][a-zа-яёéèïàç]{3,}\b")
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?|true|false")


def check(source: str, result: str) -> dict:
    d = dicts()
    synthetic = {w for name, m in d.male.items() for w in (name, *m, m[0] + "а")} | set(d.female)
    known = d.vocabulary | {norm(w) for w in synthetic} | {norm(translit(w)) for w in synthetic}
    report = Counter()
    numeric, forward, backward = Counter(), {}, {}
    pairs, source_names, source_words = [], defaultdict(set), set()
    depth, cls = 0, ""
    for a, b in zip_longest(_events(source), _events(result)):
        if a is None or b is None or a[0] != b[0] or a[1].tag != b[1].tag or a[1].attrib != b[1].attrib:
            raise ValueError("структура файлов различается")
        event, x, y = a[0], a[1], b[1]
        if event == "start":
            depth += 1
            if depth == 2:
                cls = x.tag.rsplit("}", 1)[-1]
                report["objects"] += 1
            elif depth > 2 and any(k.endswith("resource") for k in x.attrib):
                report["references"] += 1
            continue
        if depth > 2 and len(x) == 0 and x.text is not None:
            prop = x.tag.rsplit("}", 1)[-1]
            before, after = x.text, y.text or ""
            report["literals"] += 1
            if before != after:
                report["changed"] += 1
                if NUMBER.fullmatch(before.strip()):
                    numeric[f"{cls}.{prop}"] += 1
            if prop == "IdentifiedObject.name":
                report["names"] += 1
                report["inconsistent"] += forward.setdefault((cls, before), after) != after
                report["collisions"] += backward.setdefault((cls, after), before) != before
                if cls in SENSITIVE:
                    pairs.append((cls, before, after))
                    source_names[cls].add(before)
                    source_words.update(w for w in PROPER_WORD.findall(before) if norm(w) not in known)
        depth -= 1
        if depth == 1:
            x.clear()
            y.clear()
    report["unchanged_sensitive"] = sum(b == a for _, b, a in pairs)
    report["reused_numbers"] = sum(b != a and a in source_names[c] for c, b, a in pairs)
    leaks = Counter(w for _, _, a in pairs for w in PROPER_WORD.findall(a) if w in source_words)
    return {**report, "numbers_changed": dict(numeric), "leaked_words": dict(leaks.most_common(20))}


def _events(path: str):
    with open(path, "rb") as f:
        yield from ET.iterparse(f, events=("start", "end"))
