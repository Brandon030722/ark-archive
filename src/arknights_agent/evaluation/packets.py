"""Bounded source context for a retrieval baseline, never an answer-key export."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from arknights_agent.errors import InvalidData


class SourceReader:
    def __init__(self, sources: dict, workspace: Path):
        self.sources, self.workspace, self.cache = sources, workspace.resolve(), {}
        self.byte_sizes = {}

    def read(self, source):
        expected = source["snapshot_hash"]
        path = (self.workspace / source["local_cache_path"]).resolve()
        if not path.is_relative_to(self.workspace):
            raise InvalidData("evaluation source cache escapes the workspace")
        if expected not in self.cache:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise InvalidData("evaluation source cache hash mismatch")
            self.byte_sizes[expected] = len(raw)
            self.cache[expected] = (
                json.loads(raw)
                if source["media_type"].split(";", 1)[0] == "application/json"
                else raw
            )
        if "byte_size" in source and (
            type(source["byte_size"]) is not int or source["byte_size"] != self.byte_sizes[expected]
        ):
            raise InvalidData("evaluation source cache byte size mismatch")
        return self.cache[expected]

    def find(self, suffix, revision):
        matches = [
            source
            for source in self.sources.values()
            if source["source_key"].endswith(suffix) and source["source_revision"] == revision
        ]
        if len(matches) != 1:
            raise InvalidData(f"suite source is absent or ambiguous: {suffix}")
        return matches[0]


def pointer(*parts):
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def build_source_packet(case, reader: SourceReader, *, max_bytes=300_000):
    """Select original objects from the question; never inspect expected/gold.

    All skill levels are supplied so level selection remains the model's job.
    Recruitment includes the whole raw membership/rule and character selector
    fields, rather than the precomputed intersection. No network or game code.
    """
    revision, module, question = case["source_revision"], case["module"], case["question"]
    chunks, used = [], 0

    def append(source, selector, content, projection=None):
        nonlocal used
        chunk = {
            "source_key": source["source_key"],
            "source_revision": source["source_revision"],
            "snapshot_hash": source["snapshot_hash"],
            "source_url": source["source_url"],
            "pointer": selector,
            "content": content,
        }
        if projection:
            chunk["projection"] = projection
        size = len(json.dumps(chunk, ensure_ascii=False).encode())
        if used + size > max_bytes:
            raise InvalidData(
                "source packet exceeds its declared context budget; no silent truncation"
            )
        used += size
        chunks.append(chunk)

    if module == "operator":
        char_source = reader.find("character_table.json", revision)
        skill_source = reader.find("skill_table.json", revision)
        equip_source = reader.find("uniequip_table.json", revision)
        phase_source = reader.find("battle_equip_table.json", revision)
        characters, skills = reader.read(char_source), reader.read(skill_source)
        equips, phases = reader.read(equip_source), reader.read(phase_source)
        spans = []
        for ident, char in characters.items():
            if not ident.startswith("char_") or char.get("isNotObtainable"):
                continue
            name = char.get("name", "")
            if name:
                spans.extend(
                    (m.start(), m.end(), ident) for m in re.finditer(re.escape(name), question)
                )
        ids = list(
            dict.fromkeys(
                ident
                for start, end, ident in spans
                if not any(a <= start and b >= end and b - a > end - start for a, b, _ in spans)
            )
        )
        for ident in ids[:2]:
            char = characters[ident]
            append(char_source, pointer(ident), char)
            for ref in char.get("skills", []):
                skill_id = ref["skillId"]
                append(skill_source, pointer(skill_id), skills[skill_id])
            if "模组" in question or "模块" in question or "module" in question.lower():
                for equip_id in equips.get("charEquip", {}).get(ident, []):
                    equip = equips["equipDict"][equip_id]
                    if equip.get("type") == "INITIAL":
                        continue
                    append(equip_source, pointer("equipDict", equip_id), equip)
                    if equip_id in phases:
                        append(phase_source, pointer(equip_id), phases[equip_id])
    elif module == "recruitment":
        source = reader.find("gacha_table.json", revision)
        data = reader.read(source)
        for key in (
            "recruitDetail",
            "gachaTags",
            "recruitRarityTable",
            "specialTagRarityTable",
            "recruitPool",
        ):
            append(source, pointer(key), data[key])
        # Only raw selectors of explicit members; retain each field's real pointer.
        char_source = reader.find("character_table.json", revision)
        chars, detail = reader.read(char_source), data["recruitDetail"]
        # Decode only the six explicit star sections. Rich-text closing tags
        # contain slashes and robot names contain hyphens; strip tags first.
        plain = re.sub(r"<[^>]*>", "", detail)
        sections = re.findall(r"(?m)^(★{1,6})\\n([^\r\n]+)", plain)
        if len(sections) != 6 or {len(stars) for stars, _ in sections} != set(range(1, 7)):
            raise InvalidData("recruitment source packet needs all six explicit star sections")
        names = [name.strip() for _, text in sections for name in text.split("/")]
        if not all(names) or len(names) != len(set(names)):
            raise InvalidData("recruitment source packet has empty or duplicate member names")
        for name in names:
            matches = [
                (ident, char)
                for ident, char in chars.items()
                if ident.startswith("char_") and char.get("name") == name
            ]
            if len(matches) != 1:
                raise InvalidData("recruitment source packet has an unresolved explicit member")
            ident, char = matches[0]
            fields = {
                key: char[key] for key in ("name", "rarity", "profession", "position", "tagList")
            }
            append(char_source, pointer(ident), fields, "selected_raw_fields")
        for source in reader.sources.values():
            if source["source_key"].startswith("prts:"):
                page = reader.read(source)["query"]["pages"][0]
                if "公开招募" in page.get("title", ""):
                    content = page["revisions"][0]["slots"]["main"]["content"]
                    append(source, "/query/pages/0/revisions/0/slots/main/content", content)
    elif module == "garrison":
        source = reader.find("activity_table.json", revision)
        data = reader.read(source)
        append(source, "/basicInfo/act2autochess", data["basicInfo"]["act2autochess"])
        for ident in ("V2_1", "V2_2"):
            append(
                source,
                pointer("autoChessData", "versionInfoDict", ident),
                data["autoChessData"]["versionInfoDict"][ident],
            )
        # Official visible source paragraphs are retrieved without answer extraction.
        from arknights_agent.ingestion.garrison import html_document

        for source in reader.sources.values():
            if source["media_type"] == "text/html":
                nodes = html_document(reader.read(source))["html_text_nodes"]
                for index, node in enumerate(nodes):
                    if "盟约" in node or re.fullmatch(r"2026-03-(14|27)", node):
                        append(source, pointer("html_text_nodes", index), node)
        # Selection tags describe only the independently checked bond text,
        # never the whole historical namespace. The same public question,
        # phase tags and complete originals determine both model packets.
        phases = set()
        if re.search(r"V2_1|第一阶段|初始阶段|第1阶段|一阶段", question, re.I):
            phases.add("V2_1")
        if re.search(r"V2_2|第二阶段|第2阶段|二阶段", question, re.I):
            phases.add("V2_2")
        bonds = [
            native
            for label, native in (("奇迹", "miraShip"), ("投资人", "investShip"))
            if label in question
        ]
        for source in reader.sources.values():
            if (
                not bonds
                or source.get("selection_topic") != "garrison_documented"
                or source.get("selection_phase_id") not in phases
            ):
                continue
            original = reader.read(source)
            if source["source_key"].split("@", 1)[0].endswith("activity_table.json"):
                season = original["activity"]["AUTOCHESS_SEASON"]["act2autochess"]
                prefix = ("activity", "AUTOCHESS_SEASON", "act2autochess")
                for bond_id in bonds:
                    bond = season["bondInfoDict"][bond_id]
                    append(source, pointer(*prefix, "bondInfoDict", bond_id), bond)
                    effect_id = bond["effectId"]
                    for table in ("effectInfoDataDict", "effectBuffInfoDataDict"):
                        append(source, pointer(*prefix, table, effect_id), season[table][effect_id])
            else:
                page = original["query"]["pages"][0]
                if page["title"] != "卫戍协议：盟约 下半/PRTS盟约记录":
                    raise InvalidData("documented garrison packet has an unexpected Wiki title")
                content = page["revisions"][0]["slots"]["main"]["content"]
                append(source, "/query/pages/0/revisions/0/slots/main/content", content)
    else:
        raise InvalidData("unknown source packet module")
    return chunks
