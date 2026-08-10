"""Tolerant, schema-agnostic XML reading for MegaGlest data files.

MegaGlest pack schemas vary between packs and versions, so files are read
generically into a :class:`XmlNode` tree with attribute/value accessors.
Known fields are mapped in :mod:`megaglest.civ_loader`; unknown fields are
recorded for logging rather than failing the conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from ..core.errors import ParseError


@dataclass
class XmlNode:
    """A generic XML element: tag, attributes, children and text."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[XmlNode] = field(default_factory=list)
    text: str = ""

    # -- structural access -------------------------------------------------
    def get(self, tag: str) -> XmlNode | None:
        """First direct child with ``tag``, or None."""
        for child in self.children:
            if child.tag == tag:
                return child
        return None

    def get_all(self, tag: str) -> list[XmlNode]:
        """All direct children with ``tag``."""
        return [child for child in self.children if child.tag == tag]

    def attr(self, name: str, default: str | None = None) -> str | None:
        """Attribute value by name."""
        return self.attrs.get(name, default)

    def path_attr(self) -> str | None:
        """The ``path`` attribute (asset references)."""
        return self.attr("path")

    def name_attr(self) -> str | None:
        """The ``name`` attribute (entity references)."""
        return self.attr("name")

    # -- value access ------------------------------------------------------
    def value(self, default: Any = None) -> Any:
        """The ``value`` attribute, else stripped text, else ``default``."""
        if "value" in self.attrs:
            return self.attrs["value"]
        return self.text or default

    def int_value(self, default: int | None = None) -> int | None:
        """``value`` parsed as int (lenient)."""
        raw = self.value()
        if raw is None:
            return default
        try:
            return int(str(raw))
        except ValueError:
            return default

    def float_value(self, default: float | None = None) -> float | None:
        """``value`` parsed as float (lenient)."""
        raw = self.value()
        if raw is None:
            return default
        try:
            return float(str(raw))
        except ValueError:
            return default

    def bool_value(self, default: bool | None = None) -> bool | None:
        """``value`` parsed as boolean (accepts true/false/1/0/yes/no)."""
        raw = self.value()
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        normalized = str(raw).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        return default

    def as_dict(self) -> dict[str, Any]:
        """Recursive plain-dict form (raw capture for downstream phases)."""
        result: dict[str, Any] = dict(self.attrs)
        if self.text:
            result["text"] = self.text
        children: dict[str, list[dict[str, Any]]] = {}
        for child in self.children:
            children.setdefault(child.tag, []).append(child.as_dict())
        if children:
            result["children"] = children
        return result


def parse_xml(path: Path) -> XmlNode:
    """Parse a MegaGlest XML file into an :class:`XmlNode` tree.

    Comments and processing instructions are dropped; text is stripped.
    Raises :class:`ParseError` on malformed XML or unreadable files.
    """
    parser = etree.XMLParser(remove_comments=True, remove_pis=True)
    try:
        tree = etree.parse(str(path), parser)
    except (etree.XMLSyntaxError, OSError) as exc:
        raise ParseError(f"Cannot parse XML file {path}: {exc}") from exc
    return _convert(tree.getroot())


def _convert(element: etree._Element) -> XmlNode:
    node = XmlNode(tag=element.tag, attrs=dict(element.attrib))
    node.text = (element.text or "").strip()
    node.children = [_convert(child) for child in element]
    return node
