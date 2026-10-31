"""Objective-C parser plugin for okf-generator (tree-sitter-objc).

Extracts: @interface / @implementation (Class), categories (Class "Foo+Cat"),
@protocol (Interface), method definitions (Function titled by full selector),
C functions, properties (fields), #import/@import (imports) and message sends
(calls, as full selectors so the okf linker can match them to method titles).
"""
from __future__ import annotations

import re

from okf.parsers.base import TreeSitterParser, _find_all, _node_text, _prev_comment, _safe_id

__all__ = ["ObjCParser", "scrub"]

# Macros and qualifiers that tree-sitter-objc cannot parse. Each match is replaced by
# spaces of equal length so every byte offset and line number is unchanged.
_SCRUB_PATTERNS = [re.compile(p) for p in (
    rb"NS_ASSUME_NONNULL_BEGIN", rb"NS_ASSUME_NONNULL_END",
    rb"NS_SWIFT_NAME\([^)]*\)", rb"NS_SWIFT_UI_ACTOR", rb"NS_REFINED_FOR_SWIFT", rb"NS_SWIFT_NOTHROW",
    rb"API_AVAILABLE\([^)]*\)", rb"API_UNAVAILABLE\([^)]*\)", rb"API_DEPRECATED\([^)]*\)",
    rb"NS_AVAILABLE_IOS\([^)]*\)", rb"NS_DEPRECATED_IOS\([^)]*\)", rb"NS_CLASS_AVAILABLE_IOS\([^)]*\)",
    rb"NS_ENUM_AVAILABLE_IOS\([^)]*\)", rb"NS_EXTENSION_UNAVAILABLE_IOS\([^)]*\)",
    rb"NS_DESIGNATED_INITIALIZER", rb"NS_UNAVAILABLE", rb"NS_REQUIRES_SUPER", rb"NS_NOESCAPE",
    rb"NS_REQUIRES_NIL_TERMINATION", rb"NS_RETURNS_RETAINED", rb"NS_RETURNS_NOT_RETAINED",
    rb"DEPRECATED_MSG_ATTRIBUTE\([^)]*\)", rb"DEPRECATED_ATTRIBUTE", rb"__deprecated_msg\([^)]*\)",
    rb"UI_APPEARANCE_SELECTOR", rb"IB_DESIGNABLE", rb"IBInspectable",
    rb"__attribute__\(\([^)]*\)\)",
    rb"\b_Nonnull\b", rb"\b_Nullable\b", rb"\b_Null_unspecified\b",
    rb"\bnullable\b", rb"\bnonnull\b", rb"\bnull_unspecified\b", rb"\bnull_resettable\b",
)]


def scrub(src: bytes) -> bytes:
    """Blank out unparseable macros, preserving length."""
    for pat in _SCRUB_PATTERNS:
        src = pat.sub(lambda m: b" " * len(m.group(0)), src)
    return src


class _ScrubbingParser:
    def __init__(self, inner):
        self._inner = inner

    def parse(self, src: bytes):
        return self._inner.parse(scrub(src))


def _doc(node, src_bytes: bytes) -> str:
    """Preceding comment, stripped: okf's _first_line() crashes on whitespace-only docs."""
    return (_prev_comment(node, src_bytes) or "").strip()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _selector(node) -> str:
    """Build 'sel' or 'sel:with:' from a method_definition / method_declaration."""
    parts = []
    kids = node.children
    for i, ch in enumerate(kids):
        if ch.type != "identifier":
            continue
        # The identifier right after the return type is the first selector piece;
        # identifiers after a method_parameter are further pieces.
        prev = kids[i - 1].type if i > 0 else ""
        if prev not in ("method_type", "method_parameter", "-", "+"):
            continue
        nxt = kids[i + 1].type if i + 1 < len(kids) else ""
        parts.append(_node_text(ch) + (":" if nxt == "method_parameter" else ""))
    return "".join(parts)


def _method_sig(node) -> str:
    stop = None
    for ch in node.children:
        if ch.type in ("compound_statement", ";"):
            stop = ch
            break
    text = node.text[: (stop.start_byte - node.start_byte)] if stop else node.text
    return _collapse(text.decode("utf-8", "replace"))


