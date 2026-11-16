# 科技人才线索采集枢纽

本地离线运行的科技人才供需线索采集器。接收来源登记文件与镜像批次，支持 JSON、CSV 和简单 HTML 表格，将岗位、培养计划和校企合作项目统一为可追溯记录。SQLite 保存批次、页、原始记录、版本和事件；当前视图只交付有效且无争议的最新线索。

## 快速运行

需要 Python 3.11 或更新版本。项目只使用标准库。

    python -m unittest discover -s tests -v
    python -m compileall -q talent_harvest
    python -m talent_harvest demo --workspace fixtures

演示命令使用临时数据库，不改动夹具。实际使用时先登记来源，再采集镜像：

    python -m talent_harvest --db harvest.sqlite3 register fixtures/source.json
    python -m talent_harvest --db harvest.sqlite3 ingest fixtures/manifest.json
    python -m talent_harvest --db harvest.sqlite3 current
    python -m talent_harvest --db harvest.sqlite3 audit

ingest 对失败页返回非零退出码，并将原因保存在数据库；结果中每页标注其实际绑定的 source_revision。修复本地镜像文件后，重新运行同一批次即可只重试未完成页；ingest --page N 可指定页。已登记批次的清单不可修改；需要新声明时使用新批次标识。撤回来源使用 withdraw SOURCE --reason TEXT，这会让来源记录退出当前视图，保留历史与审计事件。来源撤回、补页或再次提交同一清单后，可分别用 status、current、history 和 audit 核对页状态、记录版本、当前可用数量和历史说明是否一致。

## 输入契约

来源 JSON 包含 key、name、format（json、csv 或 html）、kind（position、program 或 partnership）、可选 field_map 与 trust（0–100）。field_map 的键是规范字段名，值是镜像中的字段名。

来源声明按版本管理：再次 register 同一 key 而声明内容有变化（包括切换表头对应的 field_map、format 或 kind）时，会生成新的 revision 并完整保留历史版本，revisions SOURCE 可查看。清单的每页可用 source_revision 显式绑定声明版本；缺省时在批次登记时绑定当时的当前版本。绑定随批次固定：之后声明再更新，待重试的旧页仍按最初绑定的语义解析，已完成页的原始证据与版本不会被重新解释。解析每页前会先核对该版声明要求的业务列（record_id、organization、title、region、discipline、quantity、published_at 经 field_map 映射后的列名）与页摘要；列对不上时该页以 layout_mismatch 失败，保持可重试状态，且该页任何记录都不会进入当前供需结果。修正声明（产生新版本）后，旧批次用原绑定恢复，新版页面用新批次接收。

批次清单 JSON 包含 source、batch、带时区的 captured_at，以及顺序从 1 开始的 pages。每页包含相对 path、对文件原始字节计算的 SHA-256，以及可选的 source_revision。文件须位于清单目录下，UTF-8 编码，每页最多 2 MB，最多 10,000 条记录。JSON 页可用记录数组或 records 对象字段；CSV 的第一行为表头；HTML 页使用一个简单表格的首行作为表头。

每条记录需要 record_id、organization、title、region、discipline、quantity、published_at（YYYY-MM-DD）；reference 与 description 可选。数量为非负整数，零数量、未来发布日期、低可信来源和缺少明确引用的合作项目会被隔离。基础实体键由类别、机构、标题和地区确定；相同实体的不同来源若数量、学科或发布日期不一致，会列入争议而非当前可用数据。实体键是精确规范化后的匹配，不做模糊别名判断。

## 查询与运维

- current --quarantine 返回有效实体、争议与隔离记录；默认不附带隔离明细。
- history、trace IDENTITY、record SOURCE ID、timeline SOURCE、batch-evidence SOURCE BATCH 追踪来源与修订。
- revisions SOURCE 列出来源的声明版本历史；status SOURCE BATCH、operations、recovery、quality、audit 检查页级恢复、版本绑定和数据状态。
- summary、distribution、coverage、age YYYY-MM-DD、trend 和 conflicts 提供当前与历史摘要。
- snapshot FILE 写出当前视图与摘要；verify FILE SHA256 校验交付字节。
- preview SOURCE_JSON MANIFEST 在不登记批次的情况下检查本地文件；replay-check MANIFEST 对已完成批次复核原始镜像；checkpoint 读取游标快照。

CLI 将结果作为单行 JSON 写入标准输出，将合同错误写入标准错误。仓库中的机构、岗位和数量均为本地合成演示数据。
