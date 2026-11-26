"""Обезличивание свободного текста: решаем по каждому слову, что с ним делать.

Слово после «ПС», «ВЛ», «ул.», «ООО» считается названием, число после «КТП -» -
диспетчерским номером, число перед «кВ» - напряжением. Слово с большой буквы,
которого нет в словаре технических слов, считается именем собственным.
"""

import re
from functools import lru_cache
from typing import NamedTuple

from .names import Pseudonyms, person_key
from .words import dicts, key, norm, shape, strip_accents, tokenize


def _words(*groups: str) -> frozenset[str]:
    return frozenset(" ".join(groups).split())


STATION = _words("sub substation ssub zsub csub bsub psub rsub msub nsub ps ts tp rp ktp ztp bktp stp mtp ktpn rtp crp",
                 "пс тп рп ктп зтп бктп стп мтп ктпн ртп црп тс")  # fmt: skip
FEEDER = _words("f ф feeder фидер")
LINE = _words("ohl cl ohcl line cableline kvl вл кл вкл квл лэп")
STREET = _words("rue av avenue street road lane ул улица пр просп проспект пер переулок тр",
                "бул бульвар ш шоссе наб пл площадь мкр микрорайон")  # fmt: skip
LOCALITY = _words("village town с д п г пос пгт село деревня поселок город")
LEGAL = _words("llc ooo jsc ojsc cjsc pjsc zao oao pao ip ngdu mup emup gup snt",
               "ооо оао зао пао ао нао ип муп емуп гуп фгуп мбу гбу гау гаук мауз гауз гбуз фгбу фгку фгаоу",
               "мбоу маоу гбоу снт тсж нгду")  # fmt: skip
PERSON_LEGAL = _words("ip individual ип")
LABEL = _words("inn kpp ogrn snils passport account contract tel phone",
               "инн кпп огрн огрнип снилс паспорт лс счет договор тел телефон")  # fmt: skip
UNIT = _words("kv v kva mva mw kw кв в ква мва мвт квт")
VOLTAGE = _words("6 10 20 35 110 150 220 330 500 750")
STRUCTURAL = _words("bay section busbar bus feeder line segment reserve tap branch",
                    "ячейка секция шина фидер линия участок резерв отпайка ввод")  # fmt: skip
GENERIC_CODE = _words("ges aes tes gres ses ves ues res pes гэс аэс тэс грэс гтэс пгэс сэс вэс еэс рэс пэс")

SPECIAL = re.compile(
    r"(?P<keep>[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"
    r"|(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
    r"|(?P<phone>(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"
    r"|(?<![\d-])\d{3}-\d{2}-\d{2}(?![\d-]))"
    r"|(?P<ip>(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.]))"
)
# ЧЭС, NChES: буквы места + окончание «ЭС»
ORG_CODE = re.compile(r"(?P<p>[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]{0,4}?)(?P<s>ГРЭС|РЭС|ПЭС|ЭСК|ЭС|GRES|RES|PES|ESK|ES)")
MEASURE = re.compile(r"[UIPQS](?:max|min|out|fact|trip|gen|load|nom|sum|ij[abc]?|[abc]{1,3})")  # Imax, Uab
INITIALS = re.compile(r"\s*[A-ZА-ЯЁ]\.\s?[A-ZА-ЯЁ]\.")
KEEP_CONTEXT = {
    "station": r"[\s.\-–№#_/,]*", "feeder": r"[\s.\-–№#_]*", "line": r"[\s.\-–№#_/,]*",
    "street": r"[\s.]*", "locality": r"[\s.]*", "legal": r"\s*", "label": r"[\s.:№#()/\-]*",
}  # fmt: skip
NAMED = {"station", "line", "street", "locality", "legal"}  # после них следующее слово - название
CONTEXTS = ((STATION, "station"), (LINE, "line"), (FEEDER, "feeder"), (LEGAL, "legal"), (LABEL, "label"))


class Mode(NamedTuple):
    digits: int | None = None  # заменять все числа от стольких цифр
    streets: bool = False  # названия в поле - это улицы
    prefer_streets: bool = False
    upper_names: bool = False  # слова капсом тоже имена (названия юрлиц)


GENERAL, ORGANISATION = Mode(), Mode(upper_names=True)
ADDRESS, STREET_NAME = Mode(digits=1, prefer_streets=True), Mode(digits=1, streets=True)
IDENTIFIER, SERIAL, CODE = Mode(digits=5), Mode(digits=1), Mode(digits=4)


class Span(NamedTuple):
    action: str  # keep, name, maybe, surname, abbr, digits, email, phone, ip
    text: str
    key: str = ""  # для чисел - вид номера: object или other
    street: bool = False
    contextual: bool = False  # обычное слово, которое названием стало только здесь


