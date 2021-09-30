"""Reusable post-processing and type casting operations"""
import logging
from collections import UserList
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Generic,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

from .access import NOT_GIVEN, NotGiven, get_nested
from .toml_adapter import (
    Array,
    InlineTable,
    Item,
    Table,
    array,
    comment,
    inline_table,
    item,
    table,
)

CP = "#;"
"""Default Comment Prefixes"""

T = TypeVar("T")
S = TypeVar("S")
M = TypeVar("M", bound="MutableMapping")
KV = Tuple[str, T]
Scalar = Union[int, float, bool, str]  # TODO: missing time and datetime
CoerceFn = Callable[[str], T]
Transformation = Union[Callable[[str], Any], Callable[[M], M]]

_logger = logging.getLogger(__name__)


# ---- Intermediate representations ----
# These objects hold information about the processed values + comments
# in such a way that we can later convert them to TOML while still preserving
# the comments (if we want to).


@dataclass
class Commented(Generic[T]):
    value: Union[T, NotGiven] = field(default_factory=lambda: NOT_GIVEN)
    comment: Optional[str] = field(default_factory=lambda: None)

    def comment_only(self):
        return self.value is NOT_GIVEN

    def has_comment(self):
        return bool(self.comment)

    def value_or(self, fallback: S) -> Union[T, S]:
        return fallback if self.value is NOT_GIVEN else self.value

    def as_toml_obj(self, default_value="") -> Item:
        return create_item(self.value_or(default_value), self.comment)


class CommentedList(Generic[T], UserList):
    def __init__(self, data: List[Commented[List[T]]]):
        super().__init__(data)
        self.comment: Optional[str] = None  # TODO: remove this workaround

    def as_toml_obj(self) -> Array:
        out = array()
        multiline = len(self) > 1
        out.multiline(multiline)

        for entry in self.data:
            values = entry.value_or([])
            for value in values:
                cast(list, out).append(value)
            if not entry.has_comment():
                continue
            if not multiline:
                self.comment = entry.comment
                cast(Item, out).comment(entry.comment)
                return out
            if len(values) > 0:
                _add_comment_array_last_item(out, entry.comment)
            else:
                _add_comment_array_entire_line(out, entry.comment)

        return out


class CommentedKV(Generic[T], UserList):
    def __init__(self, data: List[Commented[List[KV[T]]]]):
        super().__init__(data)
        self.comment: Optional[str] = None  # TODO: remove this workaround

    def as_toml_obj(self) -> Union[Table, InlineTable]:
        multiline = len(self) > 1
        out: Union[Table, InlineTable] = table() if multiline else inline_table()

        for entry in self.data:
            values = (v for v in entry.value_or([cast(KV, ())]) if v)
            k: Optional[str] = None
            for value in values:
                k, v = value
                out[k] = v  # type: ignore
            if not entry.has_comment():
                continue
            if not multiline:
                out.comment(entry.comment)  # type: ignore
                self.comment = entry.comment
                return out
            if k:
                out[k].comment(entry.comment)
            else:
                out.append(None, comment(entry.comment))  # type: ignore
        return out


# ---- "Appliers" ----
# These functions are able to use transformations to modify the TOML object
# Internally, they know how to convert intermediate representations (Commented,
# CommentedKV, CommentedList, ...) into TOML values.


def apply(container: M, field: str, fn: Transformation) -> M:
    """Modify the TOML container by applying the transformation ``fn`` to the value
    stored under the ``field`` key.
    """
    value = container[field]
    try:
        processed = fn(value)
    except Exception:
        msg = f"Impossible to transform: {value} <{value.__class__.__name__}>"
        _logger.warning(msg)
        _logger.debug("Please check the following details", exc_info=True)
        return container
    return _add_to_container(container, field, processed)


def apply_nested(container: M, path: Sequence, fn: Transformation) -> M:
    *parent, last = path
    nested = get_nested(container, parent, None)
    if not nested:
        return container
    if not isinstance(nested, MutableMapping):
        msg = f"Cannot apply transformations to {nested} ({nested.__class__.__name__})"
        raise ValueError(msg)
    if last in nested:
        apply(nested, last, fn)
    return container


