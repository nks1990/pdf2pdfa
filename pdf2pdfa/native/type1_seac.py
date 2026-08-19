"""Owned Type 1 ``seac`` composite-glyph resolution.

The strict byte-level Type1 core deliberately keeps PostScript composite
semantics outside its ordinary CharString interpreter.  This module adds the
one composite operator that PDF Type1 rendering needs without introducing a
PostScript VM: ``seac asb adx ady bchar achar``.

``bchar`` and ``achar`` are Adobe StandardEncoding codes.  Their original
CharStrings are interpreted by the same owned Type1 core.  The base outline is
kept at its normal origin and the accent is translated by
``composite_lsb_x + adx - asb, ady``.  Nested seac is rejected, matching the
format constraint rather than recursively inventing composite semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .font_encoding import base_encoding
from .type1 import (
    Type1Command,
    Type1Error,
    Type1Font,
    Type1Outline,
    _Interpreter,
    _exact_int,
    _take,
)


@dataclass(frozen=True, slots=True)
class SeacSpec:
    accent_sidebearing: float
    accent_dx: float
    accent_dy: float
    base_code: int
    accent_code: int


@dataclass(frozen=True, slots=True)
class _RawOutline:
    commands: tuple[Type1Command, ...]
    width_x: float | None
    width_y: float
    left_bearing_x: float
    left_bearing_y: float
    seac: SeacSpec | None = None


class _SeacSignal(Exception):
    def __init__(self, spec: SeacSpec) -> None:
        super().__init__("seac")
        self.spec = spec


class _SeacInterpreter(_Interpreter):
    """Ordinary Type1 interpreter plus a structured seac termination signal."""

    def _escape(self, op: int) -> None:
        if op != 6:
            return super()._escape(op)
        asb, adx, ady, bchar_raw, achar_raw = _take(self.stack, 5, "seac")
        bchar = _exact_int(bchar_raw, "Type1 seac base character code")
        achar = _exact_int(achar_raw, "Type1 seac accent character code")
        if not 0 <= bchar <= 255 or not 0 <= achar <= 255:
            raise Type1Error("Type1 seac character codes shall be in 0..255")
        if self.commands:
            raise Type1Error("Type1 seac composite shall not follow painted outline commands")
        raise _SeacSignal(SeacSpec(asb, adx, ady, bchar, achar))

    def run_raw(self, data: bytes) -> _RawOutline:
        try:
            ordinary = super().run(data)
        except _SeacSignal as signal:
            if self.stack:
                raise Type1Error("Type1 seac left operands on the CharString stack")
            return _RawOutline(
                tuple(self.commands),
                self.state.width_x,
                self.state.width_y,
                self.state.x,
                self.state.y,
                signal.spec,
            )
        return _RawOutline(
            ordinary.commands,
            ordinary.width_x,
            ordinary.width_y,
            0.0,
            0.0,
            None,
        )


def _translate(commands: tuple[Type1Command, ...], dx: float, dy: float) -> tuple[Type1Command, ...]:
    translated: list[Type1Command] = []
    for command in commands:
        values = command.values
        if command.operator in {"M", "L"}:
            translated.append(
                Type1Command(command.operator, (values[0] + dx, values[1] + dy))
            )
        elif command.operator == "C":
            translated.append(
                Type1Command(
                    "C",
                    (
                        values[0] + dx, values[1] + dy,
                        values[2] + dx, values[3] + dy,
                        values[4] + dx, values[5] + dy,
                    ),
                )
            )
        elif command.operator == "Z":
            translated.append(command)
        else:
            raise Type1Error(f"unsupported Type1 outline command {command.operator!r} in seac")
    return tuple(translated)


class SeacType1Font(Type1Font):
    """Type1Font with owned StandardEncoding seac composition."""

    def _raw(self, name: str, *, exact: bool) -> _RawOutline:
        data = self.charstrings.get(name)
        if data is None and not exact:
            data = self.charstrings.get(".notdef")
        if data is None:
            raise Type1Error(f"Type1 seac references missing glyph /{name}")
        return _SeacInterpreter(self.subrs).run_raw(data)

    @staticmethod
    def _ordinary(raw: _RawOutline) -> Type1Outline:
        if raw.seac is not None:
            raise Type1Error("nested Type1 seac composite is not permitted")
        return Type1Outline(raw.commands, raw.width_x, raw.width_y)

    def outline(self, name: str) -> Type1Outline:
        raw = self._raw(name, exact=False)
        if raw.seac is None:
            return Type1Outline(raw.commands, raw.width_x, raw.width_y)

        standard = base_encoding("StandardEncoding")
        base_name = standard.get(raw.seac.base_code)
        accent_name = standard.get(raw.seac.accent_code)
        if not base_name or not accent_name:
            raise Type1Error(
                "Type1 seac references an undefined Adobe StandardEncoding code"
            )

        base_raw = self._raw(base_name, exact=True)
        accent_raw = self._raw(accent_name, exact=True)
        base = self._ordinary(base_raw)
        accent = self._ordinary(accent_raw)

        accent_dx = raw.left_bearing_x + raw.seac.accent_dx - raw.seac.accent_sidebearing
        accent_dy = raw.seac.accent_dy
        commands = base.commands + _translate(accent.commands, accent_dx, accent_dy)
        width_x = raw.width_x if raw.width_x is not None else base.width_x
        width_y = raw.width_y if raw.width_x is not None else base.width_y
        return Type1Outline(commands, width_x, width_y)
