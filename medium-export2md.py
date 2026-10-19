#!/usr/bin/env python3
"""
medium-export2md - turn the official Medium export (.zip) into Markdown for Hugo or Jekyll.

Medium lets you download all your stories as HTML (Settings > Security and apps >
Download your information). This script reads that zip (or the extracted folder),
keeps only your stories, cleans Medium's markup and writes one Markdown file per
post with front matter, downloading the images next to it.

Only the Python standard library is needed (3.9+).

Examples:
  python3 medium-export2md.py medium-export.zip -o ~/my-hugo-site
  python3 medium-export2md.py medium-export.zip -o ~/my-hugo-site --lang-codes pt=pt-br,en=en
  python3 medium-export2md.py medium-export.zip -o ~/my-jekyll-site --format jekyll --include-drafts
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

__version__ = "1.0.0"

# Private-use characters used as markers while building the Markdown.
PH_OPEN, PH_CLOSE, HARD_BREAK = "\ue000", "\ue001", "\ue002"

# --------------------------------------------------------------------------- HTML tree

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}


class Node:
    """Minimal DOM node, enough for Medium's well-formed export HTML."""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    def classes(self):
        return self.attrs.get("class", "").split()

    def has_class(self, name):
        return name in self.classes()

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag=None, cls=None, attr=None):
        found = []
        for node in self.iter():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and not node.has_class(cls):
                continue
            if attr is not None and node.attrs.get(attr[0]) != attr[1]:
                continue
            found.append(node)
        return found

    def find(self, tag=None, cls=None, attr=None):
        matches = self.find_all(tag, cls, attr)
        return matches[0] if matches else None

    def text(self):
        out = []
        for child in self.children:
            if isinstance(child, str):
                out.append(child)
            elif child.tag == "br":
                out.append("\n")
            else:
                out.append(child.text())
        return "".join(out)

    def copy_with(self, children):
        clone = Node(self.tag, self.attrs, self.parent)
        clone.children = children
        return clone


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, [(k, v or "") for k, v in attrs], self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, [(k, v or "") for k, v in attrs], self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(text):
    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- small helpers

def norm_ws(text):
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "post"


