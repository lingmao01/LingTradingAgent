"""Per-role validation: what each declared figure has to survive.

The model declares what each measurement-shaped number IS (spec §2); this
module checks that declaration against ledger evidence (spec §4). It reads no
prose word: roles come from the model, shape from :mod:`figures`.
"""

from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.agent.grounding.identity import (
    _CANONICAL_SYMBOL_RE,
    _normalize_symbol,
    _scan_symbols,
    _symbol_identity_in,
)
from src.agent.grounding.evidence import (
    EvidenceRecord,
    _column_field_for_path,
    _is_metadata_count_leaf,
    _is_number,
    _is_price_kind,
    _is_registered_metric_indicator,
    _latest_as_of,
    _metric_kind_for_path,
    _price_field_for_path,
    _timestamp_matches_claim_date,
    tail_risk_identity,
)
from src.agent.grounding.figures import (
    ROUNDED_BAND,
    Declaration,
    Figure,
    FiguresBlock,
    _SEGMENT_BREAKS,
    _lines_with_offsets,
    _normalize,
    _numbers,
    _percent_mark,
    _reading,
    magnitude_suffix,
    segment_bounds,
    strip_figures_block,
)

import re

#: An answer that relabels a locked listed identity as private contradicts the
#: resolver, which is an identity finding rather than a figure finding.
_PRIVATE_ASSERTION_RE = re.compile(
    r"(?:\b(?:is|remains|still)\s+(?:an?\s+)?(?:private company|privately held)\b|"
    r"\bnot publicly traded\b|\bunlisted company\b|"
    r"(?:是|仍是|属于)(?:一家)?(?:私人|私营|非上市)公司|未上市|没有上市)",
    re.IGNORECASE,
)

# Loader ids are ASCII but the answer follows the user's language, so a source
# is surfaced by any alias ("数据来源：腾讯财经" for ``tencent``).
_SOURCE_ALIASES = {
    "akshare": ("akshare", "ak share"),
    "baostock": ("baostock",),
    "binance": ("binance", "币安"),
    "ccxt": ("ccxt",),
    "eastmoney": ("eastmoney", "东方财富", "东财"),
    "futu": ("futu", "富途"),
    "mootdx": ("mootdx", "通达信"),
    "okx": ("okx", "欧易"),
    "pykrx": ("pykrx", "krx"),
    "sina": ("sina", "新浪"),
    "stooq": ("stooq",),
    "tencent": ("tencent", "腾讯"),
    "tushare": ("tushare",),
    "yahoo": ("yahoo", "雅虎"),
    "yfinance": ("yfinance", "yahoo", "雅虎"),
}

_CURRENCY_ALIASES = {
    "USD": ("usd", "us$", "美元", "美金"),
    # ¥ is also the yen sign, but ``_infer_currency`` maps no venue to JPY;
    # adding a JPY venue means revisiting this entry.
    "CNY": ("cny", "cnh", "rmb", "人民币", "¥", "￥"),
    "HKD": ("hkd", "hk$", "港元", "港币"),
    "KRW": ("krw", "韩元", "韩圜"),
    "INR": ("inr", "印度卢比", "卢比"),
    "CAD": ("cad", "c$", "加元", "加拿大元"),
    "GBP": ("gbp", "£", "英镑"),
    "VND": ("vnd", "₫", "越南盾"),
    "ARS": ("ars", "ar$", "阿根廷比索"),
}

# "元" counts as CNY only when no other currency's character precedes it
# (港元/美元/日元), or a Hong Kong listing would satisfy a CNY requirement.
_OTHER_CURRENCY_PREFIXES = "港美日欧韩台新加澳"

#: Relative band a value must fall in to count as matching evidence.
_TOLERANCE = 0.005

#: A plain integer is read as a price only for an instrument quoted in the
#: thousands (600519.SH, an index, BTC). Below that, a prose integer is a window,
#: a horizon or a count ("20 日均线", "200-day") and stays unchecked.
_INTEGER_PRICE_FLOOR = 1000.0

#: Index display names a sentence may write right beside a number where the
#: table row's symbol cell resolves to a wrapper fund instead ("005658.OF
#: 沪深300 … 沪深300 收盘 ≥ 4466.94"). An alias is honored only while the run
#: holds evidence for the index it names, and it is only ever used to CONFIRM a
#: figure's declared instrument — never to reassign one.
_INDEX_LABELS = {
    "上证指数": "000001.SH",
    "上证综指": "000001.SH",
    "上证": "000001.SH",
    "沪深300": "000300.SH",
    "科创50": "000688.SH",
    "创业板指": "399006.SZ",
    "创业板": "399006.SZ",
    # A letter-based Hong Kong code has no numeric stem to shorten, so its own
    # name is the label the prose uses ("HSTECH 收盘 4169.57"). The Chinese
    # spellings are the same index; a fund carrying the name in front
    # ("恒生科技联接C") is caught by the longer-name guard below.
    "恒生科技指数": "HSTECH.HK",
    "恒生科技": "HSTECH.HK",
    "HSTECH": "HSTECH.HK",
}

#: Punctuation a label and its number may be separated by ("上证 3890.51",
#: "沪深300 收盘 ≥ 4466.94"). Anything else in the gap disqualifies the
#: reading, so a fund whose display name carries the index's name
#: ("华夏沪深300ETF联接C") is never a label for the number after it.
_INLINE_GAP_CHARS = frozenset(
    " \t\u3000（）()〔〕【】[]《》〈〉：:，,、；;/／|｜·•〜～"
    "—–−-+±<>≥≤≈~＝≒=→"
)

#: The thin quote and trigger words the gap may hold beside punctuation. The
#: trigger words pair with an indicator mention ("000300.SH 收盘站上
#: SMA20(4466.94)"); anything else substantial between a label and its number
#: is prose, not a label.
_INLINE_GAP_WORDS = frozenset(
    {"收盘", "收盘价", "报收", "收报", "现报", "报价", "现价", "最新价", "最新", "指数", "收", "报", "为", "是", "在", "至", "见", "约", "点"}
    | {"站上", "站回", "跌破", "失守", "上穿", "下穿", "上破", "下破", "突破", "高于", "低于", "回到", "回升", "回落", "逼近", "接近", "触及", "位于", "上方", "下方", "均线", "年线", "季线", "月线", "日线", "线", "其"}
)

#: Moving-average and oscillator mentions the gap may carry beside those words
#: ("SMA20", "MA20", "5日均线"); they are indicator talk, never a fund name.
_INLINE_GAP_INDICATOR_RE = re.compile(r"(?:sm[ae]|e?ma|rsi|macd|boll|kdj)\d*|\d+日", re.IGNORECASE)

#: The gap words longest-first, so "收盘价" is cut before "收盘".
_INLINE_GAP_WORDS_ORDERED = tuple(sorted(_INLINE_GAP_WORDS, key=len, reverse=True))

#: How far left of a figure its inline label may sit. A canonical code is nine
#: characters wide and a trigger phrase another dozen ("000300.SH 收盘站上
#: SMA20("), so the window reaches past the code itself.
_INLINE_LABEL_WINDOW = 24


def _thin_gap(gap: str) -> bool:
    """Whether the text between a label and its number is label-side talk only.

    Punctuation and space fall away, indicator mentions are cut, and the rest
    must read as known quote/trigger words ("收盘 ≥", "收盘站上 SMA20"). Any
    other word ("ETF联接C") means the text before the number names something
    else — a fund whose display name contains the index, not a label for the
    number after it.

    Args:
        gap: The text between a label's end and the figure's start.

    Returns:
        True when the gap is thin enough to read the label as inline.
    """
    residue = "".join(
        char for char in gap if not char.isspace() and char not in _INLINE_GAP_CHARS
    )
    residue = _INLINE_GAP_INDICATOR_RE.sub("", residue)
    while residue:
        for word in _INLINE_GAP_WORDS_ORDERED:
            if residue.startswith(word):
                residue = residue[len(word) :]
                break
        else:
            return False
    return True


#: Fund-name material that extends a label into a longer display name: a note
#: opening with "沪深300ETF联接C" names one fund, not the index whose name sits
#: inside it ("华夏沪深300ETF联接C" is caught by the position rule instead).
_NAME_EXTENSION_PREFIXES = ("etf", "联接", "指数", "基金", "lof")


def _label_begins_a_longer_name(tail: str) -> bool:
    """Whether the text right after a label continues a fund's display name.

    A label names a figure only as a complete token: "科创50 触发参考位" labels
    the number that follows, while "沪深300ETF联接C" is one instrument's name
    carrying the index's name inside it.

    Args:
        tail: The text after the label's own characters.

    Returns:
        True when the tail extends the label into a longer name.
    """
    rest = tail.lstrip()
    if not rest:
        return False
    if rest[0].isascii() and rest[0].isalnum():
        return True
    return rest.casefold().startswith(_NAME_EXTENSION_PREFIXES)


#: Numbers a note may write beside its label ("1580.80 上方（科创50）"); the
#: matched text is compared with the declared value before the label counts.
_NOTE_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


#: A bare listing code a declaration's note may write instead of the canonical
#: spelling ("你 09-29 台账：019667 收益"). Five digits or more, so a window
#: ("20/50/200"), a count or a price is never read as an instrument.
_BARE_CODE_RE = re.compile(r"(?<![\d.])\d{5,6}(?![\d.])")


#: The letter base of a letter-based Hong Kong code, written on its own
#: ("HSTECH" for ``HSTECH.HK``). Case-SENSITIVE like the canonical branch, so
#: an ordinary lowercase word is never read as a listing.
_BARE_BASE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{2,14}(?![A-Za-z0-9])")


#: Price fields a rejected prose figure is pointed at, in order (#1433).
_CITABLE_FIELDS = ("close", "price", "adj_close")

#: Retrieval-denial phrases an answer can wear while the ledger holds the data.
#: Narrow on purpose: "未计算/未核验" are not retrieval claims and stay free,
#: and a phrase only counts when an observed instrument sits on its own line.
_AVAILABILITY_CLAIM_RE = re.compile(
    r"未取到|未能取到|未获取|无法获取|未取回|取不到|没有取到|没取到|未保留|未留存"
    r"|无可用(?:报价|数据|行情)|无可引用"
    r"|not retrieved|not available|unavailable"
    r"|could not (?:retrieve|fetch|obtain)|not (?:preserved|retained)",
    re.IGNORECASE,
)

#: A denial belongs to the instrument it describes, not to every instrument
#: sharing its line. The 2026-10-12 run named four indices on one line while
#: denying only ``399006.SZ`` ("…四宽基…全部核实到位…（399006.SZ 创业板指本轮未取到）"),
#: and the line-scoped check flagged the three the ledger really held. Chinese
#: puts the subject before its predicate, so a denial is attributed to the
#: nearest PRECEDING code (falling back to the nearest following one only when
#: the line leads with the phrase), and only within this many characters — a
#: code far off in another clause is not the denial's subject.
_AVAILABILITY_PROXIMITY_WINDOW = 48

#: A substitution clause introduces a stand-in ("用 513010.SH 代替", "改用 X
#: 替代", "以 X 为准"). The stand-in sits on the denial's line but is NOT the
#: instrument being denied, so naming it there is no false retrieval claim.
_SUBSTITUTION_BEFORE_RE = re.compile(r"(?:用|改用|改用为|替换为|以)\s*$")
_SUBSTITUTION_AFTER_RE = re.compile(r"^\s*(?:代替|替代|为准|来替代|来代替)")
_SUBSTITUTION_CONTEXT = 8


