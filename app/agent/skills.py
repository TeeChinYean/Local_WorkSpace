"""Skills: ``<storage>/agent/skills/<slug>/SKILL.md`` with YAML frontmatter (name, description, enabled).

Progressive disclosure (like Claude skills): enabled skills' ``name: description`` lines go into the
system prompt; the model calls ``load_skill`` / ``read_skill_file`` to get the full text.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import AgentStore

SKILL_FILE = "SKILL.md"
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_UNZIPPED_BYTES = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_SKILL_MD_BYTES = 1024 * 1024
MAX_READ_FILE_BYTES = 200 * 1024
MAX_LIST_FILES = 200
MAX_NAME = 100
MAX_DESCRIPTION = 1024

_FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


class SkillError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ----------------------------------------------------------------------------- frontmatter
def _scalar(v: str) -> Any:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\n", "\n") if v[0] == '"' else inner.replace("''", "'")
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    return v


def _simple_yaml(text: str) -> Dict[str, Any]:
    """Tiny `key: value` parser (+ folded/literal `|` `>` blocks and indented continuation lines)."""
    out: Dict[str, Any] = {}
    key: Optional[str] = None
    block: Optional[str] = None
    buf: List[str] = []

    def flush():
        nonlocal key, block, buf
        if key is not None and block is not None:
            lines = [ln.strip() for ln in buf]
            out[key] = ("\n" if block == "|" else " ").join(lines).strip()
        elif key is not None and buf:
            out[key] = (str(out.get(key) or "") + " " + " ".join(ln.strip() for ln in buf)).strip()
        key, block, buf = None, None, []

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            if block is not None:
                buf.append("")
            continue
        if line[:1] in (" ", "\t") and key is not None:
            buf.append(line)
            continue
        flush()
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw.strip() in ("|", ">", "|-", ">-", "|+", ">+"):
            block = raw.strip()[0]
            out[key] = ""
        else:
            out[key] = _scalar(raw)
    flush()
    return out


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """-> (meta, body). Uses PyYAML when installed, else the tiny built-in parser."""
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    raw = m.group(1)
    meta: Any = None
    try:
        import yaml  # type: ignore
        meta = yaml.safe_load(raw)
    except ImportError:
        meta = None
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        meta = _simple_yaml(raw)
    meta = {str(k): v for k, v in meta.items()}
    return meta, text[m.end():]


def _yaml_str(v: str) -> str:
    v = str(v).replace("\r", " ").replace("\n", " ").strip()
    if v == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`,]|^[-?]|\s$", v) or v.lower() in ("true", "false", "yes", "no", "null", "~"):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def render_skill_md(name: str, description: str, body: str, enabled: bool = True,
                    extra: Optional[Dict[str, Any]] = None) -> str:
    lines = ["---", f"name: {_yaml_str(name)}", f"description: {_yaml_str(description)}"]
    if not enabled:
        lines.append("enabled: false")
    for k, v in (extra or {}).items():
        if k in ("name", "description", "enabled") or not re.match(r"^[A-Za-z0-9_\-]+$", str(k)):
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (str, int, float)):
            lines.append(f"{k}: {_yaml_str(str(v))}")
    lines.append("---")
    body = (body or "").lstrip("\r\n")
    return "\n".join(lines) + "\n\n" + body + ("" if body.endswith("\n") or not body else "\n")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:48].strip("-")
    if not s:
        s = "skill-" + hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:8]
    return s


def _validate_meta(name: Any, description: Any) -> Tuple[str, str]:
    n = str(name or "").strip()
    d = str(description or "").strip()
    if not n:
        raise SkillError(400, "技能缺少 name")
    if not d:
        raise SkillError(400, "技能缺少 description")
    if len(n) > MAX_NAME:
        raise SkillError(400, f"技能名称过长（最多 {MAX_NAME} 字符）")
    if len(d) > MAX_DESCRIPTION:
        raise SkillError(400, f"技能描述过长（最多 {MAX_DESCRIPTION} 字符）")
    return n, d


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "no", "off", "0")


# ----------------------------------------------------------------------------- store
class SkillStore:
    def __init__(self, store: AgentStore):
        self.store = store

    @property
    def root(self) -> str:
        return self.store.skills_dir

    def _dir(self, slug: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]{0,63}$", slug or ""):
            raise SkillError(404, "技能不存在")
        return os.path.join(self.root, slug)

    def _read(self, slug: str) -> Dict[str, Any]:
        d = self._dir(slug)
        path = os.path.join(d, SKILL_FILE)
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_SKILL_MD_BYTES + 1)
        except OSError:
            raise SkillError(404, "技能不存在")
        text = raw[:MAX_SKILL_MD_BYTES].decode("utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        return {"slug": slug, "name": str(meta.get("name") or slug), "description": str(meta.get("description") or ""),
                "enabled": _as_bool(meta.get("enabled"), True), "content": text, "body": body, "meta": meta, "dir": d}

    def _files(self, d: str) -> List[str]:
        out: List[str] = []
        for base, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(base, x)))
            for fn in sorted(files):
                rel = os.path.relpath(os.path.join(base, fn), d).replace(os.sep, "/")
                out.append(rel)
                if len(out) >= MAX_LIST_FILES:
                    return out
        return out

    def list(self) -> List[Dict[str, Any]]:
        out = []
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        for slug in names:
            if not os.path.isfile(os.path.join(self.root, slug, SKILL_FILE)):
                continue
            try:
                s = self._read(slug)
            except SkillError:
                continue
            out.append({"slug": slug, "name": s["name"], "description": s["description"], "enabled": s["enabled"],
                        "files": self._files(s["dir"])})
        return out

    def get(self, slug: str) -> Dict[str, Any]:
        s = self._read(slug)
        return {"slug": slug, "name": s["name"], "description": s["description"], "enabled": s["enabled"],
                "content": s["content"], "files": self._files(s["dir"])}

    def _unique_slug(self, name: str) -> str:
        slug = slugify(name)
        if os.path.exists(os.path.join(self.root, slug)):
            raise SkillError(409, f"同名技能已存在: {slug}")
        return slug

    def _compose(self, name: Any, description: Any, content: Any, enabled: Any = None,
                 current: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
        """Content may be a full SKILL.md (with frontmatter) or just the body."""
        meta: Dict[str, Any] = dict(current["meta"]) if current else {}
        body = current["body"] if current else ""
        if content is not None:
            c = str(content)
            fm, b = parse_frontmatter(c)
            if fm:
                meta = fm
            body = b
        n = name if name is not None else meta.get("name")
        d = description if description is not None else meta.get("description")
        n, d = _validate_meta(n, d)
        en = _as_bool(enabled) if enabled is not None else _as_bool(meta.get("enabled"), True)
        md = render_skill_md(n, d, body, en, extra=meta)
        if len(md.encode("utf-8")) > MAX_SKILL_MD_BYTES:
            raise SkillError(413, "SKILL.md 过大（最多 1MB）")
        return n, d, md

    def create(self, name: Any, description: Any, content: Any, enabled: Any = None) -> Dict[str, Any]:
        with self.store.lock:
            n, _d, md = self._compose(name, description, content if content is not None else "", enabled)
            slug = self._unique_slug(n)
            d = os.path.join(self.root, slug)
            os.makedirs(d)
            _write_text(os.path.join(d, SKILL_FILE), md)
            return self.get(slug)

    def update(self, slug: str, name: Any = None, description: Any = None, content: Any = None,
               enabled: Any = None) -> Dict[str, Any]:
        with self.store.lock:
            cur = self._read(slug)
            _n, _d, md = self._compose(name, description, content, enabled, current=cur)
            _write_text(os.path.join(cur["dir"], SKILL_FILE), md)
            return self.get(slug)

    def delete(self, slug: str) -> str:
        with self.store.lock:
            d = self._dir(slug)
            if not os.path.isdir(d):
                raise SkillError(404, "技能不存在")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            dest = os.path.join(self.store.trash_dir, stamp, "skills", slug)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(d, dest)
            return dest

    # ---------------------------------------------------------------- import
    def import_file(self, filename: str, data: bytes) -> Dict[str, Any]:
        if len(data) > MAX_IMPORT_BYTES:
            raise SkillError(413, "文件过大（最多 5MB）")
        low = (filename or "").lower()
        if low.endswith(".zip") or data[:4] == b"PK\x03\x04":
            return self._import_zip(data)
        if not (low.endswith(".md") or low.endswith(".markdown") or low.endswith(".txt") or not low):
            raise SkillError(400, "仅支持 .md 或 .zip 文件")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SkillError(400, "SKILL.md 必须是 UTF-8 编码")
        meta, _body = parse_frontmatter(text)
        if not meta:
            raise SkillError(400, "SKILL.md 缺少 YAML frontmatter (name / description)")
        n, _d = _validate_meta(meta.get("name"), meta.get("description"))
        with self.store.lock:
            slug = self._unique_slug(n)
            d = os.path.join(self.root, slug)
            os.makedirs(d)
            _write_text(os.path.join(d, SKILL_FILE), text)
            return self.get(slug)

    def _import_zip(self, data: bytes) -> Dict[str, Any]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise SkillError(400, "无效的 zip 文件")
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise SkillError(400, f"zip 内文件过多（最多 {MAX_ZIP_ENTRIES} 个）")
            members: List[Tuple[zipfile.ZipInfo, List[str]]] = []
            total = 0
            for info in infos:
                parts = _safe_zip_parts(info.filename)
                if parts is None:
                    raise SkillError(400, f"zip 内包含非法路径: {info.filename}")
                if not parts or info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise SkillError(400, f"zip 内不允许符号链接: {info.filename}")
                if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
                    continue
                total += info.file_size
                if total > MAX_UNZIPPED_BYTES:
                    raise SkillError(413, "zip 解压后过大（最多 20MB）")
                members.append((info, parts))
            # locate SKILL.md: shallowest one wins
            cands = sorted((len(p), p) for _i, p in members if p[-1].lower() == "skill.md")
            if not cands:
                raise SkillError(400, "zip 中没有找到 SKILL.md")
            prefix = cands[0][1][:-1]
            skill_info = next(i for i, p in members if p == cands[0][1])
            raw = zf.read(skill_info)
            if len(raw) > MAX_SKILL_MD_BYTES:
                raise SkillError(413, "SKILL.md 过大（最多 1MB）")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise SkillError(400, "SKILL.md 必须是 UTF-8 编码")
            meta, _body = parse_frontmatter(text)
            if not meta:
                raise SkillError(400, "SKILL.md 缺少 YAML frontmatter (name / description)")
            n, _d = _validate_meta(meta.get("name"), meta.get("description"))
            with self.store.lock:
                slug = self._unique_slug(n)
                d = os.path.join(self.root, slug)
                tmp = d + ".importing"
                shutil.rmtree(tmp, ignore_errors=True)
                os.makedirs(tmp)
                try:
                    real_tmp = os.path.realpath(tmp)
                    for info, parts in members:
                        if parts[:len(prefix)] != prefix:
                            continue
                        rel = parts[len(prefix):]
                        if rel == ["SKILL.md"] or (len(rel) == 1 and rel[0].lower() == "skill.md"):
                            rel = [SKILL_FILE]
                        dest = os.path.join(tmp, *rel)
                        if os.path.commonpath([os.path.realpath(dest), real_tmp]) != real_tmp:
                            raise SkillError(400, f"zip 内包含非法路径: {info.filename}")
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with zf.open(info) as src, open(dest, "wb") as out:
                            remaining = MAX_UNZIPPED_BYTES
                            while True:
                                chunk = src.read(65536)
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                if remaining < 0:  # lying header: stop zip bombs
                                    raise SkillError(413, "zip 解压后过大（最多 20MB）")
                                out.write(chunk)
                    os.replace(tmp, d)
                except BaseException:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise
                return self.get(slug)

    # ---------------------------------------------------------------- model-facing
    def enabled_skills(self) -> List[Dict[str, Any]]:
        return [s for s in self.list() if s["enabled"]]

    def find_enabled(self, name: str) -> Optional[Dict[str, Any]]:
        key = str(name or "").strip().lower()
        if not key:
            return None
        for s in self.enabled_skills():
            if s["name"].lower() == key or s["slug"] == key or slugify(key) == s["slug"]:
                return s
        return None

    def load_skill_text(self, name: str) -> str:
        s = self.find_enabled(name)
        if not s:
            names = ", ".join(x["name"] for x in self.enabled_skills()) or "（无）"
            raise SkillError(404, f"未找到已启用的技能: {name}。可用技能: {names}")
        full = self._read(s["slug"])
        extra = [f for f in s["files"] if f != SKILL_FILE]
        tail = ""
        if extra:
            tail = ("\n\n---\n该技能目录中的其他文件（可用 read_skill_file 读取）:\n"
                    + "\n".join(f"- {f}" for f in extra))
        return full["content"] + tail

    def read_skill_file(self, name: str, path: str) -> str:
        s = self.find_enabled(name)
        if not s:
            raise SkillError(404, f"未找到已启用的技能: {name}")
        parts = _safe_zip_parts(str(path or ""))
        if not parts:
            raise SkillError(400, "非法路径")
        d = os.path.realpath(os.path.join(self.root, s["slug"]))
        full = os.path.realpath(os.path.join(d, *parts))
        if os.path.commonpath([full, d]) != d or full == d:
            raise SkillError(400, "路径超出技能目录")
        if not os.path.isfile(full):
            raise SkillError(404, f"文件不存在: {path}")
        with open(full, "rb") as f:
            raw = f.read(MAX_READ_FILE_BYTES + 1)
        if b"\x00" in raw[:8192]:
            raise SkillError(400, "二进制文件无法读取")
        text = raw[:MAX_READ_FILE_BYTES].decode("utf-8", errors="replace")
        if len(raw) > MAX_READ_FILE_BYTES:
            text += "\n…(文件过大，已截断)"
        return text

    def prompt_block(self) -> str:
        skills = self.enabled_skills()
        if not skills:
            return ""
        lines = [f"- {s['name']}: {s['description'][:300]}" for s in skills[:50]]
        return ("【可用技能 (Skills)】\n" + "\n".join(lines) +
                "\n当用户的任务与某个技能相关时，先调用 load_skill 工具（参数 name）读取该技能的完整说明，再按说明执行。")


def _safe_zip_parts(name: str) -> Optional[List[str]]:
    """Split a zip member / relative path; None when it is absolute or escapes (zip-slip)."""
    n = str(name).replace("\\", "/")
    if "\x00" in n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or ":" in p:
            return None
    return parts


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
