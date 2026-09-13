#!/usr/bin/env python3
"""Code: Terraform (868160) 正式版一键中文补丁.

用法:
    python ctzh.py                        # 自动定位游戏 exe 并应用汉化(备份原版为 .bak)
    python ctzh.py --exe <path>           # 指定 code-terraform.exe 路径
    python ctzh.py --restore              # 从 .bak 恢复原版
    python ctzh.py --extract-only         # 只提取字典结构到 leaves.json(供编辑参考)
    python ctzh.py --lang my.json         # 使用自定义语言文件

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
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import brotli
import pefile

HERE = os.path.dirname(os.path.abspath(__file__))
ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
PAD_MAX = 6_000_000
QUALITIES = (11, 10, 9)
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
ANCHOR = "console:{ready:"
CATALOG_START = "app:{title:"

_rng = random.Random(0xC0FFEE)
PAD = "".join(_rng.choice(ALNUM) for _ in range(PAD_MAX)).encode()

_JS = b""
_T = 0


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
    applied = 0
    out = host_src
    for leaf in sorted(host_leaves, key=lambda l: l["span"][0], reverse=True):
        zh = zh_by_key.get(".".join(leaf["path"]))
        if zh is None:
            continue
        s0, s1 = leaf["span"]
        out = out[:s0] + encode_leaf(leaf["style"], zh) + out[s1:]
        applied += 1
    return out, applied


# ---------- exact-size brotli solve ----------

def _init(js, t):
    global _JS, _T
    _JS, _T = js, t


def _probe(args):
    q, R = args
    blob = brotli.compress(_JS + b"\n/*" + PAD[:R] + b"*/", quality=q)
    return q, R, len(blob), blob if len(blob) == _T else None


def _solve_quality(pool, T_LEN, q):
    c0 = len(brotli.compress(_JS, quality=q))
    if c0 > T_LEN - 32:
        return None
    hi_bound = min(PAD_MAX - 100, int((T_LEN - c0) / 0.70) + 4000)
    step = max(400, hi_bound // 400)
    grid = list(range(0, hi_bound + 1, step))
    res = {}
    t0 = time.time()
    for fut in as_completed([pool.submit(_probe, (q, r)) for r in grid]):
        _q, R, n, blob = fut.result()
        if blob is not None:
            return R, blob
        res[R] = n
    prev, bracket = grid[0], None
    for r in grid[1:]:
        a, b = res[prev] - T_LEN, res[r] - T_LEN
        if (a < 0 <= b) or (a > 0 >= b):
            bracket = (prev, r); break
        prev = r
    if bracket is None:
        return None
    lo, hi = bracket
    dlo, dhi = max(0, lo - step), min(PAD_MAX - 100, hi + step)
    for fut in as_completed([pool.submit(_probe, (q, r)) for r in range(dlo, dhi + 1)]):
        _q, R, n, blob = fut.result()
        if blob is not None:
            print(f"      hit q={q} R={R:,} ({time.time()-t0:.0f}s)", flush=True)
            return R, blob
    return None


def solve(js, T_LEN, cache=None):
    """Return (q, R, blob) where blob == compress(js+\n/*PAD[R]*/, q), len == T_LEN."""
    if cache:
        blob = brotli.compress(js + b"\n/*" + PAD[:cache["R"]] + b"*/", quality=cache["q"])
        if len(blob) == T_LEN:
            print("      precomputed solution hit", flush=True)
            return cache["q"], cache["R"], blob
    workers = max(2, min(8, os.cpu_count() or 4))
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(js, T_LEN)) as pool:
        for q in QUALITIES:
            r = _solve_quality(pool, T_LEN, q)
            if r:
                return q, r[0], r[1]
    raise RuntimeError("无法为资产求得精确压缩尺寸(请确认游戏版本与补丁适用版本一致)")


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
    ap.add_argument("--restore", action="store_true", help="从 .bak 恢复原版")
    ap.add_argument("--extract-only", action="store_true", help="只导出字典结构 leaves.json")
    args = ap.parse_args()

    exe = args.exe or locate_exe()
    if args.restore:
        shutil_restore(exe)
        return

    print(f"游戏: {exe} ({os.path.getsize(exe):,} 字节)")
    orig = open(exe, "rb").read()
    data = bytearray(orig)
    entries = find_assets(bytes(data))
    print(f"资产表: 发现 {len(entries)} 项")

    # decompress + identify dictionary hosts
    hosts = []
    for rva, a in entries.items():
        if not a["path"].endswith(".js") or not a["path"].startswith("/assets/"):
            continue
        try:
            raw = brotli.decompress(bytes(data[a["data_file_off"]:a["data_file_off"] + a["data_len"]]))
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

    canon = parse_catalog(hosts[0]["src"])
    canon = sorted(canon, key=lambda l: l["span"][0])

    if args.extract_only:
        out = [{"id": i, "key": ".".join(l["path"]), "en": l["en"]} for i, l in enumerate(canon)]
        json.dump(out, open(os.path.join(HERE, "leaves.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"leaves.json: {len(out)} 条(含英文原文,供编辑参考)")
        return

    # load language + validate against this exe's English
    lang = json.load(open(args.lang, encoding="utf-8"))
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

    # solve cache keyed by exe+lang
    sol_path = os.path.join(HERE, "solutions", f"{os.path.getsize(exe)}.json")
    lang_sig = hashlib.sha256(json.dumps(zh_by_key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cached = {}
    if os.path.exists(sol_path):
        j = json.load(open(sol_path, encoding="utf-8"))
        if j.get("lang_sig") == lang_sig:
            cached = j.get("solutions", {})

    solutions = {}
    for h in hosts:
        name = os.path.basename(h["asset"]["path"])
        T_LEN = h["asset"]["data_len"]
        print(f"  {name}: 注入并求解(目标压缩尺寸 {T_LEN:,}) ...", flush=True)
        new_src, applied = inject(h["src"], h["leaves"], zh_by_key)
        js = new_src.encode("utf-8")
        q, R, blob = solve(js, T_LEN, cached.get(name))
        # byte-exact roundtrip check before anything touches disk
        dec = brotli.decompress(blob)
        if len(blob) != T_LEN or not dec.startswith(js) or dec != js + b"\n/*" + PAD[:R] + b"*/":
            raise RuntimeError(f"{name}: 求解结果校验失败")
        h["blob"] = blob
        solutions[name] = {"q": q, "R": R}
        print(f"    注入 {applied} 处, q={q} R={R:,} 校验 OK", flush=True)

    if len(solutions) == len(hosts):
        os.makedirs(os.path.join(HERE, "solutions"), exist_ok=True)
        json.dump({"lang_sig": lang_sig, "solutions": solutions},
                  open(sol_path, "w", encoding="utf-8"))

    # splice all-or-nothing in memory; only then touch the exe
    for h in hosts:
        a = h["asset"]
        data[a["data_file_off"]:a["data_file_off"] + a["data_len"]] = h["blob"]

    bak = exe + ".bak"
    if not os.path.exists(bak):
        print(f"备份原版 -> {bak}")
        open(bak, "wb").write(orig)          # untouched original bytes
    open(exe + ".tmp", "wb").write(bytes(data))
    os.replace(exe + ".tmp", exe)
    print(f"完成!{exe} 已汉化(大小 {os.path.getsize(exe):,} 不变)。启动游戏即生效。")


def shutil_restore(exe):
    bak = exe + ".bak"
    if not os.path.exists(bak):
        sys.exit("没有 .bak 备份,无需恢复")
    import shutil
    shutil.copy2(bak, exe)
    print("已从 .bak 恢复原版")


if __name__ == "__main__":
    main()
