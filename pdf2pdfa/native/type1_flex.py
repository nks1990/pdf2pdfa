"""Owned standard Type 1 OtherSubrs/Flex semantics.

This layer extends the strict Type1+seac interpreter with the standard
OtherSubrs needed by ordinary legacy Type 1 fonts. It is deliberately not a
PostScript VM: only Flex OtherSubrs 1/2/0, hint-replacement OtherSubr 3, and
counter-control hint OtherSubrs 12/13 are recognized. Proprietary/Multiple
Master OtherSubrs remain fail closed.

Flex follows the Type 1 protocol used by mature interpreters: OtherSubr 1 starts
Flex, seven OtherSubr-2 samples are collected after CharString move vectors,
sample 0 only moves the current point, samples 1..6 form two cubic Bezier
segments, and OtherSubr 0 returns final x/y through two following ``pop``
operators before ``setcurrentpoint``.
"""

from __future__ import annotations

from .type1 import Type1Command, Type1Error, UnsupportedType1Error, _exact_int
from .type1_seac import SeacType1Font, _RawOutline, _SeacInterpreter


class _FlexSeacInterpreter(_SeacInterpreter):
    def __init__(self, subrs) -> None:
        super().__init__(subrs)
        self._flex_active = False
        self._flex_points: list[tuple[float, float]] = []
        self._flex_saved_have_subpath = False
        self._flex_saved_start = (0.0, 0.0)
        self._flex_command_floor = 0
        self._pending_known_pops = 0

    def _callothersubr(self) -> None:
        if len(self.stack) < 2:
            raise Type1Error("Type1 callothersubr requires argCount and subrNo")
        subr_no = _exact_int(self.stack.pop(), "Type1 OtherSubr number")
        arg_count = _exact_int(self.stack.pop(), "Type1 OtherSubr argument count")
        if arg_count < 0 or arg_count > len(self.stack):
            raise Type1Error("Type1 callothersubr argument count exceeds operand stack")
        args = self.stack[-arg_count:] if arg_count else []
        if arg_count:
            del self.stack[-arg_count:]

        if subr_no == 1:  # start flex
            if arg_count != 0:
                raise Type1Error("Type1 Flex start OtherSubr 1 expects zero arguments")
            if self._flex_active:
                raise Type1Error("nested Type1 Flex is not permitted")
            self._flex_active = True
            self._flex_points = []
            if self.state.have_subpath:
                self._flex_saved_have_subpath = True
                self._flex_saved_start = (self.state.start_x, self.state.start_y)
            else:
                # FreeType's builder_start_point establishes the current point
                # before the seven relative flex vectors. Represent that point
                # explicitly so the first cubic has a valid contour origin.
                self.commands.append(Type1Command("M", (self.state.x, self.state.y)))
                self.state.have_subpath = True
                self.state.start_x, self.state.start_y = self.state.x, self.state.y
                self._flex_saved_have_subpath = True
                self._flex_saved_start = (self.state.x, self.state.y)
            self._flex_command_floor = len(self.commands)
            return

        if subr_no == 2:  # record flex vector destination
            if arg_count != 0:
                raise Type1Error("Type1 Flex vector OtherSubr 2 expects zero arguments")
            if not self._flex_active:
                raise Type1Error("Type1 Flex vector encountered before Flex start")
            if len(self._flex_points) >= 7:
                raise Type1Error("Type1 Flex contains more than seven vector samples")
            # Standard Flex requires a preceding r/h/vmoveto vector. The base
            # interpreter emitted that move as M; remove only a command created
            # after Flex start, never the synthetic/original contour origin.
            if len(self.commands) <= self._flex_command_floor:
                raise Type1Error("Type1 Flex vector OtherSubr 2 requires a preceding move")
            last = self.commands[-1]
            if last.operator != "M" or last.values != (self.state.x, self.state.y):
                raise Type1Error("Type1 Flex vector OtherSubr 2 shall follow a move operator")
            self.commands.pop()
            if len(self.commands) != self._flex_command_floor:
                raise Type1Error("Type1 Flex vector move emitted unexpected outline commands")
            self.state.have_subpath = self._flex_saved_have_subpath
            self.state.start_x, self.state.start_y = self._flex_saved_start
            self._flex_points.append((self.state.x, self.state.y))
            return

        if subr_no == 0:  # end flex
            if arg_count != 3:
                raise Type1Error("Type1 Flex end OtherSubr 0 expects three arguments")
            if not self._flex_active or len(self._flex_points) != 7:
                raise Type1Error("Type1 Flex end requires exactly seven vector samples")
            points = self._flex_points
            self.commands.append(
                Type1Command("C", (*points[1], *points[2], *points[3]))
            )
            self.commands.append(
                Type1Command("C", (*points[4], *points[5], *points[6]))
            )
            self.state.x, self.state.y = points[6]
            self.state.have_subpath = self._flex_saved_have_subpath
            self.state.start_x, self.state.start_y = self._flex_saved_start
            self._flex_active = False
            self._flex_points = []
            self.stack.extend([self.state.x, self.state.y])
            self._pending_known_pops += 2
            return

        if subr_no == 3:  # hint replacement
            if arg_count != 1:
                raise Type1Error("Type1 hint-replacement OtherSubr 3 expects one argument")
            self.stack.append(args[0])
            self._pending_known_pops += 1
            return

        if subr_no in (12, 13):  # counter-control hints
            self.stack.clear()
            return

        raise UnsupportedType1Error(
            f"Type1 OtherSubr {subr_no} requires PostScript/MM semantics not owned by this renderer"
        )

    def _escape(self, op: int) -> None:
        if op == 16:
            self._callothersubr()
            return
        if op == 17:
            if self._pending_known_pops <= 0:
                raise Type1Error("Type1 pop has no pending OtherSubr result")
            self._pending_known_pops -= 1
            return
        if op == 33:
            if self._pending_known_pops:
                raise Type1Error("Type1 setcurrentpoint requires all OtherSubr results to be popped")
            if len(self.stack) != 2:
                raise Type1Error("Type1 setcurrentpoint expects two operands")
            y = self.stack.pop()
            x = self.stack.pop()
            self.state.x, self.state.y = x, y
            return
        return super()._escape(op)

    def run_raw(self, data: bytes) -> _RawOutline:
        raw = super().run_raw(data)
        if self._flex_active:
            raise Type1Error("Type1 CharString ended with an open Flex sequence")
        if self._pending_known_pops:
            raise Type1Error("Type1 CharString ended before popping OtherSubr results")
        return raw


class FlexSeacType1Font(SeacType1Font):
    """Type1 font with owned standard Flex/OtherSubrs and seac composition."""

    def _raw(self, name: str, *, exact: bool) -> _RawOutline:
        data = self.charstrings.get(name)
        if data is None and not exact:
            data = self.charstrings.get(".notdef")
        if data is None:
            raise Type1Error(f"Type1 composite references missing glyph /{name}")
        return _FlexSeacInterpreter(self.subrs).run_raw(data)
