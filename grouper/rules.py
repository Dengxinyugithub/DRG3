# -*- coding: utf-8 -*-
"""CHS-DRG 3.0 分组规则表达式的词法/语法/求值实现。

规则语言（来自国家医保局《按病组（DRG）付费3.0版分组方案配置信息》）示例：

    ZYSS in OP_AA1
    (ZYZD in DI_BB2) and (ZYSS in OP_BB2)
    (ZYSS in OP1_AC1 and {ZYSS, QTSS} in OP2_AC1) or (ZYSS in OP2_AC1 and {ZYSS, QTSS} in OP1_AC1)
    length(OP_IC2 ∩ {ZYSS, QTSS})>=2 and ZYSS in OP_IC2
    ((NL=0) and (XSRTL>=29) and (XSRTL<366) and (ZYZD in DI_PV1) and (ZYSS not in OP_ALL)) or ...

支持的语法元素：and / or / in / not in / = / > / < / >= / <= / {A, B} 集合字面量 /
∩ 交集 / length(x) / 数字 / 标识符 / 圆括号。

求值口径（与方案文本一致）：
* 字段值为空（未提供主要诊断、没有主要手术等）时：
    - ``X in S``      判为 False（空集合不可能命中）
    - ``X not in S``  判为 True （"没有主要手术"确实满足"主要手术不在内涵列表"）
  这一点是原《DRG分组查询_单机版.html》的缺陷所在：它把空的 ``not in`` 也判成 False，
  导致 29 天≤年龄＜1 周岁的婴儿病例（PV1 组）永远无法入组。本实现按方案语义修正。
* 数值比较时任一操作数缺失（None 或空串）判为 False。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "RuleSyntaxError",
    "parse_rule",
    "evaluate",
    "codes_of",
    "normalize_code",
]

# --------------------------------------------------------------------------- #
# 代码规范化
# --------------------------------------------------------------------------- #

_X_RE = re.compile(r"X")


def normalize_code(value: Any) -> str:
    """统一编码写法，使调用方可以忽略大小写差异。

    * 去首尾空白、全角空格、内部空格；
    * 字母统一大写，但 ``x``/``X`` 统一为小写 ``x``（医保版 ICD 的占位符在配置表里是小写）；
    * 其余字符（数字、``.``、``+``、``*``）原样保留。

    配置表中的编码本来就符合这一规则，所以对配置数据是无损的；
    调用方传 ``p07.200``、``P07.200x001``、``K35.800X001`` 都能正确命中。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.replace("\u3000", "").replace(" ", "")
    out = []
    for ch in text:
        if ch in ("x", "X"):
            out.append("x")
        elif ch.isalpha():
            out.append(ch.upper())
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- #
# 词法分析
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<cmp>>=|<=|>|<|=)
    | (?P<lp>\()
    | (?P<rp>\))
    | (?P<lb>\{)
    | (?P<rb>\})
    | (?P<comma>,)
    | (?P<inter>∩)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "in", "not", "length"}


class RuleSyntaxError(ValueError):
    """规则表达式语法错误。"""


