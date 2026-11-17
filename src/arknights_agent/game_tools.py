"""Explicit game utilities; no LLM requests or chat interpretation.

All catalogs are pinned to a release and verified against source snapshot bytes.
OCR is an optional local macOS Vision adapter; text is only a tag suggestion.
"""

import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import recruitment, services
from .garrison import _Answer, _rows

MAX_IMAGE_BYTES = 12 * 1024 * 1024
_ocr_lock = threading.Lock()


def plain_description(value):
    # Only remove source markup, retaining angle-bracketed gameplay conditions.
    return re.sub(r"<@[^>]*>|</>", "", str(value))


def bond_tiers(raw):
    """Project explicit client condition markup, not player utterances.

    Only the same named faction's plain distinct-member condition is supported.
    Other prerequisites stay in the source description and are not simulated.
    """
    if (raw.get("activeConditionTemplate") != "count_threshold_upward"
            or raw.get("activeCondition") not in {"BOARD", "BOARD_AND_DECK"}
            or raw.get("activeType") == "MANI"):
        return []
    count = raw.get("activeCount")
    if type(count) is not int or count < 1:
        return []
    desc = plain_description(raw.get("desc", ""))
    tiers = [{"count": count, "condition": "基础人数", "description": desc.split("\n<", 1)[0]}]
    for line in desc.splitlines():
        match = re.fullmatch(r"<在场(\d+)名不同【([^】]+)】干员>(.+)", line)
        if match and match[2] == raw.get("name") and int(match[1]) > count:
            tiers.append({"count": int(match[1]), "condition": line[:line.index(">") + 1],
                          "description": match[3]})
    return sorted({tier["count"]: tier for tier in tiers}.values(), key=lambda tier: tier["count"])


def garrison_catalog(session, release, store):
    dataset = release.manifest.get("datasets", {}).get("garrison", {})
    season = dataset.get("scope", {}).get("latest_documented_season", {})
    phase = season.get("phase_id")
    coverage = dataset.get("coverage", {}).get("regulation_sequence_1", {})
    if not phase or coverage.get("phase_id") != phase:
        raise services.Conflict("当前发布没有可核验的卫戍赛季目录。")
    verifier = _Answer(session, release, "explicit squad catalog", store)

    def verified(record):
        raw = record["data"]["source_value"]
        ref = verifier.cite(record, "", raw)
        citation = next(c for c in verifier.result["citations"] if c["id"] == ref)
        prefix = f"/activity/AUTOCHESS/{season.get('activity_id')}/{phase}/"
        if (not citation["pointer"].startswith(prefix)
                or citation["source_revision"] != season.get("client_revision")):
            raise services.Conflict("卫戍条目没有绑定到所选赛季的原始来源。")
        return raw, {
            "record_id": record["id"], "evidence_id": citation["evidence_id"],
            "url": citation["source_url"], "revision": citation["source_revision"],
        }

    groups = {}
    for kind in ("bond", "unit", "unit_shop"):
        rows = [r for r in _rows(session, release, f"regulation_{kind}")
                if r["parent_native_id"] == phase]
        if len(rows) != coverage.get("record_counts", {}).get(f"regulation_{kind}"):
            raise services.Conflict("卫戍目录数量与发布覆盖记录不一致。")
        groups[kind] = {r["native_id"]: (r, *verified(r)) for r in rows}
    bonds = []
    for ident, (_, raw, source) in groups["bond"].items():
        if raw["bondId"] != ident:
            raise services.Conflict("盟约身份与来源不一致。")
        bonds.append({
            "id": ident, "name": raw["name"], "threshold": raw["activeCount"],
            "scope": raw["activeCondition"], "template": raw["activeConditionTemplate"],
            "params": raw["activeParamList"], "description": plain_description(raw["desc"]),
            "special_effect": raw["activeType"] == "MANI", "source": source,
            "tiers": bond_tiers(raw),
        })
    # Names must also be source backed. Never display chess IDs as player names.
    operators = {}
    offset = 0
    while True:
        page = services.list_records(session, release, module="operator", kind="operator",
                                     limit=200, offset=offset)
        operators.update({r["native_id"]: r for r in page})
        if len(page) < 200:
            break
        offset += 200
    units, omitted = [], []
    for ident, (_, shop, source) in groups["unit_shop"].items():
        base = groups["unit"].get(ident)
        golden = groups["unit"].get(shop.get("goldenChessId"))
        operator = operators.get(shop["charId"])
        if operator is None:
            omitted.append(ident)
            continue
        name = operator["data"]["name"]
        verifier.cite(operator, "/name", name)
        if (not base or not golden or shop["chessId"] != ident
                or base[1]["chessId"] != ident or base[1]["isGolden"] is not False
                or golden[1]["isGolden"] is not True
                or base[1]["upgradeChessId"] != golden[1]["chessId"]
                or base[1]["bondIds"] != golden[1]["bondIds"]):
            raise services.Conflict("干员与精锐形态的来源映射不一致。")
        for bond in base[1]["bondIds"]:
            row = groups["bond"].get(bond)
            if not row or not {ident, golden[1]["chessId"]}.issubset(row[1]["chessIdList"]):
                raise services.Conflict("干员盟约与盟约成员目录不一致。")
        units.append({"id": ident, "operator_id": shop["charId"], "name": name,
                      "level": shop["chessLevel"], "bonds": base[1]["bondIds"],
                      "special_obtain": shop["isHidden"], "source": source})
    return {"release_id": release.id, "phase_id": phase, "season": season["title"],
            "bonds": bonds, "units": units, "omitted_unit_count": len(omitted),
            "limitation": "按资料计算基础人数；不模拟层数、调和加成、装备和战斗效果。"}


