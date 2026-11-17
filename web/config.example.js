// config.example.js — 公开客户端地图凭据模板(此文件入库)
// 部署时复制为 web/config.local.js 并填入自己的客户端 key；local 文件不入仓库、镜像或交付包。
// 浏览器必须能读取这些 key，它们不是秘密；不得在此填写 SMTP、数据库口令或服务端密钥。
// 保持浏览器直连地图服务以兼容 Cesium；配置隔离不能替代服务商控制台的访问限制。
// 维护者须在各自控制台设置最小权限、资产范围及 allowedURL/Referer/域名白名单，核对部署地址。
window.FLOOD_KEYS = {
  tdt: "在此填入天地图客户端 key(32 位)",  // https://console.tianditu.gov.cn 配置域名/Referer 限制
  ion: "在此填入 Cesium Ion 客户端 token"   // https://ion.cesium.com/tokens 配置最小权限和 allowedURL
};
