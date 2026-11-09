# /// script
# requires-python = ">=3.10"
# ///
"""Export reviewed items to a Feishu (Lark) bitable.

Two transports, by what config export.feishu holds:
  · app_id + app_secret (Settings → Feishu binds them): the app's own credentials — nothing to install.
  · without them: lark-cli as the signed-in user (the developer's machine).

Used by the app's list action Export to Feishu (MainWindowController.exportFeishu, one call per project) and directly:

  feishu_export.py SESSION_DIR --ids IN-1 IN-2 [--dry-run]
  feishu_export.py --project inbox --ids IN-1 --dry-run
  feishu_export.py bind --app-key KEY --table URL --owner NAME   (App Secret comes on stdin)
  feishu_export.py init BASE_TOKEN TABLE_ID OWNER_OPEN_ID [--name 名字]   (lark-cli mode)

Config (~/.blurt/config.json, key export.feishu — `bind`/`init` write it): app_id, app_secret,
base_token, table_id, owner_open_id (fills 提出人), optional columns {} to rename the target columns,
status_default (待处理). The detail column (详细描述) comes from the item's `summary` field — written
when the recording was processed (the pm lens does); items without one fall back to
"{module}。{actual}；预期 {expected}". severity high/medium/low maps to 高/中/低; type passes through
only when it is one of the table's options (功能异常 · 计算错误 · 性能问题 · UI 文案 · 其他), else the
cell is left empty and the item's result says so.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import die, global_config, update_global_config  # noqa: E402
import items as items_mod  # noqa: E402

DEFAULT_COLUMNS = {  # the template table's names; override per config "columns"
    "title": "问题描述", "detail": "详细描述", "status": "状态", "severity": "严重程度",
    "type": "问题类型", "attachments": "截图", "proposer": "提出人", "time": "提出时间",
}
SEVERITY = {"high": "高", "medium": "中", "low": "低"}
TYPES = ("功能异常", "计算错误", "性能问题", "UI 文案", "其他")
UNSAFE = re.compile(r"[/\\:*?\"<>|\s]+")


class ExportError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def load_cfg() -> dict:
    """export.feishu from the global config, checked."""
    cfg = (global_config().get("export") or {}).get("feishu") or {}
    missing = [k for k in ("base_token", "table_id") if not cfg.get(k)]
    if missing:
        raise ExportError("config", f"config export.feishu is missing {', '.join(missing)} — "
                                     f"run: feishu_export.py init <base_token> <table_id> <owner_open_id>")
    cfg.setdefault("owner_open_id", "")
    cfg.setdefault("status_default", "待处理")
    cfg["columns"] = {**DEFAULT_COLUMNS, **(cfg.get("columns") or {})}
    return cfg


def _detail(item: dict) -> tuple[str, str]:
    """详细描述: the item's `summary`, or the rule fallback. → (text, source)."""
    s = (item.get("summary") or "").strip()
    if s:
        return s, "summary"
    seen, expected = item.get("actual"), f"预期 {item.get('expected')}".strip()
    phenomenon = "；".join(p for p in (seen, expected if item.get("expected") else "") if p)
    parts = [p for p in (item.get("module"), phenomenon) if p]
    return "。".join(parts), "fallback"


def row_for(item: dict, cfg: dict) -> tuple[dict, list[str]]:
    """An item → the record's cells (Feishu CellValue shapes). → (cells, notes)."""
    cols, notes = cfg["columns"], []
    title = (item.get("title") or "").strip()
    if not title:
        raise ExportError("bad_item", f"{item.get('id')}: empty title")
    cells = {cols["title"]: title, cols["detail"]: _detail(item)[0], cols["status"]: [cfg["status_default"]]}
    if item.get("severity") in SEVERITY:
        cells[cols["severity"]] = [SEVERITY[item["severity"]]]
    if item.get("type") in TYPES:
        cells[cols["type"]] = [item["type"]]
    elif item.get("type"):
        notes.append(f"type \"{item['type']}\" 不是表格选项，问题类型留空")
    if cfg["owner_open_id"]:
        cells[cols["proposer"]] = [{"id": cfg["owner_open_id"]}]
    if isinstance(item.get("created"), str) and item["created"]:
        cells[cols["time"]] = item["created"]
    return cells, notes


