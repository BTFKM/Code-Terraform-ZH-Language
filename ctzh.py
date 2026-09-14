#!/usr/bin/env python3
"""Code: Terraform (868160) 正式版一键中文补丁.

用法:
    python ctzh.py                        # 自动定位游戏 exe 并应用汉化(备份原版为 .bak)
    python ctzh.py --exe <path>           # 指定 code-terraform.exe 路径
    python ctzh.py --restore              # 从 .bak 恢复原版
    python ctzh.py --extract-only         # 只提取字典结构到 leaves.json(供编辑参考)
    python ctzh.py --lang my.json         # 使用自定义语言文件
    python ctzh.py --workers 32           # 最多使用 32 个压缩线程
    python ctzh.py --cache-only           # 只求解并缓存,不写入游戏
    python ctzh.py --method search        # 使用传统随机注释搜索

语言文件 language.json: [{"id":..,"key":"..","zh":".."}] 数组。
zh 为空/缺失的条目保持英文原文;{xxx} 占位符必须与原文一致(会校验,不一致则跳过)。

依赖: pip install brotli pefile
"""
import argparse
import hashlib
import json
import os
import random
import re
import struct
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import brotli
import pefile

HERE = os.path.dirname(os.path.abspath(__file__))
ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
PAD_MAX = 6_000_000
QUALITIES = (11, 10, 9)
FAST_QUALITIES = (4, 6, 9, 10, 11)
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
ANCHOR = "console:{ready:"
CATALOG_START = "app:{title:"

_PAD = None
_PAD_LOCK = threading.Lock()


def get_pad():
    """Generate the original deterministic padding only when search needs it."""
    global _PAD
    with _PAD_LOCK:
        if _PAD is None:
            rng = random.Random(0xC0FFEE)
            _PAD = bytes(ord(rng.choice(ALNUM)) for _ in range(PAD_MAX))
    return _PAD


# ---------- shared leaf encoder (style-preserving re-injection) ----------

def encode_template(s):
    out, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            out.append("\\\\")
        elif ch == "`":
            out.append("\\`")
        elif ch == "$" and i + 1 < n and s[i + 1] == "{":
            out.append("\\$")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
        i += 1
    return "`" + "".join(out) + "`"


def encode_quoted(s, q):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == q:
            out.append("\\" + q)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return q + "".join(out) + q


def encode_leaf(style, value):
    if style == "template":
        return encode_template(value)
    return encode_quoted(value, "'" if style == "single" else '"')


# ---------- JS object literal parsing (find + parse the i18n catalog) ----------

def match_object(src, i):
    depth, N = 0, len(src)
    while i < N:
        c = src[i]
        if c == "{":
            depth += 1; i += 1
        elif c == "}":
            depth -= 1; i += 1
            if depth == 0:
                return i
        elif c == "`":
            i += 1
            while i < N:
                if src[i] == "\\":
                    i += 2; continue
                if src[i] == "`":
                    i += 1; break
                if src[i] == "$" and i + 1 < N and src[i + 1] == "{":
                    j, d2 = i + 2, 1
                    while j < N and d2 > 0:
                        cj = src[j]
                        if cj == "`":
                            j += 1
                            while j < N:
                                if src[j] == "\\":
                                    j += 2; continue
                                if src[j] == "`":
                                    j += 1; break
                                j += 1
                            continue
                        if cj == "{": d2 += 1
                        elif cj == "}": d2 -= 1
                        j += 1
                    i = j; continue
                i += 1
        elif c in "\"'":
            q = c; i += 1
            while i < N:
                if src[i] == "\\":
                    i += 2; continue
                if src[i] == q:
                    i += 1; break
                i += 1
        elif c == "/" and i + 1 < N and src[i + 1] == "/":
            while i < N and src[i] != "\n": i += 1
        elif c == "/" and i + 1 < N and src[i + 1] == "*":
            i += 2
            while i + 1 < N and not (src[i] == "*" and src[i + 1] == "/"): i += 1
            i += 2
        else:
            i += 1
    return -1