# ---- Simple value processors ----
# These functions return plain objects, that can be directly added to the TOML document


def noop(x: T) -> T:
    return x


def is_true(value: str) -> bool:
    value = value.lower()
    return value in ("true", "1", "yes", "on")


def is_false(value: str) -> bool:
    value = value.lower()
    return value in ("false", "0", "no", "off", "none", "null", "nil")


def is_float(value: str) -> bool:
    cleaned = value.lower().lstrip("+-").replace(".", "").replace("_", "")
    return cleaned.isdecimal() and value.count(".") <= 1 or cleaned in ("inf", "nan")


def coerce_bool(value: str) -> bool:
    if is_true(value):
        return True
    if is_false(value):
        return False
    raise ValueError(f"{value!r} cannot be converted to boolean")


def coerce_scalar(value: str) -> Scalar:
    value = value.strip()
    if value.isdecimal():
        return int(value)
    if is_float(value):
        return float(value)
    if is_true(value):
        return True
    elif is_false(value):
        return False
    # Do we need this? Or is there a better way?
    # > try:
    # >     return datetime.fromisoformat(value)
    # > except ValueError:
    # >     pass
    return value


def kebab_case(field: str) -> str:
    return field.lower().replace("_", "-")


# ---- Complex value processors ----
# These functions return an intermediate representation of the value,
# that need `apply` to be added to a container


@overload
def split_comment(value: str, *, comment_prefixes=CP) -> Commented[str]:
    ...


@overload
def split_comment(
    value: str, coerce_fn: CoerceFn[T], comment_prefixes=CP
) -> Commented[T]:
    ...


def split_comment(value, coerce_fn=noop, comment_prefixes=CP):
    if not isinstance(value, str):
        return value
    value = value.strip()
    prefixes = [p for p in comment_prefixes if p in value]

    # We just process inline comments for single line options
    if not prefixes or len(value.splitlines()) > 1:
        return Commented(coerce_fn(value))

    if any(value.startswith(p) for p in comment_prefixes):
        return Commented(NOT_GIVEN, _strip_comment(value, comment_prefixes))

    prefix = prefixes[0]  # We can only analyse one...
    value, cmt = _split_in_2(value, prefix)
    return Commented(coerce_fn(value.strip()), _strip_comment(cmt, comment_prefixes))


def split_scalar(value: str, *, comment_prefixes=CP) -> Commented[Scalar]:
    return split_comment(value, coerce_scalar, comment_prefixes)


@overload
def split_list(
    value: str, sep: str = ",", *, subsplit_dangling=True, comment_prefixes=CP
) -> CommentedList[str]:
    ...


@overload
def split_list(
    value: str,
    sep: str = ",",
    *,
    coerce_fn: CoerceFn[T],
    subsplit_dangling=True,
    comment_prefixes=CP,
) -> CommentedList[T]:
    ...


@overload
def split_list(
    value: str,
    sep: str,
    coerce_fn: CoerceFn[T],
    subsplit_dangling=True,
    comment_prefixes=CP,
) -> CommentedList[T]:
    ...


def split_list(
    value, sep=",", coerce_fn=noop, subsplit_dangling=True, comment_prefixes=CP
):
    """Value encoded as a (potentially) dangling list values separated by ``sep``.

    This function will first try to split the value by lines (dangling list) using
    :func:`str.splitlines`. Then, if ``subsplit_dangling=True``, it will split each line
    using ``sep``. As a result a list of items is obtained.
    For each item in this list ``coerce_fn`` is applied.
    """
    if not isinstance(value, str):
        return value
    comment_prefixes = comment_prefixes.replace(sep, "")

    values = value.strip().splitlines()
    if not subsplit_dangling and len(values) > 1:
        sep += "\n"  # force a pattern that cannot be found in a split line

    def _split(line: str) -> list:
        return [coerce_fn(v.strip()) for v in line.split(sep) if v]

    return CommentedList([split_comment(v, _split, comment_prefixes) for v in values])