def _message_selector(msg) -> str:
    parts = []
    kids = msg.children
    for i, ch in enumerate(kids):
        if msg.field_name_for_child(i) != "method":
            continue
        nxt = kids[i + 1].type if i + 1 < len(kids) else ""
        parts.append(_node_text(ch) + (":" if nxt == ":" else ""))
    return "".join(parts)


def _class_name_and_category(node):
    """For class_interface / class_implementation → (name, category|None, is_anonymous)."""
    name = None
    category = node.child_by_field_name("category")
    saw_paren = False
    for ch in node.children:
        if ch.type == "identifier" and name is None:
            name = _node_text(ch)
        elif ch.type == "(":
            saw_paren = True
    cat = _node_text(category) if category is not None else None
    return name, cat, (saw_paren and cat is None)


def _class_title(name: str, cat: str | None) -> str:
    return f"{name}+{cat}" if cat else name


class ObjCParser(TreeSitterParser):
    LANGUAGE   = "objc"
    EXTENSIONS = {".m", ".mm", ".h"}
    _TS_MODULE = "tree_sitter_objc"
    _lang_obj  = None

    def _parser(self):
        return _ScrubbingParser(super()._parser())

    # -- module level ------------------------------------------------------

    def _module_doc(self, root, src_bytes: bytes) -> str:
        for child in root.children:
            if child.type == "comment":
                return _doc(child.next_sibling or child, src_bytes) or _node_text(child).lstrip("/ *").strip()
            if child.type not in ("preproc_include", "module_import"):
                break
        return ""

    def _collect_imports(self, root, src_bytes: bytes) -> list[str]:
        names = []
        for node in _find_all(root, "preproc_include", "module_import"):
            path = node.child_by_field_name("path")
            if path is None:
                continue
            text = _node_text(path).strip().strip('<>"')
            if text:
                names.append(text)
        return names

    def _collect_calls(self, node, src_bytes: bytes) -> list[str]:
        names = []
        for msg in _find_all(node, "message_expression"):
            sel = _message_selector(msg)
            if sel:
                names.append(sel)
            recv = msg.child_by_field_name("receiver")
            if recv is not None and recv.type == "identifier":
                r = _node_text(recv)
                if r[:1].isupper() and r not in ("NSObject",):
                    names.append(r)          # class reference → links to the Class concept
        for call in _find_all(node, "call_expression"):
            fn = call.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                names.append(_node_text(fn))
        return names

    # -- symbols -----------------------------------------------------------

    def _parse_symbols(self, root, src_bytes, resource, ts, parent_id):
        concepts = []
        for node in _find_all(root, "class_interface", "class_implementation",
                              "protocol_declaration", "function_definition"):
            if node.type == "class_interface":
                concepts.extend(self._interface(node, src_bytes, resource, ts, parent_id))
            elif node.type == "class_implementation":
                concepts.extend(self._implementation(node, src_bytes, resource, ts, parent_id))
            elif node.type == "protocol_declaration":
                concepts.extend(self._protocol(node, src_bytes, resource, ts, parent_id))
            elif node.type == "function_definition":
                c = self._c_function(node, src_bytes, resource, ts, parent_id)
                if c:
                    concepts.append(c)
        return concepts

    def _interface(self, node, src_bytes, resource, ts, parent_id):
        name, cat, anonymous = _class_name_and_category(node)
        if not name or anonymous:          # `@interface Foo ()` class extension: private, skip
            return []
        bases = []
        sup = node.child_by_field_name("superclass")
        if sup is not None:
            bases.append(_node_text(sup))
        for pa in (c for c in node.children if c.type == "parameterized_arguments"):
            bases += [_node_text(t) for t in _find_all(pa, "type_identifier")]
        sig = f"@interface {name}" + (f" ({cat})" if cat else "") + (f" : {bases[0]}" if sup is not None else "")
        if len(bases) > (1 if sup is not None else 0):
            sig += " <" + ", ".join(bases[1 if sup is not None else 0:]) + ">"
        methods = [_selector(m) for m in node.children if m.type == "method_declaration"]
        cc = self._make_concept("Class", _class_title(name, cat), _doc(node, src_bytes), sig,
                                resource, ts, parent_id, node.start_point[0] + 1,
                                methods=[m for m in methods if m], node=None)
        cc.source_lines = (node.start_point[0] + 1, node.end_point[0] + 1)
        cc.inheritance = bases
        cc.fields = self._properties(node)
        return [cc]

    def _protocol(self, node, src_bytes, resource, ts, parent_id):
        name = next((_node_text(c) for c in node.children if c.type == "identifier"), None)
        if not name:
            return []
        bases = []
        for prl in (c for c in node.children if c.type == "protocol_reference_list"):
            bases += [_node_text(i) for i in prl.children if i.type == "identifier"]
        sig = f"@protocol {name}" + (" <" + ", ".join(bases) + ">" if bases else "")
        methods = [_selector(m) for m in node.children if m.type == "method_declaration"]
        cc = self._make_concept("Interface", name, _doc(node, src_bytes), sig,
                                resource, ts, parent_id, node.start_point[0] + 1,
                                methods=[m for m in methods if m], node=None)
        cc.source_lines = (node.start_point[0] + 1, node.end_point[0] + 1)
        cc.inheritance = bases
        cc.fields = self._properties(node)
        return [cc]

    def _implementation(self, node, src_bytes, resource, ts, parent_id):
        name, cat, _ = _class_name_and_category(node)
        if not name:
            return []
        title = _class_title(name, cat)
        class_id = f"{re.sub(r'\.[^/]+$', '', resource).replace(chr(92), '/')}/{_safe_id(title)}"
        out, methods = [], []
        for defn in _find_all(node, "method_definition"):
            sel = _selector(defn)
            if not sel:
                continue
            methods.append(sel)
            fc = self._make_concept("Function", sel, _doc(defn, src_bytes), _method_sig(defn),
                                    resource, ts, class_id, defn.start_point[0] + 1,
                                    node=defn, src_bytes=src_bytes)
            out.append(fc)
        cc = self._make_concept("Class", title, _doc(node, src_bytes),
                                f"@implementation {name}" + (f" ({cat})" if cat else ""),
                                resource, ts, parent_id, node.start_point[0] + 1,
                                methods=methods, node=None)
        cc.source_lines = (node.start_point[0] + 1, node.end_point[0] + 1)
        return [cc] + out

    def _c_function(self, node, src_bytes, resource, ts, parent_id):
        decl = node.child_by_field_name("declarator")
        while decl is not None and decl.type in ("pointer_declarator", "parenthesized_declarator"):
            decl = decl.child_by_field_name("declarator") or next((c for c in decl.children if c.type.endswith("declarator")), None)
        if decl is None or decl.type != "function_declarator":
            return None
        name = _node_text(decl.child_by_field_name("declarator") or decl)
        if not name:
            return None
        params = decl.child_by_field_name("parameters")
        ret = _node_text(node.child_by_field_name("type"))
        sig = f"{ret + ' ' if ret else ''}{name}({_node_text(params).strip('()') if params else ''})"
        return self._make_concept("Function", name, _doc(node, src_bytes), _collapse(sig),
                                  resource, ts, parent_id, node.start_point[0] + 1,
                                  node=node, src_bytes=src_bytes)

    @staticmethod
    def _properties(node) -> list[dict]:
        props = []
        for p in (c for c in node.children if c.type == "property_declaration"):
            attrs = [_node_text(a) for a in _find_all(p, "property_attribute")]
            decl = next((c for c in p.children if c.type == "struct_declaration"), None)
            if decl is None:
                continue
            idents = [n for n in _find_all(decl, "identifier")]
            pname = _node_text(idents[-1]) if idents else ""
            ptype = _collapse(_node_text(decl).rstrip(";"))
            if pname and ptype.endswith(pname):
                ptype = ptype[: -len(pname)].strip()
            props.append({"name": pname, "type": ptype, "visibility": ", ".join(attrs)})
        return props