class ObjParser:
    def __init__(self, s, i=0):
        self.s, self.i, self.n = s, i, len(s)

    def ws(self):
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "/" and self.i + 1 < n and s[self.i + 1] == "/":
                while self.i < n and s[self.i] != "\n":
                    self.i += 1
            elif c == "/" and self.i + 1 < n and s[self.i + 1] == "*":
                self.i += 2
                while self.i + 1 < n and not (s[self.i] == "*" and s[self.i + 1] == "/"):
                    self.i += 1
                self.i += 2
            else:
                break

    def parse_string(self):
        s, N = self.s, self.n
        start, q = self.i, s[self.i]
        if q == "`":
            self.i += 1
            parts, has_interp, buf = [], False, []
            while self.i < N:
                c = s[self.i]
                if c == "\\":
                    nxt = s[self.i + 1]
                    dec = {"n": "\n", "t": "\t", "r": "\r", "`": "`", "\\": "\\", "$": "$", '"': '"', "'": "'", "b": "\b", "f": "\f"}.get(nxt)
                    if dec is None:
                        if nxt == "u":
                            buf.append(chr(int(s[self.i + 2:self.i + 6], 16))); self.i += 6; continue
                        elif nxt == "x":
                            buf.append(chr(int(s[self.i + 2:self.i + 4], 16))); self.i += 4; continue
                        buf.append(nxt); self.i += 2; continue
                    buf.append(dec); self.i += 2; continue
                if c == "`":
                    self.i += 1; break
                if c == "$" and self.i + 1 < N and s[self.i + 1] == "{":
                    has_interp = True
                    parts.append(("text", "".join(buf))); buf = []
                    j, d, expr_start = self.i + 2, 1, self.i + 2
                    while j < N and d > 0:
                        cj = s[j]
                        if cj == "`":
                            j += 1
                            while j < N:
                                if s[j] == "\\": j += 2; continue
                                if s[j] == "`": j += 1; break
                                j += 1
                            continue
                        if cj == "{": d += 1
                        elif cj == "}":
                            d -= 1
                            if d == 0: break
                        j += 1
                    parts.append(("interp", s[expr_start:j]))
                    self.i = j + 1
                    continue
                buf.append(c); self.i += 1
            parts.append(("text", "".join(buf)))
            decoded = None if has_interp else "".join(p[1] for p in parts if p[0] == "text")
            return start, self.i, "template", decoded, has_interp
        self.i += 1
        buf = []
        while self.i < N:
            c = s[self.i]
            if c == "\\":
                nxt = s[self.i + 1]
                dec = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "b": "\b", "f": "\f"}.get(nxt)
                if dec is None:
                    if nxt == "u":
                        buf.append(chr(int(s[self.i + 2:self.i + 6], 16))); self.i += 6; continue
                    elif nxt == "x":
                        buf.append(chr(int(s[self.i + 2:self.i + 4], 16))); self.i += 4; continue
                    buf.append(nxt); self.i += 2; continue
                buf.append(dec); self.i += 2; continue
            if c == q:
                self.i += 1; break
            buf.append(c); self.i += 1
        return start, self.i, "single" if q == "'" else "double", "".join(buf), False

    def parse_key(self):
        s = self.s
        self.ws()
        if s[self.i] in "\"'":
            return self.parse_string()[3]
        m = ""
        while self.i < self.n and (s[self.i].isalnum() or s[self.i] in "_$"):
            m += s[self.i]; self.i += 1
        return m

    def skip_expr(self, stop):
        s, n, depth = self.s, self.n, 0
        while self.i < n:
            c = s[self.i]
            if c in "([{":
                depth += 1; self.i += 1
            elif c in ")]}":
                if depth == 0 and c in stop:
                    return
                depth -= 1; self.i += 1
            elif depth == 0 and c in stop:
                return
            elif c in "`\"'":
                self.parse_string()
            else:
                self.i += 1

    def parse_value(self, path, leaves):
        self.ws()
        c = self.s[self.i]
        if c == "{":
            return self.parse_object(path, leaves)
        if c == "[":
            return self.parse_array(path, leaves)
        if c in "`\"'":
            start, end, style, decoded, has_interp = self.parse_string()
            leaves.append({"path": path, "span": [start, end], "style": style,
                           "en": decoded, "has_interp": has_interp})
            return decoded
        start = self.i
        while self.i < self.n and self.s[self.i] not in ",}]":
            self.i += 1
        return self.s[start:self.i].strip()

    def parse_object(self, path, leaves):
        assert self.s[self.i] == "{"
        self.i += 1
        obj = {}
        while True:
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1; break
            if self.s[self.i] == "." and self.s[self.i:self.i + 3] == "...":
                self.i += 3
                self.skip_expr("},")
                self.ws()
                if self.i < self.n and self.s[self.i] == ",":
                    self.i += 1
                continue
            key = self.parse_key()
            self.ws()
            assert self.s[self.i] == ":", f"expected ':' at {self.i}"
            self.i += 1
            child = path + [key] if path else [key]
            obj[key] = self.parse_value(child, leaves)
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1
            elif self.s[self.i] == "}":
                self.i += 1; break
        return obj

    def parse_array(self, path, leaves):
        assert self.s[self.i] == "["
        self.i += 1
        idx = 0
        while True:
            self.ws()
            if self.s[self.i] == "]":
                self.i += 1; break
            if self.s[self.i] == "." and self.s[self.i:self.i + 3] == "...":
                self.i += 3
                self.skip_expr("],")
                self.ws()
                if self.i < self.n and self.s[self.i] == ",":
                    self.i += 1
                continue
            self.parse_value(path + [str(idx)], leaves)
            idx += 1
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1
            elif self.s[self.i] == "]":
                self.i += 1; break


