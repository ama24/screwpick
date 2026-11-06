"""The page's script (web/index.html, plain JavaScript, no build step): it parses, and every
function it calls (also from onclick="..." in its templates) is defined. A call to a function
that is gone breaks the page at that line on every refresh."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

esprima = pytest.importorskip("esprima")

PAGE = Path(__file__).resolve().parents[1] / "src" / "sortering_pnp" / "web" / "index.html"
BUILTINS = set(
    [
        "fetch",
        "setTimeout",
        "setInterval",
        "clearInterval",
        "clearTimeout",
        "confirm",
        "alert",
        "prompt",
        "encodeURIComponent",
        "decodeURIComponent",
        "parseInt",
        "parseFloat",
        "isNaN",
        "String",
        "Number",
        "Boolean",
        "Array",
        "Object",
        "Math",
        "JSON",
        "Date",
        "Set",
        "Map",
        "Promise",
        "Error",
        "requestAnimationFrame",
        "atob",
        "btoa",
        "FormData",
        "Blob",
        "URL",
        "XMLHttpRequest",
        "structuredClone",
    ]
)
KEYWORDS = set(
    ["if", "for", "while", "switch", "return", "function", "catch", "typeof"]
)  # `if(` in onclick


def _names(tree) -> tuple[set[str], set[str]]:
    """(declared, called): function and variable names, and the plain names that are called."""
    declared: set[str] = set()
    called: set[str] = set()

    def pattern(p) -> None:
        if p is None:
            return
        if p.type == "Identifier":
            declared.add(p.name)
        elif p.type == "ArrayPattern":
            for e in p.elements:
                pattern(e)
        elif p.type == "ObjectPattern":
            for prop in p.properties:
                pattern(prop.value)
        elif p.type == "AssignmentPattern":
            pattern(p.left)
        elif p.type == "RestElement":
            pattern(p.argument)

    def walk(node) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not hasattr(node, "type"):
            return
        t = node.type
        if t == "FunctionDeclaration" and node.id:
            declared.add(node.id.name)
        if t in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"):
            for prm in node.params:
                pattern(prm)
        if t == "VariableDeclarator":
            pattern(node.id)
        if t == "CatchClause" and node.param:
            pattern(node.param)
        if t == "CallExpression" and node.callee.type == "Identifier":
            called.add(node.callee.name)
        for k, v in vars(node).items():
            if k not in ("type", "loc", "range") and (isinstance(v, list) or hasattr(v, "type")):
                walk(v)

    walk(tree.body)
    return declared, called


def test_the_page_script_parses_and_calls_only_what_exists():
    html = PAGE.read_text(encoding="utf-8")
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    declared, called = _names(esprima.parseScript(script))
    handlers = re.findall(r'on(?:click|input|change|load|submit|keyup|keydown)="([^"]*)"', html)
    for attr in handlers:
        called.update(re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", attr))
    assert len(declared) > 100  # the script was really read
    assert sorted(called - declared - BUILTINS - KEYWORDS) == []
