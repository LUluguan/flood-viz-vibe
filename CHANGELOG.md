# CHANGELOG

## [1.7] — 2026-10-25

### 官方数据上网检索与落地(用户授权联网检索, 全部注明出处)
- **广州暴雨强度公式(§四4)**: `pipeline_config.gz_rainfall_intensity` — 转录《广州市暴雨
  强度公式(2022-12版, 中心城区)》(广州市水务局 2023-03-07 公告 swj.gz.gov.cn/gkmlpt/
  content/8/8835/post_8835843.html; 总公式 q=13290.630(1+0.607lgP)/(t+39.126)^0.956 +
  更准的分重现期区间参数公式)。新端点 `/api/design_storm`(5min 步长 30-180min, 交替块法
  排列, 峰现位置可调): 任一历时最大窗口雨量严格=官方公式值(测试校验); 100年2h=181.7mm,
  60min≈135mm/h 量级合理
- **USACE/HAZUS 损失曲线转正(§四15)**: `loss_curve=hazus` 从"示意逼近"升级为公开刊载的
  USACE RES1(单层无地下室)均值曲线实测数值(1ft≈9%/2ft≈16%/3ft≈23%/4ft≈31%/5ft≈38%,
  Al Assi et al. 2023 等文献刊载并见于 FEMA Hazus 手册曲线目录; 6-8ft 续段注明需按手册
  复核); 诚实限制保留: 各建筑类型未分曲线校准, 高层损失率会被低估
- **关键设施数据(§四16)**: 新工具 `tools/fetch_facilities.py`(天地图地名搜索 V2.0, 与前端
  同一 key)抓取研究区真实 POI → `flood_out/critical_facilities.json`(11 处: 4医院/2学校/
  2消防站/3派出所, 含广东省第二人民医院/五羊消防救援站等)。新端点 `/api/cascade`:
  设施场地积水≥0.5m 判受损 → 服务半径内建筑受淹数=服务缺口 → 前端"⚡ 设施连锁"按钮
  (红标受损设施+缺口虚线, 浏览器实测 1 受损+8 缺口线)
- **电网数据如实说明**: 变电站节点在本机网络不可得(Overpass/kumi 镜像 DNS 阻断或超时,
  OSM 主 API 超时, 天地图 POI 库无变电站覆盖), 连锁分析以"设施受淹→服务缺口"替代;
  /api/cascade 已预留电网节点接口(kind="变电站" 同文件自动纳入联动)
- **测试密封化**: conftest 统一清空 LLM_CFG(.env 真实 key 不再影响任何用例, 防外网抖动,
  test_realtime_rain 因本机对 Open-Meteo DNS 忽通忽断曾抖动); LLM 行为用例自行打桩
- **修复**: design_storm 交替块排位死循环(slots 占用判断与初始化类型不一致); 逐块舍入
  累计误差(total 改由舍入后雨型求和)
- 全量回归 105 passed / 1 预期失败(zip 新鲜度, 用户动作); 真网验证: LLM function-calling
  (glm-4-flash 真实调用本地工具给出真实数值)/设计雨型/设施连锁 全链路通过

## [1.6] — 2026-10-25

### 外部服务接入(对标§四9/10/17/21; 全部可选, 缺配置/断网自动回退, 服务不依赖外网)
- **真实雨情 Open-Meteo**(§四9, 免费无key): `/api/realtime_rain` 优先实况(近24h逐时→
  强度分级/站点卡/12h趋势, 站点=格点值+确定性扰动±10%), 失败回退原演示情景
- **72h 预报驱动**(§四10, Google Flood Hub 模式): 新端点 `/api/forecast_flood` —
  Open-Meteo(GFS/ICON)未来72h逐时降雨 → 6/12/24/48/72h 累计径流 → 多洼地反演 →
  风险时间线+最不利时距淹没多边形; 前端"🌧 72h 降雨预报推演"按钮(在线模拟区);
  预报源不可用回 503(不做假预报兜底)
- **LLM 智能问答**(§四17): `/api/assistant` 配置 LLM_API_KEY(.env)后启用
  function-calling——9 个工具(情景/在线模拟/预警/易涝点/影响/疏散/过程线/实况/预报)
  直连本地核心逻辑, 3 回合编排; 失败/未配置回退规则引擎; 兼容智谱 GLM-4-Flash 免费档
  与任意 OpenAI 兼容接口
- **预警真实触达**(§四21): 橙/红预警触发企业微信群机器人 webhook(markdown)+SMTP 邮件
  (发送 /api/subscribe 订阅者, 每批≤20人); 同情景同等级 30 分钟冷却去重(本地状态文件,
  重启不重复); 后台线程发送不阻塞响应; 新模块 externals.py(全部出网集成唯一出口)
