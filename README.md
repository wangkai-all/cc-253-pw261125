# 合成测量 · 标定版本接收台

设备连续发原始读数，标定文件从另一条通道到达，可能晚到，也可能追溯更正。
本项目从零实现本地设备、标定投递、接收换算、版本存储和网页，全部使用合成测量。

```
measure/spec.py      合成设备模型、标定文档校验、换算
measure/schema.py    SQLite 表结构与只追加触发器
measure/intake.py    标定投递的分类（接受 / 重复 / 拒绝）
measure/store.py     原始读数、版本选择、换算队列、发布修订、崩溃恢复
app/server.py        HTTP 服务与 JSON API（标准库，无框架）
app/cli.py           命令行演示工具
web/                 单页界面（原生 JS，无构建步骤）
tests/               语义测试、API 测试、jsdom 页面冒烟测试
```

## 运行

```bash
python3 -m app.server --port 8787        # 页面在 http://127.0.0.1:8787/
python3 -m unittest discover -s tests -v # 61 个测试
```

无第三方依赖；数据库默认在 `var/receiver.db`，可用 `--db` 或 `MEASURE_DB` 指定。

## 数据模型

一条原始读数由 `device + seq` 唯一确定，内容是 ADC 原始计数与当时的设备配置
快照。读数一旦写入就不可修改；同一条重复投递是幂等的，内容不同则拒收。

标定文件是一个完整的、带版本的 JSON 文档：

```json
{
  "cal_id": "CAL-P", "cal_version": 3, "device": "SYNTH-P",
  "z0": 524288, "coefs": [100.4, 30.0, 0.5, 0.01],
  "validity": [50000, 16500000],
  "valid_from": "2026-01-01T00:00:00Z",
  "replaces": 2, "note": "复核后更正零点"
}
```

`u = (raw - z0) / 1e6`，`kPa = a0 + a1u + a2u² + a3u³`。
`validity` 是这份标定愿意换算的原始值窗口；`valid_from` 是它从哪个**测量时刻**
起生效。

## 选择规则

一份标定在某个测量时刻 T 生效，当且仅当 `valid_from <= T`。在某条读数的候选
集合里，**版本号最高**的生效版本被采用。两条被刻意排除的规则：

* 不用“最后收到的文件”——到达顺序不参与选择；
* 不把之后才生效的版本借给更早的读数——晚到的更正只覆盖它声明的时间段。

版本链之间也不允许静默分叉：同一 `(cal_id, cal_version)` 内容不同会被拒绝，
一个 `cal_id` 只能属于一台设备，一台设备只能有一条链。缺号允许（离线设备可能
跨版本补传），重复投递记为 `duplicate`，不产生新版本。

`semantic_identity` 只覆盖能影响输出或生效判断的字段。`replaces` 和 `note`
变化不会让已完成的换算失效，因此不会引发无意义的重新发布。

## 两种发布策略

每台设备独立设置，页面“发布策略”一栏即时切换，也可用
`POST /api/mode {"device": "...", "mode": "hold"|"provisional"}`。

* `hold`（等标定再发布）：测量时刻没有生效标定时**不产生任何发布记录**，
  读数留在队列里，等标定到达后一次性发布。代价是延迟；好处是下游永远看不到
  占位值。
* `provisional`（先发暂定值）：立刻发布 `unconverted`（无生效标定）或
  `out_of_validity`（原始值不在窗口内）的占位，标定到达后追加修订。代价是
  修订开销；好处是延迟为零。

页面“代价：延迟与修订”直接按实际发布记录统计：首次发布延迟的中位数、最终发布
延迟的中位数、被修订的比例、修订开销（发布总数 / 读数数 − 1）。切换策略只影响
尚未发布过的读数；已经发布过的值不会被策略切换重新计算，切换本身也不产生修订。

## 下游如何识别替换

发布表只追加，触发器阻止 `UPDATE` 与 `DELETE`。一次变化写成新的一行：

```
reading_id  revision  supersedes  source     status        value_kpa  cal_version
SYNTH-P:1   1         NULL        initial    unconverted   NULL       NULL
SYNTH-P:1   2         1           supersede  priced        461.98     1
SYNTH-P:1   3         2           supersede  priced        463.48     3
```

`revision` 递增，`supersedes` 指向上一条修订，`source` 区分 `initial` 与
`supersede`。历史行保持原样，下游可以据此区分“替换”与“历史被悄悄改写”。