class Walker:
    """Один проход по словам фрагмента текста с учётом контекста."""

    def __init__(self, text: str, mode: Mode) -> None:
        self.d, self.mode, self.tokens, self.spans = dicts(), mode, tokenize(text), []
        self._context("")

    def walk(self) -> list[Span]:
        for i, (kind, text) in enumerate(self.tokens):
            {"o": self._separator, "n": self._number, "w": self._word}[kind](i, text)
        return self.spans

    def emit(self, action, text, k="", street=False, contextual=False) -> None:
        self.spans.append(Span(action, text, k, street, contextual))

    def common(self, word: str) -> bool:
        return norm(word) in self.d.vocabulary

    def proper(self, word: str) -> bool:
        return norm(word) in self.d.proper

    def _context(self, name: str) -> None:
        self.ctx, self.want_name, self.want_number = name, name in NAMED, name in ("station", "feeder")
        self.marked = self.quoted = False

    def _separator(self, i, text) -> None:
        self.emit("keep", text)
        ctx = self.ctx
        if not ctx:
            return
        if re.search(r"[\r\n]", text):
            self._context("")
        elif ctx == "legal" and re.search("[\"'«»“”„]", text):
            if len(re.findall("[\"'«»“”„]", text)) % 2:
                self.quoted = not self.quoted
            self.want_name = True
        elif not self.quoted and not re.fullmatch(KEEP_CONTEXT[ctx], text):
            self._context("")
            return
        if re.search("[-–№#]", text):
            self.marked = True
            if ctx == "line":
                self.want_name = True
            elif ctx == "station" and "№" in text:
                self.want_number = True

    def _number(self, i, text) -> None:
        if self.mode.digits is not None:
            self.emit("digits" if len(text) >= self.mode.digits else "keep", text, "other")
        elif self.ctx == "label":
            self.emit("digits", text, "other")
        elif self.ctx == "feeder":  # ф.103-02 и ф.48-04 содержат номер подстанции, ф.05 - нет
            nxt = self.tokens[i + 1 : i + 3]
            compound = len(nxt) == 2 and nxt[0][1] == "-" and nxt[1][0] == "n"
            self.emit("digits" if len(text) >= 3 or compound else "keep", text, "object")
            self._context("")
        elif self.ctx == "station" and self.want_number and (self.marked or not self._voltage(i)):
            self.emit("digits", text, "object")
            self._context("")
        else:
            self.emit("keep", text)
            if self.ctx == "station" or (self.ctx == "line" and (text in VOLTAGE or self._voltage(i))):
                self.want_number = False
            elif self.ctx:
                self._context("")

    def _voltage(self, i) -> bool:
        """«35/10 кВ» или стандартное напряжение перед названием: «Sub 110 Kéniéba»."""
        standard = self.tokens[i][1] in VOLTAGE
        for kind, text in self.tokens[i + 1 :]:
            if kind == "n" or (kind == "o" and re.fullmatch(r"[\s/,.]*", text)):
                continue
            return kind == "w" and (norm(text) in UNIT or (standard and shape(text) in ("title", "upper")))
        return False

    def _word(self, i, text) -> None:
        n = norm(text)
        transparent = (self.ctx in ("station", "line") and n in UNIT) or (
            self.ctx in NAMED and len(text) == 1 and text.isupper()
        )
        if self._open(text, n) or transparent:
            self.emit("keep", text)
        elif self.want_name and self._forced(text, n):
            self._forced_name(text)
        elif self._surname(i, text):
            self._context("")
            self.emit("surname", text, key(text))
        else:
            self._context("")
            self._classify(text)

    def _open(self, text, n) -> bool:
        if text == "F":  # латинская F внутри UUID - не фидер
            return False
        for words, name in CONTEXTS:
            if n in words:
                self._context(name)
                return True
        for words, name, shapes in ((STREET, "street", ("lower", "title")), (LOCALITY, "locality", ("lower",))):
            if n in words and shape(text) in shapes:
                self._context(name)
                return True
        return False

    def _forced(self, text, n) -> bool:
        if shape(text) not in ("title", "upper") or n in STRUCTURAL or len(text) < 3:
            return False
        return not re.fullmatch("[IVXLC]+", text) and (shape(text) != "upper" or not self.common(text))

    def _forced_name(self, text) -> None:
        if shape(text) == "upper" and len(text) <= 5:
            self.emit("abbr", text, strip_accents(text.upper()))
        else:
            street = self.ctx == "street" or self.mode.streets
            self.emit("name", text, key(text), street, self.common(text) and not self.proper(text))
        self.want_number = False
        if self.ctx in ("station", "street", "locality"):
            self._context("")
        elif self.ctx == "line":
            self.want_name = False

    def _surname(self, i, text) -> bool:
        """Слово рядом с инициалами: «Макаров В.В.», «Б.Н. Ельцин»."""
        if shape(text) != "title" or len(text) < 3 or (self.common(text) and not self.proper(text)):
            return False
        after = "".join(t for _, t in self.tokens[i + 1 : i + 6])
        before = "".join(t for _, t in self.tokens[max(i - 5, 0) : i])
        return bool(INITIALS.match(after) or INITIALS.fullmatch(before.strip()[-4:]))

    def _classify(self, text) -> None:
        s, k, street, short = shape(text), key(text), self.mode.streets, len(text) <= 3
        if self.proper(text):
            self.emit("name", text, k, street)
        elif s == "lower":
            self.emit("keep" if short else "maybe", text, k, street)
        elif s == "title":
            if (short and text.isascii()) or MEASURE.fullmatch(text):
                self.emit("keep", text)
            else:
                self.emit("maybe" if self.common(text) else "name", text, k, street)
        elif self._org_code(text):
            pass
        elif s == "upper" and short:
            self.emit("keep", text)
        elif s == "upper":
            upper_name = self.mode.upper_names and len(text) >= 3 and not self.common(text)
            self.emit("name" if upper_name else "maybe", text, k, street)
        else:
            self.emit("keep", text)

    def _org_code(self, text) -> bool:
        """В «NChES» шифруем буквы места, «ES» оставляем."""
        m = None if norm(text) in GENERIC_CODE else ORG_CODE.fullmatch(text)
        if m:
            self.emit("abbr", m["p"], strip_accents(m["p"].upper()))
            self.emit("keep", m["s"])
        return bool(m)