- `.env.example` 补全全部新配置项与获取说明; CI ruff 清单纳入 externals.py/flood_core.py

### 大改造落地(可在本地完成的部分)
- **排水折减/工程预案**(§四2过渡版+§四14): flood_core.flood 新增 drain_depth_m
  (泵站/管网 24h 折减能力, 从径流深扣除); `/api/online_sim?drain_mm=` + 前端"排水折减"
  滑条(现状管网/工程预案)——"预演工程调度"从径流系数单参数扩展为雨量×C×排水三参数
- **集合预报**(§四5): 新端点 `/api/ensemble`(确定性种子蒙特卡洛 24 成员: 雨量±20%×
  C±0.07 → 逐格淹没概率场/W分位数P10-P50-P90/面积区间) + `/api/ensemble_png`(紫色
  概率场, GloFAS/GFM 概率制图风格) + 前端"🎲 集合概率场"图层开关(随情景切换刷新)
- **HAZUS 式损失曲线**(§四15): `/api/impact?loss_curve=demo|hazus` — 分建筑类型分段
  线性 depth-damage 曲线族; 诚实标注为 HAZUS-MH 量级示意逼近, 非官方表格数值
- **OGC API – Features**(§四8): `/api/ogc`(landing/conformance/collections/items),
  flood-extent(逐块带W)与 buildings 两集合, GeoJSON+分页(numberMatched/next),
  QGIS/ArcGIS 可直接加载
- **COG**(§四8): tools/make_cogs.py(内建金字塔+128分块, 已生成5档) + `/api/cog/{T}`
  端点(image/tiff), GDAL/QGIS HTTP 直读
- 测试: 新增 tests/test_v16_integrations.py(12 项, 全离线打桩: Open-Meteo/LLM/webhook/
  SMTP/冷却/排水/集合/损失曲线/OGC/COG); 全量 100 passed + 1 预期失败(zip 新鲜度守护)
- 浏览器真网验证: 实况(广州当前无雨,24h=0.0mm)/72h预报推演(未来无降雨→各时距0km²)/
  集合概率(P10/P50/P90=4.43/4.55/4.66m, 面积1.298–1.686km²)/排水折减(300mm+20mm:
  淹没1.507→1.405km², 水位4.56→4.43m) 全链路通过

### 未实施(需重型本地软件/官方数据/专用管线, 维持路线图)
- 真 SWMM 管网耦合(无管网拓扑数据)、HEC-RAS/LISFLOOD-FP 水动力与 DL 代理(离线重软件+
  训练数据)、3D Tiles 资产化(转换+前端双改)、PMTiles(需 HTTP Range 服务)、Chicago 雨型
  (需官方暴雨强度公式参数)、网络型关键设施拓扑(无电网/路网数据)、报汛照片反演水深(CV 重)、
  IoT 传感器(自建硬件)、短信网关(需企业资质)

## [1.5] — 2026-10-25

### 模型内核: 多洼地独立水面替换全局浴缸(对标分析 §四1, 优先级 P1)
- 新增 `flood_core.py`(全仓唯一实现): Wang-Liu 优先级洪泛填洼 → 洼地识别(连通平塘)
  → 出流树汇水分区 → 逐洼地水位-库容曲线解析反演(免二分) → 满蓄溢流级联(Kahn 拓扑序, 体积守恒)
- 消除旧全局单水面"隔山淹没"伪影: 排水通畅低地不再被淹; 高位悬空洼地只蓄自身汇水
- 最小制图洼地 min_pool_cells=3(约0.26ha): 30m GLO-30 上 1-2 格微坑多为噪声/已市政消纳,
  逐坑注水会虚抬淹没格数 40%+(实测 685→281 洼地, 2年淹没与旧口径差 +9%)
- `water_level_m` 展示口径 = 主蓄水洼地(蓄水量最大)水面, 与旧全局水位量级连续
  (2年 2.68m / 100年 4.57m); 新增 `water_level_max_m` 与 per-pool `levels`
- app.py `_bathtub` / pipeline/bathtub_flood.py / tools/uncertainty_bands.py 三处
  独立浴缸实现(搜索上界互不一致)收拢为 flood_core 唯一权威(ARC-01)
- 淹没多边形逐斑块携带自身水面 `properties.W`, 前端 buildWater/addFoam/addWaterDetail
  按斑块独立挤出水面, 涨水动画改为整体比例 0→1; 清理 waterPrimitive 死代码
- 全部产物已重算: flood_depth/extent/scenarios/uncertainty/web-data/瓦片(z9-14)

### 新功能: 单点水淹过程线(对标 §四13)
- `GET /api/hydrograph?lon&lat&rain_mm&c`: 24h 三角雨型(峰现0.4, 与演进动画同口径)
  16 档累积径流 → 多洼地反演 → 采样点"何时开始淹/峰值水深"序列(单调涨水段)
