"""Formulas for calculated values (spec section 2), e.g.

    profit_after_tax_etb / avg(total_equity_etb, total_equity_etb_prev)

A small recursive-descent parser over + - * / ( ) numbers, names and the functions avg, sum,
abs, min and max. Nothing is ever passed to eval, so a typed formula can only compute.
Arithmetic is exact (Decimal). A formula can be written back in symbols, in words (names
replaced by labels) or as arithmetic (names replaced by the values used).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

FUNCTIONS = {"avg": "average", "sum": "sum", "abs": "absolute value", "min": "minimum", "max": "maximum"}
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(.))")
_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}
_WORD_OPS = {"+": "+", "-": "−", "*": "×", "/": "÷"}


class FormulaError(ValueError):
    """The formula cannot be read or worked out; the message says where and why."""


@dataclass(frozen=True)
class Num:
    value: Decimal


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Neg:
    operand: object


@dataclass(frozen=True)
class Bin:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Call:
    function: str
    args: tuple


@dataclass(frozen=True)
class Formula:
    text: str  # as typed, without any "target =" prefix
    tree: object

    @property
    def names(self) -> list[str]:
        """Names used, in order of first appearance."""
        found: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, Var) and node.name not in found:
                found.append(node.name)
            elif isinstance(node, Neg):
                walk(node.operand)
            elif isinstance(node, Bin):
                walk(node.left)
                walk(node.right)
            elif isinstance(node, Call):
                for a in node.args:
                    walk(a)

        walk(self.tree)
        return found

    def evaluate(self, values: dict[str, Decimal]) -> Decimal:
        """The result; FormulaError for a missing value or a division by zero."""
        with localcontext() as ctx:
            ctx.prec = 34
            ctx.traps[DivisionByZero] = True
            ctx.traps[InvalidOperation] = True
            try:
                return _evaluate(self.tree, values)
            except (DivisionByZero, InvalidOperation):
                raise FormulaError("The formula divides by zero with these values.") from None

    def render(self, name_text: Callable[[str], str], words: bool = False) -> str:
        """The formula with each name written by name_text: symbols, labels (words=True) or values."""
        return _render(self.tree, name_text, words)


def parse(text: str) -> tuple[str | None, Formula]:
    """'roe = a / b' -> ('roe', formula a / b); 'a / b' -> (None, formula)."""
    target = None
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)(.*)$", text, re.DOTALL)
    if match:
        target, text = match.group(1), match.group(2)
    tokens = _tokenize(text)
    if not tokens:
        raise FormulaError("The formula is empty.")
    parser = _Parser(tokens)
    tree = parser.expression()
    if parser.position < len(tokens):
        raise FormulaError(f"Unexpected '{tokens[parser.position][1]}' after a complete formula.")
    return target, Formula(text.strip(), tree)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    for number, name, other in _TOKEN.findall(text):
        if number:
            tokens.append(("num", number))
        elif name:
            tokens.append(("name", name))
        elif other.strip():
            if other not in "+-*/(),":
                raise FormulaError(f"'{other}' cannot be used in a formula. Use + - * / ( ) and names.")
            tokens.append(("op", other))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens, self.position = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise FormulaError("The formula ends too early: something is missing at the end.")
        if value is not None and token[1] != value:
            raise FormulaError(f"Expected '{value}' but found '{token[1]}'.")
        self.position += 1
        return token

    def expression(self) -> object:
        node = self.term()
        while (t := self.peek()) and t[1] in ("+", "-"):
            node = Bin(self.take()[1], node, self.term())
        return node

    def term(self) -> object:
        node = self.factor()
        while (t := self.peek()) and t[1] in ("*", "/"):
            node = Bin(self.take()[1], node, self.factor())
        return node

    def factor(self) -> object:
        if (t := self.peek()) and t[1] == "-":
            self.take()
            return Neg(self.factor())
        return self.atom()

    def atom(self) -> object:
        kind, value = self.take()
        if kind == "num":
            return Num(Decimal(value))
        if kind == "name":
            if (t := self.peek()) and t[1] == "(":
                if value not in FUNCTIONS:
                    raise FormulaError(f"Unknown function '{value}'. Use one of: {', '.join(FUNCTIONS)}.")
                self.take("(")
                args = [self.expression()]
                while (t := self.peek()) and t[1] == ",":
                    self.take()
                    args.append(self.expression())
                self.take(")")
                return Call(value, tuple(args))
            return Var(value)
        if value == "(":
            node = self.expression()
            self.take(")")
            return node
        raise FormulaError(f"Unexpected '{value}'.")


def _evaluate(node: object, values: dict[str, Decimal]) -> Decimal:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Var):
        if node.name not in values:
            raise FormulaError(f"No value for '{node.name}'.")
        return values[node.name]
    if isinstance(node, Neg):
        return -_evaluate(node.operand, values)
    if isinstance(node, Bin):
        a, b = _evaluate(node.left, values), _evaluate(node.right, values)
        return {"+": a + b, "-": a - b, "*": a * b}.get(node.op) if node.op != "/" else a / b
    args = [_evaluate(a, values) for a in node.args]
    if node.function == "avg":
        return sum(args, Decimal(0)) / len(args)
    if node.function == "sum":
        return sum(args, Decimal(0))
    if node.function == "abs":
        if len(args) != 1:
            raise FormulaError("abs takes one value.")
        return abs(args[0])
    return min(args) if node.function == "min" else max(args)


def _render(node: object, name_text: Callable[[str], str], words: bool, parent: int = 0, right: bool = False) -> str:
    if isinstance(node, Num):
        return f"{node.value:f}"
    if isinstance(node, Var):
        return name_text(node.name)
    if isinstance(node, Neg):
        return "−" + _render(node.operand, name_text, words, 3) if words else "-" + _render(node.operand, name_text, words, 3)
    if isinstance(node, Call):
        name = FUNCTIONS[node.function] if words else node.function
        return f"{name}({', '.join(_render(a, name_text, words) for a in node.args)})"
    precedence = _PRECEDENCE[node.op]
    op = _WORD_OPS[node.op] if words else node.op
    text = (f"{_render(node.left, name_text, words, precedence)} {op} "
            f"{_render(node.right, name_text, words, precedence, right=True)}")
    # Brackets only where needed: a lower-precedence child, or the right side of - and /.
    needs = precedence < parent or (right and precedence == parent and parent == 1) or (right and parent == 2 and precedence == 2)
    return f"({text})" if needs else text
