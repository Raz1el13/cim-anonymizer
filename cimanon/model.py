"""Какое поле модели как обезличивается, вложенные схемы и запуск двух проходов."""

import base64
import binascii
import html
import io
import os
import re
import struct
import zipfile
import zlib
from decimal import Decimal, InvalidOperation
from functools import cache

from .names import Hasher, Pseudonyms, Store, person_key
from .text import ADDRESS, CODE, GENERAL, IDENTIFIER, LEGAL, ORGANISATION, PERSON_LEGAL, SERIAL, STREET_NAME, Texts
from .xmlio import Stream

NAME = "cim:IdentifiedObject.name"
KEEP = set(
    """
    me:Equipment.nameplate me:Curve.points me:BlobStorage.image me:BlobStorage.name me:MarkerType.diagramView
    me:MarkerType.additionalView me:EventSound.sound me:BasicIntervalSchedule.points me:RatioTapChangerTable.points
    me:DjCategory.allowedObjectsFilter me:DjAttributeSet.xsd me:PSRType.className me:PSRType.prefix
    me:DocumentType.contentType me:DocumentType.fileExtension me:HISPartitionBase.prefix me:RemotePoint.addrStr1
    rf:SensorWinding.accuracyClass cim:CurrentTransformer.accuracyClass cim:CurrentTransformer.ctClass
""".split()
)  # марки оборудования, бинарные данные, технические коды
CATALOGS = set(
    """
    md:FullModel cim:BaseVoltage cim:PSRType cim:OperationalLimitType cim:NameType cim:MeasurementValueSource
    cim:ValueAliasSet cim:ValueToAlias cim:CableInfo cim:OverheadWireInfo cim:LoadResponseCharacteristic cim:Season
    cim:DayType cim:CurrentLimit cim:VoltageLimit cim:ActivePowerLimit cim:TransformerMeshImpedance
    cim:RatioTapChangerTable cim:PhaseTapChangerTabular cim:NonConformLoadSchedule cim:NonConformLoadGroup
    me:MeasurementType me:MeasurementTypeConfiguration me:MeasurementValueType me:ClassDescriptor me:ClassNode
    me:Icon me:MarkerType me:MarkerFunction me:UnitLabel me:DocumentType me:DjCategory me:DjCategoryType
    me:DirectiveCategory me:DirectiveStage me:DirectiveType me:ViolatedLimitType me:ViolationRelevance
    me:ViolationSeverity me:AggregationMethod me:CalendarDay me:CalendarMonth me:CalendarYear me:EventSound
    me:EventSoundPriority me:RoleCategory me:OperationalLimitSetType me:OMSCustomerKind me:DefectKind
    me:ElectricalSafetyLevel me:AltitudinalSafetyLevel me:ConfinedSpaceSafetyLevel me:ORWorkType me:ORPreparationType
    me:OROutageRequestType me:WorkOrderCategoryType me:WorkOrderCheckResult me:TransitionDataType
    me:PortableGroundInfo me:PortableGeneratorInfo me:ContingencyRejectionReason me:RejectionReason me:ShiftRole
    me:FormulaTemplate me:SvDataSource me:RCDMethod me:CurrentVsTemperatureLimitCurve me:CurrentVsTapStepLimitCurve
    me:FuseInverseTrippingCharacteristic me:PEInverseTrippingCharacteristic me:ServiceContingencyCategory
    me:ServiceContingencySignificance me:ServiceDispCategory me:NominalFrequency me:MonitelDiagramCategory
    me:CurrentFlowStage so:RepairsScheduleState so:RepairsScheduleType
""".split()
)  # справочники и перечисления: названия типов, а не реальных объектов
CATALOG_PREFIXES = ("me:DTS_", "me:HIS", "me:RTDB", "me:VC", "so:ZVK")
ADDRESS_PREFIXES = ("cim:StreetDetail.", "cim:TownDetail.", "cim:StreetAddress.", "cim:PostalAddress.")
BY_PROPERTY = {
    "me:IdentifiedObject.mRIDStr": ("id", IDENTIFIER), "cim:IdentifiedObject.mRID": ("id", IDENTIFIER),
    "cim:Asset.serialNumber": ("id", SERIAL), "rh:PowerSystemResource.ccsCode": ("id", CODE),
    "cim:TelephoneNumber.localNumber": ("id", SERIAL), "cim:TelephoneNumber.areaCode": ("id", SERIAL),
    "me:Location.fiasId": ("uuid", None), "me:Location.fiasStreetId": ("uuid", None),
    "me:IPConnectionPoint.address": ("ip", None),
    "cim:Person.lastName": ("person", 0), "cim:Person.firstName": ("person", 1), "cim:Person.mName": ("person", 2),
    "cim:PositionPoint.xPosition": ("coord", None), "cim:PositionPoint.yPosition": ("coord", None),
    "cim:PositionPoint.zPosition": ("coord", None),
    "cim:StreetDetail.name": ("text", STREET_NAME), "cim:StreetDetail.addressGeneral": ("text", STREET_NAME),
    "me:MonitelDiagram.image": ("diagram", GENERAL), "me:ACLineSegment.sections": ("sections", GENERAL),
}  # fmt: skip
BY_CLASS = {
    ("cim:Customer", NAME): ("customer", None), ("cim:Person", NAME): ("person", None),
    ("cim:ServiceLocation", NAME): ("text", ADDRESS), ("cim:Meter", NAME): ("id", CODE),
    ("cim:UsagePoint", NAME): ("id", CODE),
}  # fmt: skip
UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
BASE64 = re.compile(r"[A-Za-z0-9+/=\s]{120,}")


