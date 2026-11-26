"""Псевдонимы: ключевой хеш, шифр цифр, реестры замен в SQLite и их отображение."""

import hashlib
import re
import sqlite3
import uuid
from functools import lru_cache
from typing import NamedTuple

from .words import case_like, dicts, ending, is_cyr, key, stem, style, translit

LATIN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CYRILLIC = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"
RARE_CYRILLIC = "ЁЙЩЪЫЬ"
MAX_REGISTERED_NUMBER = 6  # более длинные номера - технические id, их только шифруем


class Hasher:
    def __init__(self, secret: bytes) -> None:
        self._key = hashlib.blake2b(secret, digest_size=32).digest()

    def digest(self, *parts: object) -> bytes:
        data = b"\x1f".join(str(p).encode() for p in parts)
        return hashlib.blake2b(data, key=self._key, digest_size=16).digest()

    def num(self, *parts: object) -> int:
        return int.from_bytes(self.digest(*parts), "big")

    def uuid(self, value: str) -> str:
        return str(uuid.UUID(bytes=self.digest("uuid", value), version=4))


class Digits:
    """Замена цифр цифрами той же длины: взаимно однозначно и без неподвижных точек.

    Сеть Фейстеля задаёт секретную перестановку всех чисел данной длины, то есть
    выстраивает их в цикл. Замена числа - следующее за ним число в этом цикле.
    """

    def __init__(self, hasher: Hasher, rounds: int = 8) -> None:
        self._hasher = hasher
        self._rounds = rounds
        self.encrypt = lru_cache(maxsize=1 << 16)(self._encrypt)

    @staticmethod
    def size(length: int) -> int:
        return 9 * 10 ** (length - 1)  # числа без ведущего нуля

    def step(self, digits: str, steps: int) -> str:
        n, mod = len(digits), 10 ** (len(digits) - 1)
        head, tail = self._feistel(n, int(digits[0]) - 1, int(digits[1:] or 0), forward=True)
        index = (head * mod + tail + steps) % self.size(n)
        head, tail = self._feistel(n, index // mod, index % mod, forward=False)
        return str(head + 1) + (str(tail).zfill(n - 1) if n > 1 else "")

    def _feistel(self, n: int, head: int, tail: int, forward: bool) -> tuple[int, int]:
        sign = 1 if forward else -1
        for r in range(self._rounds) if forward else reversed(range(self._rounds)):
            if r % 2 == 0:
                head = (head + sign * self._hasher.num("digits", n, r, tail)) % 9
            else:
                tail = (tail + sign * self._hasher.num("digits", n, r, head)) % 10 ** (n - 1)
        return head, tail

    def _encrypt(self, digits: str) -> str:
        zeros = len(digits) - len(digits.lstrip("0"))
        return digits if zeros == len(digits) else digits[:zeros] + self.step(digits[zeros:], 1)


class Store:
    """SQLite: словарь замен, чтобы они не менялись между запусками."""

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pseudonym (kind TEXT, source TEXT, value TEXT,
                PRIMARY KEY (kind, source), UNIQUE (kind, value));
        """)

    def check_secret(self, hasher: Hasher) -> None:
        fingerprint = hasher.digest("fingerprint").hex()
        row = self.db.execute("SELECT value FROM meta WHERE key = 'secret'").fetchone()
        if row and row[0] != fingerprint:
            raise ValueError("база замен создана с другим секретом, нужен тот же секрет или новый файл базы")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('secret', ?)", (fingerprint,))

    def load(self, kind: str) -> dict[str, str]:
        return dict(self.db.execute("SELECT source, value FROM pseudonym WHERE kind = ?", (kind,)))

    def save(self, kind: str, items: list[tuple[str, str]]) -> None:
        with self.db:
            self.db.executemany("INSERT INTO pseudonym VALUES (?, ?, ?)", [(kind, s, v) for s, v in items])


class Registry:
    """Каждому ключу - уникальная замена.

    Ключи раздаются в порядке их хешей, поэтому результат зависит только от набора
    ключей и секрета. Генератор возвращает кандидата для попытки n или None.
    """

    def __init__(self, kind: str, generate, hasher: Hasher, store: Store) -> None:
        self.kind, self._generate, self._hasher, self._store = kind, generate, hasher, store
        self.pending: set[str] = set()
        self.values: dict[str, str] = {}

    def add(self, item: str) -> None:
        if item not in self.values:
            self.pending.add(item)

    def __contains__(self, item: str) -> bool:
        return item in self.values

    def __getitem__(self, item: str) -> str:
        return self.values[item]

    def assign(self) -> None:
        self.values.update(self._store.load(self.kind))
        used = set(self.values.values())
        new = []
        for item in sorted(self.pending - self.values.keys(), key=lambda k: self._hasher.digest(self.kind, "order", k)):
            attempt = 0
            while (value := self._generate(item, attempt)) is None or value in used:
                attempt += 1
            used.add(value)
            self.values[item] = value
            new.append((item, value))
        self.pending.clear()
        self._store.save(self.kind, new)


class Person(NamedTuple):
    surname: str
    first: str
    patronymic: str

    def full(self, like: str = "") -> str:
        name = f"{self.surname} {self.first} {self.patronymic}"
        name = translit(name) if like and not is_cyr(like) else name
        return name.upper() if like.isupper() and len(like) > 1 else name

    def replace(self, original: str) -> str:
        """Подставляет части ФИО на место слов оригинала, пробелы сохраняются."""
        words = original.split()
        if not 2 <= len(words) <= 3:
            return self.full(original)
        parts = iter(self.full(original).split())
        return re.sub(r"\S+", lambda _: next(parts), original)

    def part(self, index: int, like: str = "") -> str:
        return self[index] if not like or is_cyr(like) else translit(self[index])

    @property
    def email(self) -> str:
        return translit(f"{self.surname}.{self.first}.{self.patronymic[0]}").lower() + "@example.com"


def person_key(name: str, female: bool) -> str:
    return ("f|" if female else "m|") + name


def suffix(attempt: int) -> str:
    return str(attempt + 1) if attempt else ""  # без суффикса, потом 2, 3, ...


class Pseudonyms:
    def __init__(self, hasher: Hasher, store: Store) -> None:
        d = dicts()
        self.h, self.digits = hasher, Digits(hasher)
        self.produce, self.male, self.female = d.produce, d.male, d.female
        self.source_words: set[str] = set()  # слова исходника, которые нельзя выдавать
        self.source_people: set[str] = set()
        self.source_numbers: dict[str, set[str]] = {}

        def registry(kind, generate):
            return Registry(kind, generate, hasher, store)

        self.places = registry("place", self._fruit("place"))
        self.orgs = registry("organisation", self._fruit("organisation"))
        self.streets = registry("street", self._street)
        self.persons = registry("person", self._person)
        self.abbrs = registry("abbreviation", self._letters)
        self.ips = registry("ip", self._ip)
        self.numbers = {k: registry(f"number:{k}", self._number(k)) for k in ("object", "other")}

    def assign(self) -> None:
        self.source_people = {k.split("|", 1)[1] for k in self.persons.pending}
        self.source_numbers = {k: set(r.pending) for k, r in self.numbers.items()}
        for r in (self.places, self.orgs, self.streets, self.persons, self.abbrs, self.ips, *self.numbers.values()):
            r.assign()

    def reserve(self, word_key: str, digits: str = "") -> None:
        self.source_words.add(stem(word_key) + digits)

    def reserve_text(self, text: str) -> None:
        for word, digits in re.findall(r"([^\W\d_]+)(\d*)", text):
            self.reserve(key(word), digits)

    def add_number(self, digits: str, kind: str) -> None:
        significant = digits.lstrip("0")
        if significant and len(significant) <= MAX_REGISTERED_NUMBER:
            self.numbers[kind].add(significant)

    def number(self, digits: str, kind: str) -> str:
        zeros = len(digits) - len(digits.lstrip("0"))
        if digits[zeros:] in self.numbers[kind]:
            return digits[:zeros] + self.numbers[kind][digits[zeros:]]
        return self.digits.encrypt(digits)

    def place(self, item: str, like: str) -> str:
        return self._adjective(self.places[item], like, ending(like))

    def org(self, item: str, like: str) -> str:
        return self._adjective(self.orgs[item], like, "ый")

    def street(self, item: str, like: str) -> str:
        name, _, number = self.streets[item].partition(":")
        return style(self.male[name][0] + "ская", like) + number

    def abbr(self, item: str, like: str) -> str:
        return case_like(self.abbrs[item], like)

    def person(self, item: str) -> Person:
        return self._make_person(self.persons[item])

    def _adjective(self, value: str, like: str, end: str) -> str:
        noun, _, number = value.partition(":")
        return style(self.produce[noun][:-2] + end, like) + number

    def _make_person(self, value: str) -> Person:
        gender, surname_from, first, father = value.split(":")
        female = gender == "f"
        surname = self.male[surname_from][0] + ("а" if female else "")
        return Person(surname, first, self.male[father][2 if female else 1])

    def _is_source(self, word: str, number: str) -> bool:
        return stem(key(word)) + number in self.source_words

    def _fruit(self, kind):
        nouns = list(self.produce)

        def generate(item, attempt):
            noun = nouns[self.h.num(kind, item) % len(nouns)]
            number = suffix(attempt)
            if self._is_source(self.produce[noun], number):
                return None
            return f"{noun}:{number}" if number else noun

        return generate

    def _street(self, item, attempt):
        names = list(self.male)
        name = names[self.h.num("street", item) % len(names)]
        number = suffix(attempt)
        if self._is_source(self.male[name][0] + "ская", number):
            return None
        return f"{name}:{number}" if number else name

    def _person(self, item, attempt):
        female = item.startswith("f|")
        males = list(self.male)

        def pick(words, role):
            return words[self.h.num("person", role, item, attempt) % len(words)]

        value = f"{'f' if female else 'm'}:{pick(males, 'surname')}:{pick(self.female if female else males, 'first')}"
        value += f":{pick(males, 'patronymic')}"
        person = self._make_person(value)
        names = {person.full().lower(), person.full("latin").lower()}
        return None if names & self.source_people else value

    def _number(self, kind):
        def generate(item, attempt):
            # первый круг пропускает номера, которые есть в исходнике, второй берёт любые свободные
            lap = attempt // Digits.size(len(item))
            candidate = self.digits.step(item, attempt + 1)
            if (lap == 0 and candidate in self.source_numbers[kind]) or (lap < 2 and candidate == item):
                return None
            return candidate

        return generate

    def _letters(self, item, attempt):
        alphabet = CYRILLIC if is_cyr(item) else LATIN
        if is_cyr(item) and attempt >= 100:
            alphabet += RARE_CYRILLIC
        letters = "".join(alphabet[self.h.num("letters", item, attempt, i) % len(alphabet)] for i in range(len(item)))
        return None if letters == item else letters

    def _ip(self, item, attempt):
        v = self.h.num("ip", item, attempt)
        return f"10.{v % 256}.{v // 256 % 256}.{v // 65536 % 254 + 1}"