- index.html 点击三维场景任意点 → 过程线浮层(SVG 水深实线 + 累积雨量虚线)
- 浏览器实测: 易涝点(地面1.23m) 100年雨型 → T+6h 开始淹没, 峰值 0.568m

### 安全加固(2026-10 审计 BE/FE 系列)
- 口令哈希 sha256 单轮 → PBKDF2-HMAC-SHA256 12万轮(格式 pbkdf2$iters$salt$hash);
  旧格式登录验证成功后自动平滑升级; 全程 hmac.compare_digest 常数时间比较
- `_tokens` 过期清扫(512 触发清 12h 过期 + 4096 硬上限; 旧实现只增不减)
- 登录叠加纯 IP 维度 30次/分限流(封堵轮换用户名的口令喷洒)
- `/api/online_sim` 频控 30次/分(全站最贵 CPU 端点, 旧版无频控)
- `report_status` 读改写加 `_report_lock`(与公众提交并发不再丢报汛)
- 前端 token 改 `x-auth-token` 请求头(index.html /api/auth/me 与 mobile.html 状态更新;
  旧 query 传参会进访问日志与浏览器历史); `esc()` 从 mobile.html 上移 web/common.js 共用
- /api/health 的 store_quarantines 接通 store._state_quarantines(旧恒为 0)
- assistant 影响/损失分支补数据缺失守卫(旧版缺栅格时 TypeError 500)
- .gitignore 补 `reports/reports.json`(公众自由文本上报此前被 git 跟踪入公开仓库);
  **待用户执行 `git rm --cached reports/reports.json` 并确认是否处理历史提交**

### 性能与修复
- `_theme_blend` numpy 向量化(原逐像素 Python 双循环 85.5 万格/次, 语义与输出逐像素一致)
- unet.html 分号截断修复(by_region 分区域 IoU 恢复渲染)
- dashboard.html loadHist / mobile.html 状态更新补失败反馈
- sync_delivery 静态清单补 flood_core.py(交付副本可独立运行)
- 测试: 新增 test_flood_core(6 项算法不变量) + test_v15_regressions(10 项),
  更新 2 处旧算法口径断言(depth_hist >5m 档 / online_sim 水位单调区间)
- 全量回归 88 passed / 1 failed(C2132_作品.zip 新鲜度守护, 提交前需用户重打包)
- 浏览器端到端验证: 页面加载/508 斑块独立水面/过程线交互/24h 演进动画 全通过

## [1.4] — 2026-10-06

### 仓库工程化整理(文件分类)
- `pipeline/` ← 15 个数据管线脚本(prep_*/fetch_*/bathtub/load_flood_pg/export_*/
  train/eval/infer/realevent_beijiang); 统一 ROOT=仓库根 + sys.path 引导, README 命令同步
- `web/data/` ← 12 个前端生成数据(*_data.js / buildings_3d*.js / flood_*.js /
  flood_depth_*y.png / unet_samples.js); 页面引用与导出脚本写出路径同步
- `deploy/` ← watchdog.bat / load_buildings.sql; `docs/` ← RUNTIME_CHECKLIST / WORKFLOW
- `data_raw/` ← 7 个零引用中间数据(gz_buildings*.json 等)移出 git 跟踪(保留本地)
- 根目录只保留服务层模块/共享库/前端入口页/工程约定文件
- sync_delivery 同步集升级为"git 跟踪全集 ∪ 静态清单", 守护测试改为逐文件完整性核查;
  test_local_resources 扩展为 11 页"页面本地引用逐一存在"断链守护

## [1.3] — 2026-10-06

### 安全(全量代码审计修复)
- Cesium Ion token 历史泄露确认 → 交付包/历史核查流程固化(轮换由用户在 Ion 控制台执行)
- `web/config.local.js` 凭据从交付副本强制移除(sync_delivery.py DENY 黑名单 + 守护测试)
- 登录接口爆破防护: IP+用户名双键限流(5 次/分钟, 此前 60 次错口令无一 429)
- `/api/predict` 流式截断读取(64MB 上限不再全量进内存) + 5 次/分钟频控 + 非 GeoTIFF 归 400
- `/api/subscribe` 加固: 3 次/分钟限流 + 总量上限 409 + 白名单式邮箱校验 + 入库剥标签
- token 支持 `Authorization: Bearer`(query 兼容); uvicorn 访问日志 `?token=` 自动打码
- 报讯写盘故障(OSError/磁盘满/文件占用)归 503, 不再伪装成"字段格式错"400

