"""Lossless, model-facing deduplication. Never replace source/review originals."""
from __future__ import annotations

import copy
from collections import Counter

from arknights_agent import services

ENCODING = 'exact-shared-values-v1'
MARKER = '$shared_value'
TEXT_MARKER = '$shared_text'
RESERVED = {MARKER, TEXT_MARKER, '_shared_values', '_observation_encoding'}
MODEL_GUIDANCE = (
    '工具结果若标有_observation_encoding=exact-shared-values-v1，'
    '其中{$shared_value:编号}逐字等同于同条结果_shared_values内的完整值；'
    '{$shared_text:[片段]}按顺序连接，引用片段为该共享值的原始JSON文本；'
    '只是重复数据共用一份，不是删节、概述或证据编号。正文和来源引用不能使用这些内部编号。'
)


def expand_observation(value):
    """Decode once, without interpreting strings or following external refs."""
    if value.get('_observation_encoding') != ENCODING:
        return copy.deepcopy(value)
    shared = value['_shared_values']

    def expand(item):
        if isinstance(item, dict):
            if set(item) == {MARKER}:
                return copy.deepcopy(shared[item[MARKER]])
            if set(item) == {TEXT_MARKER}:
                return ''.join(part if isinstance(part, str) else
                               services.canonical_json(shared[part[MARKER]]) for part in item[TEXT_MARKER])
            return {key: expand(child) for key, child in item.items()}
        if isinstance(item, list):
            return [expand(child) for child in item]
        return item

    return expand({key: item for key, item in value.items()
                   if key not in {'_observation_encoding', '_shared_values'}})


def share_observation(value):
    """Hoist exact repeated JSON values only when the whole envelope shrinks 10%."""
    counts, originals, strings = Counter(), {}, []
    collision = False

    def scan(item):
        nonlocal collision
        if isinstance(item, dict):
            collision |= bool(RESERVED.intersection(item))
            for child in item.values():
                scan(child)
        elif isinstance(item, list):
            for child in item:
                scan(child)
        elif isinstance(item, str) and len(item) >= 256:
            strings.append(item)
        if isinstance(item, (dict, list, str)):
            key = services.canonical_json(item)
            if len(key.encode()) >= 256:
                counts[key] += 1
                originals[key] = item

    scan(value)
    if collision:
        return value, None
    shared, refs = {}, {}
    # Source renderers often embed the same JSON value in prose, a claim,
    # and a citation. Match exact serialized bytes, never natural language.
    literals = sorted((k for k, v in originals.items() if isinstance(v, (dict, list))), key=len, reverse=True)[:512]
    literals = [key for key in literals if any(key in text for text in strings)]
    for key in literals:
        counts[key] += 1

    def reference(key):
        if key not in refs:
            refs[key] = f'v{len(refs) + 1}'
            shared[refs[key]] = copy.deepcopy(originals[key])
        return {MARKER: refs[key]}

    def encode(item):
        if isinstance(item, (dict, list, str)):
            key = services.canonical_json(item)
            if counts[key] > 1:
                return reference(key)
        if isinstance(item, dict):
            return {key: encode(child) for key, child in item.items()}
        if isinstance(item, list):
            return [encode(child) for child in item]
        if isinstance(item, str) and len(item) >= 256:
            parts = [item]
            for literal in literals:
                expanded = []
                for part in parts:
                    if not isinstance(part, str) or literal not in part:
                        expanded.append(part)
                        continue
                    chunks = part.split(literal)
                    for index, chunk in enumerate(chunks):
                        if index:
                            expanded.append(reference(literal))
                        if chunk:
                            expanded.append(chunk)
                parts = expanded
            if any(isinstance(part, dict) for part in parts):
                return {TEXT_MARKER: parts}
        return item

    candidate = encode(value)
    if not shared:
        return value, None
    candidate.update(_observation_encoding=ENCODING, _shared_values=shared)
    before = len(services.canonical_json(value).encode())
    after = len(services.canonical_json(candidate).encode())
    if after > before * .90:
        return value, None
    # Runtime verification also protects future changes to the codec.
    if expand_observation(candidate) != value:
        raise ValueError('lossless_observation_mismatch')
    return candidate, {'protocol': ENCODING, 'before_bytes': before,
                       'after_bytes': after, 'original_sha256': services.content_digest(value)}