@overload
def split_kv_pairs(
    value: str,
    key_sep: str = "=",
    *,
    pair_sep=",",
    subsplit_dangling=True,
    comment_prefixes=CP,
) -> CommentedKV[str]:
    ...


@overload
def split_kv_pairs(
    value: str,
    key_sep: str = "=",
    *,
    coerce_fn: CoerceFn[T],
    pair_sep=",",
    subsplit_dangling=True,
    comment_prefixes=CP,
) -> CommentedKV[T]:
    ...


@overload
def split_kv_pairs(
    value: str,
    key_sep: str,
    coerce_fn: CoerceFn[T],
    pair_sep=",",
    subsplit_dangling=True,
    comment_prefixes=CP,
) -> CommentedKV[T]:
    ...


def split_kv_pairs(
    value,
    key_sep="=",
    coerce_fn=noop,
    pair_sep=",",
    subsplit_dangling=True,
    comment_prefixes=CP,
):
    """Value encoded as a (potentially) dangling list of key-value pairs.

    This function will first try to split the value by lines (dangling list) using
    :func:`str.splitlines`. Then, if ``subsplit_dangling=True``, it will split each line
    using ``pair_sep``. As a result a list of key-value pairs is obtained.
    For each item in this list, the key is separated from the value by ``key_sep``.
    ``coerce_fn`` is used to convert the value in each pair.
    """
    comment_prefixes = comment_prefixes.replace(key_sep, "")
    comment_prefixes = comment_prefixes.replace(pair_sep, "")

    values = value.strip().splitlines()
    if not subsplit_dangling and len(values) > 1:
        pair_sep += "\n"  # force a pattern that cannot be found in a split line

    def _split_kv(line: str) -> List[KV]:
        pairs = (
            item.split(key_sep, maxsplit=1)
            for item in line.strip().split(pair_sep)
            if key_sep in item
        )
        return [(k.strip(), coerce_fn(v.strip())) for k, v in pairs]

    return CommentedKV([split_comment(v, _split_kv, comment_prefixes) for v in values])


# ---- Access Helpers ----


# ---- Public Helpers ----


def create_item(value, comment):
    obj = item(value)
    if comment is not None:
        obj.comment(comment)
    return obj


# ---- Private Helpers ----


def _split_in_2(v: str, sep: str) -> Tuple[str, Optional[str]]:
    items = iter(v.split(sep, maxsplit=1))
    first = next(items)
    second = next(items, None)
    return first, second


def _strip_comment(msg: Optional[str], prefixes: str = CP) -> Optional[str]:
    if not msg:
        return None
    return msg.strip().lstrip(prefixes).strip()


def _add_comment_array_last_item(toml_array: Array, cmt: str):
    # Workaround for bug in tomlkit, it should be: toml_array[-1].comment(cmt)
    # TODO: Remove when tomlkit fixes it
    last = toml_array[-1]
    last.comment(cmt)
    trivia = last.trivia
    # begin workaround -->
    _before_patch = last.as_string
    last.as_string = lambda: _before_patch() + "," + trivia.comment_ws + trivia.comment


def _add_comment_array_entire_line(toml_array: Array, cmt_msg: str):
    # Workaround for bug in tomlkit, it should be: toml_array.append(comment(cmt))
    # TODO: Remove when tomlkit fixes it
    cmt = comment(cmt_msg)
    cmt.trivia.trail = ""
    cmt.__dict__["value"] = cmt.as_string()
    cast(list, toml_array).append(cmt)


def _add_to_container(container: M, field: str, value: Any) -> M:
    # Add a value to a TOML container
    if not hasattr(value, "as_toml_obj"):
        container[field] = value
        return container

    obj: Item = value.as_toml_obj()
    container[field] = obj
    if (
        hasattr(value, "comment")
        and value.comment is not None
        and hasattr(obj, "comment")
    ):
        # BUG: we should not need to repeat the comment
        obj.comment(value.comment)
    return container