@lru_cache(maxsize=1 << 16)
def plan(text: str, mode: Mode) -> tuple[Span, ...]:
    spans, pos = [], 0
    for m in SPECIAL.finditer(text):
        spans += Walker(text[pos : m.start()], mode).walk()
        spans.append(Span(m.lastgroup, m.group()))
        pos = m.end()
    return (*spans, *Walker(text[pos:], mode).walk())


class Texts:
    """Первый проход: собираем, что придётся заменить. Второй: заменяем."""

    def __init__(self, names: Pseudonyms) -> None:
        self.p = names
        self.known: set[str] = set()  # ключи слов, которые являются названиями везде
        self.anonymize = lru_cache(maxsize=1 << 16)(self._anonymize)

    def collect(self, text: str, mode: Mode) -> None:
        spans = plan(text, mode)
        for i, s in enumerate(spans):
            if s.action == "name":
                (self.p.streets if s.street else self.p.places).add(s.key)
                glued = spans[i + 1].text if i + 1 < len(spans) and spans[i + 1].text.isdigit() else ""
                self.p.reserve(s.key, glued)  # чтобы не выдать как замену «Lukovyy4» из исходника
                if not s.contextual:
                    self.known.add(s.key)
            elif s.action == "surname":
                self.p.persons.add(_surname_key(s))
            elif s.action == "digits":
                self.p.add_number(s.text, s.key)
            elif s.action == "abbr":
                self.p.abbrs.add(s.key)
            elif s.action == "email":
                self.p.persons.add(person_key("email:" + s.text.lower(), False))
            elif s.action == "phone":
                self.collect_digits(s.text)
            elif s.action == "ip":
                self.p.ips.add(s.text)

    def collect_digits(self, text: str) -> None:
        for digits in re.findall(r"\d+", text):
            self.p.add_number(digits, "other")

    def cipher_digits(self, text: str) -> str:
        return re.sub(r"\d+", lambda m: self.p.number(m.group(), "other"), text)

    def _anonymize(self, text: str, mode: Mode) -> str:
        return "".join(self._render(s, mode) for s in plan(text, mode))

    def _render(self, s: Span, mode: Mode) -> str:
        if s.action == "keep" or (s.action == "maybe" and not self._is_known(s)):
            return s.text
        if s.action in ("name", "maybe"):
            if (s.street or mode.prefer_streets) and s.key in self.p.streets:
                return self.p.street(s.key, s.text)
            if s.key in self.p.places:
                return self.p.place(s.key, s.text)
            return self.p.street(s.key, s.text) if s.key in self.p.streets else s.text
        if s.action == "digits":
            return self.p.number(s.text, s.key)
        if s.action == "surname":
            return self.p.person(_surname_key(s)).part(0, s.text)
        if s.action == "abbr":
            return self.p.abbr(s.key, s.text)
        if s.action == "email":
            return self.p.person(person_key("email:" + s.text.lower(), False)).email
        if s.action == "phone":
            prefix = re.match(r"\s*(?:\+7|8)", s.text)
            start = prefix.end() if prefix else 0
            return s.text[:start] + self.cipher_digits(s.text[start:])
        return self.p.ips[s.text]

    def _is_known(self, s: Span) -> bool:
        return s.key in self.known and not (s.text.islower() and norm(s.text) in dicts().vocabulary)


def _surname_key(s: Span) -> str:
    return person_key("surname:" + s.key, s.text.lower().endswith(("а", "ая", "a", "aya")))