def _lark(args: list[str], timeout: int = 120) -> str:
    if not shutil.which("lark-cli"):
        raise ExportError("no_cli", "lark-cli not found in PATH — npm i -g @larksuite/cli, then authorize it")
    r = subprocess.run(["lark-cli", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ExportError("lark", "lark-cli failed: " + _why(r))
    return r.stdout


def _why(r: subprocess.CompletedProcess) -> str:
    """The actual error from a failed lark-cli run — its JSON error block, not the update notice."""
    for raw in (r.stderr, r.stdout):
        txt = (raw or "").strip()
        if not txt:
            continue
        try:  # {"ok": false, "error": {...}} — message (+ hint) is the reason
            e = json.loads(txt).get("error") or {}
            if isinstance(e, dict) and e.get("message"):
                return str(e["message"]) + (f"（{e['hint']}）" if e.get("hint") else "")
        except ValueError:
            pass
        lines = [l for l in txt.splitlines() if "lark-cli update" not in l and "available," not in l]
        if lines:
            return " | ".join(lines[:3] + lines[-3:])[:400]
    return f"exit {r.returncode}"


OPEN = "https://open.feishu.cn"


class FeishuApp:
    """Transport with the app's own credentials (Settings → Feishu binds them): no lark-cli, nothing to install.
    The endpoints are the ones lark-cli's commands wrap (its --dry-run shows the same paths)."""

    def __init__(self, app_id: str, app_secret: str, cfg: dict):
        self.app_id, self.app_secret = app_id, app_secret
        self.base, self.table = cfg["base_token"], cfg["table_id"]
        self.columns = cfg.get("columns") or DEFAULT_COLUMNS
        self._token = ""

    def token(self) -> str:
        if not self._token:
            req = urllib.request.Request(
                OPEN + "/open-apis/auth/v3/tenant_access_token/internal",
                data=json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode(), method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            d = self._send(req)
            self._token = d.get("tenant_access_token") or ""
            if not self._token:
                raise ExportError("auth", "飞书没有发访问令牌：App Key 或 App Secret 不对")
        return self._token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(OPEN + path, method=method)
        if body is not None:
            req.data = json.dumps(body, ensure_ascii=False).encode()
            req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", "Bearer " + self.token())
        return self._send(req)

    def _send(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                d = json.loads(raw)                     # Feishu answers errors as JSON too
            except ValueError:
                raise ExportError("network", f"飞书回了 HTTP {e.code}（不是 JSON）——多半是网络被接管了")
        except urllib.error.URLError as e:
            raise ExportError("network", f"连不上飞书（{getattr(e, 'reason', e)}），检查网络后重试")
        code, msg = d.get("code", 0), d.get("msg", "")
        if code != 0:
            raise ExportError("feishu", _feishu_error(code, msg))
        return d.get("data") or {}

    def create_record(self, cells: dict) -> str:
        d = self.call("POST", f"/open-apis/base/v3/bases/{self.base}/tables/{self.table}/records/batch_create",
                      {"create_records": [{"fields": cells}]})
        rids = d.get("record_id_list") or []
        if not rids:
            raise ExportError("feishu", "飞书建了记录但没给记录 id")
        return rids[0]

    def delete_record(self, record_id: str) -> None:
        self.call("POST", f"/open-apis/base/v3/bases/{self.base}/tables/{self.table}/records/batch_delete",
                  {"record_id_list": [record_id]})

    def upload(self, record_id: str, path: Path, caption: str) -> None:
        """One frame → the cell: upload_all (drive media, parent bitable_file), then append_attachments."""
        name = UNSAFE.sub("-", caption or path.stem).strip("-") or path.stem
        file_name = f"{path.stem}-{name}{path.suffix}"
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                 for k, v in (("file_name", file_name), ("parent_type", "bitable_file"),
                              ("parent_node", self.base), ("size", str(path.stat().st_size)))]
        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\n"
                     f"Content-Type: {mime}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(OPEN + "/open-apis/drive/v1/medias/upload_all", data=b"".join(parts), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", "Bearer " + self.token())
        tok = self._send(req).get("file_token") or ""
        if not tok:
            raise ExportError("feishu", f"附件上传没返回文件 token：{path.name}")
        self.call("POST", f"/open-apis/base/v3/bases/{self.base}/tables/{self.table}/append_attachments",
                  {"attachments": {record_id: {self.columns["attachments"]: [{"file_token": tok}]}}})

    def table_meta(self) -> dict:
        return self.call("GET", f"/open-apis/base/v3/bases/{self.base}/tables/{self.table}")

    def find_user(self, name: str) -> str:
        """提出人 by name — best effort: the app needs the Contacts search scope; any failure means "not matched"."""
        try:
            users = self.call("POST", "/open-apis/search/v1/user",
                              {"query": name, "user_id_type": "open_id"}).get("users") or []
            for u in users:                      # the search is fuzzy; the exact name wins
                if u.get("name") == name:
                    return u.get("open_id") or ""
            return (users[0].get("open_id") or "") if users else ""
        except ExportError:
            return ""


def _feishu_error(code: int, msg: str) -> str:
    """The API's msg, plus the hint for the two mistakes the first bind is likely to make."""
    s = f"飞书拒绝了（错误码 {code}）：{msg}"
    if code in (99991661, 99991663, 99991668, 10003):   # app access token / key invalid
        s += "——App Key 或 App Secret 不对"
    elif "permission" in msg.lower() or code == 91403:
        s += "——应用缺这个接口的权限，或表格没有共享给应用（多维表格 → 分享 → 协作，把应用加为可编辑）"
    return s


def as_app(cfg: dict) -> FeishuApp | None:
    """The direct transport when the bound credentials are in the config; None = lark-cli as the user."""
    if cfg.get("app_id") and cfg.get("app_secret"):
        return FeishuApp(cfg["app_id"], cfg["app_secret"], cfg)
    return None


def create_record(cells: dict, cfg: dict) -> str:
    if app := as_app(cfg):
        return app.create_record(cells)
    out = _lark(["base", "+record-batch-create", "--base-token", cfg["base_token"], "--table-id", cfg["table_id"],
                 "--json", json.dumps({"create_records": [cells]}, ensure_ascii=False), "--as", "user",
                 "-q", ".data.record_id_list[0]"]).strip()
    if not out:
        raise ExportError("lark", "lark-cli returned no record id")
    return out.splitlines()[-1]


def upload(record_id: str, path: Path, caption: str, cfg: dict) -> None:
    """One frame → the 截图 cell. lark-cli mode copies to /tmp first (it only reads cwd / /tmp / ~/files)."""
    if app := as_app(cfg):
        return app.upload(record_id, path, caption)
    td = tempfile.mkdtemp(prefix="blurt-feishu-", dir="/tmp")
    try:
        name = UNSAFE.sub("-", caption or path.stem).strip("-") or path.stem
        local = Path(td) / f"{path.stem}-{name}{path.suffix}"
        shutil.copyfile(path, local)
        out = _lark(["base", "+record-upload-attachment", "--base-token", cfg["base_token"],
                     "--table-id", cfg["table_id"], "--record-id", record_id, "--field-id",
                     cfg["columns"]["attachments"], "--file", str(local), "--as", "user", "-q", ".ok"])
        if out.strip().splitlines()[-1:] != ["true"]:
            raise ExportError("lark", f"attachment upload failed: {path.name}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def delete_record(record_id: str, cfg: dict) -> None:
    if app := as_app(cfg):
        return app.delete_record(record_id)
    _lark(["base", "+record-delete", "--base-token", cfg["base_token"], "--table-id", cfg["table_id"],
           "--record-id", record_id, "--yes", "--as", "user", "-q", ".ok"])


def export_items(items: list[dict], cfg: dict, dry_run: bool = False) -> list[dict]:
    """Items (doc-shaped: absolute frame paths) → per-item results."""
    results = []
    for item in items:
        rid = item.get("id", "?")
        try:
            cells, notes = row_for(item, cfg)
            frames = [f for f in (item.get("frames") or []) if isinstance(f, dict)
                      and isinstance(f.get("path"), str) and os.path.isfile(f["path"])]
            if dry_run:
                results.append({"id": rid, "ok": True, "dry_run": True, "cells": cells,
                                "attachments": [Path(f["path"]).name for f in frames], "notes": notes})
                continue
            record_id = create_record(cells, cfg)
            try:
                for f in frames:
                    upload(record_id, Path(f["path"]), f.get("caption") or "", cfg)
            except Exception as e:  # no half rows: undo the record, say what failed
                rolled = False
                try:
                    delete_record(record_id, cfg)
                    rolled = True
                except ExportError:
                    pass
                results.append({"id": rid, "ok": False,
                                "error": f"{getattr(e, 'msg', e)}" + ("" if rolled else f"；记录 {record_id} 删除失败，请手动删"),
                                "code": getattr(e, "code", "lark"), "rolled_back": rolled,
                                "record_id": record_id})
                continue
            results.append({"id": rid, "ok": True, "record_id": record_id, "attachments": len(frames),
                            "detail_source": _detail(item)[1], "notes": notes})
        except (ExportError, subprocess.TimeoutExpired, OSError) as e:
            results.append({"id": rid, "ok": False, "error": str(getattr(e, "msg", e)),
                            "code": getattr(e, "code", "io")})
    return results


def _parse_table_link(link: str) -> tuple[str, str]:
    """A bitable's address bar → (base_token, table_id): https://xxx.feishu.cn/base/BCQ…?table=tblX&view=…"""
    b = re.search(r"/base/([A-Za-z0-9]+)", link)
    if not b:
        raise ExportError("bad_link", "链接里找不到表格：要粘贴多维表格在浏览器地址栏的完整链接")
    t = re.search(r"[?&]table=(tbl[A-Za-z0-9]+)", link)
    if not t:
        raise ExportError("bad_link", "链接里没有 table 参数——先点开要导出的那张数据表，再复制地址栏链接")
    return b.group(1), t.group(1)


def bind(app_key: str, secret: str, table_link: str, owner_name: str) -> dict:
    """The settings pane's one button: check the credentials against the table, resolve the proposer, save the
    config. Returns the single-line JSON the pane shows (never raises, never dies: a handled miss is a message)."""
    old = (global_config().get("export") or {}).get("feishu") or {}
    if not app_key:
        return {"ok": False, "error": "要填 App Key"}
    if table_link:
        try:
            base_token, table_id = _parse_table_link(table_link)
        except ExportError as e:
            return {"ok": False, "error": e.msg}
    elif old.get("base_token") and old.get("table_id"):
        base_token, table_id = old["base_token"], old["table_id"]      # re-binding: keep the table
    else:
        return {"ok": False, "error": "要粘贴表格链接（在浏览器打开多维表格，复制地址栏链接）"}
    secret = secret or (old.get("app_secret") or "")
    if not secret:
        return {"ok": False, "error": "要填 App Secret"}
    cfg = {**old, "app_id": app_key, "app_secret": secret, "base_token": base_token, "table_id": table_id}
    app = FeishuApp(app_key, secret, cfg)
    try:
        app.token()
        meta = app.table_meta()
    except ExportError as e:
        return {"ok": False, "error": e.msg}
    table_name = meta.get("name") or table_id
    proposer = app.find_user(owner_name) if owner_name else ""
    if owner_name:
        cfg["owner_name"], cfg["owner_open_id"] = owner_name, proposer
    cfg["table_name"] = table_name
    try:
        update_global_config({"export": {"feishu": cfg}})
    except Exception as e:                                            # ConfigUnreadable: a clean sentence
        return {"ok": False, "error": str(e)}
    out = {"ok": True, "table": table_name}
    if owner_name:
        out["proposer"] = bool(proposer)     # false = not matched (the app lacks the Contacts search scope)
    return out


def init(base_token: str, table_id: str, owner_open_id: str, name: str | None) -> dict:
    """Write export.feishu into the global config (creating the file if needed), the locked way the app writes too."""
    feishu = {"base_token": base_token, "table_id": table_id, "owner_open_id": owner_open_id}
    if name:
        feishu["owner_name"] = name
    update_global_config({"export": {"feishu": feishu}})
    return feishu


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("args", nargs="*", help="SESSION_DIR | init BASE_TOKEN TABLE_ID OWNER_OPEN_ID")
    p.add_argument("--project")
    p.add_argument("--ids", nargs="+", default=[], help="item ids (default: every item but discarded)")
    p.add_argument("--dry-run", action="store_true", help="show the rows without writing anything")
    p.add_argument("--name", help="the proposer's display name (only with init)")
    p.add_argument("--app-key", help="the Feishu custom app's App Key (only with bind)")
    p.add_argument("--table", help="the bitable's address-bar link (only with bind)")
    p.add_argument("--owner", help="the proposer's display name (only with bind)")
    a = p.parse_args()
    if a.args and a.args[0] == "init":
        if len(a.args) != 4:
            die("init BASE_TOKEN TABLE_ID OWNER_OPEN_ID [--name 名字]")
        feishu = init(*a.args[1:], a.name)
        print(json.dumps({"ok": True, "export.feishu": feishu}, ensure_ascii=False))
        return
    if a.args and a.args[0] == "bind":
        # the App Secret never travels as an argument: it comes on stdin's first line (empty = keep the saved one)
        secret = "" if sys.stdin.isatty() else sys.stdin.readline().strip()
        result = bind(a.app_key or "", secret, (a.table or "").strip(), (a.owner or "").strip())
        # one line, like every CLI the app runs (Scripts.json reads line by line)
        print(json.dumps(result, ensure_ascii=False))
        return
    try:
        if a.args:
            scope = {"session": str(Path(a.args[0]).resolve())}
        elif a.project:
            scope = {"project": items_mod.resolve_project(a.project)}
        else:
            die("pass SESSION_DIR or --project P")
        doc = items_mod.make_doc(scope)
    except items_mod.ItemsError as e:
        die(str(e))
    items = [i for i in doc["items"] if i.get("state") != "discarded"
             and (not a.ids or i["id"] in a.ids)]
    if a.ids:
        missing = [i for i in a.ids if i not in {x["id"] for x in doc["items"]}]
        if missing:
            die(f"no such items: {', '.join(missing)}")
    try:
        cfg = load_cfg()
    except ExportError as e:
        die(e.msg)
    results = export_items(items, cfg, a.dry_run)
    # one line: the app reads a CLI's stdout line by line and takes the last line that parses (Scripts.json)
    print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