## 交错与中断

换算是“认领 → 快照 → 计算（不持写锁）→ 条件提交”：

1. 在数据库里原子认领一条排队读数，防止多个换算进程重复处理；
2. 在 `BEGIN IMMEDIATE` 中记录本次采用的版本（`semantic_identity`）后提交；
3. 在事务外计算；
4. 重新开启事务，若当前的采用版本或发布策略与快照不一致，本次运行标记
   `stale`，什么也不写，并把任务放回队列；否则写入修订、删除任务、结束运行。

因此，补传的读数、边界更正与换算进程交错时，同一读数不会混用两版，旧计算的
结果也不可能覆盖新标定的结果。

崩溃恢复：运行记录在计算开始前落盘，任务在此期间保持认领状态。进程被硬杀后，
`resume()` 把 `running` 运行标成 `interrupted` 并释放任务；发布、任务删除和运行
结束在同一个事务里，所以崩溃时要么全都没有发生，要么全部完成。页面“模拟崩溃”
按钮就运行到这里，接着点“恢复并补算”即可看到同一条读数最终只发布一次。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/bootstrap` | 页面所需的全部聚合状态 |
| GET | `/api/readings` `?device=` | 读数列表 |
| GET | `/api/readings/{id}` | 单条读数、版本比较、修订历史 |
| GET | `/api/compare?reading_id=` | 只取版本比较 |
| POST | `/api/readings` | 单条读数（`raw` 或 `kPa`） |
| POST | `/api/readings/bulk` | 批量补传（并行通道） |
| POST | `/api/calibrations` | 投递标定文档 |
| POST | `/api/calibrations/generate` | 按参数生成一份合成标定 |
| POST | `/api/mode` | 切换发布策略 |
| POST | `/api/process` | 执行换算队列（`crash: true` 模拟中断） |
| POST | `/api/recover` | 恢复中断并补算 |
| GET | `/api/events` | 事件与运行记录 |
| GET | `/api/feed?since_id=` | 下游增量订阅：只追加的发布行 |

单条读数响应里 `comparison.rows` 列出每份已接受标定对该读数的结果、是否生效、
是否在窗口内，以及 `naive_latest_received`——如果按“最近收到的一份”会选到哪份、
与正确选择是否一致。它是反例展示，不是可用答案。

## 命令行演示

```bash
python3 -m app.cli --db var/demo.db init --mode provisional
python3 -m app.cli --db var/demo.db seed --device SYNTH-P --count 6 --start-ts 2026-02-01T00:00:00Z
python3 -m app.cli --db var/demo.db process
python3 -m app.cli --db var/demo.db deliver --device SYNTH-P --version 1 --valid-from 2026-01-01T00:00:00Z --drift 1.0
python3 -m app.cli --db var/demo.db deliver --device SYNTH-P --version 2 --valid-from 2026-03-01T00:00:00Z --drift 3.0
python3 -m app.cli --db var/demo.db process
python3 -m app.cli --db var/demo.db show
```

## 测试

`tests/test_store.py` 覆盖原始值不可变、按测量时刻选择、版本冲突、追溯更正、
超界不回落、策略切换、旧版本计算被丢弃、崩溃恢复与并发换算；
`tests/test_edges.py` 覆盖边界时刻、窗口端点、非单调系数、重复投递的规范化，
以及从快照重放到全新数据库。其中并发用例启动
四个换算进程同时消费队列，另一个线程持续投递新版本，断言每条读数的修订序列
按版本单调、且每个修订都能对应到一份真实被接受的文档。

`tests/test_api.py` 覆盖 HTTP 层。`tests/ui_smoke.mjs` 用 jsdom 驱动真实页面与
真实服务（`node tests/ui_smoke.mjs 8787`），依次点击发送、标定、崩溃、恢复，并
断言对比表与修订历史。合成模型的“真值”只用于比较展示与测试断言，换算本身
完全由收到的标定文件决定。

## 目录外的临时产物

页面冒烟测试需要 jsdom，它不在项目依赖里（项目本身无第三方依赖）：

```bash
npm_config_cache=/tmp/npmcache npm install --prefix /tmp/jsdomtest jsdom
JSDOM_PATH=/tmp/jsdomtest/node_modules/jsdom node tests/ui_smoke.mjs 8787
```
