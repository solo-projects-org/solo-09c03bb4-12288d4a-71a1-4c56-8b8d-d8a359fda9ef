# 清分样本卡号脱敏下载

清分工程师要把刷卡流水、卡档、优惠资格的样本包交给联调团队核对关联结果。
交样前必须换掉原卡号，但**哪些记录能配上不能变**。本工具在网页上完成脱敏：

1. 上传含源表样本行的模型 JSON（或一键载入本地样例）；
2. 选择一个源表卡号列；
3. 工具沿各模型的**直接选列与改名**把等值 `INNER JOIN` 的连接键追溯到源列
   （含跨模型引用），与所选列通过连接键**直接或间接相连**的源列整体换号；
4. 下载脱敏后的样本包 JSON —— 关联结果不变，且**不含原号与新号的对照**。

纯本地运行，不执行任何 SQL，不需要连接数仓。

## 换号规则

- 同一原号在整个包内换成同一新号；不同原号不合并；
- 新号不等于任何一个待换的原号（`MASK00000001` 起递增，自动避开冲突）；
- `null` 原样保留；其他字段、所有行、模型 SQL 原样保留；
- 待换号列的值**只允许字符串或 null**，否则视为无效样本；
- 下载的 JSON 与输入同构，不含原号对照。

## 支持的 SQL 范围

只解析以下子集，遇到其他写法即报错并指出所在模型与位置，不生成下载：

- `SELECT` 投影：**带限定名的直接选列**（`t.col`）与**改名**（`t.col AS alias`）；
- `FROM` / `INNER JOIN`：直接引用源表或上游模型（可起别名）；
- `ON`：**列 = 列** 等值条件，多条等式用 `AND` 连接。

不支持（示例）：`WHERE`、`GROUP BY`、`DISTINCT`、`ORDER BY`、`LIMIT`、CTE、子查询、
集合运算、函数/表达式投影、星号、未限定列名、非 `INNER` 连接、`USING`、
`OR` 或非常量等式、DML 等。

## 目录结构

```
.
├── docker-compose.yml          # 一键启动
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py                  # Flask 路由（/、/api/sample、/api/validate、/api/mask）
    ├── masking.py              # SQL 子集解析、连接键追溯（并查集）、换号
    ├── data/
    │   └── sample_clearing.json  # 本地样例：卡号改名后参与两段连接
    ├── templates/
    │   └── index.html          # 单页前端（无外部 CDN 依赖）
    └── tests/
        ├── test_masking.py     # 追溯与换号规则、SQL 子集、样本校验
        └── test_api.py         # 路由与错误处理
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>：

1. 点击「载入本地样例」，或上传自己的 JSON；
2. 在左侧选择源表与卡号列（如 `ods_card_info.card_no`）；
3. 点击「生成脱敏下载」，浏览器自动下载 `*.masked.json`，页面展示换号摘要与预览。

停止：`docker compose down`。

## 输入 JSON 格式

```json
{
  "source_tables": [
    { "table": "ods_card_info",
      "columns": ["card_no", "card_type"],
      "rows": [
        {"card_no": "6222021001110001", "card_type": "普通卡"},
        {"card_no": null, "card_type": "待录入"}
      ] }
  ],
  "models": [
    { "name": "dwd_card_profile",
      "description": "可选说明",
      "sql": "SELECT c.card_no AS card_num, c.card_type AS card_type FROM ods_card_info c" }
  ]
}
```

- `source_tables[].table` 为源表名，`columns` 为字段清单，`rows` 为样本行（每行一个对象）；
- `models[].name` 为模型名，模型 SQL 中 `FROM/JOIN` 同名关系即视为读取该上游模型，
  其余名字按源表处理，必须在 `source_tables` 中声明；
- 样本行允许只携带部分字段、或携带清单外字段（原样保留）；
  但**待换号列**在每行都必须存在且值为字符串或 null。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 返回内置样例 JSON |
| POST | `/api/validate` | 校验 JSON 与全部模型 SQL，返回源表字段（含行数）与模型读取关系 |
| POST | `/api/mask?table=<源表>&column=<列>` | 返回 `{ok, masked, summary}`，`masked` 即可下载的脱敏样本包 |

错误统一返回 HTTP 400 与 `{"ok": false, "error": "中文原因（含模型名/表名/行号）"}`，
此时不生成下载。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r app/requirements.txt
python app/app.py                    # 监听 8000
python app/tests/test_masking.py     # 核心逻辑测试
python app/tests/test_api.py         # API 测试
```