def tokenize(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise RuleSyntaxError(f"无法识别的字符 {text[pos]!r}（位置 {pos}）：{text[:80]}")
        pos = match.end()
        kind = match.lastgroup
        value = match.group()
        if kind == "ws":
            continue
        if kind == "ident" and value in _KEYWORDS:
            tokens.append((value.upper(), value))
        else:
            tokens.append((kind.upper(), value))
    tokens.append(("EOF", ""))
    return tokens


# --------------------------------------------------------------------------- #
# 语法分析 -> AST
# --------------------------------------------------------------------------- #
# AST 节点（元组，便于缓存与调试）：
#   ("num", 0)            ("ident", "NL")
#   ("set", [node, ...])  ("inter", l, r)        ("len", node)
#   ("cmp", op, l, r)     ("in", op, l, r)       ("and", l, r)   ("or", l, r)


class _Parser:
    def __init__(self, tokens: Sequence[Tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Tuple[str, str]:
        return self.tokens[self.pos]

    def next(self) -> Tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> Tuple[str, str]:
        tok = self.peek()
        if tok[0] != kind:
            raise RuleSyntaxError(f"期望 {kind}，实际 {tok[0]}（{tok[1]!r}）")
        return self.next()

    # expr := and_expr (or and_expr)*
    def parse_expr(self) -> Any:
        node = self.parse_and()
        while self.peek()[0] == "OR":
            self.next()
            node = ("or", node, self.parse_and())
        return node

    # and_expr := cmp_expr (and cmp_expr)*
    def parse_and(self) -> Any:
        node = self.parse_cmp()
        while self.peek()[0] == "AND":
            self.next()
            node = ("and", node, self.parse_cmp())
        return node

    # cmp_expr := set_expr [ (in|not in|比较符) set_expr ]
    def parse_cmp(self) -> Any:
        left = self.parse_set_expr()
        kind, value = self.peek()
        if kind == "IN":
            self.next()
            return ("in", "in", left, self.parse_set_expr())
        if kind == "NOT":
            self.next()
            self.expect("IN")
            return ("in", "not in", left, self.parse_set_expr())
        if kind == "CMP":
            self.next()
            return ("cmp", value, left, self.parse_set_expr())
        return left

    # set_expr := primary (∩ primary)*
    def parse_set_expr(self) -> Any:
        node = self.parse_primary()
        while self.peek()[0] == "INTER":
            self.next()
            node = ("inter", node, self.parse_primary())
        return node

    # primary := '(' expr ')' | '{' set_expr (',' set_expr)* '}' | length '(' set_expr ')' | num | ident
    def parse_primary(self) -> Any:
        kind, value = self.peek()
        if kind == "LP":
            self.next()
            node = self.parse_expr()
            self.expect("RP")
            return node
        if kind == "LB":
            self.next()
            items = [self.parse_set_expr()]
            while self.peek()[0] == "COMMA":
                self.next()
                items.append(self.parse_set_expr())
            self.expect("RB")
            return ("set", items)
        if kind == "LENGTH":
            self.next()
            self.expect("LP")
            node = self.parse_set_expr()
            self.expect("RP")
            return ("len", node)
        if kind == "NUM":
            self.next()
            return ("num", int(value) if value.isdigit() else float(value))
        if kind == "IDENT":
            self.next()
            return ("ident", value)
        raise RuleSyntaxError(f"期望表达式，实际 {kind}（{value!r}）")


def parse_rule(text: str) -> Optional[Any]:
    """把规则字符串解析成 AST；空规则返回 None（表示"默认匹配"）。"""
    if text is None:
        return None
    source = str(text).strip()
    if not source:
        return None
    return _Parser(tokenize(source)).parse_expr()


# --------------------------------------------------------------------------- #
# 求值
# --------------------------------------------------------------------------- #

CodeSet = Set[str]


def codes_of(value: Any) -> CodeSet:
    """把字段值/集合值统一成编码集合。

    已经是 set/frozenset 的直接返回（配置里的集合是 frozenset，复制上万条编码
    会让单次分组慢一个数量级）。
    """
    if value is None:
        return frozenset()
    if isinstance(value, (set, frozenset)):
        return value
    if isinstance(value, (list, tuple)):
        return {normalize_code(item) for item in value if normalize_code(item)}
    code = normalize_code(value)
    return {code} if code else frozenset()


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _value(node: Any, context: Dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "ident":
        name = node[1]
        if name in context["fields"]:
            return context["fields"][name]
        if name == "MCC":
            return context["mcc"]
        if name == "CC":
            return context["cc"]
        try:
            return context["sets"][name]
        except KeyError as exc:  # pragma: no cover - 配置校验会提前拦截
            raise RuleSyntaxError(f"规则引用了不存在的集合：{name}") from exc
    if kind == "set":
        result: CodeSet = set()
        for item in node[1]:
            result |= codes_of(_value(item, context))
        return result
    if kind == "inter":
        return codes_of(_value(node[1], context)) & codes_of(_value(node[2], context))
    if kind == "len":
        return len(codes_of(_value(node[1], context)))
    raise RuleSyntaxError(f"不支持的节点类型：{kind}")


def evaluate(node: Any, context: Dict[str, Any]) -> bool:
    """对 AST 求值，返回布尔结果。context 需含 fields / sets / mcc / cc。"""
    if node is None:
        return True
    kind = node[0]
    if kind == "and":
        return evaluate(node[1], context) and evaluate(node[2], context)
    if kind == "or":
        return evaluate(node[1], context) or evaluate(node[2], context)
    if kind == "in":
        op, left_node, right_node = node[1], node[2], node[3]
        left = codes_of(_value(left_node, context))
        if not left:
            # 关键语义：没有该字段 -> "不在集合中"成立；"在集合中"不成立。
            return op == "not in"
        right = codes_of(_value(right_node, context))
        # 用较小的集合逐条探测，避免对上万条的集合做交集复制
        if isinstance(right, (set, frozenset)):
            if len(left) <= 64:
                hit = any(code in right for code in left)
            else:
                hit = not right.isdisjoint(left)
        else:
            hit = bool(set(left) & set(right))
        return hit if op == "in" else not hit
    if kind == "cmp":
        op, left_node, right_node = node[1], node[2], node[3]
        left = _value(left_node, context)
        right = _value(right_node, context)
        # 集合之间用 = 比较（配置里未出现，稳妥起见支持）
        if isinstance(left, (set, frozenset)) or isinstance(right, (set, frozenset)):
            left_set, right_set = codes_of(left), codes_of(right)
            if op == "=":
                return left_set == right_set
            raise RuleSyntaxError(f"集合不支持比较符 {op}")
        lhs, rhs = _as_number(left), _as_number(right)
        if lhs is None or rhs is None:
            return False
        if op == "=":
            return lhs == rhs
        if op == ">":
            return lhs > rhs
        if op == "<":
            return lhs < rhs
        if op == ">=":
            return lhs >= rhs
        if op == "<=":
            return lhs <= rhs
        raise RuleSyntaxError(f"不支持的比较符 {op}")
    # 纯集合/数值表达式作为条件：非空即为真
    value = _value(node, context)
    if isinstance(value, (set, frozenset)):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def referenced_identifiers(node: Any) -> Set[str]:
    """收集 AST 引用到的标识符，用于配置校验。"""
    found: Set[str] = set()

    def walk(item: Any) -> None:
        if not isinstance(item, tuple):
            if isinstance(item, list):
                for sub in item:
                    walk(sub)
            return
        if item and item[0] == "ident":
            found.add(item[1])
            return
        for sub in item[1:]:
            walk(sub)

    walk(node)
    return found


def iter_codes(values: Iterable[Any]) -> CodeSet:
    """把多个字段值合并成编码集合。"""
    out: CodeSet = set()
    for value in values:
        out |= codes_of(value)
    return out
