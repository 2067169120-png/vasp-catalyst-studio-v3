"""Strict authored-HTML accessibility contracts for the WebView shell."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1]
    / "vcstudio"
    / "gui_web"
    / "assets"
    / "index.html"
)
UI_JS = INDEX.with_name("ui.js")
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class Node:
    def __init__(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        parent: "Node | None",
        line: int,
    ) -> None:
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.line = line
        self.children: list[Node] = []
        self.text: list[str] = []

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def ancestors(self):
        parent = self.parent
        while parent is not None:
            yield parent
            parent = parent.parent


class TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document", [], None, 0)
        self.stack = [self.root]
        self.nodes: list[Node] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = Node(tag.lower(), attrs, self.stack[-1], self.getpos()[0])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag.lower() not in VOID:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].text.append(data)


def _tree() -> TreeParser:
    parser = TreeParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _descendants(node: Node):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _is_authored_hidden(node: Node) -> bool:
    return "hidden" in node.attrs or node.attrs.get("type", "").lower() == "hidden"


def test_every_visible_authored_form_control_has_a_programmatic_name():
    tree = _tree()
    ids = Counter(
        node.attrs["id"] for node in tree.nodes if node.attrs.get("id")
    )
    explicit_labels = {
        node.attrs["for"]
        for node in tree.nodes
        if node.tag == "label" and node.attrs.get("for")
    }
    unnamed: list[str] = []

    for node in tree.nodes:
        if node.tag not in {"input", "select", "textarea"} or _is_authored_hidden(node):
            continue
        labelledby = (node.attrs.get("aria-labelledby") or "").split()
        has_name = bool(
            (node.attrs.get("aria-label") or "").strip()
            or (labelledby and all(ids[reference] == 1 for reference in labelledby))
            or node.attrs.get("id") in explicit_labels
            or any(ancestor.tag == "label" for ancestor in node.ancestors())
        )
        if not has_name:
            unnamed.append(
                f"line {node.line}: <{node.tag} id={node.attrs.get('id')!r}>"
            )

    assert not unnamed, "Unnamed visible form controls:\n" + "\n".join(unnamed)


def test_all_authored_id_references_resolve_to_one_unique_element():
    tree = _tree()
    ids = Counter(
        node.attrs["id"] for node in tree.nodes if node.attrs.get("id")
    )
    duplicates = sorted(identifier for identifier, count in ids.items() if count != 1)
    assert not duplicates, f"Duplicate authored IDs: {duplicates}"

    broken: list[str] = []
    for node in tree.nodes:
        references: list[tuple[str, str]] = []
        if node.tag == "label" and node.attrs.get("for"):
            references.append(("for", node.attrs["for"]))
        for attribute in (
            "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns"
        ):
            references.extend(
                (attribute, reference)
                for reference in (node.attrs.get(attribute) or "").split()
            )
        for attribute, reference in references:
            if ids[reference] != 1:
                broken.append(
                    f"line {node.line}: {node.tag}[{attribute}] -> {reference!r} "
                    f"({ids[reference]} matches)"
                )

    assert not broken, "Broken authored ID references:\n" + "\n".join(broken)


def test_each_physical_page_has_one_h1_and_never_skips_a_heading_level():
    tree = _tree()
    pages = [
        node
        for node in tree.nodes
        if node.tag == "section"
        and "page" in node.classes
        and node.attrs.get("data-page")
    ]
    assert pages

    failures: list[str] = []
    for page in pages:
        headings = [
            node
            for node in _descendants(page)
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
        ]
        h1s = [heading for heading in headings if heading.tag == "h1"]
        if len(h1s) != 1:
            failures.append(
                f"{page.attrs.get('id')}: expected one h1, found {len(h1s)}"
            )
        previous = 0
        for heading in headings:
            level = int(heading.tag[1])
            if previous and level > previous + 1:
                failures.append(
                    f"{page.attrs.get('id')} line {heading.line}: "
                    f"heading jumps h{previous} -> h{level}"
                )
            previous = level

    assert not failures, "Invalid page heading outlines:\n" + "\n".join(failures)


def test_legacy_visual_heading_classes_are_native_headings():
    tree = _tree()
    visual_heading_classes = {"acc-h", "gen-h", "set-h", "pj-h", "db-h"}
    offenders = [
        f"line {node.line}: <{node.tag} class={node.attrs.get('class')!r}>"
        for node in tree.nodes
        if node.classes & visual_heading_classes
        and node.tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}
    ]
    assert not offenders, "Pseudo-headings remain:\n" + "\n".join(offenders)


def test_explicit_text_translation_anchors_are_leaf_nodes():
    tree = _tree()
    offenders = [
        f"line {node.line}: <{node.tag} data-i18n={node.attrs.get('data-i18n')!r}>"
        for node in tree.nodes
        if node.attrs.get("data-i18n") and node.children
    ]
    assert not offenders, (
        "data-i18n on a container could delete or overwrite interactive descendants; "
        "move the key to a leaf span:\n" + "\n".join(offenders)
    )


def test_accordions_are_native_heading_button_disclosures():
    tree = _tree()
    accordions = [node for node in tree.nodes if node.attrs.get("data-acc")]
    assert accordions
    failures: list[str] = []

    for accordion in accordions:
        headings = [child for child in accordion.children if "acc-h" in child.classes]
        if len(headings) != 1 or headings[0].tag not in {"h2", "h3"}:
            failures.append(
                f"line {accordion.line}: accordion needs one direct h2/h3.acc-h"
            )
            continue
        buttons = [
            child
            for child in headings[0].children
            if child.tag == "button" and "acc-toggle" in child.classes
        ]
        if len(buttons) != 1 or buttons[0].attrs.get("type") != "button":
            failures.append(
                f"line {headings[0].line}: heading needs one direct button.acc-toggle"
            )

    assert not failures, "Invalid native accordion markup:\n" + "\n".join(failures)

    source = UI_JS.read_text(encoding="utf-8")
    assert "setAttribute('role', 'button')" not in source
    assert "setAttribute('tabindex', '0')" not in source
    assert "head.setAttribute('aria-controls'" in source
    assert "head.setAttribute('aria-expanded'" in source
    assert "'vcs-acc-' + idStem + '-panel-'" in source
