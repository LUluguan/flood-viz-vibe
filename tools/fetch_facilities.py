# -*- coding: utf-8 -*-
"""
fetch_facilities.py — 抓取研究区关键设施 POI(天地图地名搜索 V2.0), 产出
flood_out/critical_facilities.json, 供 /api/cascade(设施受淹→服务缺口, 对标§四16)消费。

数据源与网络说明(2026-10-25 实测):
- 天地图地名搜索(本机可达, key=web/config.local.js 与前端同源): 医院/学校/消防站/
  派出所/泵站等类别有效; 变电站公开 POI 库覆盖不佳(返回全国其他类型)。
- Overpass/OSM 主 API(power=substation 电网节点)在本机网络不可达(DNS/超时)。
  若未来网络可达, 可扩展 SUBSTATION_SOURCES 下载并落入同一 JSON(kind="变电站"),
  cascade 端点即自动纳入电网节点(无该文件时跳过并如实标注)。

运行: python tools/fetch_facilities.py   (需外网; 断网时保留既有 JSON 不覆盖)
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "flood_out", "critical_facilities.json")
KEY_FILE = os.path.join(ROOT, "web", "config.local.js")

KEYWORDS = ["医院", "学校", "消防站", "派出所", "泵站", "变电站", "幼儿园"]
BBOX = (113.30, 23.09, 113.34, 23.13)          # 与 pipeline_config.STUDY_WS 一致
MAPBOUND = "113.300000,23.090000,113.340000,23.130000"
SPECIFY_HINT = "广州市(地图视野限定, 本地按 bbox 严格过滤)"
PAGES = 2                                       # 每类抓 2 页 × 60 条


def _tdt_key():
    cfg = io.open(KEY_FILE, encoding="utf-8").read()
    m = re.search(r'tdt\s*:\s*"([0-9a-f]{32})"', cfg)
    if not m:
        raise SystemExit("web/config.local.js 缺 tdt key(先按模板配置)")
    return m.group(1)


def search_keyword(tk, kw):
    got = []
    for page in range(PAGES):
        post = {"keyWord": kw, "queryType": "1", "mapBound": MAPBOUND,
                "level": 15, "start": page * 60, "count": 60}
        url = ("https://api.tianditu.gov.cn/v2/search?postStr=" +
               urllib.parse.quote(json.dumps(post, ensure_ascii=False)) +
               "&type=query&tk=" + tk)
        req = urllib.request.Request(url, headers={
            "Referer": "http://localhost:8001/",
            "User-Agent": "Mozilla/5.0 C2132-flood-webgis/1.7"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        except Exception as e:
            print("FAIL %s p%d -> %s %s" % (kw, page, type(e).__name__, str(e)[:60]))
            break
        pois = d.get("pois") or []
        if not pois:
            break
        for p in pois:
            ll = (p.get("lonlat") or "").split(",")
            if len(ll) != 2:
                continue
            lon, lat = float(ll[0]), float(ll[1])
            if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
                continue
            got.append({"kind": kw, "name": p.get("name", ""),
                        "addr": p.get("address", ""),
                        "lon": round(lon, 6), "lat": round(lat, 6)})
        time.sleep(0.25)
    return got


def main():
    tk = _tdt_key()
    out = []
    for kw in KEYWORDS:
        rows = search_keyword(tk, kw)
        print("%s -> in-bbox %d" % (kw, len(rows)))
        out.extend(rows)
    # 去重(同名同类 60m 内)
    dedup = []
    for x in out:
        if not any(x["name"] == y["name"] and abs(x["lon"] - y["lon"]) < 6e-4
                   and abs(x["lat"] - y["lat"]) < 6e-4 for y in dedup):
            dedup.append(x)
    out = dedup
    if not out:
        print("无结果: 保留既有 JSON 不覆盖" if os.path.exists(OUT) else "无结果且无既有文件")
        return
    payload = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "source": "天地图地名搜索V2.0(queryType=1+mapBound, 研究区bbox过滤)",
               "keywords": KEYWORDS, "bbox": list(BBOX), "features": out}
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False, indent=1))
    from collections import Counter
    print("saved", OUT, len(out), dict(Counter(x["kind"] for x in out)))


if __name__ == "__main__":
    sys.exit(main())
