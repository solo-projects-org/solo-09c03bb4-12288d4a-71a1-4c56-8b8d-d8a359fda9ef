"""Flask API 层测试：路由、错误码与样例端到端。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


client = app.test_client()
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "sample_clearing.json")
with open(SAMPLE, encoding="utf-8") as fh:
    sample = json.load(fh)

# 首页
r = client.get("/")
check("首页 200", r.status_code == 200 and "卡号脱敏".encode() in r.data)

# 样例接口
r = client.get("/api/sample")
check("样例接口", r.status_code == 200 and r.get_json()["models"])

# 校验通过
r = client.post("/api/validate", json=sample)
body = r.get_json()
check("validate 通过", r.status_code == 200 and body["ok"] is True)
check("validate 返回源表行数",
      {t["table"]: t["row_count"] for t in body["source_tables"]}
      == {"ods_card_transaction": 6, "ods_card_info": 6, "ods_discount_eligibility": 3},
      str(body))
check("validate 返回模型依赖",
      {m["name"]: m["upstream"] for m in body["models"]}
      == {"dwd_card_profile": [],
          "dwd_txn_with_card": ["dwd_card_profile"],
          "dws_txn_discount": ["dwd_txn_with_card"],
          "dws_city_discount": ["dwd_card_profile"]},
      str(body["models"]))

# 脱敏下载
r = client.post("/api/mask?table=ods_card_info&column=card_no", json=sample)
body = r.get_json()
check("mask 通过", r.status_code == 200 and body["ok"] is True)
check("mask 摘要三列", body["summary"]["column_count"] == 3)
check("mask 摘要不同卡号数", body["summary"]["distinct_values"] == 6)
masked = body["masked"]
check("masked 结构与输入同构",
      [t["table"] for t in masked["source_tables"]]
      == [t["table"] for t in sample["source_tables"]]
      and len(masked["models"]) == len(sample["models"]))
orig = {"6222021001110001", "6222021001110002", "6222021001110003",
        "6222021001110004", "6222021001110005", "6222021001110009"}
remaining = {row["card_no"] for t in masked["source_tables"] for row in t["rows"]
             if row.get("card_no")}
check("masked 原号全部消失", not (remaining & orig), str(remaining & orig))
check("masked 不含原号对照", "mapping" not in masked and "summary" not in masked)
check("masked 模型 SQL 保留",
      [m["sql"] for m in masked["models"]] == [m["sql"] for m in sample["models"]])
# 同一原号跨表同新：流水 T1 与卡档第一行原是同一张卡
txn1 = masked["source_tables"][0]["rows"][0]["card_no"]
card1 = masked["source_tables"][1]["rows"][0]["card_no"]
check("masked 跨表同号同新", txn1 == card1 and txn1 is not None)

# 缺参数
r = client.post("/api/mask", json=sample)
check("缺参数 400", r.status_code == 400 and r.get_json()["ok"] is False)

# 坏 JSON
r = client.post("/api/validate", data="{oops", content_type="application/json")
check("坏 JSON 400", r.status_code == 400 and "JSON" in r.get_json()["error"])

# 空 body
r = client.post("/api/validate", data="", content_type="application/json")
check("空 body 400", r.status_code == 400 and "空" in r.get_json()["error"])

# 顶层非对象
r = client.post("/api/validate", json=[1, 2, 3])
check("顶层非对象 400", r.status_code == 400)

# 不支持的 SQL：指出模型，不生成下载
bad_sql = {
    "source_tables": [{"table": "t", "columns": ["k"], "rows": [{"k": "V1"}]}],
    "models": [{"name": "boom", "sql": "SELECT a.k AS k FROM t a WHERE a.k = '1'"}],
}
r = client.post("/api/mask?table=t&column=k", json=bad_sql)
check("不支持SQL 400 带模型名",
      r.status_code == 400 and "boom" in r.get_json()["error"]
      and "WHERE" in r.get_json()["error"])
check("不支持SQL 无下载", "masked" not in r.get_json())

# 缺列：指出位置
bad_col = {
    "source_tables": [{"table": "t", "columns": ["k"], "rows": [{"k": "V1"}]}],
    "models": [{"name": "m1", "sql": "SELECT a.ghost AS g FROM t a"}],
}
r = client.post("/api/mask?table=t&column=k", json=bad_col)
check("缺列 400 带位置",
      r.status_code == 400 and "m1" in r.get_json()["error"]
      and "ghost" in r.get_json()["error"])

# 无效样本：数字卡号，指出表与行
bad_sample = {
    "source_tables": [{"table": "t", "columns": ["k"],
                       "rows": [{"k": "V1"}, {"k": 6222021001110001}]}],
    "models": [],
}
r = client.post("/api/mask?table=t&column=k", json=bad_sample)
err = r.get_json().get("error", "")
check("无效样本 400 带位置",
      r.status_code == 400 and "第 2 行" in err and "数字" in err, err)
check("无效样本 无下载", "masked" not in r.get_json())

# 目标列不存在
r = client.post("/api/mask?table=ods_card_info&column=ghost", json=sample)
check("目标列不存在 400", r.status_code == 400 and "ghost" in r.get_json()["error"])

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("API 测试全部通过。")