def _attributed_denials(line: str) -> set[str]:
    """Return the symbols a line's retrieval-denial phrases actually deny.

    Each phrase binds to its nearest preceding code — Chinese puts the subject
    before its predicate — or to its nearest following code only when the line
    leads with the phrase, and only within
    :data:`_AVAILABILITY_PROXIMITY_WINDOW` characters. A code introduced by a
    substitution clause ("用 X 代替") is exempt: it stands in for the denied
    instrument rather than being denied itself.

    Args:
        line: One line of the draft.

    Returns:
        The canonical symbols the line denies.
    """
    claims = [match.span() for match in _AVAILABILITY_CLAIM_RE.finditer(line)]
    if not claims:
        return set()
    symbols = [
        (_normalize_symbol(match.group(0)), match.start(), match.end())
        for match in _CANONICAL_SYMBOL_RE.finditer(line)
    ]
    if not symbols:
        return set()
    exempt = {
        symbol
        for symbol, start, end in symbols
        if _SUBSTITUTION_BEFORE_RE.search(
            line[max(0, start - _SUBSTITUTION_CONTEXT) : start]
        )
        or _SUBSTITUTION_AFTER_RE.match(line[end : end + _SUBSTITUTION_CONTEXT])
    }
    denied: set[str] = set()
    for claim_start, claim_end in claims:
        preceding = [
            (claim_start - end, symbol)
            for symbol, start, end in symbols
            if end <= claim_start
        ]
        if preceding:
            gap, symbol = min(preceding, key=lambda pair: pair[0])
        else:
            following = [
                (start - claim_end, symbol)
                for symbol, start, end in symbols
                if start >= claim_end
            ]
            if not following:
                continue
            gap, symbol = min(following, key=lambda pair: pair[0])
        if gap <= _AVAILABILITY_PROXIMITY_WINDOW:
            denied.add(symbol)
    return denied - exempt


#: Retrieval claims one draft can be rejected for; bounded so the correction
#: message cannot turn into a data dump.
_MAX_AVAILABILITY_ISSUES = 8

#: Values one availability rejection quotes back; bounded like the issue count.
_MAX_AVAILABILITY_QUOTES = 6

#: Field names one rejection names for the scope it was declared from; bounded
#: so a long session's wide tool result cannot turn a correction into a dump.
_MAX_SCOPE_FIELDS = 12


@dataclass(frozen=True)
class ValidationResult:
    """Final-answer grounding decision.

    ``released_text`` is the draft without its declaration block, which is a
    contract with the gate and never reaches the user.
    """

    valid: bool
    issues: list[dict[str, Any]] = field(default_factory=list)
    released_text: str = ""


def _close(value: float, target: float) -> bool:
    """Whether two values agree inside the evidence tolerance."""
    return abs(value - target) <= max(abs(target) * _TOLERANCE, 1e-9)


def _close_any(value: float, targets: Iterable[float]) -> bool:
    """Whether ``value`` agrees with any of ``targets``."""
    return any(_close(value, target) for target in targets)


def _verbatim_any(value: float, targets: Iterable[float]) -> bool:
    """Whether ``value`` equals any of ``targets`` verbatim, with no rounding band."""
    return any(abs(value - target) <= max(abs(target) * 1e-9, 1e-9) for target in targets)


#: Full-width digits and comma, read as their ASCII forms: user-authored text
#: writes "１２６，０００" as readily as "126,000" (spec §2, ``held``).
_FULLWIDTH_NUMBER_TRANS = str.maketrans("０１２３４５６７８９，", "0123456789,")


def _user_provided_numbers(texts: Iterable[str]) -> list[tuple[float, bool]]:
    """Every value the user's own words state, with percent-ness.

    The corpus is read with the same number reader the draft goes through
    (thousands separators, decimal commas, look-alike characters), so a value
    the user wrote as "126,000" reconciles with a figure written "126000" and
    "＋4%" reconciles with "4%". A magnitude mark changes the stated value
    ("12.6万" is 126000), so it counts here too.

    Args:
        texts: The user's own messages and the memories recalled in their name.

    Returns:
        ``(value, percent)`` per number, in reading order.
    """
    values: list[tuple[float, bool]] = []
    for text in texts:
        normalized = _normalize((text or "").translate(_FULLWIDTH_NUMBER_TRANS)).text
        for token in _numbers(normalized):
            unit, _ = _percent_mark(normalized, token.end)
            reading = _reading(token.sign, token.digits, unit)
            if reading is None:
                continue
            magnitude = 1.0 if unit > 0 else magnitude_suffix(normalized, token.end)[0]
            values.append((reading[0] * magnitude, unit > 0))
    return values


def _nearest(value: float, targets: Iterable[float], limit: int = 3) -> list[float]:
    """The observed values closest to a rejected figure.

    Args:
        value: The rejected figure's value.
        targets: Every value the relevant evidence pool holds.
        limit: How many to name.

    Returns:
        Up to ``limit`` distinct observed values, closest first.
    """
    unique = sorted({float(target) for target in targets}, key=lambda item: (abs(item - value), item))
    return unique[:limit]


def _written_half_unit(text: str) -> float:
    """Half a unit of the last digit a figure was written with ("37%" -> 0.5)."""
    body = text.strip().rstrip("%％").strip()
    decimals = len(body.split(".", 1)[1]) if "." in body else 0
    return 0.5 * 10.0 ** (-decimals)


def _explicit_sign(text: str) -> int:
    """``1`` or ``-1`` when a figure was written with a sign ("+36.8%"), else ``0``."""
    head = text.strip()[:1]
    return 1 if head == "+" else -1 if head == "-" else 0


def _is_plain_count(figure: Figure) -> bool:
    """Whether a figure can be a count or a parameter the model chose.

    A weight, threshold, window, probability or multiplier is unchecked; a
    figure with a currency mark is a price or an amount and is checked as
    observed. A price column is refused before this is asked.
    """
    return not figure.currency


def _note_tokens(note: str) -> set[str]:
    """Citation fragments in a note: CJK character pairs and ASCII words of four letters or more.

    Pairs, because Chinese is not space-separated: "财报毛利率桥" is visible in
    "毛利率下降" the way "gross margin bridge" is visible in "gross margin".
    Four letters, because "the" or "and" would make any English line visible.

    Args:
        note: A declaration's free-text note.

    Returns:
        The casefolded fragments; digits, spaces and punctuation separate runs.
    """
    tokens: set[str] = set()
    run, kind = "", ""
    for char in note.casefold() + " ":
        if "㐀" <= char <= "鿿":
            current = "cjk"
        elif char.isascii() and char.isalpha():
            current = "ascii"
        else:
            current = ""
        if current != kind:
            if kind == "cjk" and len(run) >= 2:
                tokens.update(run[index : index + 2] for index in range(len(run) - 1))
            elif kind == "ascii" and len(run) >= 4:
                tokens.add(run)
            run, kind = "", current
        if current:
            run += char
    return tokens


def _strip_sign(node: ast.AST) -> ast.AST:
    """The operand under any unary ``+`` or ``-``."""
    while isinstance(node, ast.UnaryOp):
        node = node.operand
    return node


def _signed_literal(node: ast.AST) -> float | None:
    """A numeric literal including its unary signs, without losing provenance."""
    if isinstance(node, ast.Constant) and _is_number(node.value):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _signed_literal(node.operand)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
    return None


def _is_sum(node: ast.AST) -> bool:
    """Whether a node is a binary ``+`` or ``-``."""
    return isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub))


def _is_unit_factor(node: ast.AST) -> bool:
    """Whether a sum is ``1 ± c ± …`` over constants: a percentage change as a factor."""
    node = _strip_sign(node)
    if not _is_sum(node):
        return False
    while _is_sum(node):
        if not isinstance(_strip_sign(node.right), ast.Constant):
            return False
        node = _strip_sign(node.left)
    return isinstance(node, ast.Constant) and node.value == 1


def _unanchored_term(tree: ast.Expression, observed: Callable[[float], bool]) -> bool:
    """Whether a formula adds or subtracts a term holding no observed operand.

    Multiplicative constants are free, so two forms are not offsets: ``1 ± c``
    used as a factor ("0.666 × (1 − 0.03)") and the 1 beside a quotient
    ("0.666 / 1.053 − 1"). Neither reaches a value a free multiplier could not.
    Both are recognised by structure: near a price of 1 the constant 1 is itself
    within tolerance of an observation.

    Args:
        tree: The parsed formula.
        observed: Whether an operand is a value this session observed.

    Returns:
        True when some added or subtracted term is unanchored.
    """

    def anchored(node: ast.AST) -> bool:
        literal = _signed_literal(node)
        if literal is not None:
            return observed(literal)
        return any(anchored(child) for child in ast.iter_child_nodes(node))

    def visit(node: ast.AST, factor: bool) -> bool:
        node = _strip_sign(node)
        if not isinstance(node, ast.BinOp):
            return False
        if not _is_sum(node):
            return visit(node.left, True) or visit(node.right, True)
        if factor and _is_unit_factor(node):
            return False
        for side, other in ((node.left, node.right), (node.right, node.left)):
            unsigned_side, other = _strip_sign(side), _strip_sign(other)
            unit_beside_ratio = (
                isinstance(unsigned_side, ast.Constant)
                and unsigned_side.value == 1
                and isinstance(other, ast.BinOp)
                and isinstance(other.op, ast.Div)
            )
            if not _is_sum(unsigned_side) and not unit_beside_ratio and not anchored(side):
                return True
        return visit(node.left, False) or visit(node.right, False)

    return visit(tree.body, False)


