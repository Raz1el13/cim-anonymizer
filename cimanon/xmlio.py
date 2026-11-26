"""Потоковое чтение RDF/XML и перезапись, в которой меняется только текст значений.

Файл читается кусками, поэтому память не зависит от его размера. При перезаписи
все байты копируются как есть, кроме значений, которые вернул обработчик:
отступы, порядок атрибутов, BOM и строки без отступа остаются нетронутыми.
"""

from xml.parsers import expat

CHUNK = 1 << 22


class Stream:
    """handler(cls, obj_id, prop, value) -> новое значение или None; refs(cls, obj_id, prop, target)."""

    def __init__(self, handler, refs=None, sink=None) -> None:
        self.handler, self.refs, self.sink = handler, refs, sink
        self.parser = expat.ParserCreate()
        self.parser.buffer_text = True
        self.parser.StartElementHandler = self._start
        self.parser.EndElementHandler = self._end
        self.parser.CharacterDataHandler = self._text
        self.stack: list[list] = []  # [имя, позиция начала тега, есть дочерние, части текста]
        self.cls = self.obj = ""
        self.buf, self.buf_start, self.written, self.last = bytearray(), 0, 0, 0

    def run(self, source) -> None:
        while chunk := source.read(CHUNK):
            if self.sink:
                self.buf += chunk
            self.parser.Parse(chunk, False)
            # до начала открытого свойства байты уже не изменятся
            self._flush(self.stack[2][1] if len(self.stack) > 2 else self.last)
        self.parser.Parse(b"", True)
        self._flush(self.buf_start + len(self.buf))

    def _start(self, name, attrs) -> None:
        self.last = self.parser.CurrentByteIndex
        if self.stack:
            self.stack[-1][2] = True
        self.stack.append([name, self.last, False, []])
        if len(self.stack) == 2:
            self.cls, self.obj = name, attrs.get("rdf:about") or attrs.get("rdf:ID", "")
        elif len(self.stack) > 2 and self.refs and "rdf:resource" in attrs:
            self.refs(self.cls, self.obj, name, attrs["rdf:resource"])

    def _text(self, data) -> None:
        if len(self.stack) > 2:
            self.stack[-1][3].append(data)

    def _end(self, name) -> None:
        self.last = end = self.parser.CurrentByteIndex
        _, start, has_children, parts = self.stack.pop()
        if len(self.stack) < 2 or has_children or not parts:
            return
        value = "".join(parts)
        new = self.handler(self.cls, self.obj, name, value)
        if self.sink and new is not None and new != value:
            self._flush(self._after_tag(start))
            self.sink.write(new.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode())
            self.written = end

    def _after_tag(self, start: int) -> int:
        """Позиция сразу за '>' открывающего тега (кавычки в атрибутах пропускаем)."""
        i, quote = start - self.buf_start, 0
        while True:
            c = self.buf[i]
            if quote:
                quote = 0 if c == quote else quote
            elif c in (0x22, 0x27):
                quote = c
            elif c == 0x3E:
                return self.buf_start + i + 1
            i += 1

    def _flush(self, pos: int) -> None:
        if not self.sink or pos <= self.written:
            return
        self.sink.write(self.buf[self.written - self.buf_start : pos - self.buf_start])
        self.written = pos
        del self.buf[: pos - self.buf_start]
        self.buf_start = pos