@cache
def field_kind(cls: str, prop: str) -> tuple[str, object]:
    if (cls, prop) in BY_CLASS:
        return BY_CLASS[cls, prop]
    if prop in BY_PROPERTY:
        return BY_PROPERTY[prop]
    if prop in KEEP:
        return "keep", None
    if prop.startswith(ADDRESS_PREFIXES):
        return "text", ADDRESS
    if cls in CATALOGS or cls.startswith(CATALOG_PREFIXES):
        return "keep", None
    return "text", GENERAL  # незнакомое поле считаем потенциально чувствительным


class Fields:
    def __init__(self, names: Pseudonyms, texts: Texts, hasher: Hasher) -> None:
        self.p, self.t, self.h = names, texts, hasher
        self.customers: list[tuple[str, str]] = []
        self.kind_ref: dict[str, str] = {}
        self.kind_name: dict[str, str] = {}
        self.legal: set[str] = set()
        self.changed = 0

    def reference(self, cls, obj, prop, target) -> None:
        if prop == "me:Customer.OMSKind":  # тип абонента лежит в отдельном объекте
            self.kind_ref[obj] = target

    def collect(self, cls, obj, prop, value) -> None:
        kind, arg = field_kind(cls, prop)
        if cls == "me:OMSCustomerKind" and prop == NAME:
            self.kind_name[obj] = value
        if kind == "customer":
            self.customers.append((obj, value))
        elif kind == "id" and UUID.fullmatch(value):
            pass
        elif kind in ("text", "id") and not _is_base64(value):
            self.t.collect(value, arg)
        elif kind in ("diagram", "sections"):
            _walk_blob(kind, value, lambda text: self.t.collect(text, arg) or text)
        elif kind == "ip" and value.strip():
            self.p.ips.add(value.strip())
        elif kind == "person":
            self.p.persons.add(person_key("object:" + obj, False))

    def prepare(self) -> None:
        """Абоненты разбираются после первого прохода, когда известны их типы."""
        for obj, name in self.customers:
            if _is_legal(name, self.kind_name.get(self.kind_ref.get(obj), "")):
                self.legal.add(obj)
            form, rest = _legal_form(name)
            if name.strip().isdigit():
                self.t.collect_digits(name)
            elif obj not in self.legal:
                self.p.persons.add(_name_key(name))
            elif not form:
                self.t.collect(name, ORGANISATION)
            elif _person_form(form):
                self.p.persons.add(_name_key(rest))
            else:
                self.p.orgs.add(_squash(name))
                self.p.reserve_text(rest)

    def render(self, cls, obj, prop, value) -> str:
        new = self._render(cls, obj, prop, value)
        self.changed += new != value
        return new

    def _render(self, cls, obj, prop, value) -> str:
        kind, arg = field_kind(cls, prop)
        if kind in ("text", "id"):
            skip = _is_base64(value) or (kind == "id" and UUID.fullmatch(value))
            return value if skip else self.t.anonymize(value, arg)
        if kind in ("diagram", "sections"):
            return _walk_blob(kind, value, lambda text: self.t.anonymize(text, arg), rewrite=True)
        if kind == "uuid":
            return self.h.uuid(value.strip().lower()) if value.strip() else value
        if kind == "ip":
            return self.p.ips[value.strip()] if value.strip() else value
        if kind == "person":
            person = self.p.person(person_key("object:" + obj, False))
            return person.full(value) if arg is None else person.part(arg, value)
        if kind == "customer":
            return self._customer(obj, value)
        if kind == "coord":
            return self._coordinate(prop, value)
        return value

    def _customer(self, obj, value) -> str:
        if value.strip().isdigit():
            return self.t.cipher_digits(value)
        if obj not in self.legal:
            return self.p.person(_name_key(value)).replace(value)
        form, rest = _legal_form(value)
        if not form:
            return self.t.anonymize(value, ORGANISATION)
        if _person_form(form):  # ИП: правовая форма + ФИО
            return form + self.p.person(_name_key(rest)).replace(rest)
        return form + self.p.org(_squash(value), rest.strip()) + rest[len(rest.rstrip()) :]

    def _coordinate(self, prop, value) -> str:
        """Сдвиг на секретное смещение: расстояния между точками сохраняются."""
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return value
        shift = Decimal(str(round((2 * (self.h.num("coordinate", prop) % 10**6 / 10**6) - 1) * 0.5, 6)))
        return f"{number + shift:.{max(-number.as_tuple().exponent, 0)}f}"