def _evaluate_formula(expression: str) -> tuple[float, list[float], ast.Expression] | None:
    """Evaluate a numeric ``+ - * /`` expression without executing code.

    Args:
        expression: An arithmetic run, possibly using ``× ÷ −`` and commas.

    Returns:
        ``(result, operands, parsed tree)``, or None when the run is not a
        well-formed expression over at least two numeric operands.
    """
    normalized = (
        expression.replace("×", "*")
        .replace("✕", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("（", "(")
        .replace("）", ")")
        .replace(",", "")
        .replace("%", "")
        .strip()
    )
    if not normalized:
        return None
    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    inputs: list[float] = []

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        value = _signed_literal(node)
        if value is not None:
            inputs.append(value)
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        raise ValueError("unsupported formula")

    try:
        value = visit(tree)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if len(inputs) < 2 or not math.isfinite(value):
        return None
    return value, inputs, tree


# A parenthetical whose content holds a word — "(分子分母均为用户台账 held)",
# "(自 5 月高点回撤)" — is an explanation the note adds after its formula, unlike
# a grouping of numbers alone ("(a − b) / b"), which is part of the formula.
_EXPLANATORY_GROUP_RE = re.compile(r"[（(][^（()）]*[^\W\d_][^（()）]*[）)]")


def _without_explanations(expression: str) -> str:
    """The expression with its parenthesized explanations cut ("100/x（口径）").

    A group holding a word is prose, not arithmetic; a group of numbers alone
    is the formula's own grouping and stays. Groups are cut innermost-first so
    a nested explanation cannot hide an outer one.
    """
    text = expression
    while True:
        cut = _EXPLANATORY_GROUP_RE.sub(" ", text)
        if cut == text:
            return text
        text = cut


def _formula_in_note(note: str) -> tuple[float, list[float], ast.Expression] | None:
    """Find the derivation a note states.

    The whole note is tried first, then each segment between result separators
    ("0.666 × 0.97 = 0.646"); separators are punctuation, not vocabulary. A
    formula may also carry its explanation in parentheses right after it
    ("268996 / 389911（分子分母均为用户台账 held）"); such explained forms are
    tried last, so "(a − b) / b" still reads as the formula it is first.

    Args:
        note: The declaration's free-text note.

    Returns:
        ``(result, operands, parsed tree)`` for the first parseable segment, or None.
    """
    candidates = [note]
    parts = [note]
    for separator in ("≈", "≒", "＝", "=", "→", "->"):
        parts = [piece for part in parts for piece in part.split(separator)]
    # A formula followed by its explanation ("(a − b) / b，自高点回撤"). A bare
    # "," is not split: it groups thousands inside a formula.
    for separator in ("，", "；", "：", "; ", ", "):
        parts = [piece for part in parts for piece in part.split(separator)]
    candidates.extend(part for part in parts if part.strip())
    candidates.extend(
        explained
        for candidate in list(candidates)
        for explained in [_without_explanations(candidate)]
        if explained.strip()
    )
    for candidate in candidates:
        evaluated = _evaluate_formula(candidate)
        if evaluated is not None:
            return evaluated
    return None


def _states_as_of_date(content: str, as_of: str) -> bool:
    """Return whether the answer states the carried evidence's as-of date.

    Any rendering a market answer realistically uses counts: ``2026-09-29``,
    ``2026/09/29``, ``2026.09.29``, ``2026年9月29日`` (spaces optional), and
    the month-day shorthands (``09-29`` / ``9/29`` / ``9月29日``) — the
    carry-over is session-scoped, so the year is fixed by the conversation.

    Args:
        content: The candidate answer.
        as_of: The ISO date (``YYYY-MM-DD``) the carried evidence reaches.

    Returns:
        True when any accepted rendering appears in the answer.
    """
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", as_of)
    if match is None:
        return as_of in content
    year, month, day = match.groups()
    month_number, day_number = int(month), int(day)
    if re.search(
        rf"{year}\s*[年\-/.]\s*0?{month_number}\s*[月\-/.]\s*0?{day_number}\s*日?",
        content,
    ):
        return True
    return any(
        rendering in content
        for rendering in (
            f"{month}-{day}",
            f"{month}/{day}",
            f"{month}/{day_number}",
            f"{month_number}/{day_number}",
            f"{month}月{day}日",
            f"{month_number}月{day_number}日",
        )
    )


class _PolicyMixin:
    """Policy behaviour of :class:`GroundingLedger`."""

    def _validate_identity(self, content: str) -> list[dict[str, Any]]:
        """Validate aggregate state and listed/private contradictions."""
        issues: list[dict[str, Any]] = []
        status = self.identity_status
        # Only a run that named an instrument can get its identity wrong: the
        # trigger phrase matches the user message, so "什么是市盈率估值法？" would
        # otherwise fail every draft. ``ambiguous`` is absent on purpose: a
        # shortlist is an answer and consumers stay blocked in ``authorize_tool_call``.
        if (
            self._identity_required
            and self._identities
            and status in {"unresolved", "conflicting", "invalidated"}
        ):
            issues.append(
                {
                    "code": "identity_not_locked",
                    "status": status,
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "identity_not_locked",
                    "message": (
                        f"Instrument identity is {status}; a final market conclusion "
                        "requires locked identity."
                    ),
                }
            )
        listed = [
            record
            for record in self._identities.values()
            if record.status == "locked"
            and record.instrument_type in {"listed_security", "fund"}
        ]
        if listed and _PRIVATE_ASSERTION_RE.search(content):
            symbols = sorted(record.symbol for record in listed if record.symbol)
            issues.append(
                {
                    "code": "listed_identity_relabelled_private",
                    "symbols": symbols,
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "listed_relabelled_private",
                    "message": (
                        f"Locked listed identity {', '.join(symbols)} was relabelled as "
                        "private/unlisted without a conflicting resolver result."
                    ),
                }
            )
        return issues

    def _carried_as_of(self, records: Sequence[EvidenceRecord]) -> str | None:
        """Return the as-of date of a wholly carried price pool, or None.

        A run that fetched any of the pool itself is actively quoting live
        data, so no blanket recency note applies; only a pool entirely
        inherited from earlier runs needs its observation date stated.

        Args:
            records: The run's comparable price records.

        Returns:
            The latest carried observation date, or None when the pool is
            empty or holds even one of the run's own prints.
        """
        if not records or any(record.carried_from is None for record in records):
            return None
        return _latest_as_of(records)

    def _validate_figures(
        self,
        content: str,
        block: FiguresBlock,
        figures: Sequence[Figure],
    ) -> list[dict[str, Any]]:
        """Check every measurement-shaped number against its declared role.

        Args:
            content: The candidate answer.
            block: Its parsed declaration block.
            figures: Every number located in its prose.

        Returns:
            One issue per figure that its role does not survive, plus one per
            malformed declaration line and the provenance findings.
        """
        issues: list[dict[str, Any]] = [
            {
                "code": "figures_block_malformed",
                "line": line_no,
                "claim": raw,
                "value": None,
                "role": None,
                "span": None,
                "symbol": None,
                "reason": "unparseable_declaration",
                "message": (
                    f"figures block line {line_no} could not be read as "
                    "`value | role | note | ref`: " + raw
                ),
            }
            for line_no, raw in block.malformed
        ]
        records = self._comparable_price_records()
        # Symbol resolution is broader than price comparison. A session may hold
        # quotes for the primary listing and only non-price evidence for another
        # explicitly named instrument (for example issuer fundamentals). If claim
        # identity only sees price-comparable records, that second symbol becomes
        # invisible and its referenced evidence is later filtered under the
        # primary symbol. Keep numeric matching fail-closed, but let any observed
        # numeric evidence contribute its explicit symbol to claim resolution.
        symbol_records = [
            record
            for record in self._evidence
            if record.status == "observed"
            and record.value is not None
            and record.symbol
        ]
        # Preserve the existing price-derived document fallback. A report may
        # mention a secondary instrument only for fundamentals; that should make
        # explicit claims about the secondary resolvable without making otherwise
        # unattributed primary-listing figures ambiguous.
        document_symbol = self._symbol_for_claim(content, records)
        positions = _lines_with_offsets(content)
        line_symbols = [
            self._symbol_for_claim(line, symbol_records) for line, _ in positions
        ]
        declared_observed = {
            declaration.value
            for declaration in block.declarations
            if declaration.role == "observed"
        }
        # Multipliers a declared derivation uses: a count equal to one is that factor.
        derived_constants = [
            operand
            for declaration in block.declarations
            if declaration.role == "derived"
            for evaluated in [_formula_in_note(declaration.note)]
            if evaluated is not None
            for operand in evaluated[1]
        ]
        # The block's own chain from the user's numbers, computed once: a
        # derivation may rest on a total the block itself derived from amounts
        # the user stated.
        block_anchored = self._block_anchored_values(block)
        checked_price = False
        for figure in figures:
            if figure.shape not in ("measured", "bare"):
                continue
            declaration = block.match(figure.value, figure.percent, figure.digits)
            symbol = self._figure_symbol(
                content,
                figure,
                declaration,
                line_symbols,
                document_symbol,
                symbol_records,
            )
            if figure.shape == "bare" and not self._poses_as_price(
                figure, self._price_band(symbol, records)
            ):
                continue
            if declaration is not None:
                written = self._written_symbol(
                    content, figure, line_symbols, symbol_records
                )
                if written and symbol and written != symbol:
                    # A declaration names where a number came from; it cannot
                    # move a figure the sentence attaches to another instrument.
                    # Three honest readings survive that rule: the same value
                    # declared for a share sibling the row writes, a sentence
                    # naming the declared instrument right beside the figure
                    # ("沪深300 收盘 ≥ 4466.94"), and a row that names both and
                    # writes the declared instrument's own level in it
                    # ("| HSTECH / 013309.OF | … 4278.197 |").
                    aligned = self._declaration_for(
                        block, figure, written, symbol_records
                    )
                    if aligned is not None:
                        declaration, symbol = aligned, written
                    else:
                        line_text = (
                            positions[figure.line][0]
                            if 0 <= figure.line < len(positions)
                            else ""
                        )
                        if not self._writing_names_symbol(
                            content, figure, line_text, symbol, symbol_records
                        ):
                            issues.append(
                                self._figure_issue(
                                    "numeric_claim_conflict",
                                    figure,
                                    declaration.role,
                                    symbol,
                                    "symbol_mismatch",
                                    f"is declared for {symbol} but the answer writes it about {written}",
                                )
                            )
                            continue
            if declaration is None:
                found = self._check_observed(figure, None, symbol, records)
                if found and not figure.column and not self._check_held(figure, symbol):
                    # The user's own number needs no declaration to be theirs: an
                    # undeclared figure the user corpus reconciles is held, exactly
                    # as an undeclared figure a print reconciles is observed. A
                    # price-column cell is excluded — there it poses as a print.
                    continue
                if block.present and found:
                    issues.append(
                        self._figure_issue(
                            "figure_undeclared",
                            figure,
                            None,
                            symbol,
                            "undeclared",
                            "is not declared in the figures block and is not an observed "
                            "value; declare it as observed / derived / proposed / cited / "
                            "count / held, or remove it",
                        )
                    )
                    continue
                checked_price = True
                issues.extend(found)
                continue
            role = declaration.role
            if figure.column and role != "observed":
                issues.append(
                    self._figure_issue(
                        "numeric_claim_conflict",
                        figure,
                        role,
                        symbol,
                        "cited_as_observed" if role == "cited" else "role_in_price_column",
                        f"is declared {role}, but it sits in the {figure.column} column, "
                        "which holds observed prints only",
                    )
                )
                continue
            if role == "held":
                issues.extend(self._check_held(figure, symbol))
                continue
            if role == "count":
                posing = (
                    _is_plain_count(figure)
                    and self._poses_as_price(figure, self._price_band(symbol, records))
                    and not _close_any(figure.value, derived_constants)
                )
                if not _is_plain_count(figure) or posing:
                    checked_price = True
                    issues.extend(
                        self._count_as_observed(
                            figure,
                            declaration,
                            symbol,
                            records,
                            why=(
                                "it sits in the instrument's observed price range and no "
                                "declared derivation uses it"
                                if posing
                                else "a count cannot carry a currency mark"
                            ),
                        )
                    )
                continue
            if role == "cited":
                line = (
                    positions[figure.line][0]
                    if 0 <= figure.line < len(positions)
                    else ""
                )
                issues.extend(
                    self._check_cited(figure, declaration, symbol, declared_observed, line)
                )
                continue
            checked_price = True
            if role == "derived":
                issues.extend(
                    self._check_derived(
                        figure, declaration, symbol, records, block_anchored
                    )
                )
                continue
            if role == "proposed":
                issues.extend(
                    self._check_proposed(
                        figure, declaration, symbol, records, block_anchored
                    )
                )
                continue
            issues.extend(self._check_observed(figure, declaration, symbol, records))
        as_of = self._carried_as_of(records) if checked_price else None
        # The declaration block is stripped before the user reads the answer, so
        # a date written only there never reaches them.
        if as_of and not _states_as_of_date(strip_figures_block(content, block), as_of):
            issues.append(
                {
                    "code": "carried_price_not_dated",
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "carried_price_not_dated",
                    "as_of": as_of,
                    "message": (
                        "The quoted prices were fetched earlier in this session; "
                        f"state their as-of date ({as_of}) in the answer."
                    ),
                }
            )
        market_records = self._price_records()
        if checked_price and market_records:
            issues.extend(self._validate_price_provenance(content, market_records))
        return issues

    @staticmethod
    def _figure_issue(
        code: str,
        figure: Figure,
        role: str | None,
        symbol: str | None,
        reason: str,
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        """Build one figure-scoped issue with value, role, span, symbol and reason."""
        issue = {
            "code": code,
            "value": figure.text,
            "role": role,
            "span": [figure.start, figure.end],
            "symbol": symbol,
            "reason": reason,
            "claim": figure.text,
            "message": f"{figure.text} {message}.",
        }
        issue.update(extra)
        return issue

    def _figure_symbol(
        self,
        content: str,
        figure: Figure,
        declaration: Declaration | None,
        line_symbols: Sequence[str | None],
        document_symbol: str | None,
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """Resolve which instrument a figure is about (spec §4).

        Order: the declaration's ``ref``/``note``, the table row's symbol column,
        the figure's punctuation segment, its line, then the whole answer. The
        segment precedes the line because a comparison report names both
        instruments on one header line and then writes about one of them.
        """
        declared = self._declaration_symbol(declaration, records)
        if declared:
            return declared
        return self._written_symbol(content, figure, line_symbols, records) or document_symbol

    def _declaration_symbol(
        self,
        declaration: Declaration | None,
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """The instrument a declaration names, however the note spells it.

        A declaration is the model's own statement of where a number came from,
        so its note is read whole: the canonical spelling, the index's display
        name ("HSTECH SMA20"), or a bare code ("你 09-29 台账：019667 收益").
        Without the last two the note resolves to no instrument and the number
        is attributed to whatever row it was written in, which reads as a
        misattribution of the declaration itself.

        Args:
            declaration: The covering declaration, or None when there is none.
            records: The session's observed evidence, for the known-symbol filter.

        Returns:
            The canonical symbol the declaration names, or None.
        """
        if declaration is None:
            return None
        text = f"{declaration.ref} {declaration.note}"
        return (
            self._symbol_for_claim(text, records)
            or self._declared_label_symbol(declaration, records)
            or self._bare_symbol_in(text, records)
        )

    @staticmethod
    def _bare_symbol_in(text: str, records: Sequence[EvidenceRecord]) -> str | None:
        """The one instrument a bare spelling of it names, if the text carries one.

        Only a token that is exactly one held instrument's own stem or letter
        base counts, so a note naming two funds' bare codes names neither.
        Read in a declaration's note only: a number in the prose is prose, and
        a five-digit one is as likely a price as a listing.

        Args:
            text: The declaration's ``ref`` and ``note``.
            records: The session's observed evidence.

        Returns:
            The canonical symbol the bare spelling names, or None.
        """
        aliases: dict[str, set[str]] = {}
        for symbol in {record.symbol for record in records if record.symbol}:
            stem, _, venue = symbol.partition(".")
            numeric = stem.isdigit() and 5 <= len(stem) <= 6
            if numeric or (venue == "HK" and stem.isalpha()):
                aliases.setdefault(stem, set()).add(symbol)
        if not aliases:
            return None
        tokens = set(_BARE_CODE_RE.findall(text)) | set(_BARE_BASE_RE.findall(text))
        found = {
            next(iter(aliases[token]))
            for token in tokens
            if token in aliases and len(aliases[token]) == 1
        }
        return next(iter(found)) if len(found) == 1 else None

    def _declared_label_symbol(
        self,
        declaration: Declaration,
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """The index a declaration's own display-name label names, if one does.

        A note without a canonical code may still lead with the name the prose
        writes ("科创50 触发参考位，取观测 SMA20 = 1580.803"), or name the index
        after the declared number across a thin gap ("1580.80 上方（科创50）").
        The reading is restricted to labels whose index the ledger holds, and a
        label that merely begins a longer fund name ("沪深300ETF联接C") never
        counts — that names the fund, not the index inside it.

        Args:
            declaration: The covering declaration line.
            records: The session's observed evidence, for the known-symbol filter.

        Returns:
            The canonical symbol the declaration's label names, or None.
        """
        known = {record.symbol for record in records if record.symbol}
        labels = sorted(
            (
                (label, symbol)
                for label, symbol in _INDEX_LABELS.items()
                if symbol in known
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        if not labels:
            return None
        text = f"{declaration.ref} {declaration.note}".strip()
        if not text:
            return None
        head = text.lstrip("".join(_INLINE_GAP_CHARS))
        for label, symbol in labels:
            if head.startswith(label) and not _label_begins_a_longer_name(
                head[len(label) :]
            ):
                return symbol
        for label, symbol in labels:
            position = text.find(label)
            while position != -1:
                before = text[:position]
                numbers = list(_NOTE_NUMBER_RE.finditer(before))
                if numbers:
                    match = numbers[-1]
                    near = float(match.group(0).replace(",", ""))
                    tolerance = abs(declaration.value) * ROUNDED_BAND
                    if (
                        abs(abs(near) - abs(declaration.value)) <= tolerance
                        and _thin_gap(before[match.end() :])
                    ):
                        return symbol
                position = text.find(label, position + 1)
        return None

    def _written_symbol(
        self,
        content: str,
        figure: Figure,
        line_symbols: Sequence[str | None],
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """The instrument the answer's own text attaches a figure to, if one.

        The table row's symbol column, then the figure's punctuation segment,
        then its line; the whole-answer fallback is left to the caller.
        """
        if figure.symbol:
            # A table's symbol cell is text, not an identity: it may carry the
            # display name beside the code ("上证指数 000001.SH"). Only the
            # symbol spelling inside it may be compared; a cell without one has
            # no identity to contradict, so it falls through to the segment and
            # line, which resolve against evidence.
            normalized = _symbol_identity_in(figure.symbol)
            if normalized:
                return normalized
        left, right = segment_bounds(content, figure.start, figure.end)
        segment_symbol = self._symbol_for_claim(content[left:right], records)
        if segment_symbol:
            return segment_symbol
        if 0 <= figure.line < len(line_symbols) and line_symbols[figure.line]:
            return line_symbols[figure.line]
        return None

    def _declaration_for(
        self,
        block: FiguresBlock,
        figure: Figure,
        written: str,
        records: Sequence[EvidenceRecord],
    ) -> Declaration | None:
        """The covering declaration that names the instrument the prose wrote.

        Share siblings of one fund (an ETF's A/C feeder classes) declare the
        same number, so the block's first covering declaration may be the
        sibling the row is not about. The one whose ref/note resolves to the
        written instrument is the declaration the row actually used.
        """
        return block.match(
            figure.value,
            figure.percent,
            figure.digits,
            accepts=lambda item: self._declaration_symbol(item, records) == written,
        )

    @staticmethod
    def _names_symbol(text: str, symbol: str) -> bool:
        """Whether ``text`` names ``symbol``, by its own code or a display label.

        Unlike :meth:`_symbol_for_claim` this asks about one known instrument,
        not about which instrument a text is about: a symbol cell naming two
        instruments at once ("HSTECH / 013309.OF") names each of them.

        Args:
            text: The cell, segment or line to read.
            symbol: The canonical symbol to look for.

        Returns:
            True when the text names the symbol.
        """
        if not text:
            return False
        for match in _CANONICAL_SYMBOL_RE.finditer(text):
            if _normalize_symbol(match.group(0)) == symbol:
                return True
        for label, target in _INDEX_LABELS.items():
            if target != symbol:
                continue
            position = text.find(label)
            while position != -1:
                if not _label_begins_a_longer_name(text[position + len(label) :]):
                    return True
                position = text.find(label, position + 1)
        return False

    def _writing_names_symbol(
        self,
        content: str,
        figure: Figure,
        line_text: str,
        symbol: str,
        records: Sequence[EvidenceRecord],
    ) -> bool:
        """Whether the writing itself names the instrument a figure is declared for.

        A declaration may name an instrument the row is not about — an index's
        level written in a row whose symbol cell resolves to a wrapper fund. Two
        readings keep that honest: a label right beside the figure ("005658.OF
        沪深300 … 收盘 ≥ 4466.94"), and the row or line the figure sits in
        naming the declared instrument somewhere at all. A row that names only
        the other instrument is still a misattribution.

        Args:
            content: The candidate answer.
            figure: The figure whose declaration is being confirmed.
            line_text: The figure's own line.
            symbol: The instrument the declaration names.
            records: The session's observed evidence.

        Returns:
            True when the writing names the declared instrument.
        """
        if self._inline_symbol(content, figure, records) == symbol:
            return True
        return self._names_symbol(figure.symbol or "", symbol) or self._names_symbol(
            line_text, symbol
        )

    def _inline_symbol(
        self,
        content: str,
        figure: Figure,
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """The instrument the writing names immediately beside a figure, if one.

        A table row's symbol cell can resolve to a wrapper fund while the
        number beside it belongs to the index the sentence itself names
        ("005658.OF 沪深300 … 收盘站上 SMA20(4466.94)"). Only a label a short
        lookback reaches — across punctuation and at most thin quote/trigger
        words and indicator mentions — reads as inline; a display name that
        merely contains an index's name ("华夏沪深300ETF联接C") is not a label
        for the number after it. The reading is only ever used to confirm a
        figure's declared instrument, never to reassign one.
        """
        known = {record.symbol for record in records if record.symbol}
        if not known:
            return None
        window = content[max(0, figure.start - _INLINE_LABEL_WINDOW) : figure.start]
        cut = max((window.rfind(char) for char in _SEGMENT_BREAKS), default=-1)
        tail = window[cut + 1 :]
        if not tail:
            return None
        best_end = -1
        best_symbol: str | None = None
        for match in _CANONICAL_SYMBOL_RE.finditer(tail):
            symbol = _normalize_symbol(match.group(0))
            if symbol in known and match.end() >= best_end:
                best_end, best_symbol = match.end(), symbol
        for label, symbol in _INDEX_LABELS.items():
            if symbol not in known:
                continue
            position = tail.rfind(label)
            if position != -1 and position + len(label) >= best_end:
                best_end, best_symbol = position + len(label), symbol
        if best_symbol is None:
            return None
        if not _thin_gap(tail[best_end:]):
            return None
        return best_symbol

    def _referenced(
        self,
        ref: str,
        symbol: str | None,
        figure: Figure | None,
    ) -> tuple[list[EvidenceRecord], list[float]] | None:
        """The evidence named by an exact field, call+field, one call, or one tool.

        An exact evidence-field ref is accepted only when that field occurs in
        one call. When it repeats across calls, ``call_id::field`` is the
        unambiguous tightest scope, and its field part resolves the OHLC column
        roles through their spelling equivalence class (``ind::close`` finds
        ``latest_close``). Otherwise a ``ref`` naming a call id or a
        tool name keeps the existing call/tool scope, and the only one that can
        ground a non-price figure (revenue, IC, volume). Records of another
        symbol are dropped when the figure's symbol is known; a currency-marked
        figure keeps only money-denominated records, a percent only the others,
        less metadata counts.

        Args:
            ref: The declaration's ``ref``.
            symbol: The figure's resolved symbol, or None.
            figure: The figure whose shape narrows the kind, or None for the
                operands of a derivation.

        Returns:
            ``(records, metric values)``, or None when ``ref`` names no field,
            call, or tool.
        """
        key = (ref or "").strip()
        if not key:
            return None

        # A composite ref names one exact field from one exact call. This is
        # the unambiguous form when the same analysis field appears in more
        # than one tool call during a run.
        if "::" in key:
            call_id, field = (part.strip() for part in key.split("::", 1))
            if not call_id or not field:
                return [], []
            records, entries = self._field_sources(field, symbol)
            records = [record for record in records if record.call_id == call_id]
            metrics = [float(entry["value"]) for entry in entries if entry.get("call_id") == call_id]
            if not records and not metrics:
                # The model may name the semantic column (``close``) where the
                # tool spells its own (``latest_close``). A table column already
                # matches through that equivalence class (P0-1); the tightest
                # ref must not be stricter than the loose one, or the same real
                # close passes as ``ind`` and fails as ``ind::close``. Only the
                # OHLC column roles qualify, so an unknown field still resolves
                # to nothing.
                canonical = _column_field_for_path(field)
                if canonical:
                    records = [
                        record
                        for record in self._evidence
                        if record.call_id == call_id
                        and record.status == "observed"
                        and record.value is not None
                        and _column_field_for_path(record.field) == canonical
                    ]
        else:
            records, entries = self._field_sources(key, symbol)
            if records or entries:
                if self._ambiguous_field_sources(key, symbol):
                    # Calls disagree on this field, so the ref cannot say
                    # which value it quotes (VaR at 95% vs 99%). Fail closed
                    # rather than pool them; the issue names the calls.
                    return [], []
                metrics = [float(entry["value"]) for entry in entries]
            else:
                records = [
                    record
                    for record in self._evidence
                    if key in (record.call_id, record.tool)
                    and record.status == "observed"
                    and record.value is not None
                ]
                metrics = [
                    float(entry["value"])
                    for entry in self._analysis_entries(symbol)
                    if key in (entry.get("call_id"), entry.get("tool"))
                    and entry.get("value") is not None
                ]
                if not records and not metrics:
                    # Preserve the legacy loose-ref contract: a ref such as a
                    # symbol that names no field/call/tool falls back to the
                    # ordinary evidence path. Ambiguous or composite field
                    # refs return earlier as an explicit empty scope instead.
                    return None
        if symbol:
            records = [
                record for record in records if not record.symbol or record.symbol == symbol
            ]
        if figure is not None and figure.column:
            # A table cell quotes its own column, not whatever else the call
            # returned; the column role matches through its field equivalence
            # class, so a ``close`` column accepts the tool's own spelling
            # (``latest_close``, ``close_price``) of that same column.
            records = [
                record
                for record in records
                if _column_field_for_path(record.field) == figure.column
            ]
            metrics = []
        if figure is not None and figure.percent:
            records = [
                record
                for record in records
                if not _is_price_kind(record) and not _is_metadata_count_leaf(record.field)
            ]
        elif figure is not None and figure.currency:
            records = [record for record in records if _is_price_kind(record)]
            metrics = []
        return records, metrics

    def _analysis_entries(self, symbol: str | None) -> list[dict[str, Any]]:
        """Scope metrics by explicit observed symbols recorded on their call.

        Symbol-less aggregate calls remain eligible. A multi-symbol call cannot
        attribute its otherwise unlabelled metric to one particular instrument.
        Symbol-labelled EvidenceRecords remain available through their own path.
        """
        if not symbol:
            return list(self._analysis_metrics)
        call_symbols: dict[str, set[str]] = {}
        for record in self._evidence:
            if record.symbol and record.status == "observed":
                call_symbols.setdefault(record.call_id, set()).add(record.symbol)
        return [
            entry for entry in self._analysis_metrics
            if not call_symbols.get(entry.get("call_id"))
            or call_symbols[entry.get("call_id")] == {symbol}
        ]

    def _field_sources(
        self, field: str, symbol: str | None
    ) -> tuple[list[EvidenceRecord], list[dict[str, Any]]]:
        """Observed values of the evidence field ``field`` names, for ``symbol``.

        ``field`` is a full path (``data.tail_risk.var_95``) or its trailing
        part (``var_95``, ``tail_risk.var_95``): a short ref used to fall
        through to the whole evidence pool, where a VaR 99% claim quoting the
        95% value found its match (#1444 review). Another symbol's records are
        dropped here, before any count of calls, so two instruments' closes do
        not make ``close`` ambiguous.
        """
        def named(path: Any) -> bool:
            return isinstance(path, str) and (path == field or path.endswith("." + field))

        records = [
            record
            for record in self._evidence
            if named(record.field)
            and record.status == "observed"
            and record.value is not None
            and (not symbol or not record.symbol or record.symbol == symbol)
        ]
        entries = [
            entry
            for entry in self._analysis_entries(symbol)
            if named(entry.get("field")) and entry.get("value") is not None
        ]
        return records, entries

    def _ambiguous_field_sources(self, field: str, symbol: str | None) -> list[str]:
        """The ``call::path`` sources a field-only ref cannot choose between.

        Ambiguous means more than one (call, path) source AND more than one
        value among them: two runs of one call returning the same number leave
        nothing to choose. Each source is written as the ref that names it.

        Returns:
            Sorted ``call::path`` refs, or an empty list when the ref is exact.
        """
        if not field or "::" in field:
            return []
        records, entries = self._field_sources(field, symbol)
        sources = {(record.call_id, record.field, float(record.value)) for record in records}
        sources |= {
            (str(entry.get("call_id")), entry.get("field"), float(entry["value"])) for entry in entries
        }
        if len({(call, path) for call, path, _ in sources}) < 2 or len({value for *_, value in sources}) < 2:
            return []
        return sorted({f"{call}::{path}" for call, path, _ in sources})

    def _ref_scope_material(
        self, ref: str, symbol: str | None
    ) -> tuple[list[EvidenceRecord], list[str]]:
        """What the declaration's ref actually holds, before any column narrowing.

        The correction prompt has to say which fields the referenced call(s)
        did return, so this reads the raw scope — an exact call_id, or a tool
        name that names its calls — with the figure's symbol narrowing the
        records and never a column, a kind or a value (#1433 follow-up: the
        "returned no such value" notice pointed at an empty list and the model
        read it as "the data was never retrieved").

        Args:
            ref: The declaration's ``ref``.
            symbol: The figure's resolved symbol, or None.

        Returns:
            ``(records, field names)``; the names are sorted, deduplicated and
            capped at :data:`_MAX_SCOPE_FIELDS` with a trailing ellipsis.
        """
        key = (ref or "").strip().split("::", 1)[0].strip()
        if not key:
            return [], []
        records = [
            record
            for record in self._evidence
            if key in (record.call_id, record.tool)
            and record.status == "observed"
            and record.value is not None
            and (not symbol or not record.symbol or record.symbol == symbol)
        ]
        fields = {record.field for record in records if record.field}
        for entry in self._analysis_entries(symbol):
            if key in (entry.get("call_id"), entry.get("tool")) and entry.get("field"):
                fields.add(str(entry["field"]))
        names = sorted(fields)
        if len(names) > _MAX_SCOPE_FIELDS:
            names = names[:_MAX_SCOPE_FIELDS] + ["…"]
        return records, names

    def _tail_risk_sources(
        self,
        records: Sequence[EvidenceRecord],
        entries: Iterable[Mapping[str, Any]] = (),
    ) -> list[tuple[str, float]]:
        """``(identity, value)`` for every tail-risk value among these sources.

        Identity is read off the field name (:func:`tail_risk_identity`), so
        ``var_95`` and ``es_95`` are two identities and ``cvar_99`` / ``es_99``
        are one.
        """
        sources: list[tuple[str, float]] = []
        for record in records:
            identity = tail_risk_identity(record.field)
            if identity and record.status == "observed" and record.value is not None:
                sources.append((identity, float(record.value)))
        for entry in entries:
            identity = tail_risk_identity(str(entry.get("field") or ""))
            if identity and entry.get("value") is not None:
                sources.append((identity, float(entry["value"])))
        return sources

    @staticmethod
    def _tail_risk_field_refs(
        records: Sequence[EvidenceRecord],
        entries: Iterable[Mapping[str, Any]] = (),
    ) -> list[str]:
        """Exact call_id::field refs available for tail-risk evidence."""
        refs = {
            f"{record.call_id}::{record.field}"
            for record in records
            if record.call_id
            and record.field
            and record.status == "observed"
            and record.value is not None
            and tail_risk_identity(record.field)
        }
        refs |= {
            f"{entry.get('call_id')}::{entry.get('field')}"
            for entry in entries
            if entry.get("call_id")
            and entry.get("field")
            and entry.get("value") is not None
            and tail_risk_identity(str(entry.get("field") or ""))
        }
        return sorted(refs)

    def _tool_field_ref_candidates(
        self, ref: str, symbol: str | None
    ) -> list[str]:
        """Exact call refs for a mistaken tool_name::field declaration."""
        key = (ref or "").strip()
        if "::" not in key:
            return []
        scope, field = (part.strip() for part in key.split("::", 1))
        if not scope or not field:
            return []
        records, entries = self._field_sources(field, symbol)
        refs = {
            f"{record.call_id}::{record.field}"
            for record in records
            if record.tool == scope and record.call_id and record.field
        }
        refs |= {
            f"{entry.get('call_id')}::{entry.get('field')}"
            for entry in entries
            if entry.get("tool") == scope and entry.get("call_id") and entry.get("field")
        }
        return sorted(refs)

    def _tail_risk_ref_required(
        self,
        figure: Figure,
        records: Sequence[EvidenceRecord],
        entries: Iterable[Mapping[str, Any]] = (),
    ) -> list[str]:
        """Tail-risk identities this figure could be quoting, when there are several.

        #1425's remaining half, decided as a policy rather than patched: a
        session that observed more than one tail-risk identity cannot tell which
        one an undeclared or call-scoped figure means — the gate reads a
        number's shape, never the words "VaR 99%" beside it — so the figure has
        to name its field. A ref that names the field narrows the scope to one
        identity before this runs, so a declared figure never reaches here.

        Empty when the scope holds at most one identity (nothing to confuse) or
        when the figure matches none of the tail-risk values, which keeps every
        other kind of figure on exactly today's path.

        Returns:
            Sorted identities, or an empty list when no ref is required.
        """
        sources = self._tail_risk_sources(records, entries)
        if len({identity for identity, _ in sources}) < 2:
            return []
        return sorted(
            {
                identity
                for identity, value in sources
                if self._matches_evidence(figure, [value], [value])
            }
        )

    def _price_pool(
        self,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        *,
        column: str | None = None,
        date: str | None = None,
    ) -> list[float]:
        """Observed price values a figure may be compared against."""
        return [
            float(record.value)
            for record in self._price_candidates(symbol, records, column=column, date=date)
        ]

    def _price_candidates(
        self,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        *,
        column: str | None = None,
        date: str | None = None,
    ) -> list[EvidenceRecord]:
        """Observed price records a figure may be compared against.

        Filtered by symbol, then by OHLC field and trade date when the figure sits
        under those table headers (spec §4).
        """
        candidates = list(records)
        if symbol:
            candidates = [record for record in candidates if record.symbol == symbol]
        elif len({record.symbol for record in records if record.symbol}) > 1:
            # Two instruments and no resolved symbol: an indicator reading is
            # symbol-bound, so it cannot ground a figure attributed to neither.
            candidates = [record for record in candidates if record.field != "indicator"]
        if column:
            # The column is an OHLC role matched through its field equivalence
            # class: a table's ``close`` column sees the ``latest_close`` /
            # ``close_price`` spelling of the same column, never a high, a
            # quote or an indicator level.
            candidates = [
                record
                for record in candidates
                if _column_field_for_path(record.field) == column
            ]
        if date:
            candidates = [
                record
                for record in candidates
                if record.timestamp
                and _timestamp_matches_claim_date(record.timestamp, date)
            ]
        return [record for record in candidates if record.value is not None]

    def _row_pool(self, symbol: str | None, *, money_only: bool = False) -> list[float]:
        """Numbers a market-data row carried (volume, amount, turnover, …).

        Only ``get_market_data`` and run-dir CSV rows count, so a generic tool's
        numeric leaves never widen the check. ``money_only`` keeps the
        money-denominated fields a currency-marked figure may quote.
        """
        return [
            float(record.value)
            for record in self._evidence
            if record.status == "observed"
            and record.value is not None
            and record.tool in {"get_market_data", "bash"}
            and (not symbol or record.symbol == symbol)
            and (not money_only or _is_price_kind(record))
        ]

    @staticmethod
    def _nearest_prints(
        figure: Figure,
        scope: Sequence[EvidenceRecord],
        fallback: Sequence[float],
    ) -> list[float]:
        """The observed values a rejected figure is pointed at (#1433).

        A non-percent figure is pointed at its own table column, else at the
        closes (then last or adjusted prices) in scope, never at every field of
        every bar; a percent at the values it was compared with.

        Args:
            figure: The rejected figure.
            scope: The evidence records its check was scoped to.
            fallback: The values its check compared it against.

        Returns:
            Up to three observed values, closest first.
        """
        if not figure.percent:
            for name in (figure.column,) if figure.column else _CITABLE_FIELDS:
                values = [
                    float(record.value)
                    for record in scope
                    if record.value is not None
                    and (_price_field_for_path(record.field) or record.field) == name
                ]
                if values:
                    return _nearest(figure.value, values)
        return _nearest(figure.value, fallback)

    def _count_as_observed(
        self,
        figure: Figure,
        declaration: Declaration,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        *,
        why: str = "a count cannot carry a currency mark",
    ) -> list[dict[str, Any]]:
        """Check a ``count`` that looks like a price as the observation it claims to be."""
        found = self._check_observed(figure, declaration, symbol, records)
        for issue in found:
            issue["role"] = "count"
            issue["message"] = (
                f"{figure.text} is declared count, but {why}, so it was checked as "
                "observed: " + issue["message"][len(figure.text) + 1 :]
            )
        return found

    def _price_band(
        self, symbol: str | None, records: Sequence[EvidenceRecord]
    ) -> tuple[float, float] | None:
        """The observed price range of a figure's instrument, or None when unknown."""
        if symbol is None and len({record.symbol for record in records if record.symbol}) > 1:
            return None
        prices = [value for value in self._price_pool(symbol, records) if value > 0]
        return (min(prices), max(prices)) if prices else None

    @staticmethod
    def _poses_as_price(figure: Figure, band: tuple[float, float] | None) -> bool:
        """Whether an unmarked number sits where its instrument's price does.

        A decimal inside the observed range (±10%) reads as a quote. An integer
        does only for an instrument quoted in the thousands, between half and
        twice its range. A percent is never a price.

        Args:
            figure: The figure.
            band: The instrument's observed ``(low, high)``, or None.

        Returns:
            True when the number should be checked as a price.
        """
        if band is None or figure.percent:
            return False
        low, high = band
        value = abs(figure.value)
        if "." in (figure.digits or figure.text):
            return low * 0.9 <= value <= high * 1.1
        return low >= _INTEGER_PRICE_FLOOR and low * 0.5 <= value <= high * 2.0

    def _metric_pool(
        self, symbol: str | None, *, registered: bool = True
    ) -> list[float]:
        """Metric values from completed analysis results and metric-named leaves.

        A leaf counts when its field name maps to an analysis kind or, with
        ``registered``, when its tool registered it as a metric reading (an RSI
        level, a MACD line). Registered readings are measured in their own
        units, not as ratios, so the percent pool leaves them out: an RSI of
        55.2 must not answer a win rate written as 55%.
        """
        values = [
            float(entry["value"])
            for entry in self._analysis_entries(symbol)
            if entry.get("value") is not None
        ]
        values.extend(
            float(record.value)
            for record in self._evidence
            if record.status == "observed"
            and record.value is not None
            and (
                _metric_kind_for_path(record.field) is not None
                or (
                    registered
                    and _is_registered_metric_indicator(record.tool, record.field)
                )
            )
            and (not symbol or not record.symbol or record.symbol == symbol)
        )
        return values

    def _matches_evidence(
        self,
        figure: Figure,
        direct: Sequence[float],
        scaled: Sequence[float],
        *,
        legacy_direct: bool = False,
    ) -> bool:
        """Whether a figure equals evidence, at its own scale or a metric's.

        ``direct`` is compared literally. ``scaled`` absorbs fraction vs percent
        (0.182 vs 18.2%) and the sign of a fall (drawdown -0.094 quoted as 9.4%).
        Both are held to the digits the figure was written with
        (:meth:`_within_written_precision`); ``legacy_direct`` keeps the flat evidence
        band for ``direct`` when it is an undeclared price checked against prints.
        """
        if legacy_direct:
            if _close_any(figure.value, direct):
                return True
            if figure.scale != 1.0 and _close_any(figure.value * figure.scale, direct):
                return True
        elif any(
            self._within_written_precision(figure, figure.value, target)
            for target in direct
        ):
            return True
        elif figure.scale != 1.0 and any(
            self._within_written_precision(
                figure, figure.value * figure.scale, target, figure.scale
            )
            for target in direct
        ):
            return True
        magnitudes = [abs(target) for target in scaled]
        return any(
            self._within_written_precision(figure, candidate, target, unit)
            for candidate, unit in (
                (abs(figure.value), 1.0),
                (abs(figure.value) / 100.0, 0.01),
            )
            for target in magnitudes
        )

    @staticmethod
    def _within_written_precision(
        figure: Figure, candidate: float, target: float, unit: float = 1.0
    ) -> bool:
        """Whether a figure is ``target`` correctly rounded to the digits it was written with.

        Raw evidence uses the relative :data:`_TOLERANCE` band. A figure written
        with decimals uses :data:`figures.ROUNDED_BAND` (the same 0.5% cap),
        narrowed to half a unit of its last written decimal. A sufficiently
        precise rendering such as
        0.82467 -> 0.825 survive; 0.82 exceeds the 0.5% relative policy.
        A figure written without decimals keeps the raw relative band alone: an
        integer's precision is not known ("6,700" may be rounded to hundreds).

        Args:
            figure: The prose figure.
            candidate: The figure's value in the units being compared.
            target: The evidence value.
            unit: How many compared units one written unit is (0.01 when a percent
                is compared as a fraction).
        """
        written = figure.digits or figure.text
        band = abs(target) * (ROUNDED_BAND if "." in written else _TOLERANCE)
        if "." in written:
            band = min(band, _written_half_unit(written) * unit * (1 + 1e-9))
        return abs(candidate - target) <= max(band, 1e-9)

    def _check_held(
        self,
        figure: Figure,
        symbol: str | None,
    ) -> list[dict[str, Any]]:
        """Check a ``held`` figure against the values the user themselves wrote.

        The user corpus (their messages plus the memories recalled in their
        name) is read with the same reader the prose goes through, so thousands
        separators, full-width digits, decimal commas and magnitude marks
        reconcile; the value itself must match exactly, because a restated user
        figure that no longer equals what they wrote is no longer their number.
        A percent's sign alone is the direction of a move ("距峰值 −20%" for the
        rule the user wrote as "回撤熔断 20%"), which the prose states and the
        corpus need not repeat, so magnitudes reconcile it as they do for an
        observed metric. A held figure is the user's own data: it is checked
        against the user corpus, never against tool evidence, and a figure that
        reconciles is not cut. One that does not reconcile is a misquotation and
        fails like any other unverified figure.

        Args:
            figure: The prose figure declared held.
            symbol: The instrument the figure was resolved to, if any.

        Returns:
            An empty list when the value appears in the user corpus, else one
            issue saying the figure is not the user's.
        """
        value = figure.value * (1.0 if figure.percent else figure.scale)
        provided = [
            other
            for other, percent in self._user_provided_values
            if percent == figure.percent
        ]
        if _verbatim_any(value, provided) or (
            figure.percent
            and _verbatim_any(abs(value), [abs(other) for other in provided])
        ):
            return []
        return [
            self._figure_issue(
                "held_value_not_provided",
                figure,
                "held",
                symbol,
                "not_in_user_messages",
                "is declared held, but no matching value appears in the user's own "
                "messages or the recalled memories; quote their figure exactly, or "
                "remove it",
            )
        ]

    def _check_observed(
        self,
        figure: Figure,
        declaration: Declaration | None,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
    ) -> list[dict[str, Any]]:
        """An observed figure must appear in evidence of its own kind.

        A currency-marked figure is answered only by money-denominated values
        and a percent only by the rest; a table cell only by its column's field.
        """
        scoped = self._referenced(declaration.ref, symbol, figure) if declaration else None
        if scoped is not None:
            scoped_records, metric_values = scoped
            if (
                declaration is not None
                and not scoped_records
                and not metric_values
            ):
                call_field_candidates = self._tool_field_ref_candidates(
                    declaration.ref, symbol
                )
                if call_field_candidates:
                    return [
                        self._figure_issue(
                            "numeric_claim_conflict",
                            figure,
                            "observed",
                            symbol,
                            "field_ref_needs_call_id",
                            f"is declared observed from {declaration.ref}, whose left side is "
                            "a tool name rather than one exact call id",
                            source_tool_call_ids=[declaration.ref],
                            ambiguous_sources=call_field_candidates,
                            field_ref_candidates=call_field_candidates,
                        )
                    ]
            values = [float(record.value) for record in scoped_records] + metric_values
            money = figure.currency and not figure.percent
            # A call- or tool-scoped ref pools every field that call returned,
            # so it cannot choose between the tail-risk identities in it (#1425).
            scoped_entries = [
                entry
                for entry in self._analysis_entries(symbol)
                if declaration.ref in (entry.get("call_id"), entry.get("tool"))
            ]
            tail_risk = self._tail_risk_ref_required(
                figure, scoped_records, scoped_entries
            )
            scoped_identities: list[str] = []
            if tail_risk:
                scoped_sources = self._tail_risk_sources(scoped_records, scoped_entries)
                scoped_identities = sorted({identity for identity, _ in scoped_sources})
                # Whatever else the call returned may still answer the figure.
                blocked = {
                    value for identity, value in scoped_sources if identity in tail_risk
                }
                values = [value for value in values if value not in blocked]
            if self._matches_evidence(figure, values, [] if money else values):
                return []
            if tail_risk:
                return [
                    self._figure_issue(
                        "numeric_claim_conflict",
                        figure,
                        "observed",
                        symbol,
                        "tail_risk_needs_field_ref",
                        f"is declared observed from {declaration.ref}, which returned "
                        f"{', '.join(scoped_identities)} and matches "
                        f"{', '.join(tail_risk)}; a tail-risk figure has to name the "
                        "field it quotes",
                        source_tool_call_ids=[declaration.ref],
                        ambiguous_sources=scoped_identities,
                        field_ref_candidates=self._tail_risk_field_refs(
                            scoped_records, scoped_entries
                        ),
                    )
                ]
            ambiguous = self._ambiguous_field_sources(declaration.ref, symbol)
            if ambiguous:
                return [
                    self._figure_issue(
                        "numeric_claim_conflict",
                        figure,
                        "observed",
                        symbol,
                        "ambiguous_field_ref",
                        f"is declared observed from {declaration.ref}, which names "
                        f"{', '.join(ambiguous)}, and they hold different values",
                        source_tool_call_ids=[declaration.ref],
                        ambiguous_sources=ambiguous,
                        field_ref_candidates=ambiguous,
                    )
                ]
            scope_records, scope_fields = self._ref_scope_material(declaration.ref, symbol)
            # The scope may hold the field but no value equal to the figure (a
            # mismatched close), or the call may return none of that field; the
            # notice says which, names the fields the call did return, and
            # points at the scope's own values instead of an empty list.
            column = f" in its {figure.column} column" if figure.column else ""
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "observed",
                    symbol,
                    "not_in_referenced_call",
                    f"is declared observed from {declaration.ref}, whose results "
                    f"{'for ' + symbol + ' ' if symbol else ''}do not contain a "
                    f"matching value{column}",
                    source_tool_call_ids=[declaration.ref],
                    field=figure.column,
                    observed_fields=scope_fields,
                    observed_nearest=self._nearest_prints(
                        figure, scoped_records or scope_records, values
                    ),
                )
            ]
        candidates = self._price_candidates(
            symbol, records, column=figure.column, date=figure.date
        )
        prices = [float(record.value) for record in candidates]
        if figure.percent:
            # A percent is a ratio; no price or volume may answer it, and a
            # registered reading in its own units (an RSI level) is no ratio
            # either.
            direct: list[float] = []
            scaled = (
                [] if figure.column else self._metric_pool(symbol, registered=False)
            )
        elif figure.column:
            direct, scaled = prices, []
        elif figure.currency:
            direct, scaled = prices + self._row_pool(symbol, money_only=True), []
        else:
            direct, scaled = prices + self._row_pool(symbol), self._metric_pool(symbol)
        # An undeclared tail-risk figure is matched against every tail-risk
        # value in the session, so it needs a field ref for the same reason a
        # call-scoped one does (#1425).
        session_records = [
            record
            for record in self._evidence
            if not symbol or not record.symbol or record.symbol == symbol
        ]
        tail_risk = self._tail_risk_ref_required(
            figure, session_records, self._analysis_entries(symbol)
        )
        session_identities: list[str] = []
        if tail_risk:
            session_sources = self._tail_risk_sources(
                session_records, self._analysis_entries(symbol)
            )
            session_identities = sorted({identity for identity, _ in session_sources})
            blocked = {
                value for identity, value in session_sources if identity in tail_risk
            }
            direct = [value for value in direct if value not in blocked]
            scaled = [value for value in scaled if value not in blocked]
        if not direct and not scaled and not tail_risk:
            return [
                self._figure_issue(
                    "numeric_claim_unavailable",
                    figure,
                    "observed",
                    symbol,
                    "no_evidence",
                    "is declared observed but this session holds no matching tool "
                    "evidence to check it against",
                    field=figure.column,
                    date=figure.date,
                )
            ]
        if self._matches_evidence(figure, direct, scaled, legacy_direct=True):
            return []
        if tail_risk:
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "observed",
                    symbol,
                    "tail_risk_needs_field_ref",
                    f"matches {', '.join(tail_risk)}, and this session observed "
                    f"{', '.join(session_identities)}, so the figure has to name "
                    "the field it quotes",
                    ambiguous_sources=session_identities,
                    field_ref_candidates=self._tail_risk_field_refs(
                        session_records, self._analysis_entries(symbol)
                    ),
                )
            ]
        attributable = symbol is not None or len(
            {record.symbol for record in records if record.symbol}
        ) <= 1
        # A mistyped indicator reading gets its correction from the metric
        # domain: an RSI level's mismatch answered with the instrument's price
        # range points the rewrite at the wrong units, and the model reads the
        # rejection as the data being missing rather than misread. The price
        # domain keeps the figure whenever it sits where the price does, or
        # when its nearest metric value is no closer than the nearest print.
        metric_closer = (
            bool(scaled)
            and not self._poses_as_price(figure, self._price_band(symbol, records))
            and (
                not direct
                or min(abs(abs(figure.value) - abs(value)) for value in scaled)
                < min(abs(figure.value - value) for value in direct)
            )
        )
        if metric_closer:
            reported = sorted(scaled)
            nearest = (
                sorted(
                    {float(value) for value in scaled},
                    key=lambda item: (abs(abs(item) - abs(figure.value)), item),
                )[:3]
                if attributable
                else []
            )
            label = "indicator"
        else:
            # The range quoted back to the model is the session's price-comparable
            # evidence. The mixed row/metric pools stay in the match (an unmarked
            # figure may be a row number) but never in the printed range: they read
            # "1.04–1.33601e+06" — a close next to a volume — which tells the model
            # nothing it can correct against.
            reported = sorted(prices) if prices else sorted(direct or scaled)
            nearest = (
                self._nearest_prints(figure, candidates, reported)
                if attributable
                else []
            )
            label = figure.column or "observed"
        return [
            self._figure_issue(
                "numeric_claim_conflict",
                figure,
                "observed",
                symbol,
                "value_mismatch",
                "is declared observed but conflicts with the "
                f"{label} evidence "
                f"{reported[0]:g}–{reported[-1]:g}",
                field=figure.column,
                date=figure.date,
                observed_min=reported[0],
                observed_max=reported[-1],
                observed_nearest=nearest,
            )
        ]

    def _block_anchored_values(self, block: FiguresBlock) -> set[float]:
        """Values the block's own chain rests on the user's numbers, to a fixpoint.

        A report chains arithmetic: a lot is the sum of the amounts the user
        stated, and a weight is the quotient of two such lots. Every link has to
        hold on its own — a ``held`` line must reconcile verbatim with the user
        corpus, and a ``derived`` line must evaluate to the value it declares
        over operands already anchored — so the closure can only ever re-state
        the user's own figures, never launder a market print into them.

        Args:
            block: The parsed declaration block.

        Returns:
            Every value so anchored, excluding the corpus values themselves.
        """
        corpus = [value for value, _ in self._user_provided_values]

        def in_corpus(declaration: Declaration) -> bool:
            return _verbatim_any(declaration.value, corpus) or (
                declaration.percent
                and _verbatim_any(abs(declaration.value), [abs(value) for value in corpus])
            )

        anchored = {
            declaration.value
            for declaration in block.declarations
            if declaration.role == "held" and in_corpus(declaration)
        }
        changed = True
        while changed:
            changed = False
            for declaration in block.declarations:
                if declaration.role != "derived" or declaration.value in anchored:
                    continue
                evaluated = _formula_in_note(declaration.note)
                if evaluated is None:
                    continue
                result, operands, _ = evaluated
                if not operands or not all(
                    _verbatim_any(operand, anchored) for operand in operands
                ):
                    continue
                stated = result * 100.0 if declaration.percent else result
                if not _close(stated, declaration.value):
                    continue
                anchored.add(declaration.value)
                changed = True
        return anchored

    def _derivation(
        self,
        declaration: Declaration | None,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        *,
        money: bool = False,
        price_shaped: bool = False,
        anchored: Iterable[float] = (),
    ) -> tuple[float, list[float]] | str | None:
        """Evaluate a declaration's note as an observation-anchored formula.

        Returns the ``(result, operands)`` pair when the note is arithmetic
        over at least two operands, at least one of which the run observed or
        the user themselves provided (a held value, matched verbatim) and
        every added or subtracted term of which holds such an operand;
        otherwise the reason it is not. A quote-shaped figure
        (``price_shaped``) must rest on observed prices alone: the user's own
        figures are not evidence of what the market printed, so they are no
        anchor for a number that reads as a price. With several instruments'
        bars and no resolved symbol a run observation would be borrowed from
        one of them by accident, so there only the user's own figures may
        anchor the arithmetic.

        Args:
            declaration: The covering declaration.
            symbol: The instrument the figure was resolved to, if any.
            records: The session's observed evidence.
            money: Whether the figure carries a money mark.
            price_shaped: Whether the number sits where its instrument's price does.
            anchored: Values the block itself derives from the user's own
                numbers (:meth:`_block_anchored_values`).

        Returns:
            The ``(result, operands)`` pair, or the reason it is not a derivation.
        """
        if declaration is None or not declaration.note.strip():
            return "no_formula"
        evaluated = _formula_in_note(declaration.note)
        if evaluated is None:
            return "formula_not_evaluable"
        result, operands, tree = evaluated
        # The user's own figures anchor a derivation, and so do the block's own
        # totals and quotients once they are shown to rest on them: a weight is
        # legitimately 268996 / 389911 where both are sums of the amounts the
        # user stated.
        provided = [value for value, _ in self._user_provided_values]
        provided.extend(anchored)
        if price_shaped:
            # A quote-shaped number must rest on observed prices alone: the
            # user's own figures say nothing about what the market printed.
            provided = []
        if not symbol and len({record.symbol for record in records if record.symbol}) > 1:
            # Two instruments' bars and no resolved symbol: only arithmetic
            # the user's own figures anchor verbatim is kept.
            if not any(_verbatim_any(operand, provided) for operand in operands):
                return "no_symbol"
        # A money-marked result is derived from money: an RSI or a volume is not
        # a price to take a discount of.
        anchors = self._price_pool(symbol, records) + self._row_pool(symbol, money_only=money)
        if not money:
            anchors += self._metric_pool(symbol)
        scoped = self._referenced(declaration.ref, symbol, None)
        if scoped is not None:
            anchors.extend(
                float(record.value)
                for record in scoped[0]
                if not money or _is_price_kind(record)
            )
            if not money:
                anchors.extend(scoped[1])
        if not anchors and not provided:
            return "no_evidence"

        def observed(operand: float) -> bool:
            return _close_any(operand, anchors) or _verbatim_any(operand, provided)

        if not any(observed(operand) for operand in operands):
            return "formula_not_anchored"
        if _unanchored_term(tree, observed):
            return "additive_operand_not_observed"
        return result, operands

    @staticmethod
    def _result_matches(figure: Figure, result: float) -> bool:
        """Whether a formula's result is the value the prose figure states.

        The band is half a unit of the last digit the PROSE was written with
        ("约 37%" for 36.75%), so a coarser declaration cannot widen it. A "%"
        figure is compared only in percentage points, since against the fraction
        a half-unit band spans fifty points; a bare figure is tried both ways.
        Magnitudes are compared, because a fall is noted either as
        ``(low − high) / high`` or as the drop, unless the prose wrote a sign.
        """
        # The normalized reading, so "0,666" is three decimals and "−5,13%" is signed.
        half_unit = _written_half_unit(figure.digits or figure.text)
        targets = {result * 100.0} if figure.percent else {result, result * 100.0}
        sign = _explicit_sign(figure.sign or figure.text)
        value = abs(figure.value)
        return any(
            abs(value - abs(target)) <= max(abs(target) * _TOLERANCE, half_unit, 1e-9)
            for target in targets
            if not sign or target * sign >= 0
        )

    def _check_derived(
        self,
        figure: Figure,
        declaration: Declaration | None,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        anchored: Iterable[float] = (),
    ) -> list[dict[str, Any]]:
        """A derived figure must be the arithmetic its note states.

        A number that sits where the instrument's price does is held to
        observed prices for its anchors; only figures outside the price's
        shape may rest on the user's own numbers.
        """
        posing = self._poses_as_price(figure, self._price_band(symbol, records))
        derivation = self._derivation(
            declaration,
            symbol,
            records,
            money=figure.currency,
            price_shaped=posing,
            anchored=anchored,
        )
        if isinstance(derivation, str):
            return [
                self._figure_issue(
                    "numeric_claim_conflict"
                    if derivation != "no_evidence"
                    else "numeric_claim_unavailable",
                    figure,
                    "derived",
                    symbol,
                    derivation,
                    "is declared derived, but its note adds or subtracts an operand "
                    "this session did not observe"
                    if derivation == "additive_operand_not_observed"
                    else "is declared derived, but its note is not arithmetic over at "
                    "least two operands with one of them observed in this session",
                )
            ]
        result, _ = derivation
        if self._result_matches(figure, result):
            return []
        # Reported in the figure's own units, as ``_result_matches`` compares it.
        scaled = result * 100.0 if figure.percent else result
        shown = f"{scaled:.6g}%" if figure.percent else f"{scaled:.6g}"
        return [
            self._figure_issue(
                "numeric_claim_conflict",
                figure,
                "derived",
                symbol,
                "derivation_result_mismatch",
                f"is declared derived, but its own formula evaluates to {shown}",
                derived_result=shown,
            )
        ]

    def _check_proposed(
        self,
        figure: Figure,
        declaration: Declaration | None,
        symbol: str | None,
        records: Sequence[EvidenceRecord],
        anchored: Iterable[float] = (),
    ) -> list[dict[str, Any]]:
        """A proposed level (entry, target, stop) is derived or inside the observed range.

        It cannot be required to equal a print, only to be anchored; a level far
        outside what the session saw is the invention this gate exists to stop.
        A level is a price, so a percent is never one, and a level attributed to
        no instrument of several has no range to lie in.
        """
        if figure.percent:
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "proposed",
                    symbol,
                    "proposed_not_a_price",
                    "is declared proposed, but a proposed level is a price and a percent "
                    "is not one; declare it derived with its arithmetic, or cited",
                )
            ]
        if not symbol and len({record.symbol for record in records if record.symbol}) > 1:
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "proposed",
                    symbol,
                    "no_symbol",
                    "is a proposed level, but the run holds prices for more than one "
                    "instrument and nothing attributes it to one",
                )
            ]
        derivation = self._derivation(
            declaration, symbol, records, money=figure.currency, anchored=anchored
        )
        if not isinstance(derivation, str) and self._result_matches(figure, derivation[0]):
            return []
        candidates = self._price_candidates(symbol, records)
        prices = [float(record.value) for record in candidates]
        if not prices:
            return [
                self._figure_issue(
                    "numeric_claim_unavailable",
                    figure,
                    "proposed",
                    symbol,
                    "no_evidence",
                    "is a proposed level but this session observed no price for "
                    "the instrument to anchor it to",
                )
            ]
        if min(prices) <= figure.value <= max(prices):
            return []
        return [
            self._figure_issue(
                "numeric_claim_conflict",
                figure,
                "proposed",
                symbol,
                "outside_observed_range",
                "is a proposed level outside the observed range "
                f"{min(prices):g}–{max(prices):g} and its note derives no value",
                observed_min=min(prices),
                observed_max=max(prices),
                observed_nearest=self._nearest_prints(figure, candidates, prices),
            )
        ]

    def _check_cited(
        self,
        figure: Figure,
        declaration: Declaration,
        symbol: str | None,
        declared_observed: set[float],
        line: str,
    ) -> list[dict[str, Any]]:
        """A cited figure names a source the reader can see and does not pose as a print.

        Its value is unchecked, so the citation may not launder an observation
        (the value may not also be declared observed). The block is stripped
        before release, so a source only the note names is no citation: a note
        token (``_note_tokens``) must appear on the figure's own line.
        """
        if not declaration.note.strip():
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "cited",
                    symbol,
                    "citation_without_source",
                    "is declared cited but names no source in its note",
                )
            ]
        if any(_close(figure.value, value) for value in declared_observed):
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "cited",
                    symbol,
                    "cited_as_observed",
                    "is declared cited yet presented as an observed value of this "
                    "instrument",
                )
            ]
        folded = line.casefold()
        if not any(token in folded for token in _note_tokens(declaration.note)):
            return [
                self._figure_issue(
                    "numeric_claim_conflict",
                    figure,
                    "cited",
                    symbol,
                    "citation_not_visible",
                    "is declared cited, but no source its note names appears on its "
                    "line, and the note is stripped before anyone reads the answer",
                )
            ]
        return []

    def _validate_unsourced_symbols(
        self,
        content: str,
        figures: Sequence[Figure],
        block: FiguresBlock,
    ) -> list[dict[str, Any]]:
        """Reject figures attached to an instrument no tool in this run handled.

        Naming a symbol is fine, but a line pairing an unhandled canonical symbol
        with a measured figure has no origin other than model memory (#886/#887).
        A figure declared ``cited`` is exempt, since a citation is an origin;
        so is ``held``, whose origin is the user's own words.
        """
        issues: list[dict[str, Any]] = []
        reported: set[str] = set()
        for index, (line, offset) in enumerate(_lines_with_offsets(content)):
            unknown = sorted(
                symbol
                for symbol in _scan_symbols(line)
                - self._session_symbols
                - reported
                if symbol.rsplit(".", 1)[0] not in self._session_symbol_roots
            )
            if not unknown:
                continue
            carried = [
                figure
                for figure in figures
                if figure.line == index and figure.shape == "measured"
            ]
            if not carried:
                continue
            if all(
                (
                    block.match(figure.value, figure.percent, figure.digits)
                    or _NO_DECLARATION
                ).role
                in {"cited", "held"}
                for figure in carried
            ):
                continue
            for symbol in unknown:
                reported.add(symbol)
                issues.append(
                    {
                        "code": "unsourced_symbol_figures",
                        "symbol": symbol,
                        "value": None,
                        "role": None,
                        "reason": "symbol_never_handled",
                        "claim": line.strip()[:200],
                        "span": [offset, offset + len(line)],
                        "message": (
                            f"No tool call in this session passed in or returned {symbol}, "
                            "yet the answer attaches figures to it. Retrieve it, or report "
                            "it as not retrieved."
                        ),
                    }
                )
        return issues

    def _validate_availability_claims(self, content: str) -> list[dict[str, Any]]:
        """Reject reporting an instrument as unretrieved while the ledger holds it.

        The distortion this prevents (2026-10-08 run): layer-1 compaction had
        emptied the model's working context, and the released answer told the
        user that data the session really observed was "调用成功但未保留/未取到".
        Line-scoped and code-scoped on purpose — the denial phrase and the code
        it describes must sit on the same line, and that code must have an
        observed record; a genuinely unfetched instrument stays free to be
        reported as not retrieved. The code a phrase denies is the nearest one
        beside it (:func:`_attributed_denials`), never every code sharing the
        line, so a line naming several instruments is not blanketed (2026-10-12
        run).

        Only THIS run's own observations make a denial false: the claim reads
        "本轮未取到" (not retrieved *this run*), and the ledger also carries
        observed prices from earlier runs of the session (``carried_from`` set).
        Reporting as not-retrieved-this-run an instrument whose only evidence
        this run inherited is honest, so inherited records are excluded from the
        contradiction (2026-10-12 run, ``399006.SZ``).

        Args:
            content: The model draft, figures block included.

        Returns:
            One issue per instrument denied, up to
            :data:`_MAX_AVAILABILITY_ISSUES`.
        """
        observed: dict[str, list[EvidenceRecord]] = {}
        for record in self._evidence:
            if (
                record.status != "observed"
                or record.value is None
                or not record.symbol
                or record.carried_from
            ):
                continue
            observed.setdefault(_normalize_symbol(record.symbol), []).append(record)
        if not observed:
            return []
        issues: list[dict[str, Any]] = []
        reported: set[str] = set()
        for line, offset in _lines_with_offsets(content):
            if not _AVAILABILITY_CLAIM_RE.search(line):
                continue
            for symbol in sorted(_attributed_denials(line)):
                if symbol in reported or symbol not in observed:
                    continue
                records = observed[symbol]
                reported.add(symbol)
                # Quote the newest call for this instrument first: the old
                # message quoted the session's FIRST records (an early
                # get_market_data OHLC print) while the latest indicator call
                # held the sma_50/… the draft had written about, so the model
                # answered "not retrieved" for values it fetched moments earlier
                # (2026-10-10 run).
                latest_call = records[-1].call_id
                latest = [record for record in records if record.call_id == latest_call]
                # Indicator-style leaves first, raw price-column prints second:
                # the old message led with an early call's open/high while the
                # draft's own sma_50 came from the latest call (2026-10-10).
                ordered = [
                    record
                    for record in latest
                    if _column_field_for_path(record.field) is None
                ] + [
                    record
                    for record in latest
                    if _column_field_for_path(record.field) is not None
                ]
                quoted = "; ".join(
                    f"{record.call_id}::{record.field} = "
                    f"{format(float(record.value), '.10g')}"
                    for record in ordered[:_MAX_AVAILABILITY_QUOTES]
                )
                message = (
                    f"The answer reports {symbol} as not retrieved "
                    f"(its line: {line.strip()[:120]!r}), but this run's "
                    f"ledger holds {len(records)} observed value(s) for it; its "
                    f"latest call ({latest_call}) returned: {quoted}"
                )
                as_of = _latest_as_of(records)
                if as_of:
                    message += f" (as of {as_of})"
                message += ". Remove the claim or state the observed values instead."
                issues.append(
                    {
                        "code": "availability_claim_contradicts_evidence",
                        "symbol": symbol,
                        "value": None,
                        "role": None,
                        "reason": "claim_contradicts_ledger",
                        "claim": line.strip()[:200],
                        "span": [offset, offset + len(line)],
                        "message": message,
                    }
                )
                if len(issues) >= _MAX_AVAILABILITY_ISSUES:
                    return issues
        return issues

    @staticmethod
    def _symbol_for_claim(
        content: str,
        records: Sequence[EvidenceRecord],
    ) -> str | None:
        """Return one canonical evidence symbol explicitly named in a claim."""
        known = {record.symbol for record in records if record.symbol}
        matches = {
            _normalize_symbol(match.group(0))
            for match in _CANONICAL_SYMBOL_RE.finditer(content)
            if _normalize_symbol(match.group(0)) in known
        }
        return next(iter(matches)) if len(matches) == 1 else None

    def _validate_price_provenance(
        self,
        content: str,
        records: Sequence[EvidenceRecord],
    ) -> list[dict[str, Any]]:
        """Require canonical symbol, actual source, and quote currency in output."""
        issues: list[dict[str, Any]] = []
        folded = content.casefold()
        symbols = sorted({record.symbol for record in records if record.symbol})
        # ``_scan_symbols`` canonicalizes, so an answer that writes Shanghai as
        # ``600519.SS`` still surfaces the ``600519.SH`` identity it names.
        written = _scan_symbols(content)
        mentioned = [
            symbol
            for symbol in symbols
            if symbol in written or symbol.casefold() in folded
        ]
        if not mentioned:
            issues.append(
                {
                    "code": "canonical_symbol_not_surfaced",
                    "symbols": symbols,
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "symbol_not_surfaced",
                    "message": (
                        "A price claim must surface its locked canonical symbol and "
                        "venue suffix."
                    ),
                }
            )
        target_symbols = set(mentioned or (symbols if len(symbols) == 1 else []))
        target_records = [
            record
            for record in records
            if not target_symbols or record.symbol in target_symbols
        ]

        sources = sorted(
            {
                record.source
                for record in target_records
                if record.source and record.source.casefold() not in {"auto", "unknown"}
            }
        )
        missing_sources = [
            source
            for source in sources
            if not any(
                alias in folded
                for alias in _SOURCE_ALIASES.get(source.casefold(), (source.casefold(),))
            )
        ]
        if missing_sources:
            issues.append(
                {
                    "code": "data_source_not_surfaced",
                    "sources": missing_sources,
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "source_not_surfaced",
                    "message": (
                        "Price claims must name the actual data source: "
                        + ", ".join(missing_sources)
                        + "."
                    ),
                }
            )

        currencies = sorted(
            {record.currency for record in target_records if record.currency and record.quote_unit != "points"}
        )
        missing_currencies = [
            currency
            for currency in currencies
            if not self._currency_is_surfaced(currency, content)
        ]
        if missing_currencies:
            issues.append(
                {
                    "code": "currency_not_surfaced",
                    "currencies": missing_currencies,
                    "value": None,
                    "role": None,
                    "span": None,
                    "symbol": None,
                    "reason": "currency_not_surfaced",
                    "message": (
                        "Price claims must name their quote currency: "
                        + ", ".join(missing_currencies)
                        + "."
                    ),
                }
            )
        return issues

    @staticmethod
    def _currency_is_surfaced(currency: str, content: str) -> bool:
        """Return whether a quote currency or an unambiguous alias is visible."""
        folded = content.casefold()
        code = currency.upper()
        tokens = _CURRENCY_ALIASES.get(code, (currency.casefold(),))
        if any(token.casefold() in folded for token in tokens):
            return True
        if code != "CNY":
            return False
        return any(
            char == "元"
            and (index == 0 or content[index - 1] not in _OTHER_CURRENCY_PREFIXES)
            for index, char in enumerate(content)
        )

    @staticmethod
    def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate validator findings while preserving order."""
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for issue in issues:
            key = json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        return unique


#: The role an undeclared figure is validated under (spec §4, undeclared mode).
_NO_DECLARATION = Declaration(
    index=0, value_text="", value=0.0, percent=False, role="observed", note="", ref=""
)
