"""Non-executing, loss-aware projection of the reviewed wiki templates.

Never expand modules, HTML, URLs or instructions found in source text. Original
Unicode spans are retained separately; rendered text is a reading aid only.
"""

import html
import re


def templates(text, name=None):
    stack, found = [], []
    for match in re.finditer(r"\{\{|\}\}", text):
        if match[0] == "{{":
            stack.append(match.start())
        elif stack:
            start = stack.pop()
            raw = text[start : match.end()]
            if name is None or raw[2:].split("|", 1)[0].strip() == name:
                found.append((start, match.end(), raw))
    return sorted(found)


def parameters(raw):
    """Split only top-level pipes, preserving nested templates and wiki links."""
    body, parts, start, depth, links = raw[2:-2], [], 0, 0, 0
    i = 0
    while i < len(body):
        token = body[i : i + 2]
        if token in ("{{", "}}", "[[", "]]"):
            if token == "{{":
                depth += 1
            elif token == "}}":
                depth -= 1
            elif token == "[[":
                links += 1
            else:
                links -= 1
            i += 2
            continue
        if body[i] == "|" and depth == 0 and links == 0:
            parts.append(body[start:i].strip())
            start = i + 1
        i += 1
    parts.append(body[start:].strip())
    positional, named = [], {}
    for value in parts[1:]:
        # A named parameter begins with a simple key, never nested HTML or links.
        match = re.match(r"^([\w一-龥]+)=(.*)$", value, re.S)
        if match:
            named[match[1]] = match[2]
        else:
            positional.append(value)
    return parts[0], positional, named


def plain(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    # Innermost templates only. Preserve unknown arguments visibly instead of
    # pretending to execute MediaWiki's Lua modules or dropping their conditions.
    for _ in range(32):

        def render(match):
            name, args, named = parameters(match[0])
            if name.lower() in ("color", "font"):
                return args[-1] if args else named.get("text", "")
            if name in ("mdi", "#widget:torappu"):
                return ""
            if name.startswith("#var:"):
                return "〔原文变量 " + name[5:] + "，未展开〕"
            if name == "=":
                return "="
            if name == "!":
                return "|"
            return " ".join(
                [*args, *[v for k, v in named.items() if k in ("text", "desc1", "desc2", "内容")]]
            )

        new = re.sub(r"\{\{[^{}]*\}\}", render, text)
        if new == text:
            break
        text = new
    for _ in range(8):
        new = re.sub(
            r"\[\[([^\[\]]*)\]\]",
            lambda m: "" if m[1].startswith(("文件:", "File:")) else m[1].split("|")[-1],
            text,
        )
        if new == text:
            break
        text = new
    text = re.sub(r"<br\s*/?>", "；", text, flags=re.I)
    text = re.sub(
        r"</?(?:span|div|small|big|b|i|u|s|sup|sub|ref|nowiki|p|ol|ul|li|table|tr|td|th)\b[^>]*>",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"'{2,5}", "", text)
    return html.unescape(text).strip()


def scenes(text):
    """Return explicitly owned forward menus, never reverse nextSceneId edges."""
    result = []
    for start, end, raw in templates(text, "ISEvent/scene"):
        _, args, named = parameters(raw)
        if len(args) < 4:
            continue
        options, information_notes, menu_sequence = [], [], []
        menu_text = named.get("选项", "")
        menu_offset = raw.find(menu_text) if menu_text else 0
        for option_start, option_end, option in templates(menu_text, "ISEvent/choose"):
            _, pos, fields = parameters(option)
            position = {
                "source_start": start + menu_offset + option_start,
                "source_end": start + menu_offset + option_end,
                "source_position": len(menu_sequence),
                "position_semantics": "unicode_offsets_in_supplied_document",
            }
            if pos and pos[0] == "desc":
                information_notes.append({
                    "type": "desc", "text": plain(fields.get("desc1", "")),
                    "additional_text": plain(fields.get("desc2", "")),
                    "destination": fields.get("dest"), "branches": fields.get("subChoose"),
                    "raw": option, **position,
                })
                menu_sequence.append({"kind": "information_note", "index": len(information_notes) - 1})
                continue
            details = {}
            # Collection templates' third argument is the relic ID;
            # other template types use that slot for unrelated display icons.
            if pos and pos[0] in ("collection", "collection2"):
                if len(pos) > 2 and re.fullmatch(r"\d+", pos[2]):
                    details["收藏品"] = pos[2]
                if fields.get("iconId"):
                    details["iconId"] = fields["iconId"]
            options.append(
                {
                    "label": plain(pos[1]) if len(pos) > 1 else "说明",
                    "type": pos[0] if pos else "",
                    "reward_and_cost": plain(fields.get("desc1", "未提供数值说明")),
                    "condition": plain(fields.get("desc2", "")),
                    "destination": fields.get("dest"),
                    "branches": fields.get("subChoose"),
                    "source_details": details,
                    "linked_entries": list(
                        dict.fromkeys(
                            target
                            for target in re.findall(
                                r"\[\[([^|\]\n]+)",
                                fields.get("desc1", "") + "\n" + fields.get("desc2", ""),
                            )
                            if not target.startswith(("文件:", "File:"))
                        )
                    ),
                    "raw": option,
                    **position,
                }
            )
            menu_sequence.append({"kind": "option", "index": len(options) - 1})
        result.append(
            {
                "start": start,
                "end": end,
                "title": plain(args[2]),
                "scene_label": plain(args[0]),
                "description": plain(args[3]),
                "raw_description": args[3],
                "floor": named.get("floor"),
                "event_type": named.get("etype"),
                "options": options,
                "information_notes": information_notes,
                "menu_sequence": menu_sequence,
            }
        )
    return result