def find_catalog(src, anchor=ANCHOR, back=600_000):
    ci = src.find(anchor)
    if ci == -1:
        return None
    start = max(0, ci - back)
    cands = [(start + m.end() - 1, m.group(1))
             for m in re.finditer(r"([A-Za-z0-9_$]+)\s*=\s*(\()?\{", src[start:ci])]
    for abs_open, var in reversed(cands):
        end = match_object(src, abs_open)
        if end > ci:
            return var, abs_open, end
    return None


def parse_catalog(js_text):
    """Return leaves list (path/span/style/en) of the catalog in a bundle,
    with the optional single-language 'en' wrapper normalized away."""
    r = find_catalog(js_text)
    if r is None:
        return None
    _var, o, _e = r
    p = ObjParser(js_text, o)
    leaves = []
    obj = p.parse_object([], leaves)
    if len(obj) == 1 and "en" in obj:  # demo-style {en:{...}} wrapper
        for leaf in leaves:
            leaf["path"] = leaf["path"][1:]
    return leaves


# ---------- PE asset table discovery ----------

def find_assets(exe_bytes):
    pe = pefile.PE(data=exe_bytes, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_BASERELOC"]])
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = [(s.VirtualAddress, max(s.Misc_VirtualSize, s.SizeOfRawData), s.PointerToRawData)
            for s in pe.sections]

    def rva_to_off(rva):
        for va, span, raw in secs:
            if va <= rva < va + span:
                return raw + (rva - va)
        return None

    q = lambda off: struct.unpack_from("<Q", exe_bytes, off)[0]
    entries = {}
    for entry in pe.DIRECTORY_ENTRY_BASERELOC:
        for rel in entry.entries:
            if rel.type != pefile.RELOCATION_TYPE["IMAGE_REL_BASED_DIR64"]:
                continue
            rva = rel.rva
            off = rva_to_off(rva)
            if off is None or off + 32 > len(exe_bytes):
                continue
            plen, dlen = q(off + 8), q(off + 24)
            if not (0 < plen < 512) or not (0 < dlen < 0x100_000_00):
                continue
            poff, doff = rva_to_off(q(off) - base), rva_to_off(q(off + 16) - base)
            if poff is None or doff is None:
                continue
            p = exe_bytes[poff:poff + plen]
            if not p.startswith(b"/") or not all(32 <= c < 127 for c in p):
                continue
            entries[rva] = {"path": p.decode(), "plen": plen, "data_len": dlen,
                            "data_file_off": doff, "entry_off": off}
    pe.close()
    return entries


# ---------- injection ----------

def ph(s):
    return sorted(PLACEHOLDER.findall(s or ""))


def inject(host_src, host_leaves, zh_by_key):
    applied, cursor = 0, 0
    parts = []
    for leaf in sorted(host_leaves, key=lambda l: l["span"][0]):
        zh = zh_by_key.get(".".join(leaf["path"]))
        if zh is None:
            continue
        s0, s1 = leaf["span"]
        parts.extend((host_src[cursor:s0], encode_leaf(leaf["style"], zh)))
        cursor = s1
        applied += 1
    parts.append(host_src[cursor:])
    return "".join(parts), applied


# ---------- durable, content-addressed solution cache ----------

def atomic_write(path, content):
    """Flush a complete file to disk, then replace its destination atomically."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ctzh-", suffix=".tmp",
                                         delete=False) as stream:
            temp_path = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)


def validate_solution(js, target, params, blob):
    if len(blob) != target:
        raise ValueError("压缩尺寸与目标不符")
    if type(params.get("q")) is not int or not 0 <= params["q"] <= 11:
        raise ValueError("无效压缩质量")
    if params.get("method") == "metadata":
        expected = js
    elif params.get("method") == "search":
        R = params.get("R")
        if type(R) is not int or not 0 <= R <= PAD_MAX:
            raise ValueError("无效随机填充长度")
        expected = js + b"\n/*" + get_pad()[:R] + b"*/"
    else:
        raise ValueError("未知求解方式")
    if brotli.decompress(blob) != expected:
        raise ValueError("解压内容校验失败")


class SolutionCache:
    """Each asset commits separately; a manifest only points to a durable blob."""
    def __init__(self, directory):
        self.directory = os.path.join(os.path.abspath(directory), "v2")
        os.makedirs(self.directory, exist_ok=True)

    def identity(self, js, target):
        js_sig = hashlib.sha256(js).hexdigest()
        key = hashlib.sha256(f"ctzh-v2:{target}:{js_sig}".encode()).hexdigest()
        return key, js_sig

    def load(self, js, target, method="auto"):
        key, js_sig = self.identity(js, target)
        methods = ("metadata", "search") if method == "auto" else (method,)
        for kind in methods:
            manifest = os.path.join(self.directory, f"{key}.{kind}.json")
            if not os.path.exists(manifest):
                continue
            try:
                with open(manifest, encoding="utf-8") as stream:
                    entry = json.load(stream)
                if (entry["version"] != 2 or entry["js_sha256"] != js_sig
                        or entry["target"] != target or entry["params"]["method"] != kind):
                    raise ValueError("缓存版本或内容指纹不符")
                digest = entry["blob_sha256"]
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("无效缓存指纹")
                with open(os.path.join(self.directory, f"{key}.{digest}.br"), "rb") as stream:
                    blob = stream.read()
                if hashlib.sha256(blob).hexdigest() != digest:
                    raise ValueError("缓存数据已损坏")
                validate_solution(js, target, entry["params"], blob)
                return entry["params"], blob
            except (OSError, ValueError, KeyError, TypeError, brotli.error) as error:
                print(f"    忽略无效缓存 {os.path.basename(manifest)}: {error}", flush=True)
        return None

    def save(self, js, target, params, blob):
        validate_solution(js, target, params, blob)
        key, js_sig = self.identity(js, target)
        digest = hashlib.sha256(blob).hexdigest()
        entry = {"version": 2, "js_sha256": js_sig, "target": target,
                 "params": params, "blob_sha256": digest}
        atomic_write(os.path.join(self.directory, f"{key}.{digest}.br"), blob)
        atomic_write(os.path.join(self.directory, f"{key}.{params['method']}.json"),
                     json.dumps(entry, ensure_ascii=False, indent=2).encode("utf-8"))


# ---------- exact-size brotli: one compression + metadata padding ----------

def metadata_padding(size):
    """Exactly size bytes of RFC 7932 section 9.2 metadata (no decoded data).

    At a byte boundary: ISLAST=0, MNIBBLES=11, reserved=0,
    MSKIPBYTES (2 bits), MSKIPLEN-1 (0/8/16/24 bits), zero alignment.
    Length fields must use their shortest representation. A zero-length
    block occupies one byte, so even the 1- and 2-byte gaps are fillable.
    """
    if size < 0:
        raise ValueError("填充长度不能为负")
    parts = []
    while size:
        for nbytes in (3, 2, 1):
            count = min(size - nbytes - 1, 1 << (8 * nbytes))
            minimum = 1 if nbytes == 1 else (1 << (8 * (nbytes - 1))) + 1
            if count >= minimum:
                header = 6 | (nbytes << 4) | ((count - 1) << 6)
                parts.append(header.to_bytes(nbytes + 1, "little") + bytes(count))
                size -= count + nbytes + 1
                break
        else:
            parts.append(b"\x06")
            size -= 1
    return b"".join(parts)


def solve_metadata(js, target, stop=None):
    for q in FAST_QUALITIES:
        if stop is not None and stop.is_set():
            raise InterruptedError("求解已停止")
        compressor = brotli.Compressor(quality=q)
        # FLUSH completes the current block and byte-aligns the stream with
        # an empty metadata block. FINISH then emits an empty final block.
        prefix = compressor.process(js) + compressor.flush()
        suffix = compressor.finish()
        if suffix != b"\x03":
            return None  # Unsupported encoder behavior: use verified search.
        gap = target - len(prefix) - len(suffix)
        if gap >= 0:
            blob = prefix + metadata_padding(gap) + suffix
            params = {"method": "metadata", "q": q, "padding": gap}
            try:
                validate_solution(js, target, params, blob)
            except (ValueError, brotli.error):
                return None
            return params, blob
    return None


# ---------- legacy search, with bounded native compression threads ----------

def _probe(js, target, q, R, stop):
    if stop.is_set():
        return None
    blob = brotli.compress(js + b"\n/*" + get_pad()[:R] + b"*/", quality=q)
    return R, len(blob), blob if len(blob) == target else None


def _probes(pool, js, target, q, candidates, workers, stop):
    """Keep at most workers futures in flight; never queue an entire scan."""
    candidates = iter(candidates)
    pending = set()
    try:
        while True:
            while len(pending) < workers and not stop.is_set():
                try:
                    R = next(candidates)
                except StopIteration:
                    break
                pending.add(pool.submit(_probe, js, target, q, R, stop))
            if not pending:
                return
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if result is not None:
                    yield result
    finally:
        for future in pending:
            future.cancel()


def _crossing(observations, target):
    ordered = sorted(observations)
    pairs = [(lo, hi) for lo, hi in zip(ordered, ordered[1:])
             if (observations[lo] - target) * (observations[hi] - target) < 0]
    return min(pairs, key=lambda pair: pair[1] - pair[0]) if pairs else None


def _solve_quality(pool, js, target, q, workers, stop):
    # The old parent process accidentally compressed an empty global _JS here.
    first = _probe(js, target, q, 0, stop)
    if first is None:
        return None
    _, baseline, blob = first
    if blob is not None:
        return {"method": "search", "q": q, "R": 0}, blob
    if baseline > target:
        return None
    observations = {0: baseline}
    hi = min(PAD_MAX, max(64, int((target - baseline) / 0.70) + 256))
    while True:
        result = _probe(js, target, q, hi, stop)
        if result is None:
            return None
        R, size, blob = result
        if blob is not None:
            return {"method": "search", "q": q, "R": R}, blob
        observations[R] = size
        if size > target:
            break
        if hi == PAD_MAX:
            return None
        hi = min(PAD_MAX, hi * 2)

    started = time.monotonic()
    last_report = started
    # Interpolation usually gets close in one batch; dense scans handle the
    # non-monotonic size changes caused by Brotli's coding decisions.
    for _ in range(8):
        lo, hi = _crossing(observations, target)
        guess = round(lo + (target - observations[lo]) * (hi - lo)
                      / (observations[hi] - observations[lo]))
        guess = max(lo + 1, min(hi - 1, guess))
        candidates = sorted((r for r in range(max(lo + 1, guess - workers),
                                               min(hi, guess + workers + 1))
                             if r not in observations), key=lambda r: abs(r - guess))
        if not candidates:
            break
        for R, size, blob in _probes(pool, js, target, q, candidates, workers, stop):
            if blob is not None:
                return {"method": "search", "q": q, "R": R}, blob
            observations[R] = size
            if time.monotonic() - last_report >= 15:
                print(f"      q={q}: 已试 {len(observations)} 次, {time.monotonic()-started:.0f}s",
                      flush=True)
                last_report = time.monotonic()
    lo, hi = _crossing(observations, target)
    center = (lo + hi) // 2
    for margin in (400, 1600):
        candidates = sorted((r for r in range(max(0, lo - margin), min(PAD_MAX, hi + margin) + 1)
                             if r not in observations), key=lambda r: abs(r - center))
        for R, size, blob in _probes(pool, js, target, q, candidates, workers, stop):
            if blob is not None:
                return {"method": "search", "q": q, "R": R}, blob
            observations[R] = size
            if time.monotonic() - last_report >= 15:
                print(f"      q={q}: 已试 {len(observations)} 次, {time.monotonic()-started:.0f}s",
                      flush=True)
                last_report = time.monotonic()
    return None


def solve(js, target, workers=None, on_success=None):
    """Legacy search. Commit a hit BEFORE waiting for in-flight calls to finish."""
    workers = workers or default_workers()
    get_pad()
    stop = threading.Event()
    # Google's Brotli binding releases the GIL while compressing, so threads
    # run on separate CPU cores and share JS/PAD without Windows spawn copies.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        for q in QUALITIES:
            result = _solve_quality(pool, js, target, q, workers, stop)
            if result is not None:
                stop.set()
                validate_solution(js, target, *result)
                if on_success is not None:
                    on_success(*result)
                return result
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
    raise RuntimeError("无法求得精确压缩尺寸:请确认游戏版本与语言包;此前成功的结果已保留")


def default_workers():
    return max(1, getattr(os, "process_cpu_count", os.cpu_count)() or 1)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须至少为 1")
    return number


def load_legacy_cache(exe_size, lang_sig):
    path = os.path.join(HERE, "solutions", f"{exe_size}.json")
    try:
        with open(path, encoding="utf-8") as stream:
            entry = json.load(stream)
        if entry.get("lang_sig") == lang_sig and isinstance(entry.get("solutions"), dict):
            return entry["solutions"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as error:
        print(f"忽略无效旧缓存: {error}", flush=True)
    return {}


def solve_hosts(hosts, cache, legacy, workers, method):
    """Parallelize fast jobs across hosts; search jobs share one CPU budget."""
    stop = threading.Event()

    def save(host, params, blob):
        cache.save(host["js"], host["asset"]["data_len"], params, blob)
        host["blob"] = blob
        detail = (f"metadata={params['padding']:,}" if params["method"] == "metadata"
                  else f"R={params['R']:,}")
        print(f"    {host['name']}: 注入 {host['applied']} 处, q={params['q']} {detail}, "
              "校验 OK, 已缓存到磁盘", flush=True)

    def fast_or_cached(host):
        if stop.is_set():
            raise InterruptedError("求解已停止")
        js, target = host["js"], host["asset"]["data_len"]
        result = cache.load(js, target, method)
        if result is not None:
            host["blob"] = result[1]
            print(f"    {host['name']}: 磁盘缓存命中, 已校验", flush=True)
            return True
        if method != "search":
            result = solve_metadata(js, target, stop)
            if result is not None:
                save(host, *result)
                return True
        if method != "metadata":
            params = legacy.get(host["name"])
            if isinstance(params, dict):
                q, R = params.get("q"), params.get("R")
                if type(q) is int and 0 <= q <= 11 and type(R) is int and 0 <= R <= PAD_MAX:
                    if stop.is_set():
                        raise InterruptedError("求解已停止")
                    blob = brotli.compress(js + b"\n/*" + get_pad()[:R] + b"*/", quality=q)
                    if len(blob) == target:
                        save(host, {"method": "search", "q": q, "R": R}, blob)
                        return True
        return False

    if not hosts:
        return
    pool = ThreadPoolExecutor(max_workers=min(workers, len(hosts)))
    pending = []
    try:
        futures = {pool.submit(fast_or_cached, host): host for host in hosts}
        while futures:
            done, _ = wait(futures, timeout=15, return_when=FIRST_COMPLETED)
            if not done:
                print(f"    仍在压缩 {len(futures)} 个文件;已完成的结果均已落盘", flush=True)
            for future in done:
                host = futures.pop(future)
                if not future.result():
                    pending.append(host)
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
    for host in pending:
        if method == "metadata":
            raise RuntimeError(f"{host['name']}: 快速压缩无法装入目标尺寸,可用 --method auto 回退搜索")
        print(f"  {host['name']}: 转入随机注释搜索,最多 {workers} 个线程", flush=True)
        solve(host["js"], host["asset"]["data_len"], workers,
              on_success=lambda params, blob, h=host: save(h, params, blob))


# ---------- main ----------

def locate_exe():
    for root in (os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"D:\Steam"):
        vdf = os.path.join(root, "steam", "steamapps", "libraryfolders.vdf")
        if not os.path.exists(vdf):
            continue
        for m in re.finditer(r'"path"\s*"(.*?)"', open(vdf, encoding="utf-8", errors="ignore").read()):
            cand = m.group(1).replace("\\\\", "\\")
            e = os.path.join(cand, "steamapps", "common", "CodeTerraform", "code-terraform.exe")
            if os.path.exists(e):
                return e
            e2 = os.path.join(cand, "common", "CodeTerraform", "code-terraform.exe")
            if os.path.exists(e2):
                return e2
    sys.exit("找不到游戏:请用 --exe 指定 code-terraform.exe 的完整路径")


def main():
    ap = argparse.ArgumentParser(description="Code: Terraform 中文版补丁")
    ap.add_argument("--exe", help="code-terraform.exe 路径(默认自动查找)")
    ap.add_argument("--lang", default=os.path.join(HERE, "language.json"))
    actions = ap.add_mutually_exclusive_group()
    actions.add_argument("--restore", action="store_true", help="从 .bak 恢复原版")
    actions.add_argument("--extract-only", action="store_true", help="只导出字典结构 leaves.json")
    actions.add_argument("--cache-only", action="store_true", help="只求解并缓存,不改写游戏")
    ap.add_argument("--workers", type=positive_int, default=default_workers(),
                    help="压缩线程上限(默认使用全部可用 CPU 逻辑线程)")
    ap.add_argument("--method", choices=("auto", "metadata", "search"), default="auto",
                    help="auto:快速填充并自动回退; metadata:只用快速填充; search:传统搜索")
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "solutions"), help="求解缓存目录")
    ap.add_argument("--inplace", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    exe = args.exe or locate_exe()
    if args.restore:
        shutil_restore(exe)
        return

    print(f"游戏: {exe} ({os.path.getsize(exe):,} 字节)")
    with open(exe, "rb") as stream:
        orig = stream.read()
    entries = find_assets(orig)
    print(f"资产表: 发现 {len(entries)} 项")

    # decompress + identify dictionary hosts
    hosts = []
    for rva, a in entries.items():
        if not a["path"].endswith(".js") or not a["path"].startswith("/assets/"):
            continue
        try:
            raw = brotli.decompress(orig[a["data_file_off"]:a["data_file_off"] + a["data_len"]])
        except Exception:
            continue
        if CATALOG_START.encode() not in raw:
            continue
        js = raw.decode("utf-8")
        leaves = parse_catalog(js)
        if leaves and len(leaves) > 5000:
            hosts.append({"asset": a, "src": js, "leaves": leaves})
    if not hosts:
        sys.exit("未找到字典宿主 bundle:游戏版本可能不受支持,或文件已被修改")
    print(f"字典宿主: {len(hosts)} 个 -> " + ", ".join(os.path.basename(h['asset']['path']) for h in hosts))

    canon = sorted(hosts[0]["leaves"], key=lambda l: l["span"][0])

    if args.extract_only:
        out = [{"id": i, "key": ".".join(l["path"]), "en": l["en"]} for i, l in enumerate(canon)]
        atomic_write(os.path.join(HERE, "leaves.json"),
                     json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"))
        print(f"leaves.json: {len(out)} 条(含英文原文,供编辑参考)")
        return

    # load language + validate against this exe's English
    with open(args.lang, encoding="utf-8") as stream:
        lang = json.load(stream)
    by_key = {e["key"]: e for e in lang}
    zh_by_key, bad = {}, 0
    for i, leaf in enumerate(canon):
        key = ".".join(leaf["path"])
        e = by_key.get(key)
        if not e or not (e.get("zh") or "").strip():
            continue
        if ph(leaf["en"]) != ph(e["zh"]):
            bad += 1
            continue
        zh_by_key[key] = e["zh"].strip()
    print(f"语言包: {len(lang)} 条,可注入 {len(zh_by_key)} 条"
          + (f",占位符不合规跳过 {bad} 条" if bad else ""))

    # Preserve shipped q/R hints; new caches are isolated by actual JS + length.
    lang_sig = hashlib.sha256(json.dumps(zh_by_key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    legacy = load_legacy_cache(len(orig), lang_sig)
    cache = SolutionCache(args.cache_dir)
    print(f"压缩线程上限: {args.workers}; 求解方式: {args.method}", flush=True)
    print(f"逐项缓存: {cache.directory}", flush=True)
    for h in hosts:
        h["name"] = os.path.basename(h["asset"]["path"])
        print(f"  {h['name']}: 准备注入(目标压缩尺寸 {h['asset']['data_len']:,}) ...", flush=True)
        new_src, applied = inject(h["src"], h["leaves"], zh_by_key)
        h["js"], h["applied"] = new_src.encode("utf-8"), applied
    started = time.monotonic()
    solve_hosts(hosts, cache, legacy, args.workers, args.method)
    print(f"全部求解完成: {time.monotonic()-started:.2f}s", flush=True)
    if args.cache_only:
        print("已完成全部缓存。再次运行相同命令并去掉 --cache-only 即可应用汉化。")
        return

    # splice all-or-nothing in memory; only then touch the exe
    data = bytearray(orig)
    for h in hosts:
        a = h["asset"]
        if (not 0 <= a["data_file_off"] <= len(data) - a["data_len"]
                or len(h["blob"]) != a["data_len"]):
            raise RuntimeError(f"{h['name']}: 资产范围或压缩长度校验失败")
        data[a["data_file_off"]:a["data_file_off"] + a["data_len"]] = h["blob"]
    if len(data) != len(orig):
        raise RuntimeError("打包后文件尺寸发生变化,已停止写入")

    bak = exe + ".bak"
    if not os.path.exists(bak):
        print(f"备份原版 -> {bak}")
        atomic_write(bak, orig)
    atomic_write(exe, data)
    print(f"完成!{exe} 已汉化(大小 {os.path.getsize(exe):,} 不变)。启动游戏即生效。")


def shutil_restore(exe):
    bak = exe + ".bak"
    if not os.path.exists(bak):
        sys.exit("没有 .bak 备份,无需恢复")
    import shutil
    shutil.copy2(bak, exe)
    print("已从 .bak 恢复原版")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。已成功求解并落盘的结果会在下次运行时直接复用。", file=sys.stderr)
        sys.exit(130)
