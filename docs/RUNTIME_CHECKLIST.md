# RUNTIME_CHECKLIST — 部署/评审机自检

## 交付副本(开发源码目录)运行前提
代码副本与数据分目录交付时, 服务端按 `代码目录/dem|flood_out|unet_out|realevent_out` 解析路径:
- **方式一(推荐)**: 先 `python tools/sync_delivery.py --check` 只读比较。旧副本含禁止文件时须人工复核，
  或指定一个**新的**交付目录同步；脚本不会自动删除历史文件。`--destination <新目录>` 会同步
  代码及四类运行数据（STAC 缓存除外），禁止覆盖仓库/源目录。
- **方式二(手工)**: 把 `C2132_相关数据/` 下的 `dem`、`flood_out`、`unet_out`、`realevent_out`
  复制到本目录, 再启动服务。
缺数据时首页可打开, 但 `/api/scenarios`、`/api/impact` 等核心端点返回 404(2026-10 审计 DEL-02)。

## 必须随包附带(不可再生, 缺失即功能缺失)
| 项 | 用途 | 缺失表现 |
| --- | --- | --- |
| dem/study_dtm.tif | 浴缸法地形 | 淹没端点 404 `{"error":"无 100 年水深栅格"}` |
| flood_out/scenarios.json + flood_depth_*.tif + flood_extent_*.geojson | 情景数据 | 场景/水体 404 |
| flood_out/design_storm_24h.json | 在线模拟雨量 | 在线模拟外推 |
| unet_out/unet_water.pt | UNet 推理 | /api/predict 503 |
| unet_out/samples/ + eval_metrics.json | 精度卡 | unet/dashboard 精度卡空 |
| web/ (Cesium+ECharts) | 前端 | 页面白屏 |
| web/config.local.js | 天地图/Ion key | 底图走兜底或无底图(凭据文件, 禁入交付包, 部署时按 config.example.js 自备) |
| realevent_out/ (yingde/meizhou) | 真实事件 | 真实事件页空 |

## 可再生(管线脚本, 见 README 数据管线)
precip_tif/(pipeline/prep_precip.py) · flood_tiles/(tools/tile_flood.py) · uncertainty.json(tools/uncertainty_bands.py) · dem/study_pop.tif(pipeline/fetch_pop.py, 需 4.6GB 下载)

NetCDF 预处理仅支持有可用 netCDF4 的平台；本机 Windows + Python 3.13 的 PyPI 解析无匹配发行版，请在兼容环境生成 `precip_tif/`，Web 服务不依赖该库。NEAREST 训练标签与原始掩膜导出的源码已修复，但旧模型未重训、旧演示样本未重导、历史精度卡未重新计算，不能将源码修复当作已完成的数据升级。

## 仓库布局(2026-10 工程化整理)
`pipeline/` 数据管线脚本 · `web/data/` 前端生成数据 · `deploy/` 部署辅助 · `docs/` 文档
(本清单随之位于 docs/; 其余不变)

## 提交包自检(2026-10 审计 DEL-01/03)
- 交付包内**不得**含 `web/config.local.js`、`.env`、`web_users.json`(凭据);
- `C2132_作品.zip` 修改时间必须晚于最后一次提交(`pytest -k zip` 有断言守护), 单一 zip,
  不用分卷(Windows 资源管理器无法双击解压分卷包)。

## 安全运行
- 默认安全模式禁用公开弱口令。无用户表时先设置至少 12 字符的 `FLOOD_ADMIN_PASSWORD`；
  仅本机演示显式 `FLOOD_DEMO=1`，不要用于公网。已有用户不会自动重置。
- 公众报汛只开放审核后摘要/粗坐标；描述、精确坐标和照片需管理员令牌。
- `GET /api/warning` 永不触发邮件/企业微信。真实触达需管理员 `POST /api/warning/publish`，
  收件人须已确认且分区匹配。确认邮件需 `PUBLIC_BASE_URL` 与 SMTP，`SMTP_SECURITY=ssl|starttls|none`。
- 地图客户端 key 是公开客户端凭据而非服务端秘密，应在服务商控制台限制 allowed URLs/Referer
  与配额。构建镜像、同步副本和打包不带真实 `config.local.js`、`.env` 或用户表。

## 自检命令
```
curl http://127.0.0.1:8001/api/health   # artifacts 全 true 即关键产物齐
python -m pip install -r requirements-cpu.txt
python -m pip install -r requirements-dev.txt
pytest -q                               # 业务测试应全绿
pytest -q -k "zip or credentials or tracked_complete"
                                        # 旧ZIP/旧副本未人工处置时保持交付阻断，不得skip规避
```