def _is_base64(value: str) -> bool:
    return len(value) >= 120 and " " not in value.strip() and BASE64.fullmatch(value) is not None


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def _name_key(name: str) -> str:
    words = re.findall(r"[^\W\d_][^\W\d_'-]*", name)
    return person_key(_squash(name), len(words) >= 2 and words[1].lower().endswith(("a", "ya", "а", "я")))


def _legal_form(name: str) -> tuple[str, str]:
    """«ООО Ромашка» -> («ООО », «Ромашка»), без формы -> («», name)."""
    if name.lower().startswith("individual entrepreneur "):
        return name[:24], name[24:]
    first, _, rest = name.partition(" ")
    return (name[: len(first) + 1], rest) if rest and _squash(first).strip(".") in LEGAL else ("", name)


def _person_form(form: str) -> bool:
    return _squash(form.split()[0]) in PERSON_LEGAL


def _is_legal(name: str, kind: str) -> bool:
    """Тип берём из модели, а если его нет - по виду имени (2-3 слова с большой буквы = ФИО)."""
    kind = kind.lower()
    if "natural" in kind or "физ" in kind:
        return False
    if kind or _legal_form(name)[0]:
        return True
    words = name.split()
    return not (2 <= len(words) <= 3 and all(re.fullmatch(r"[A-ZА-ЯЁ][a-zа-яё'-]+\d*", w) for w in words))


# Вложенные документы: схемы подстанций (base64 ZIP с XML и PNG) и участки линий (base64 XML).
LABELS = {
    "diagram": {b"Node": {b"Text", b"ElementName", b"Value", b"text", b"CurrentExpression"},
                b"NavigationLink": {b"name"}, b"Diagram": {b"Description"}, b"LExpression": {b"name"}},
    "sections": {b"Section": {b"Name"}},
}  # fmt: skip
TAG = re.compile(rb'<(\w+)((?:[^>"]|"[^"]*")*)>')
ATTR = re.compile(rb'(\s(\w+)=")([^"]*)(")')
ELEMENT_TEXT = re.compile(rb"(<(Name|Cell)>)([^<]*)(</\2>)")


def _walk_blob(kind: str, value: str, func, rewrite: bool = False) -> str:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return value
    if kind == "sections":
        new = _xml(data, func, LABELS[kind]) if _is_xml(data) else data
    elif data.startswith(b"PK\x03\x04"):
        new = _package(data, func, rewrite)
    else:
        return value
    return base64.b64encode(new).decode() if rewrite and new != data else value


def _package(data: bytes, func, rewrite: bool) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for entry in src.infolist():
            content = src.read(entry)
            if content.startswith(b"PK\x03\x04"):
                content = _package(content, func, rewrite)
            elif content.startswith(b"\x89PNG"):  # миниатюра нарисована с исходными подписями
                content = _blank_png(*struct.unpack(">II", content[16:24]))
            elif _is_xml(content):
                content = _xml(content, func, LABELS["diagram"])
                content = ELEMENT_TEXT.sub(lambda m: _replace(m, func, attribute=False), content)
                content = content.replace(b'ThumbnailOutdated="false"', b'ThumbnailOutdated="true"')
            if rewrite:
                dst.writestr(entry, content)
    return out.getvalue() if rewrite else data


def _xml(data: bytes, func, labels) -> bytes:
    def tag(m):
        wanted = labels.get(m[1])
        if not wanted:
            return m[0]
        attrs = ATTR.sub(lambda a: _replace(a, func) if a[2] in wanted else a[0], m[2])
        return b"<" + m[1] + attrs + b">"

    return TAG.sub(tag, data)


def _replace(m, func, attribute: bool = True) -> bytes:
    try:
        old = html.unescape(m[3].decode())
    except UnicodeDecodeError:
        return m[0]
    new = func(old)
    if new == old:
        return m[0]
    new = new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if attribute:
        new = new.replace('"', "&quot;").replace("\r", "&#xD;").replace("\n", "&#xA;").replace("\t", "&#x9;")
    return m[1] + new.encode() + m[4]


def _is_xml(data: bytes) -> bool:
    return data.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")


@cache
def _blank_png(width: int, height: int) -> bytes:
    def chunk(name, payload):
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = zlib.compress(b"\x00" * ((width + 1) * height), 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def anonymize(source: str, target: str, state: str, secret: bytes) -> int:
    """Два прохода: собрать всё, что надо заменить, назначить замены, переписать файл."""
    hasher, store = Hasher(secret), Store(state)
    store.check_secret(hasher)
    names = Pseudonyms(hasher, store)
    fields = Fields(names, Texts(names), hasher)
    with open(source, "rb") as f:
        Stream(fields.collect, fields.reference).run(f)
    fields.prepare()
    names.assign()
    with open(source, "rb") as f, open(target + ".partial", "wb") as out:
        Stream(fields.render, sink=out).run(f)
    os.replace(target + ".partial", target)
    return fields.changed