### 修复
- `prep_design_storm.py` NameError(RETURNS 收拢后未跟进), README 标注"必跑"的脚本恢复可复现
- 频控 `_rate_ok` 真正持 `_rate_lock`(check-then-act 原子性)

### 一致性(ARC-01)
- 淹没阈值 0.05 三处硬编码/面积魔数五处/colorize 双份实现全部收拢:
  `pipeline_config`(DEPTH_BINS/cell_area_m2) + `flood_render.py`(色带唯一实现)
- XYZ 瓦片脚本(tools/tile_flood.py)自持的 12 行色带渐变同样接入 flood_render(ARC-01d)

### 工程化
- sync_delivery.py 升级: 根级文件改为"静态清单 ∪ git 跟踪根级文件 ∪ flood_depth_*y.png"
  (DEL-04: 水深 PNG 曾漏出清单 → 交付版 flood.html 水深图层全 404); web/ 全目录纳入同步
  (echarts.min.js 曾不在任何清单); 数据目录(dem/flood_out/unet_out/realevent_out)一并同步
  使副本可独立运行, 凭据黑名单强制移除; 交付守护测试升级(能出数 200/无凭据/资产完整性/zip 新鲜度)
- CI: 先重建交付副本再跑测试(守护不再空转) + ruff E9,F 门禁 + junit/依赖冻结归档
- 依赖升级: pillow 12.3.0 / python-multipart 0.0.32(OSV HIGH 修复版)
- 死导入清理 15 处 + `_reports_file` 重复定义删除

### 测试
- 新增 test_audit_regressions.py(写盘 503/登录限流/订阅加固/predict 400/日志打码/影子常量扫描等)
- test_deploy_files.py 重写(交付副本四项守护 + zip 新鲜度断言)

## [1.2] — 2026-10-05

### 安全加固
- 存储型 XSS 双层消毒: 后端 `_clean_text` 剥标签 + 前端 `esc()` 转义
- 匿名上报 IP 频控(20 条/60 秒, `FLOOD_TRUST_PROXY` 开关控制 XFF 信任)
- 凭据外置: 天地图 key 与 Ion token 移入 gitignore 的 `web/config.local.js`
- 静态文件 basename 黑名单(web_users/subscribers/reports.json 一律 404)
- `redirect_slashes=False` 消除 `//` 路径兜底链
- `torch.load` 加 `weights_only=True` 防 pickle 注入

### 工程化
- 淹没水深预渲染 XYZ 瓦片服务(`tools/tile_flood.py`, z9–14)
- 重现期情景 ±20% 降雨敏感性不确定性带
- Copernicus EMS 模板三件套专题图(extent/facilities/hazard)
- UNet 演示样本改取 `val_scenes`(未参与训练), 精度卡含 by_region 分区
- `_Memo` 统一 10 个缓存的查算存(带锁防 dogpile + FIFO 淘汰)
- 报讯 store 读写加锁 + 原子写(tmp+os.replace) + 损坏隔离(.corrupt-ts)
- `/api/health` v2: db_reachable/scenarios_source/artifacts/cache 命中率/store_quarantines
- 底图懒兜底: 天地图 4s 探测失败才加 Ion(省 60+ 首屏外网请求)
- getJSON 统一 r.ok 检查 + 15s 超时(AbortController)
- pipeline_config.py 收拢全仓常量(bbox/DEPTH_CAP/RETURNS 等 7+ 处)

### 数据修正
- 双事件(英德/梅州) NEAREST 重跑: 云量真值(22.3%/61.6%), SAR 变化检测修复
- UNet 演示样本改取验证场景(val_scenes), 消除训练集泄漏
- reports.json 清理至 4 条真实/演示记录(删 53 条测试残留)
- train_unet 保存 val_IoU 最优权重(旧版无条件存最后一个 epoch)

### 测试
- 新增 test_zero_coverage.py(health/png/critical_assets/predict/subscribe)
- 新增并发上报 10 线程不丢不重测试
- 新增凭据外置检查(无内嵌 key/JWT, config.local.js 未跟踪)
- pytest.ini: network marker 默认过滤(日常回归约 10s, 外网链路 opt-in)
- store 隔离: conftest.py autouse fixture 重定向到 tmp_path

## [1.1] — 2026-10-04

### 功能
- 分级预警条 / 疏散分析(A* 避水寻路) / 防汛智能问答 / 专题图导出 / 实时雨情
- 公众报汛(移动端 H5) / 用户登录(管理员/公众) / 情景双屏对比 / GeoScene 2D 分析

## [1.0] — 2026-09

### 初始版本
- 三维洪涝模拟 / 数据大屏 / 真实事件反演(英德) / UNet 水体提取
- 浴缸法水位反演 / P-III 设计暴雨 / Gumbel 重现期拟合
