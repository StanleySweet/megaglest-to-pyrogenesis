#!/usr/bin/env python3
"""Validate entity templates the way the engine does.

The 0 A.D. engine validates the *merged* template: the parent chain is
resolved (``CTemplateLoader::LoadTemplateFile``) and each layer's content is
applied over the accumulated base (``CParamNode::ApplyLayer``). This tool
replicates those two functions:

- ``foo|bar`` parents load ``bar`` (base) then ``foo`` (layer) on top;
  a file's own ``parent`` attribute is loaded before the file itself.
- ``ApplyLayer`` recurses into same-name children (deep merge). Only
  ``replace=""`` swaps wholesale; ``disable`` deletes the base child;
  ``merge`` keeps the base value and only merges children; ``op`` add/mul
  combines numerics; ``datatype="tokens"`` unions token lists (``-tok``
  removes); ``filtered`` keeps only layer-mentioned children. The special
  attributes are consumed and never appear in the merged document.
- Children live in a ``std::map``, so the merged document serializes its
  components sorted alphabetically -- matching the schema's sequence of
  optional components (why a valid raw file can still fail here: the
  public parent chain contributes required sub-elements, e.g. a leaf
  ``<Capturable/>`` only passes because the parent's ``CapturePoints``
  survives the recursive merge).

Usage::

    python tools/validate_templates_merged.py \
        --mod-dir /tmp/demo_a10_final/demo_a10 \
        --public "/Applications/0 A.D..app/Contents/Resources/data/mods/public/public.zip" \
        --rng /tmp/entity.rng
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from lxml import etree

_SKIP_ATTRS = frozenset({"replace", "op", "merge", "filtered"})


class Node:
    """Mirror of CParamNode: text value, attributes, name -> child map."""

    __slots__ = ("attrs", "children", "value")

    def __init__(self) -> None:
        self.value = ""
        self.attrs: dict[str, str] = {}
        self.children: dict[str, Node] = {}


def _iter_elements(parent: etree._Element):
    """Element children only (lxml keeps comments/PIs, whose tag isn't str)."""
    for child in parent:
        if isinstance(child.tag, str):
            yield child


def _element_to_node(element: etree._Element) -> Node:
    """Base-layer files are data: copy value, attrs and children verbatim."""
    node = Node()
    node.value = element.text or ""
    node.attrs = dict(element.attrib)
    for child in _iter_elements(element):
        node.children[child.tag] = _element_to_node(child)
    return node


def _apply_layer(node: Node | None, layer: etree._Element) -> Node | None:
    """Engine CParamNode::ApplyLayer: deep-merge one layer element.

    Returns None when the element must not exist in the merged doc
    (``disable``, or ``merge`` with no base child).
    """
    attrs = dict(layer.attrib)
    if "disable" in attrs:
        return None
    replacing = "replace" in attrs
    filtering = "filtered" in attrs
    merging = "merge" in attrs
    if merging and node is None:
        return None
    if replacing or node is None:
        node = Node()

    has_value = False
    # datatype="tokens": union the base and layer token lists (unless the
    # layer wholesale-replaces); "-token" entries remove.
    if attrs.get("datatype") == "tokens":
        old = [] if replacing else node.value.split()
        tokens = list(old)
        for token in (layer.text or "").split():
            if token.startswith("-"):
                victim = token[1:]
                if victim in tokens:
                    tokens.remove(victim)
            elif token not in old:
                tokens.append(token)
        node.value = " ".join(tokens)
        has_value = True
    op = attrs.get("op")
    if op:
        node.value = _apply_op(node.value, layer.text or "", op)
        has_value = True
    if not has_value and not merging:
        node.value = layer.text or ""

    for key, value in attrs.items():
        if key not in _SKIP_ATTRS:
            node.attrs[key] = value

    if filtering:
        # Only children the layer mentions survive.
        kept: dict[str, Node] = {}
        for child in _iter_elements(layer):
            merged = _apply_layer(node.children.get(child.tag), child)
            if merged is not None:
                kept[child.tag] = merged
        node.children = kept
    else:
        for child in _iter_elements(layer):
            merged = _apply_layer(node.children.get(child.tag), child)
            if merged is not None:
                node.children[child.tag] = merged
    return node


def _apply_op(base: str, mod: str, op: str) -> str:
    """Engine fixed arithmetic; non-numeric input degrades to the layer value."""
    try:
        old = float(base) if base else 0.0
        value = float(mod)
    except ValueError:
        return mod
    if op == "add":
        return _trim(old + value)
    if op == "mul":
        return _trim(old * value)
    if op == "mul_round":
        return _trim(round(old * value))
    return mod


def _trim(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _to_element(node: Node, tag: str) -> etree._Element:
    element = etree.Element(tag)
    for key in sorted(node.attrs):
        element.set(key, node.attrs[key])
    element.text = node.value or None
    for name in sorted(node.children):
        element.append(_to_element(node.children[name], name))
    return element


def _template_file(
    name: str,
    mod_templates: dict[str, Path],
    public_templates: dict[str, Path],
) -> Path | None:
    """CTemplateLoader search: special/filter -> mixins -> root, mod first."""
    for folder in ("special/filter/", "mixins/", ""):
        key = f"{folder}{name}"
        if key in mod_templates:
            return mod_templates[key]
        if key in public_templates:
            return public_templates[key]
    return None


def _load_root(path: Path, name: str) -> etree._Element:
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            member = next(info for info in zf.infolist() if info.filename.endswith(f"/{name}.xml"))
            return etree.fromstring(zf.read(member))
    return etree.parse(path).getroot()


def _load_into(
    node: Node,
    name: str,
    mod_templates: dict[str, Path],
    public_templates: dict[str, Path],
    depth: int,
) -> bool:
    """CTemplateLoader::LoadTemplateFile: resolve the chain onto ``node``."""
    if depth > 100:
        print(f"  probable inheritance loop at '{name}'", file=sys.stderr)
        return False
    if "|" in name:
        # 'foo|bar': bar is the parent/base, foo is layered on top.
        layer, base = name.split("|", 1)
        if not _load_into(node, base, mod_templates, public_templates, depth + 1):
            return False
        return _load_into(node, layer, mod_templates, public_templates, depth + 1)
    path = _template_file(name, mod_templates, public_templates)
    if path is None:
        print(f"  missing parent template '{name}'", file=sys.stderr)
        return False
    root = _load_root(path, name)
    parent = root.get("parent")
    if parent and not _load_into(node, parent, mod_templates, public_templates, depth + 1):
        return False
    merged = _apply_layer(node, root)
    # The root layer always applies; a None result is impossible here.
    assert merged is not None
    node.value = merged.value
    node.attrs = merged.attrs
    node.children = merged.children
    return True


def _index_templates(root: Path, public: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    mod_templates: dict[str, Path] = {}
    for path in sorted(root.rglob("*.xml")):
        mod_templates[str(path.relative_to(root)).removesuffix(".xml")] = path

    public_templates: dict[str, Path] = {}
    if public.suffix == ".zip":
        with zipfile.ZipFile(public) as zf:
            for info in zf.infolist():
                if info.filename.startswith("simulation/templates/") and info.filename.endswith(
                    ".xml"
                ):
                    name = info.filename.removeprefix("simulation/templates/").removesuffix(".xml")
                    public_templates[name] = public
    else:
        for path in sorted((public / "simulation/templates").rglob("*.xml")):
            public_templates[
                str(path.relative_to(public / "simulation/templates")).removesuffix(".xml")
            ] = path
    return mod_templates, public_templates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mod-dir", required=True, type=Path)
    parser.add_argument("--public", required=True, type=Path)
    parser.add_argument("--rng", required=True, type=Path)
    args = parser.parse_args()

    rng = etree.RelaxNG(etree.parse(args.rng))
    mod_templates, public_templates = _index_templates(
        args.mod_dir / "simulation/templates", args.public
    )

    invalid = 0
    for name in sorted(mod_templates):
        if name.startswith("template_"):
            continue
        node = Node()
        if not _load_into(node, name, mod_templates, public_templates, 0):
            invalid += 1
            print(f"INVALID {name} (unresolvable chain)")
            continue
        merged_xml = _to_element(node, "Entity")
        ok = rng.validate(merged_xml)
        if not ok:
            invalid += 1
            print(f"INVALID {name}")
            for err in rng.error_log:
                print(f"  {err.message}")
    total = len(mod_templates)
    print(f"{total} templates, {invalid} invalid (merged)")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