def yaml_str(value):
    return json.dumps(value, ensure_ascii=False)


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def first_sentences(text, soft=80, hard=160):
    """Short description from a paragraph: whole sentences, roughly 80-160 chars."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = ""
    for sentence in sentences:
        if out and len(out) >= soft and len(out) + 1 + len(sentence) > hard:
            break
        out = f"{out} {sentence}".strip()
        if len(out) >= soft and len(out) > hard:
            break
    if len(out) > 300:
        out = out[:297].rsplit(" ", 1)[0] + "\u2026"
    return out


MEDIUM_ID_RE = re.compile(r"([0-9a-f]{8,16})$")
TRACKING_PARAMS = {"source", "sk", "sharedUserId", "gi", "postPublishedType", "utm_source",
                   "utm_medium", "utm_campaign"}


def clean_href(href, visible_text):
    href = href.strip()
    # Medium auto-links sometimes swallow the sentence's final punctuation.
    while href and href[-1] in ".,;:!?" and not visible_text.rstrip().endswith(href[-1]):
        href = href[:-1]
    parts = urllib.parse.urlsplit(href)
    if parts.netloc.endswith("medium.com") and parts.query:
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if k not in TRACKING_PARAMS]
        href = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    return href


# --------------------------------------------------------------------------- languages

STOPWORDS = {
    "pt": set("não você uma com os isso também são ao pela pelo nas nos seu sua mas ou já muito "
              "está do da dos das em um que para como mais quando sem".split()),
    "en": set("the and of to is in that it for you with on this are be as your not we can "
              "from by have will what when".split()),
    "es": set("el los las del con una también pero muy está su sus y en un es lo al que para "
              "como más cuando sin".split()),
}


def detect_language(text, fallback):
    words = re.findall(r"[a-záàâãéêíóôõúçñ]+", text.lower())
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else fallback


# --------------------------------------------------------------------------- code blocks

LANG_LABELS = {
    "yaml": "yaml", "yml": "yaml", "bash": "bash", "shell": "bash", "sh": "bash", "zsh": "bash",
    "console": "console", "terminal": "bash", "json": "json", "python": "python", "py": "python",
    "go": "go", "golang": "go", "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript", "sql": "sql", "xml": "xml", "html": "html",
    "css": "css", "dockerfile": "dockerfile", "docker": "dockerfile", "ini": "ini",
    "toml": "toml", "java": "java", "rust": "rust", "ruby": "ruby", "php": "php",
    "powershell": "powershell", "ps1": "powershell", "makefile": "makefile", "hcl": "hcl",
    "terraform": "hcl", "nginx": "nginx", "diff": "diff", "c": "c", "c++": "cpp", "cpp": "cpp",
    "c#": "csharp", "csharp": "csharp", "kotlin": "kotlin", "groovy": "groovy",
    "jinja": "jinja", "jinja2": "jinja", "text": "text", "plaintext": "text",
    "plain text": "text", "code snippet": "", "code": "",
}
# Medium's "auto" highlighting (data-code-block-mode=1) guesses; only trust common guesses.
TRUSTED_AUTO_LANGS = {"yaml", "json", "bash", "shell", "python", "go", "javascript",
                      "typescript", "sql", "xml", "dockerfile", "ini", "toml", "java", "rust",
                      "ruby", "php", "powershell", "makefile", "nginx", "diff", "cpp", "csharp",
                      "kotlin", "groovy", "html"}


SHELL_COMMAND = re.compile(
    r"^(\$ |(sudo|podman|docker|oc|kubectl|dnf|yum|apt|apt-get|curl|wget|git|cd|ls|cat|echo|"
    r"export|helm|ansible|ansible-playbook|govc|systemctl|journalctl|ssh|scp|pip|pip3|npm|make|"
    r"mkdir|chmod|chown|cp|mv|rm|tar|grep|find|sed|awk|openssl|virsh|nmcli|firewall-cmd|"
    r"setsebool|semanage|ip|ping|dig|watch|for|while|source|terraform|aws|az|gcloud)(\s|$))")


def looks_like_shell(code):
    lines = [l.strip() for l in code.splitlines() if l.strip() and not l.strip().startswith("#")]
    return bool(lines) and bool(SHELL_COMMAND.match(lines[0]))


def code_language(pre, label, code):
    if label is not None:
        return LANG_LABELS[label] or "text"
    mode = pre.attrs.get("data-code-block-mode", "")
    lang = pre.attrs.get("data-code-block-lang", "").lower()
    if mode == "2" and lang:          # chosen by the author
        return lang
    if looks_like_shell(code):        # Medium's auto-detection calls shell "lua", "cpp"...
        return "bash"
    if mode == "1" and lang in TRUSTED_AUTO_LANGS:
        return lang
    return "text"


def fence_for(code):
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
    return "`" * (longest + 1)


# --------------------------------------------------------------------------- data model

@dataclass
class Post:
    source: str
    medium_id: str
    title: str
    export_subtitle: str
    body: Node | None
    published: datetime | None
    canonical: str
    is_draft: bool
    is_response: bool
    export_date: datetime | None
    lang: str = "en"
    slug: str = ""
    description: str = ""
    selected: bool = False
    tags: list = field(default_factory=list)
    bundle: "Bundle | None" = None
    out_file: Path | None = None
    warnings: list = field(default_factory=list)

    @property
    def date(self):
        return self.published or self.export_date or datetime.now(timezone.utc)


@dataclass
class Bundle:
    """One Hugo page bundle: a post plus its translations, sharing the images."""
    posts: list
    dir_name: str = ""
    images: dict = field(default_factory=dict)   # medium image id -> file name
    files: dict = field(default_factory=dict)    # relative path -> bytes


def load_posts(export):
    files = {}
    if export.is_dir():
        folder = export / "posts" if (export / "posts").is_dir() else export
        for path in sorted(folder.glob("*.html")):
            files[path.name] = path.read_text(encoding="utf-8", errors="replace")
    else:
        with zipfile.ZipFile(export) as zf:
            for info in zf.infolist():
                match = re.search(r"(?:^|/)posts/([^/]+\.html)$", info.filename)
                if match and not info.is_dir():
                    files[match.group(1)] = zf.read(info).decode("utf-8", errors="replace")
    return [parse_post(name, html) for name, html in sorted(files.items())]


def parse_post(name, html):
    root = parse_html(html)
    h1 = root.find("h1", "p-name")
    title = norm_ws(h1.text()) if h1 else norm_ws((root.find("title") or Node("x")).text())
    subtitle = root.find("section", attr=("data-field", "subtitle"))
    body = root.find("section", attr=("data-field", "body"))
    time = root.find("time", "dt-published")
    canonical = root.find("a", "p-canonical")
    footer = root.find("footer")
    export_date = None
    if footer:
        match = re.search(r"Exported from Medium on ([A-Z][a-z]+ \d{1,2}, \d{4})", norm_ws(footer.text()))
        if match:
            export_date = datetime.strptime(match.group(1), "%B %d, %Y").replace(hour=12, tzinfo=timezone.utc)
    stem = re.sub(r"\.html$", "", name)
    match = MEDIUM_ID_RE.search(stem.rsplit("-", 1)[-1])
    canonical_url = canonical.attrs.get("href", "") if canonical else ""
    if not match and canonical_url:
        match = MEDIUM_ID_RE.search(canonical_url.rstrip("/").rsplit("-", 1)[-1].rsplit("/", 1)[-1])
    published = parse_iso(time.attrs.get("datetime")) if time else None
    return Post(
        source=name,
        medium_id=match.group(1) if match else slugify(stem),
        title=title,
        export_subtitle=norm_ws(subtitle.text()) if subtitle else "",
        body=body,
        published=published,
        canonical=canonical_url,
        is_draft=name.startswith("draft_") or published is None,
        is_response=body is None or body.find(cls="graf--title") is None,
        export_date=export_date,
    )


def collect_blocks(body):
    """Medium body -> list of (section index, block node)."""
    blocks = []
    sections = [n for n in body.children if isinstance(n, Node) and n.tag == "section"]
    for index, section in enumerate(sections):
        for inner in section.iter():
            if inner.has_class("section-inner"):
                blocks.extend((index, child) for child in inner.children if isinstance(child, Node))
    if not blocks:  # unexpected layout: fall back to every top-level "graf"
        blocks = [(0, n) for n in body.iter() if n.has_class("graf")]
    return blocks


def post_slug(post):
    if post.canonical:
        last = urllib.parse.urlsplit(post.canonical).path.rstrip("/").rsplit("/", 1)[-1]
        last = re.sub(r"-?[0-9a-f]{8,16}$", "", last)
        if last and not post.canonical.rstrip("/").endswith("/p/" + post.medium_id):
            return slugify(last)
    return slugify(post.title)


def linked_post_ids(node, ids):
    found = []
    for anchor in node.find_all("a"):
        href = anchor.attrs.get("href", "") or anchor.attrs.get("data-href", "")
        for other in ids:
            if other in href and other not in found:
                found.append(other)
    return found


# --------------------------------------------------------------------------- images

class ImageFetcher:
    USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) medium-export2md/{__version__}"
    EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                  "image/webp": ".webp", "image/svg+xml": ".svg"}

    def __init__(self, enabled):
        self.enabled = enabled
        self.cache = {}

    def fetch(self, image_id, width, src):
        """Returns (bytes, extension) or raises the last error."""
        key = image_id or src
        if key in self.cache:
            return self.cache[key]
        candidates = []
        if image_id:
            size = min(int(width or 2000), 4000)
            candidates += [f"https://miro.medium.com/v2/resize:fit:{size}/{image_id}",
                           f"https://cdn-images-1.medium.com/max/{size}/{image_id}"]
        if src:
            candidates.append(src)
        error = None
        for url in candidates:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read()
                    ctype = response.headers.get_content_type()
                ext = Path(image_id or src).suffix.lower()
                if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
                    ext = self.EXTENSIONS.get(ctype, ".jpg")
                self.cache[key] = (data, ext)
                return data, ext
            except (urllib.error.URLError, OSError, ValueError) as exc:
                error = exc
        raise error or OSError("no image URL")


# --------------------------------------------------------------------------- Markdown

EMPHASIS = {"em", "i"}
STRONG = {"strong", "b"}


class MarkdownWriter:
    """Converts one Medium post body into Markdown for the chosen generator."""

    def __init__(self, post, ctx):
        self.post = post
        self.ctx = ctx
        self.fmt = ctx.fmt
        self.placeholders = []

    # -- placeholders protect generator syntax (shortcodes, Liquid) from escaping
    def keep(self, text):
        self.placeholders.append(text)
        return f"{PH_OPEN}{len(self.placeholders) - 1}{PH_CLOSE}"

    # -- text escaping
    @staticmethod
    def escape(text):
        text = text.replace("\\", "\\\\")
        text = re.sub(r"([*`])", r"\\\1", text)
        text = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", text)
        text = re.sub(r"<(?=[A-Za-z/!?])", r"\\<", text)
        text = re.sub(r"&(?=#?\w+;)", "&amp;", text)
        text = text.replace("~~", "\\~\\~").replace("$$", "\\$\\$")
        if "](" in text or "][" in text:
            text = text.replace("[", "&#91;")
        return text

    @staticmethod
    def escape_line_starts(text):
        def fix(line):
            if re.match(r"(#{1,6}|>|[-+*])(\s|$)|[=-]+\s*$|(\*\s*){3,}$|(_\s*){3,}$", line):
                return "\\" + line
            return re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", line)
        return "\n".join(fix(line) for line in text.split("\n"))

    # -- inline content
    @staticmethod
    def merge_siblings(children):
        merged = []
        for child in children:
            prev = merged[-1] if merged else None
            if (isinstance(child, Node) and isinstance(prev, Node) and child.tag == prev.tag
                    and child.tag in {"strong", "b", "em", "i", "code", "a"}
                    and child.attrs.get("href") == prev.attrs.get("href")):
                merged[-1] = prev.copy_with(prev.children + child.children)
            else:
                merged.append(child)
        return merged

    @classmethod
    def fully_in(cls, node, tags):
        """True if every visible character of node sits inside one of `tags`."""
        if isinstance(node, str):
            return not node.strip()
        if node.tag in tags:
            return True
        if node.tag == "br":
            return True
        content = [c for c in node.children if not (isinstance(c, str) and not c.strip())]
        return bool(content) and all(cls.fully_in(c, tags) for c in content)

    @classmethod
    def unwrap(cls, node, tags):
        if isinstance(node, str):
            return [node]
        children = []
        for child in node.children:
            children.extend(cls.unwrap(child, tags))
        if node.tag in tags:
            return children
        return [node.copy_with(children)]

    @staticmethod
    def wrap(marker, inner):
        match = re.match(r"^(\s*)(.*?)(\s*)$", inner, re.S)
        lead, core, trail = match.groups()
        if not core.strip(HARD_BREAK):
            return inner
        return f"{lead}{marker}{core}{marker}{trail}"

    def inline(self, children, plain_headings=False):
        children = self.merge_siblings(children)
        # Hoist emphasis that covers a whole run (Medium splits it around code spans).
        elements = [c for c in children if isinstance(c, Node) and c.tag != "br"]
        if len(elements) > 1:
            for tags, marker in ((STRONG, "**"), (EMPHASIS, "*")):
                if all(self.fully_in(c, tags) for c in children):
                    stripped = []
                    for child in children:
                        stripped.extend(self.unwrap(child, tags))
                    inner = self.inline(stripped, plain_headings)
                    return inner if (plain_headings and tags is STRONG) else self.wrap(marker, inner)
        out = []
        for child in children:
            if isinstance(child, str):
                out.append(self.escape(re.sub(r"[ \t\r\n]+", " ", child.replace("\u00a0", " "))))
            else:
                out.append(self.inline_node(child, plain_headings))
        return "".join(out)

    def inline_node(self, node, plain_headings=False):
        tag = node.tag
        if tag == "br":
            return HARD_BREAK
        if tag in STRONG:
            inner = self.inline(node.children, plain_headings)
            return inner if plain_headings else self.wrap("**", inner)
        if tag in EMPHASIS:
            return self.wrap("*", self.inline(node.children, plain_headings))
        if tag == "code":
            return self.inline_code(node)
        if tag == "a":
            return self.link(node, plain_headings)
        if tag == "img":
            return self.image(node, "")
        return self.inline(node.children, plain_headings)

    @staticmethod
    def code_span(text):
        text = re.sub(r"\s*\n\s*", " ", text.replace("\u00a0", " "))
        if not text.strip():
            return text
        if "`" not in text:
            return f"`{text}`"
        ticks = "`" * (max(len(m) for m in re.findall(r"`+", text)) + 1)
        return f"{ticks} {text} {ticks}"

    def inline_code(self, node):
        if node.find("a"):
            out = []
            for child in self.merge_siblings(node.children):
                if isinstance(child, Node) and child.tag == "a":
                    href = self.href(child)
                    out.append(f"[{self.code_span(child.text())}]({href})" if href
                               else self.code_span(child.text()))
                else:
                    text = child if isinstance(child, str) else child.text()
                    # punctuation Medium kept inside the code span stays plain text
                    out.append(self.escape(text) if re.fullmatch(r"[\s.,;:!?]*", text)
                               else self.code_span(text))
            return "".join(out)
        span = self.code_span(node.text())
        if self.fully_in_content(node, STRONG):
            span = f"**{span}**"
        if self.fully_in_content(node, EMPHASIS):
            span = f"*{span}*"
        return span

    @classmethod
    def fully_in_content(cls, node, tags):
        content = [c for c in node.children if not (isinstance(c, str) and not c.strip())]
        return bool(content) and all(isinstance(c, Node) and cls.fully_in(c, tags) for c in content)

    def href(self, anchor):
        raw = anchor.attrs.get("href") or anchor.attrs.get("data-href") or ""
        if not raw:
            return ""
        href = clean_href(raw, anchor.text())
        target = self.ctx.post_for_url(href, exclude=self.post)
        if target is not None:
            internal = self.ctx.internal_link(target, self.post)
            if internal:
                return self.keep(internal)
        if re.search(r"[\s()<>]", href):
            href = f"<{href.replace(' ', '%20')}>"
        return href

    def link(self, anchor, plain_headings=False):
        visible = norm_ws(anchor.text())
        if re.fullmatch(r"[\s.,;:!?)]*", visible):
            return self.escape(anchor.text())  # a link on a lone "." is noise
        href = self.href(anchor)
        text = self.inline(anchor.children, plain_headings).strip()
        raw = clean_href(anchor.attrs.get("href") or anchor.attrs.get("data-href") or "", visible)
        target = self.ctx.post_for_url(raw, exclude=self.post)
        if target is not None and re.match(r"https?://", visible):
            text = self.escape(target.title)  # bare Medium URL -> the post's title
        if not href:
            return text
        if not text:
            return ""
        return f"[{text}]({href})"

    # -- images
    def image(self, img, caption):
        image_id = img.attrs.get("data-image-id", "")
        src = img.attrs.get("src", "")
        url = self.ctx.image_url(self.post, image_id, img.attrs.get("data-width"), src)
        alt = norm_ws(img.attrs.get("alt", "")).replace("[", "").replace("]", "")
        title = ""
        if caption:
            title = ' "' + caption.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return f"![{alt}]({url}{title})"

    # -- blocks
    def finish_inline(self, text, escape_starts=True):
        text = re.sub(rf"^[\s{HARD_BREAK}]+|[\s{HARD_BREAK}]+$", "", text)
        text = re.sub(rf"[ \t]*{HARD_BREAK}[ \t]*", HARD_BREAK, text)
        lines = text.split(HARD_BREAK)
        if escape_starts:
            lines = [self.escape_line_starts(line) for line in lines]
        return HARD_BREAK.join(lines)

    def heading(self, node):
        level = "##" if node.tag in {"h1", "h2", "h3"} else "###"
        text = self.inline(node.children, plain_headings=True)
        text = norm_ws(text.replace(HARD_BREAK, " "))
        return f"{level} {text}" if text else ""

    def code_block(self, pres, label):
        chunks = []
        for pre in pres:
            text = pre.text().replace("\u00a0", " ").replace("\r", "")
            chunks.append(text.strip("\n").rstrip())
        code = "\n\n".join(chunks)
        lang = code_language(pres[0], label, code)
        fence = fence_for(code)
        return f"{fence}{lang}\n{code}\n{fence}"

    def list_block(self, node):
        items = []
        number = int(node.attrs.get("start", "1") or 1)
        for li in (c for c in node.children if isinstance(c, Node) and c.tag == "li"):
            marker = "-" if node.tag == "ul" else f"{number}."
            number += 1
            body = self.finish_inline(self.inline(li.children))
            indent = " " * (len(marker) + 1)
            items.append(f"{marker} " + body.replace(HARD_BREAK, HARD_BREAK + indent))
        return "\n".join(items)

    def figure(self, node):
        caption_node = node.find("figcaption")
        caption = norm_ws(caption_node.text()) if caption_node else ""
        out = []
        images = node.find_all("img")
        for i, img in enumerate(images):
            out.append(self.image(img, caption if i == len(images) - 1 else ""))
        for frame in node.find_all("iframe"):
            src = frame.attrs.get("src") or frame.attrs.get("data-src") or ""
            if src.startswith("/"):
                src = "https://medium.com" + src
            youtube = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([\w-]{6,})", src)
            if youtube and self.fmt == "hugo":
                out.append(self.keep(f"{{{{< youtube {youtube.group(1)} >}}}}"))
            else:
                out.append(f"[Embedded content]({src})")
                self.post.warnings.append(f"embed kept as a link, check it: {src}")
        if caption and not images:
            out.append(f"*{self.escape(caption)}*")
        return "\n\n".join(out)

    def mixtape(self, node):
        anchor = node.find("a")
        if anchor is None:
            return ""
        strong = anchor.find("strong")
        title = norm_ws(strong.text() if strong else anchor.text())
        href = self.href(anchor)
        return f"[{self.escape(title)}]({href})" if href else self.escape(title)

    def block(self, node):
        tag = node.tag
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return self.heading(node)
        if tag in {"ul", "ol"}:
            return self.list_block(node)
        if tag == "blockquote":
            body = self.finish_inline(self.inline(node.children))
            return "> " + body.replace(HARD_BREAK, HARD_BREAK + "> ")
        if tag == "figure":
            return self.figure(node)
        if tag == "hr":
            return "---"
        if tag == "div" and node.has_class("graf--mixtapeEmbed"):
            return self.mixtape(node)
        if tag == "img":
            return self.image(node, "")
        return self.finish_inline(self.inline(node.children))

    # -- whole post
    def convert(self, blocks):
        out = []
        last_section = None
        i = 0
        while i < len(blocks):
            section, node = blocks[i]
            # "YAML" / "bash" paragraphs pasted above a code block are just labels
            label = None
            if node.tag == "p" and i + 1 < len(blocks) and blocks[i + 1][1].tag == "pre":
                key = norm_ws(node.text()).lower()
                if key in LANG_LABELS:
                    label = key
                    i += 1
                    section, node = blocks[i]
            if node.tag == "pre":
                pres = [node]
                while i + 1 < len(blocks) and blocks[i + 1][1].tag == "pre" \
                        and blocks[i + 1][0] == section:
                    i += 1
                    pres.append(blocks[i][1])
                markdown = self.code_block(pres, label)
            else:
                markdown = self.block(node)
            i += 1
            if not markdown.strip():
                continue
            if last_section is not None and section != last_section:
                out.append("---")
            last_section = section
            out.append(markdown)
        text = "\n\n".join(out)
        return text.replace(HARD_BREAK, "\\\n" if self.fmt == "hugo" else "  \n")

    def resolve(self, text):
        def sub(match):
            return self.placeholders[int(match.group(1))]
        if self.fmt == "hugo":
            # Hugo parses shortcodes even inside code blocks: escape them.
            text = (text.replace("{{<", "{{</*").replace(">}}", "*/>}}")
                        .replace("{{%", "{{%/*").replace("%}}", "*/%}}"))
            return re.sub(f"{PH_OPEN}(\\d+){PH_CLOSE}", sub, text)
        # Jekyll runs Liquid over the whole post: protect it when needed.
        if "{{" in text or "{%" in text:
            text = re.sub(f"{PH_OPEN}(\\d+){PH_CLOSE}",
                          lambda m: "{% endraw %}" + sub(m) + "{% raw %}", text)
            return "{% raw %}\n" + text + "\n{% endraw %}"
        return re.sub(f"{PH_OPEN}(\\d+){PH_CLOSE}", sub, text)


# --------------------------------------------------------------------------- conversion run

class Context:
    def __init__(self, args, posts):
        self.args = args
        self.fmt = args.format
        self.posts = posts
        self.by_id = {p.medium_id: p for p in posts}
        self.images = ImageFetcher(enabled=not args.no_images)
        self.tz = None
        if args.tz:
            if ZoneInfo is None:
                sys.exit("--tz needs Python 3.9+")
            self.tz = ZoneInfo(args.tz)
        self.lang_codes = parse_lang_codes(args.lang_codes)
        self.multilingual = bool(self.lang_codes)

    def local(self, dt):
        return dt.astimezone(self.tz) if self.tz else dt.astimezone()

    def lang_code(self, post):
        return self.lang_codes.get(post.lang, post.lang)

    def post_for_url(self, href, exclude=None):
        if "medium.com" not in href and "/p/" not in href:
            return None
        for medium_id, post in self.by_id.items():
            if post is not exclude and post.selected and medium_id in href:
                return post
        return None

    def internal_link(self, target, source):
        if target.is_draft and not source.is_draft:
            return None  # the target will not be built; keep the Medium URL
        if self.fmt == "hugo":
            path = f"/{self.args.section}/{target.bundle.dir_name}"
            lang = f' lang="{self.lang_code(target)}"' if self.multilingual else ""
            return f'{{{{< relref path="{path}"{lang} >}}}}'
        if target.is_draft:
            return None
        return f"{{% post_url {self.local(target.date):%Y-%m-%d}-{target.slug} %}}"

    def image_url(self, post, image_id, width, src):
        bundle = post.bundle
        key = image_id or src
        if key not in bundle.images:
            if not self.images.enabled:
                bundle.images[key] = None
            else:
                try:
                    data, ext = self.images.fetch(image_id, width, src)
                    name = f"img-{len([v for v in bundle.images.values() if v]) + 1:02d}{ext}"
                    bundle.images[key] = name
                    bundle.files[name] = data
                except Exception as exc:  # keep going, the remote URL still works
                    bundle.images[key] = None
                    post.warnings.append(f"image not downloaded ({exc}), kept remote URL")
        name = bundle.images[key]
        if not name:
            return src
        if self.fmt == "hugo":
            return name
        writer_url = f"/{self.args.assets_dir}/{bundle.dir_name}/{name}"
        return post._writer.keep(f"{{{{ '{writer_url}' | relative_url }}}}")


def parse_lang_codes(value):
    codes = {}
    for pair in filter(None, (value or "").split(",")):
        detected, _, code = pair.partition("=")
        codes[detected.strip()] = (code or detected).strip()
    return codes


def build_bundles(ctx, posts):
    """Group translations (posts that link to each other near the top) into one bundle."""
    parent = {p.medium_id: p.medium_id for p in posts}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    if ctx.multilingual and ctx.fmt == "hugo":
        ids = list(parent)
        if not ctx.args.no_pair:
            for post in posts:
                head = Node("#head")
                head.children = [n for _, n in post._blocks[:4]]
                for other in linked_post_ids(head, [i for i in ids if i != post.medium_id]):
                    if ctx.by_id[other].lang != post.lang:
                        pairs.append((post.medium_id, other))
        for manual in ctx.args.pair or []:
            a, _, b = manual.partition(":")
            if a in parent and b in parent:
                pairs.append((a, b))
            else:
                print(f"warning: --pair {manual}: id not found among converted posts", file=sys.stderr)
    for a, b in pairs:
        ra, rb = root(a), root(b)
        if ra != rb:
            langs_a = {p.lang for p in posts if root(p.medium_id) == ra}
            langs_b = {p.lang for p in posts if root(p.medium_id) == rb}
            if not langs_a & langs_b:
                parent[rb] = ra
    groups = {}
    for post in posts:
        groups.setdefault(root(post.medium_id), []).append(post)
    order = list(ctx.lang_codes)
    bundles = []
    for members in groups.values():
        members.sort(key=lambda p: (order.index(p.lang) if p.lang in order else len(order), p.date))
        bundle = Bundle(posts=members)
        primary = members[0]
        if ctx.fmt == "hugo":
            bundle.dir_name = f"{ctx.local(min(p.date for p in members)):%Y%m%d}-{primary.slug}"
        else:
            bundle.dir_name = primary.slug
        for post in members:
            post.bundle = bundle
        bundles.append(bundle)
    return bundles


def load_tags(path):
    tags = {}
    if not path:
        return tags
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            key, _, values = line.partition(":")
            tags[key.strip()] = [t.strip() for t in values.split(",") if t.strip()]
    return tags


def description_for(post, blocks, ctx):
    sub = post.export_subtitle
    if sub and not sub.endswith("\u2026"):
        return sub  # a subtitle the author wrote, not Medium's automatic excerpt
    ids = [i for i in ctx.by_id if i != post.medium_id]
    for _, node in blocks:
        if node.tag != "p":
            continue
        text = norm_ws(node.text())
        if len(text) < 40 or linked_post_ids(node, ids):
            continue
        return first_sentences(text)
    return sub.rstrip("\u2026").strip()


def prepare(post, ctx):
    """Strip title/subtitle from the body and fill slug, language and description."""
    blocks = collect_blocks(post.body)
    subtitle = ""
    for i, (_, node) in enumerate(blocks):
        if node.has_class("graf--title"):
            del blocks[i]
            if i < len(blocks):
                nxt = blocks[i][1]
                export_sub = post.export_subtitle.rstrip("\u2026").strip()
                text = norm_ws(nxt.text())
                if nxt.has_class("graf--subtitle") or (
                        nxt.tag in {"h2", "h3", "h4"} and export_sub and text.startswith(export_sub[:60])):
                    subtitle = text
                    del blocks[i]
            break
    post._blocks = blocks
    plain = " ".join(norm_ws(n.text()) for _, n in blocks if n.tag in {"p", "li", "h3", "h4", "blockquote"})
    post.lang = ctx.args.lang or detect_language(plain, ctx.args.default_lang)
    post.slug = post_slug(post)
    post.description = subtitle or description_for(post, blocks, ctx)


def front_matter(post, ctx):
    date = ctx.local(post.date).replace(microsecond=0)
    tags = post.tags
    if ctx.fmt == "hugo":
        lines = [
            f"date: '{date.isoformat()}'",
            f"draft: {'true' if post.is_draft else 'false'}",
            f"slug: {yaml_str(post.slug)}",
            f"title: {yaml_str(post.title)}",
            f"description: {yaml_str(post.description)}",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
        ]
    else:
        lines = [
            "layout: post",
            f"title: {yaml_str(post.title)}",
            f"date: {date:%Y-%m-%d %H:%M:%S %z}",
            f"description: {yaml_str(post.description)}",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
            f"lang: {post.lang}",
        ]
    return "---\n" + "\n".join(lines) + "\n---\n"


def output_paths(post, ctx, root):
    if ctx.fmt == "hugo":
        folder = root / "content" / ctx.args.section / post.bundle.dir_name
        name = f"index.{ctx.lang_code(post)}.md" if ctx.multilingual else "index.md"
        return folder / name, folder
    assets = root / ctx.args.assets_dir / post.bundle.dir_name
    if post.is_draft:
        return root / "_drafts" / f"{post.slug}.md", assets
    return root / "_posts" / f"{ctx.local(post.date):%Y-%m-%d}-{post.slug}.md", assets


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="medium-export2md",
        description="Convert the official Medium export (.zip or folder) into Markdown for Hugo or Jekyll.")
    parser.add_argument("export", type=Path, help="medium-export .zip file or its extracted folder")
    parser.add_argument("-o", "--output", type=Path, default=Path("medium-markdown"),
                        help="site root to write into (default: ./medium-markdown)")
    parser.add_argument("--format", choices=["hugo", "jekyll"], default="hugo")
    parser.add_argument("--section", default="posts", help="Hugo content section (default: posts)")
    parser.add_argument("--assets-dir", default="assets/images/posts",
                        help="Jekyll folder for images (default: assets/images/posts)")
    parser.add_argument("--include-drafts", action="store_true",
                        help="also convert unpublished drafts (written with draft: true / into _drafts)")
    parser.add_argument("--lang-codes", metavar="MAP",
                        help="Hugo multilingual: map detected languages to your site's codes, "
                             "e.g. pt=pt-br,en=en. Writes index.<code>.md and joins translations "
                             "in one bundle. The first code is your default language.")
    parser.add_argument("--lang", help="force one language for every post (pt, en, es...)")
    parser.add_argument("--default-lang", default="en", help="used when detection is unsure (default: en)")
    parser.add_argument("--pair", action="append", metavar="ID:ID",
                        help="mark two Medium post ids as translations of each other (repeatable)")
    parser.add_argument("--no-pair", action="store_true", help="do not auto-detect translations")
    parser.add_argument("--tags", metavar="FILE",
                        help="text file with lines 'medium-id: tag1, tag2' (Medium's export has no tags)")
    parser.add_argument("--tz", help="time zone for dates, e.g. America/Sao_Paulo (default: this machine's)")
    parser.add_argument("--no-images", action="store_true", help="keep images on Medium's CDN")
    parser.add_argument("--force", action="store_true", help="overwrite files that already exist")
    parser.add_argument("--dry-run", action="store_true", help="show what would be written")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if not args.export.exists():
        parser.error(f"{args.export} not found")
    posts = load_posts(args.export)
    if not posts:
        sys.exit("No posts found. Is this the zip from Medium's 'Download your information'?")

    responses = [p for p in posts if p.is_response]
    drafts = [p for p in posts if p.is_draft and not p.is_response]
    for post in posts:
        post.selected = not post.is_response and (args.include_drafts or not post.is_draft)
    selected = [p for p in posts if p.selected]

    ctx = Context(args, posts)
    tags = load_tags(args.tags)
    for post in selected:
        prepare(post, ctx)
        post.tags = tags.get(post.medium_id, [])
    build_bundles(ctx, selected)

    written = skipped = 0
    for post in sorted(selected, key=lambda p: p.date):
        writer = MarkdownWriter(post, ctx)
        post._writer = writer
        body = writer.resolve(writer.convert(post._blocks))
        md_file, asset_dir = output_paths(post, ctx, args.output)
        content = front_matter(post, ctx) + "\n" + body.rstrip() + "\n"
        status = "draft" if post.is_draft else "post"
        rel = md_file.relative_to(args.output) if md_file.is_relative_to(args.output) else md_file
        if md_file.exists() and not args.force:
            print(f"skip   {rel} (exists, use --force)")
            skipped += 1
            continue
        print(f"{'would write' if args.dry_run else 'write'}  [{status}, {post.lang}] {rel}")
        for warning in post.warnings:
            print(f"         ! {warning}")
        written += 1
        if args.dry_run:
            continue
        md_file.parent.mkdir(parents=True, exist_ok=True)
        md_file.write_text(content, encoding="utf-8")
        for name, data in post.bundle.files.items():
            target = asset_dir / name
            if not target.exists() or args.force:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    verb = "would be written" if args.dry_run else "written"
    print(f"\n{written} file(s) {verb}, {skipped} skipped.", end=" ")
    if drafts and not args.include_drafts:
        print(f"{len(drafts)} draft(s) ignored (use --include-drafts).", end=" ")
    if responses:
        print(f"{len(responses)} response(s)/comment(s) ignored.", end=" ")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
