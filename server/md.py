# -*- coding: utf-8 -*-
"""极简 Markdown 渲染器（服务端，零依赖）。

只支持 Skill / 预设正文里真实出现的语法：
标题、段落、粗体、斜体、行内代码、代码块、表格、有序/无序列表、
引用块、分隔线、链接。渲染前先整体转义，绝不执行任何 HTML。
"""

import html
import re

__all__ = ["render", "esc"]

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
EM_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
CODE_RE = re.compile(r"`([^`\n]+)`")
HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
UL_RE = re.compile(r"^(\s{0,4})[-*+]\s+(.*)$")
OL_RE = re.compile(r"^(\s{0,4})(\d+)[.)]\s+(.*)$")
TBL_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")

SLOT = "\x00%d\x00"


def esc(s):
    return html.escape(s, quote=False)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _inline(text, link_resolver=None):
    """text 是原始（未转义）文本。"""
    s = esc(text)

    slots = []

    def save_code(m):
        slots.append("<code>%s</code>" % m.group(1))
        return SLOT % (len(slots) - 1)

    s = CODE_RE.sub(save_code, s)
    s = BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = EM_RE.sub(r"<em>\1</em>", s)

    def do_link(m):
        label, href = m.group(1), m.group(2).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return '<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (esc(href), label)
        if link_resolver is not None:
            url = link_resolver(href)
            if url:
                return '<a href="%s">%s</a>' % (esc(url), label)
        return '<a href="#" class="pkg-link">%s</a>' % label

    s = LINK_RE.sub(do_link, s)
    s = re.sub("\x00(\\d+)\x00", lambda m: slots[int(m.group(1))], s)
    return s


def _is_block_start(line):
    s = line.strip()
    if not s:
        return True
    if s.startswith("```") or s.startswith("|") or s.startswith(">"):
        return True
    if HEAD_RE.match(line) or HR_RE.match(line):
        return True
    if UL_RE.match(line) or OL_RE.match(line):
        return True
    return False


def render(src, link_resolver=None):
    if not src:
        return ""
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    n = len(lines)
    out = []
    i = 0

    def il(t):
        return _inline(t, link_resolver)

    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        if s.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % esc("\n".join(buf)))
            continue

        m = HEAD_RE.match(line)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, il(m.group(2).strip()), lvl))
            i += 1
            continue

        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if s.startswith("|") and i + 1 < n and TBL_SEP_RE.match(lines[i + 1]):
            head = _cells(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            buf = ["<table><thead><tr>"]
            buf += ["<th>%s</th>" % il(c) for c in head]
            buf.append("</tr></thead><tbody>")
            for r in rows:
                buf.append("<tr>" + "".join("<td>%s</td>" % il(c) for c in r) + "</tr>")
            buf.append("</tbody></table>")
            out.append("".join(buf))
            continue

        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % il(" ".join(buf)))
            continue

        mu = UL_RE.match(line)
        mo = OL_RE.match(line)
        if mu or mo:
            ordered = mo is not None
            items = []
            while i < n:
                mm = (OL_RE if ordered else UL_RE).match(lines[i])
                if mm:
                    items.append(mm.group(3) if ordered else mm.group(2))
                    i += 1
                    continue
                # 续行：缩进的普通行并入上一条
                if items and lines[i].strip() and not _is_block_start(lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % il(t) for t in items), tag))
            continue

        buf = [line.strip()]
        i += 1
        while i < n and not _is_block_start(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % il(" ".join(buf)))

    return "\n".join(out)
