"""masking.py 测试：连接键追溯、换号规则、SQL 子集与样本校验。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from masking import MaskingError, build_package, connected_columns, mask_payload  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def expect_error(name, fn, *keywords):
    try:
        fn()
    except MaskingError as e:
        msg = str(e)
        ok = all(k in msg for k in keywords)
        check(name, ok, msg)
        return
    check(name, False, "未抛出 MaskingError")


def make_payload(models, card_values=("C001", "C002"), extra_card_values=()):
    return {
        "source_tables": [
            {"table": "t_card", "columns": ["card_no", "ctype"],
             "rows": [{"card_no": v, "ctype": "普通卡"} for v in card_values]
                      + [{"card_no": None, "ctype": "待录入"}]},
            {"table": "t_txn", "columns": ["txn_id", "card_no", "amt"],
             "rows": [{"txn_id": f"T{i}", "card_no": v, "amt": 1.5}
                      for i, v in enumerate(card_values + extra_card_values, 1)]},
        ],
        "models": models,
    }


# 1. 改名 + 跨模型两段连接：三个源列连通 ------------------------------------------------
p = {
    "source_tables": [
        {"table": "ods_card", "columns": ["card_no", "ctype"],
         "rows": [{"card_no": "A1", "ctype": "普通卡"},
                  {"card_no": "A2", "ctype": "学生卡"},
                  {"card_no": None, "ctype": "待录入"}]},
        {"table": "ods_txn", "columns": ["tid", "card_no", "amt"],
         "rows": [{"tid": "T1", "card_no": "A1", "amt": 2.0},
                  {"tid": "T2", "card_no": "A2", "amt": 3.0},
                  {"tid": "T3", "card_no": "A9", "amt": 1.0}]},
        {"table": "ods_disc", "columns": ["card_no", "dtype"],
         "rows": [{"card_no": "A2", "dtype": "五折"},
                  {"card_no": "A7", "dtype": "免费"}]},
    ],
    "models": [
        {"name": "m_profile", "sql":
         "SELECT c.card_no AS card_num, c.ctype AS ctype FROM ods_card c"},
        {"name": "m_txn", "sql":
         "SELECT t.tid AS tid, t.card_no AS card_no, p.card_num AS card_num "
         "FROM ods_txn t INNER JOIN m_profile p ON t.card_no = p.card_num"},
        {"name": "m_disc", "sql":
         "SELECT w.tid AS tid, d.dtype AS dtype "
         "FROM m_txn w INNER JOIN ods_disc d ON w.card_no = d.card_no"},
    ],
}
masked, summary = mask_payload(p, "ods_card", "card_no")
cols = {(c["table"], c["column"]) for c in summary["renumbered_columns"]}
check("两段连接-三源列连通", cols == {("ods_card", "card_no"),
                                  ("ods_txn", "card_no"), ("ods_disc", "card_no")}, str(cols))
check("两段连接-摘要列数", summary["column_count"] == 3)
# 卡档 A1/A2 + 流水 A1/A2/A9 + 资格 A2/A7 = {A1,A2,A9,A7}
check("两段连接-不同卡号数", summary["distinct_values"] == 4,
      str(summary["distinct_values"]))

rows = {t["table"]: t["rows"] for t in masked["source_tables"]}
new_card = {r["card_no"] for r in rows["ods_card"] if r["card_no"] is not None}
check("换号-原号全部消失", not ({"A1", "A2"} & new_card), str(new_card))
check("换号-同号同新（卡档与流水 A1）",
      rows["ods_card"][0]["card_no"] == rows["ods_txn"][0]["card_no"])
check("换号-不同号不合并",
      rows["ods_card"][0]["card_no"] != rows["ods_card"][1]["card_no"])
check("换号-null 保留",
      rows["ods_card"][2]["card_no"] is None)
check("换号-新号不等于任何原号",
      not ({r["card_no"] for t in rows.values() for r in t if "card_no" in r and r["card_no"]}
           & {"A1", "A2", "A9", "A7"}))
check("保留-其他字段不动",
      rows["ods_txn"][0]["amt"] == 2.0 and rows["ods_card"][0]["ctype"] == "普通卡")
check("保留-行数不动",
      [len(t["rows"]) for t in masked["source_tables"]] == [3, 3, 2])
check("保留-模型 SQL 不动",
      [m["sql"] for m in masked["models"]] == [m["sql"] for m in p["models"]])
check("下载-不含原号对照",
      "mapping" not in masked and "原号" not in json.dumps(masked, ensure_ascii=False))

# 连接可匹配性不变：脱敏前后 INNER JOIN 结果一致
def join_pairs(rows_a, rows_b):
    keys_b = {}
    for j, r in enumerate(rows_b):
        if r["card_no"] is not None:
            keys_b.setdefault(r["card_no"], []).append(j)
    pairs = set()
    for i, r in enumerate(rows_a):
        if r["card_no"] is None:
            continue
        for j in keys_b.get(r["card_no"], []):
            pairs.add((i, j))
    return pairs

before = join_pairs(p["source_tables"][1]["rows"], p["source_tables"][0]["rows"])
after = join_pairs(rows["ods_txn"], rows["ods_card"])
check("换号-关联结果不变", before == after, f"{before} vs {after}")

# 确定性：同一输入两次换号结果一致
masked2, _ = mask_payload(p, "ods_card", "card_no")
check("换号-确定性", masked == masked2)

# 2. 未连通列不受影响 ------------------------------------------------------------------
check("未连通-ctype 不动",
      all(r["ctype"] in ("普通卡", "学生卡", "待录入") for r in rows["ods_card"]))

# 3. 孤立列：不参与任何连接键，只换自己 ------------------------------------------------
p_iso = make_payload([
    {"name": "m1", "sql": "SELECT a.card_no AS cn FROM t_card a"},
])
m_iso, s_iso = mask_payload(p_iso, "t_txn", "card_no")
check("孤立列-仅自身换号", [ (c["table"], c["column"]) for c in s_iso["renumbered_columns"] ]
      == [("t_txn", "card_no")])
check("孤立列-他表不动",
      all(r["card_no"] in ("C001", "C002", None) for r in m_iso["source_tables"][0]["rows"]))

# 4. AND 多条等式 ----------------------------------------------------------------------
p_and = {
    "source_tables": [
        {"table": "ta", "columns": ["k", "city"], "rows": [{"k": "X1", "city": "01"}]},
        {"table": "tb", "columns": ["k", "city"], "rows": [{"k": "X1", "city": "01"}]},
    ],
    "models": [
        {"name": "m1", "sql":
         "SELECT a.k AS k, a.city AS city FROM ta a INNER JOIN tb b "
         "ON a.k = b.k AND a.city = b.city"},
    ],
}
_, s_and = mask_payload(p_and, "ta", "k")
check("AND等式-仅k连通", {(c["table"], c["column"]) for c in s_and["renumbered_columns"]}
      == {("ta", "k"), ("tb", "k")})
_, s_and2 = mask_payload(p_and, "ta", "city")
check("AND等式-city独立连通", {(c["table"], c["column"]) for c in s_and2["renumbered_columns"]}
      == {("ta", "city"), ("tb", "city")})

# 5. 自连接 ----------------------------------------------------------------------------
p_self = {
    "source_tables": [
        {"table": "ta", "columns": ["k", "parent_k"], "rows": [{"k": "X1", "parent_k": "X0"}]},
    ],
    "models": [
        {"name": "m1", "sql":
         "SELECT a.k AS k, b.k AS parent FROM ta a INNER JOIN ta b ON a.parent_k = b.k"},
    ],
}
_, s_self = mask_payload(p_self, "ta", "k")
check("自连接-k与parent_k连通", {(c["table"], c["column"]) for c in s_self["renumbered_columns"]}
      == {("ta", "k"), ("ta", "parent_k")})

# 6. 新号避开与 MASK 前缀冲突的原号 -----------------------------------------------------
p_coll = {
    "source_tables": [
        {"table": "ta", "columns": ["k"], "rows": [{"k": "MASK00000001"}, {"k": "Z9"}]},
    ],
    "models": [],
}
m_coll, s_coll = mask_payload(p_coll, "ta", "k")
news = {r["k"] for r in m_coll["source_tables"][0]["rows"]}
check("新号避开原号", "MASK00000001" not in news and "Z9" not in news and len(news) == 2, str(news))

# 7. 跨列同号同新 ----------------------------------------------------------------------
p_cross = {
    "source_tables": [
        {"table": "ta", "columns": ["k"], "rows": [{"k": "SHARED"}]},
        {"table": "tb", "columns": ["k"], "rows": [{"k": "SHARED"}]},
    ],
    "models": [
        {"name": "m1", "sql": "SELECT a.k AS k FROM ta a INNER JOIN tb b ON a.k = b.k"},
    ],
}
m_cross, _ = mask_payload(p_cross, "ta", "k")
check("跨列同号同新",
      m_cross["source_tables"][0]["rows"][0]["k"]
      == m_cross["source_tables"][1]["rows"][0]["k"])

# 8. 无效样本：待换号列的值不是字符串/null ----------------------------------------------
def bad_value_payload(value):
    return {
        "source_tables": [
            {"table": "ta", "columns": ["k"], "rows": [{"k": "V1"}, {"k": value}]},
        ],
        "models": [],
    }

expect_error("无效样本-数字",
             lambda: mask_payload(bad_value_payload(6222021001110001), "ta", "k"),
             "ta", "第 2 行", "数字")
expect_error("无效样本-布尔",
             lambda: mask_payload(bad_value_payload(True), "ta", "k"),
             "ta", "第 2 行", "布尔")
expect_error("无效样本-数组",
             lambda: mask_payload(bad_value_payload(["V"]), "ta", "k"),
             "ta", "第 2 行", "数组")
expect_error("无效样本-缺列",
             lambda: mask_payload({
                 "source_tables": [{"table": "ta", "columns": ["k"], "rows": [{"k": "V1"}, {}]}],
                 "models": [],
             }, "ta", "k"),
             "ta", "第 2 行", "缺少")
expect_error("无效样本-行非对象",
             lambda: mask_payload({
                 "source_tables": [{"table": "ta", "columns": ["k"], "rows": [["V1"]]}],
                 "models": [],
             }, "ta", "k"),
             "ta", "第 1 行")

# 非换号列的数字值不受影响
p_num = {
    "source_tables": [
        {"table": "ta", "columns": ["k", "amt"], "rows": [{"k": "V1", "amt": 2.5}]},
    ],
    "models": [],
}
m_num, _ = mask_payload(p_num, "ta", "k")
check("非换号列-数字原样保留", m_num["source_tables"][0]["rows"][0]["amt"] == 2.5)

# 9. SQL 子集错误 ----------------------------------------------------------------------
def build_only(payload):
    return build_package(payload)

def one_model(sql, name="m_bad"):
    return {"source_tables": [
                {"table": "ta", "columns": ["k", "v"], "rows": []},
                {"table": "tb", "columns": ["k", "v"], "rows": []}],
            "models": [{"name": name, "sql": sql}]}

expect_error("WHERE-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a WHERE a.v = '1'")), "m_bad", "WHERE")
expect_error("DISTINCT-报错", lambda: build_only(one_model(
    "SELECT DISTINCT a.k AS k FROM ta a")), "m_bad", "DISTINCT")
expect_error("GROUP BY-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a GROUP BY a.k")), "m_bad", "GROUP BY")
expect_error("ORDER BY-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a ORDER BY a.k")), "m_bad", "ORDER BY")
expect_error("LIMIT-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a LIMIT 3")), "m_bad", "LIMIT")
expect_error("CTE-报错", lambda: build_only(one_model(
    "WITH c AS (SELECT a.k AS k FROM ta a) SELECT c.k AS k FROM c")), "m_bad", "CTE")
expect_error("UNION-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a UNION SELECT b.k AS k FROM tb b")), "m_bad", "集合运算")
expect_error("函数投影-报错", lambda: build_only(one_model(
    "SELECT upper(a.k) AS k FROM ta a")), "m_bad", "直接选列")
expect_error("未限定列-报错", lambda: build_only(one_model(
    "SELECT k FROM ta a")), "m_bad", "未带表名")
expect_error("星号-报错", lambda: build_only(one_model(
    "SELECT * FROM ta a")), "m_bad", "星号")
expect_error("t.*-报错", lambda: build_only(one_model(
    "SELECT a.* FROM ta a")), "m_bad", "星号")
expect_error("LEFT JOIN-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a LEFT JOIN tb b ON a.k = b.k")), "m_bad", "INNER")
expect_error("裸JOIN-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a JOIN tb b ON a.k = b.k")), "m_bad", "INNER")
expect_error("CROSS JOIN-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a CROSS JOIN tb b")), "m_bad", "INNER")
expect_error("逗号连接-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a, tb b")), "m_bad", "INNER")
expect_error("USING-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b USING (k)")), "m_bad", "USING")
expect_error("缺ON-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b")), "m_bad", "ON")
expect_error("OR条件-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b ON a.k = b.k OR a.v = b.v")), "m_bad", "OR")
expect_error("非等值-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b ON a.k > b.k")), "m_bad", "等值")
expect_error("常量等式-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b ON a.k = b.k AND b.v = 'x'")), "m_bad", "常量")
expect_error("FROM子查询-报错", lambda: build_only(one_model(
    "SELECT s.k AS k FROM (SELECT a.k AS k FROM ta a) s")), "m_bad", "FROM")
expect_error("未知表-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM tx a")), "m_bad", "tx")
expect_error("未知别名-报错", lambda: build_only(one_model(
    "SELECT z.k AS k FROM ta a")), "m_bad", "z")
expect_error("源表缺列-报错", lambda: build_only(one_model(
    "SELECT a.ghost AS g FROM ta a")), "m_bad", "ta", "ghost")
expect_error("连接键缺列-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb b ON a.k = b.ghost")), "m_bad", "tb", "ghost")
expect_error("输出列重复-报错", lambda: build_only(one_model(
    "SELECT a.k AS k, a.v AS k FROM ta a")), "m_bad", "重复")
expect_error("别名重复-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a INNER JOIN tb a ON a.k = a.k")), "m_bad", "重复")
expect_error("多语句-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM ta a; SELECT b.k AS k FROM tb b")), "m_bad", "一条")
expect_error("非SELECT-报错", lambda: build_only(one_model(
    "INSERT INTO ta SELECT b.k FROM tb b")), "m_bad", "SELECT")
expect_error("库名限定-报错", lambda: build_only(one_model(
    "SELECT a.k AS k FROM db.ta a")), "m_bad", "库名")
expect_error("语法错误-报错", lambda: build_only(one_model(
    "SELCT a.k FROM ta a")), "m_bad", "解析失败")

# 上游模型缺列
expect_error("上游缺列-报错", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}],
    "models": [
        {"name": "m1", "sql": "SELECT a.k AS k FROM ta a"},
        {"name": "m2", "sql": "SELECT x.ghost AS g FROM m1 x"},
    ]}), "m2", "m1", "ghost")

# 模型循环 / 自读
expect_error("循环依赖-报错", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}],
    "models": [
        {"name": "m1", "sql": "SELECT x.k AS k FROM m2 x"},
        {"name": "m2", "sql": "SELECT y.k AS k FROM m1 y"},
    ]}), "循环")
expect_error("模型自读-报错", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}],
    "models": [{"name": "m1", "sql": "SELECT x.k AS k FROM m1 x"}],
}), "自身")
expect_error("模型与源表重名-报错", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}],
    "models": [{"name": "ta", "sql": "SELECT x.k AS k FROM ta x"}],
}), "重名")

# 无关模型的坏 SQL 也会阻止下载
expect_error("无关模型坏SQL-报错", lambda: mask_payload({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": [{"k": "V1"}]}],
    "models": [{"name": "m_bad", "sql": "SELECT a.k FROM ta a WHERE a.k = '1'"}],
}, "ta", "k"), "m_bad", "WHERE")

# 10. JSON 结构错误 --------------------------------------------------------------------
expect_error("缺source_tables", lambda: build_only({"models": []}), "source_tables")
expect_error("缺models", lambda: build_only({"source_tables": []}), "models")
expect_error("源表重复", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []},
                      {"table": "ta", "columns": ["k"], "rows": []}],
    "models": []}), "重复")
expect_error("列重复", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k", "k"], "rows": []}],
    "models": []}), "ta", "重复")
expect_error("缺rows", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"]}],
    "models": []}), "rows")
expect_error("模型缺sql", lambda: build_only({
    "source_tables": [],
    "models": [{"name": "m1"}]}), "m1", "sql")
expect_error("模型重名", lambda: build_only({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}],
    "models": [{"name": "m1", "sql": "SELECT a.k AS k FROM ta a"},
               {"name": "m1", "sql": "SELECT a.k AS k FROM ta a"}]}), "重复")
expect_error("目标表不存在", lambda: mask_payload({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}], "models": []},
    "tx", "k"), "tx")
expect_error("目标列不存在", lambda: mask_payload({
    "source_tables": [{"table": "ta", "columns": ["k"], "rows": []}], "models": []},
    "ta", "ghost"), "ta", "ghost")

# 11. 样例文件端到端 -------------------------------------------------------------------
sample_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "sample_clearing.json")
with open(sample_path, encoding="utf-8") as fh:
    sample = json.load(fh)

pkg = build_package(sample)
check("样例-4个模型", len(pkg.models) == 4)
check("样例-卡号三列连通",
      set(connected_columns(pkg, "ods_card_info", "card_no"))
      == {("ods_card_info", "card_no"), ("ods_card_transaction", "card_no"),
          ("ods_discount_eligibility", "card_no")})
check("样例-城市两列连通",
      set(connected_columns(pkg, "ods_card_info", "issue_city"))
      == {("ods_card_info", "issue_city"), ("ods_discount_eligibility", "issue_city")})

m_sample, s_sample = mask_payload(sample, "ods_card_transaction", "card_no")
check("样例-换号列数", s_sample["column_count"] == 3)
srows = {t["table"]: t["rows"] for t in m_sample["source_tables"]}
orig_cards = {"6222021001110001", "6222021001110002", "6222021001110003",
              "6222021001110004", "6222021001110005", "6222021001110009"}
remaining = {r["card_no"] for t in srows.values() for r in t if r.get("card_no")}
check("样例-原卡号全部消失", not (remaining & orig_cards), str(remaining & orig_cards))
check("样例-null 保留",
      srows["ods_card_transaction"][4]["card_no"] is None
      and srows["ods_card_info"][5]["card_no"] is None)
check("样例-金额不动", srows["ods_card_transaction"][0]["amount"] == 2.5)
check("样例-站点不动", srows["ods_card_transaction"][0]["station_id"] == "S101")
check("样例-城市不动", srows["ods_card_info"][0]["issue_city"] == "3301")
check("样例-模型SQL不动",
      [m["sql"] for m in m_sample["models"]] == [m["sql"] for m in sample["models"]])

# 样例关联结果不变：流水 ⨝ 卡档、流水 ⨝ 资格
def sample_join(rows_a, rows_b, key="card_no"):
    kb = {}
    for j, r in enumerate(rows_b):
        if r.get(key) is not None:
            kb.setdefault(r[key], []).append(j)
    return {(i, j) for i, r in enumerate(rows_a) if r.get(key) is not None
            for j in kb.get(r[key], [])}

src = {t["table"]: t["rows"] for t in sample["source_tables"]}
check("样例-流水卡档关联不变",
      sample_join(src["ods_card_transaction"], src["ods_card_info"])
      == sample_join(srows["ods_card_transaction"], srows["ods_card_info"]))
check("样例-流水资格关联不变",
      sample_join(src["ods_card_transaction"], src["ods_discount_eligibility"])
      == sample_join(srows["ods_card_transaction"], srows["ods_discount_eligibility"]))

# 选城市列时卡号不动
m_city, s_city = mask_payload(sample, "ods_card_info", "issue_city")
check("样例-选城市列仅两列换号", s_city["column_count"] == 2)
city_rows = {t["table"]: t["rows"] for t in m_city["source_tables"]}
check("样例-选城市列卡号不动",
      city_rows["ods_card_info"][0]["card_no"] == "6222021001110001")
check("样例-城市已换号",
      city_rows["ods_card_info"][0]["issue_city"] != "3301"
      and city_rows["ods_card_info"][0]["issue_city"]
      == city_rows["ods_discount_eligibility"][0]["issue_city"])

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("全部测试通过。")
