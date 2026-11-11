"""清分样本卡号脱敏下载 Web 应用。"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, render_template, request

from masking import MaskingError, build_package, mask_payload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(BASE_DIR, "data", "sample_clearing.json")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB


@app.errorhandler(MaskingError)
def handle_masking_error(exc: MaskingError):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(413)
def handle_too_large(_exc):
    return jsonify({"ok": False, "error": "上传文件超过 8 MB 限制。"}), 400


def _parse_payload(raw: bytes) -> dict:
    if not raw.strip():
        raise MaskingError("上传内容为空，请选择包含 source_tables 和 models 的 JSON 文件。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise MaskingError("文件不是 UTF-8 编码，请另存为 UTF-8 后重试。") from exc
    except json.JSONDecodeError as exc:
        raise MaskingError(f"JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MaskingError("JSON 顶层必须是对象，包含 source_tables 和 models 两个数组。")
    return payload


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/sample")
def sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.post("/api/validate")
def validate():
    """校验上传的 JSON 与全部模型 SQL，返回源表字段清单供选择卡号列。"""
    payload = _parse_payload(request.get_data())
    package = build_package(payload)
    return jsonify({
        "ok": True,
        "source_tables": [
            {
                "table": t,
                "columns": cols,
                "row_count": len(package.source_rows[t]),
            }
            for t, cols in package.source_schemas.items()
        ],
        "models": [
            {"name": name, "upstream": sorted(package.models[name].upstream)}
            for name in package.order
        ],
    })


@app.post("/api/mask")
def run_mask():
    """生成脱敏样本包。响应中 masked 为可下载的完整 JSON（不含原号对照）。"""
    payload = _parse_payload(request.get_data())
    target_table = request.args.get("table", "").strip()
    target_column = request.args.get("column", "").strip()
    if not target_table or not target_column:
        raise MaskingError("请先选择要换号的源表卡号列。")
    masked, summary = mask_payload(payload, target_table, target_column)
    return jsonify({"ok": True, "masked": masked, "summary": summary})


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8000)