def recruitment_catalog(session, release, store):
    checked = recruitment.calculate(session, release, [], 540, store=store)
    records = [services.get_record(session, release, ident)
               for ident in checked["calculation_inputs"]["tag_record_ids"]]
    tags = [r["name"] for r in sorted(records, key=lambda r: r["data"]["tagId"])]
    return {"release_id": release.id, "tags": tags, "ocr_available": ocr_available(),
            "ocr_engine": "本机文字识别" if ocr_available() else None}


def ocr_available():
    return platform.system() == "Darwin" and shutil.which("swiftc") is not None


def recognized_tags(rows, catalog):
    """Match complete OCR labels, not substrings or guesses about user intent."""
    hits = {}
    allowed = set(catalog)
    for row in rows[:300]:
        # OCR often inserts spaces inside a Chinese tag. No synonym guessing.
        text = re.sub(r"\s+", "", str(row.get("text", "")))
        if text in allowed:
            hits[text] = max(hits.get(text, 0), float(row.get("confidence", 0)))
    return [{"name": tag, "confidence": round(min(1, max(0, score)), 3)}
            for tag, score in hits.items()]


def recognize_image(raw, tags):
    if not ocr_available():
        raise HTTPException(503, "这台服务器暂不支持本地识图，请手动选择标签。")
    if not _ocr_lock.acquire(blocking=False):
        raise HTTPException(429, "另有图片正在识别，请稍后重试。")
    try:
        source = Path(__file__).with_name("vision_ocr.swift")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
        cache = Path.home() / "Library/Caches/ark-archive/ocr"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        executable = cache / f"vision-{digest}"
        if not executable.is_file():
            # Unique temporary output makes first-use compilation atomic.
            with tempfile.TemporaryDirectory(dir=cache) as build:
                target = Path(build) / "vision"
                subprocess.run([shutil.which("swiftc"), str(source), "-o", str(target)],
                               capture_output=True, check=True, timeout=90)
                target.replace(executable)
        with tempfile.TemporaryDirectory(prefix="ark-ocr-") as scratch:
            image = Path(scratch) / "image"
            image.write_bytes(raw)
            result = subprocess.run([str(executable), str(image)], capture_output=True,
                                    check=True, timeout=25)
        return {"tags": recognized_tags(json.loads(result.stdout), tags),
                "requires_confirmation": True, "engine": "local_apple_vision"}
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "本地识图超时。可换一张清晰的标签截图，或手动选择。") from exc
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise HTTPException(422, "图片未能识别。请用清晰的单张截图，或手动选择标签。") from exc
    finally:
        _ocr_lock.release()


class RecruitmentToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    release_id: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(max_length=5)
    duration_minutes: int = Field(ge=60, le=540, default=540)
    confirmed: bool


def install_routes(app):
    def session_dep():
        with app.state.session_factory() as session:
            yield session

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "请从当前工作台打开此工具。")

    @app.get("/v1/game-tools/garrison")
    def squad(session=Depends(session_dep)):
        release = services.resolve_release(session, None)
        return garrison_catalog(session, release, app.state.snapshot_store)

    @app.get("/v1/game-tools/recruitment")
    def recruitment_tags(session=Depends(session_dep)):
        release = services.resolve_release(session, None)
        return recruitment_catalog(session, release, app.state.snapshot_store)

    @app.post("/v1/game-tools/recruitment/calculate")
    def combinations(body: RecruitmentToolRequest, request: Request, session=Depends(session_dep)):
        same_origin(request)
        if not body.confirmed:
            raise HTTPException(422, "请先确认屏幕上的标签。")
        release = services.resolve_release(session, body.release_id)
        result = recruitment.calculate(session, release, body.tags, body.duration_minutes,
                                       store=app.state.snapshot_store)
        return {key: result[key] for key in ("release_id", "input", "combinations", "warnings", "sources")}

    @app.post("/v1/game-tools/recruitment/recognize")
    async def recognize(request: Request, release_id: str):
        same_origin(request)
        if request.headers.get("content-type", "").split(";")[0] not in {
            "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"
        }:
            raise HTTPException(415, "请选择 PNG、JPEG、WebP 或 HEIC 图片。")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "图片超过12MB，请裁剪到标签区域后再试。")
        if not raw:
            raise HTTPException(422, "图片为空。")

        def work():
            with app.state.session_factory() as session:
                release = services.resolve_release(session, release_id)
                tags = recruitment_catalog(session, release, app.state.snapshot_store)["tags"]
            return recognize_image(bytes(raw), tags)

        return await run_in_threadpool(work)
