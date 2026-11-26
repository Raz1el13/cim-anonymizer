"""Справочники, словообразование и разбиение текста на слова."""

import re
import unicodedata
from functools import cache
from importlib import resources
from typing import NamedTuple

_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = "a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch - y - e yu ya".split()
TRANSLIT = {c: lat.strip("-") for c, lat in zip(_CYR, _LAT, strict=True)}

_RUNS = re.compile(r"([^\W\d_]+)|(\d+)|([\W_]+)")
_GLUED_UNIT = re.compile(r"(?:кВА|кВт|кВ|МВА|МВт|kVA|kW|kV|MVA|MW)(?=[^\W\d_])")  # «кВяч.4» = «кВ» + «яч»


def is_cyr(text: str) -> bool:
    return any("Ѐ" <= c <= "ӿ" for c in text)


def translit(text: str) -> str:
    out = ""
    for c in text:
        lat = TRANSLIT.get(c.lower())
        out += c if lat is None else lat.capitalize() if c.isupper() else lat
    return out.upper() if text.isupper() and len(text) > 1 else out


def strip_accents(text: str) -> str:
    """Kéniéti -> Kenieti, кириллицу не трогаем (иначе й станет и)."""
    return "".join(
        c if is_cyr(c) else "".join(p for p in unicodedata.normalize("NFKD", c) if not unicodedata.combining(p))
        for c in text
    )


def norm(word: str) -> str:
    """Форма для поиска в словаре."""
    return strip_accents(word.lower().replace("ё", "е"))


def key(word: str) -> str:
    """Ключ слова без учёта алфавита: Алькеево и Alkeevo совпадают."""
    return translit(word.lower()).replace("'", "")


def stem(word_key: str) -> str:
    return word_key.rstrip("aeiouy") or word_key


def style(text: str, like: str) -> str:
    """Пишем text тем же алфавитом и регистром, что и like."""
    text = text if is_cyr(like) else translit(text)
    if like.isupper() and len(like) > 1:
        return text.upper()
    return text.lower() if like.islower() else text[:1].upper() + text[1:]


def case_like(letters: str, like: str) -> str:
    return "".join(c.lower() if s.islower() else c.upper() for c, s in zip(letters, like, strict=True))


def ending(word: str) -> str:
    """Окончание прилагательного-замены по исходному названию: Северная -> -ая."""
    low = word.lower()
    if is_cyr(low) and low.endswith(("ы", "и", "ые", "ие")) and not low.endswith(("ий", "ый")):
        return "ые"
    k = key(word)
    return "ая" if k.endswith("a") else "ое" if k.endswith(("o", "e")) else "ый"


def adjective(noun: str) -> str:
    """Груша -> грушевый, клубника -> клубничный, тыква -> тыквенный."""
    w = noun.lower()
    for ends, cut, suffix in (
        (("ка", "ко"), 2, "чный"), (("ок",), 2, "очный"), (("ец",), 2, "ечный"), (("ва",), 1, "енный"),
        (("ня",), 1, "ёвый"), (("ль",), 0, "ный"), (("ь",), 1, "ный"), (("й", "я"), 1, "евый"), (("и",), 0, "евый"),
    ):  # fmt: skip
        if w.endswith(ends):
            return w[: len(w) - cut] + suffix
    if w.endswith(("а", "о")):
        w = w[:-1]
        return w + ("евый" if w[-1] in "жшчщц" else "овый")
    return w + "овый"


def surname(name: str) -> str:
    """Иван -> Иванов, Юрий -> Юрьев, Никита -> Никитин."""
    if name.endswith("ий"):
        return name[:-2] + "ьев"
    if name.endswith(("ей", "ай", "ь")):
        return name[:-1] + "ев"
    return name[:-1] + "ин" if name.endswith(("а", "я")) else name + "ов"


def patronymic(name: str, female: bool) -> str:
    if name.endswith(("а", "я")):
        return name[:-1] + ("ична" if female else "ич")
    return surname(name) + ("на" if female else "ич")


class Dicts(NamedTuple):
    produce: dict[str, str]  # фрукт -> прилагательное
    male: dict[str, tuple[str, str, str]]  # имя -> (фамилия, отчество м, отчество ж)
    female: list[str]
    vocabulary: frozenset[str]  # технические и обычные слова
    proper: frozenset[str]  # слова, которые всегда считаем именами


def _rows(name: str) -> list[list[str]]:
    text = resources.files(__package__).joinpath("data", name).read_text("utf-8")
    lines = (line.strip() for line in text.splitlines())
    return [[f.strip() for f in line.split("|")] for line in lines if line and not line.startswith("#")]


@cache
def dicts() -> Dicts:
    rows = _rows("produce.txt")
    produce = {r[0]: r[2] if len(r) > 2 else adjective(r[0]) for r in rows}
    male = {
        r[0]: tuple(r[1:4]) if len(r) == 4 else (surname(r[0]), patronymic(r[0], False), patronymic(r[0], True))
        for r in _rows("names_male.txt")
    }
    female = [r[0] for r in _rows("names_female.txt")]
    # фрукты и имена уже использовались при прошлом обезличивании, значит это имена собственные
    proper = {r[0] for r in rows} | {r[1] for r in rows if len(r) > 1 and r[1] and " " not in r[1]}
    proper |= {adj[:-2] + e for adj in produce.values() for e in ("ый", "ая", "ое", "ые")}
    proper |= set(male) | {m[0] for m in male.values()} | set(female)
    proper = {norm(w) for w in proper}
    proper |= {norm(translit(w)) for w in proper}
    vocabulary = {norm(r[0]) for r in _rows("vocabulary.txt")} - proper
    return Dicts(produce, male, female, frozenset(vocabulary), frozenset(proper))


def _script(c: str) -> str:
    return "cyr" if "Ѐ" <= c <= "ӿ" else "lat" if c.isascii() or "À" <= c <= "ɏ" else "other"


def _split_word(w: str) -> list[str]:
    """Режем по смене алфавита и по CamelCase: Bougouroв, MadisonPlant, kVKoulikoro."""
    unit = _GLUED_UNIT.match(w)
    if unit:
        return [unit.group(), *_split_word(w[unit.end() :])]
    parts, start = [], 0
    for i in range(1, len(w)):
        prev, cur, nxt = w[i - 1], w[i], w[i + 1 : i + 2]
        camel = cur.isupper() and nxt.islower() and (prev.islower() or (prev.isupper() and i - start >= 2))
        if _script(prev) != _script(cur) or camel:
            parts.append(w[start:i])
            start = i
    return [*parts, w[start:]]


def tokenize(text: str) -> list[tuple[str, str]]:
    """Список (тип, текст): w - слово, n - число, o - всё остальное."""
    tokens = []
    for word, number, other in _RUNS.findall(text):
        if word:
            tokens += [("w", part) for part in _split_word(word)]
        else:
            tokens.append(("n", number) if number else ("o", other))
    return tokens


def shape(word: str) -> str:
    if word.islower():
        return "lower"
    if word.isupper():
        return "upper"
    return "title" if word[0].isupper() and word[1:].islower() else "mixed"
