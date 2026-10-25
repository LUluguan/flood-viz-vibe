# -*- coding: utf-8 -*-
"""
flood_core.py — 多洼地独立水面浴缸法内核(全仓唯一实现)。

背景(对标分析 §四1, 2026-10): 原全局浴缸法假设整片陆域一个水平面, 高地背后与河道
不连通的洼地会被"隔山淹没"(伪影), 而真正的低洼汇水区又与高地共享同一 W。本模块改为
文献公认的第一步改良(modified bathtub / depressional storage):

  1) Wang-Liu 优先级洪泛填洼(priority-flood): 河道(z<=0)与网格边界为出口,
     得到填洼地形 filled 与每格的出流父节点(发现者);
  2) 洼地识别与汇水分区: filled>z 的连通平塘各为一个洼地; 沿出流树回溯,
     每个陆域格的径流最终汇入哪个洼地(或直接入河/出界);
  3) 逐洼地水量反演: 每个洼地只接纳"自身汇水面积"上的径流(q × A_catch),
     按该洼地的水位-库容曲线(hypsometric)解析反演独立水面 W_d(免二分迭代);
  4) 溢流级联: 来水超过洼地库容时, 余水经溢口注入下游洼地(出流树拓扑序
     传递, 无环), 直至入河出境——体积守恒不被"平塘上限"截断丢失。

体积守恒: Σ(各洼地蓄水) + Σ(溢流出境) == q × Σ(各洼地汇水面积); 直排河道的
陆域径流不进任何洼地; 无洼地地形不淹没(排水通畅的理想化假设)。

消费方: pipeline/bathtub_flood.py(离线情景)、app.py(在线模拟/问答/过程线)、
tools/uncertainty_bands.py(敏感性带)——三处原各有一份浴缸实现(搜索上界还互不
一致), 本模块收拢为唯一权威(ARC-01 口径)。
"""
import heapq
from collections import deque

import numpy as np

# 8 邻域偏移(行, 列)
_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _label_components(mask):
    """8 邻域连通域标记。mask: 2D bool。返回 (labels int32, 数量)。未标记为 -1。"""
    rows, cols = mask.shape
    lab = np.full((rows, cols), -1, dtype=np.int32)
    n = 0
    for r0, c0 in zip(*np.nonzero(mask), strict=False):   # nonzero 两个返回值长度恒等
        if lab[r0, c0] >= 0:
            continue
        dq = deque([(r0, c0)])
        lab[r0, c0] = n
        while dq:
            r, c = dq.popleft()
            for dr, dc in _NB:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and mask[nr, nc] and lab[nr, nc] < 0:
                    lab[nr, nc] = n
                    dq.append((nr, nc))
        n += 1
    return lab, n


class DepressionModel:
    """对一份固定 DTM 预计算填洼/洼地/汇水分区, 之后任意径流深 q 的反演都是查表级开销。

    z: 2D float 数组, 研究区 DTM(河道/边界外为 z<=0, 陆域 z>0, 单位 m)。
    构建 O(n·log n)(n≈2 万格, 百毫秒级); flood() O(洼地数 × log 像元数)。
    线程安全: 构建完成后只读(内部无数组就地写), flood 可多线程并发。
    """

    def __init__(self, z):
        self.z = np.ascontiguousarray(z, dtype="float64")
        self.rows, self.cols = z.shape
        self._build()

    # ---------- 构建: 三步(填洼 → 分区 → 库容曲线), 各步只写自身属性 ----------
    def _build(self):
        self._priority_flood()
        self._partition()
        self._curves()

    # ---------- 步骤 1: 优先级洪泛填洼 + 出流树 ----------
    def _priority_flood(self):
        """Wang-Liu 优先级洪泛: 河道/边界为出口, 低地后淹; 记录发现父节点与弹出序。"""
        rows, cols = self.rows, self.cols
        z = self.z
        outlet = (z <= 0)
        outlet[0, :] = True
        outlet[-1, :] = True
        outlet[:, 0] = True
        outlet[:, -1] = True
        filled = z.astype("float64").copy()
        visited = outlet.copy()
        parent = np.full(rows * cols, -1, dtype=np.int64)
        order = np.empty(rows * cols, dtype=np.int64)
        heap = []
        k = 0
        for r, c in zip(*np.nonzero(outlet), strict=False):   # 同上, 长度恒等
            heapq.heappush(heap, (float(z[r, c]), int(r * cols + c)))
        while heap:
            _, cur = heapq.heappop(heap)
            order[k] = cur
            k += 1
            cr, cc = divmod(cur, cols)
            fc = filled[cr, cc]
            for dr, dc in _NB:
                nr, nc = cr + dr, cc + dc
                if not (0 <= nr < rows and 0 <= nc < cols) or visited[nr, nc]:
                    continue
                visited[nr, nc] = True
                if z[nr, nc] < fc:
                    filled[nr, nc] = fc            # 抬到入流侧水面(填洼)
                nidx = nr * cols + nc
                parent[nidx] = cur
                heapq.heappush(heap, (float(filled[nr, nc]), int(nidx)))
        self.filled = filled
        self._parent = parent
        self._order = order[:k]

    # ---------- 步骤 2: 洼地识别 + 汇水分区 ----------
    def _partition(self):
        """平塘连通域=洼地; 沿出流树(弹出序)回溯得每格汇水归属与各洼地汇水格数。"""
        z = self.z
        dep_mask = self.filled > z
        dep_lab, self.n_dep = _label_components(dep_mask)
        self._dep_lab = dep_lab.ravel()              # 塘体足迹(平塘本身, 淹没深度只写这里)
        dep_of = np.full(z.size, -1, dtype=np.int32)   # 汇水归属(足迹+上游来水格)
        if self.n_dep:
            dep_of[self._dep_lab >= 0] = self._dep_lab[self._dep_lab >= 0]
            # 出流树回溯: order 为弹出序, 父必先于子; 平塘格标签固定, 其余格继承父的归属
            for cur in self._order:
                if dep_of[cur] >= 0:
                    continue
                p = int(self._parent[cur])
                if p >= 0 and dep_of[p] >= 0:
                    dep_of[cur] = dep_of[p]
        self._dep_of = dep_of
        land_flat = (z > 0).ravel()
        self.catchment = (np.bincount(dep_of[land_flat & (dep_of >= 0)],
                                      minlength=self.n_dep).astype(np.int64)
                          if self.n_dep else np.zeros(0, dtype=np.int64))

    # ---------- 步骤 3: 每个洼地的水位-库容曲线 + 溢口目标 ----------
    def _curves(self):
        """预计算各洼地排序高程/累计库容/满蓄容量/溢口与级联去向(反演时纯查表)。"""
        flat_z = self.z.ravel()
        flat_filled = self.filled.ravel()
        self._zs = []          # 平塘格原始高程升序
        self._cumv = []        # 水位升到 zs[k] 的累计库容(单格面积=1, 调用方乘 cell_area)
        self._spill = []       # 溢口高程(=平塘填洼水面)
        self._cap = []         # 满蓄库容(水位=溢口, 单格面积=1)
        self._cells = []       # 平塘格 flat 索引
        self._spill_target = []  # 溢流注入的下游洼地(-1=入河/出界)
        first_seen = np.full(self.n_dep, -1, dtype=np.int64)
        for cur in self._order:                     # 单遍: 记录每个平塘最早被发现的格(=溢口)
            d = self._dep_lab[cur]
            if d >= 0 and first_seen[d] < 0:
                first_seen[d] = cur
        for d in range(self.n_dep):
            idx = np.nonzero(self._dep_lab == d)[0]   # 塘体足迹(非汇水区)
            zs = np.sort(flat_z[idx])
            self._zs.append(zs)
            # 库容曲线: cumv[k] = V(水位=zs[k]) = k*zs[k] - Σ_{i<k} zs[i]  (cumv[0]=0)
            s = np.concatenate([[0.0], np.cumsum(zs)])          # s[k] = Σ_{i<k} zs[i]
            self._cumv.append(np.arange(len(zs)) * zs - s[:-1])
            # 满蓄库容(水位=溢口时, n 个塘格全部淹没): n*spill - Σ zs
            spill = float(flat_filled[idx].max())
            self._spill.append(spill)
            self._cap.append(len(zs) * spill - float(np.sum(zs)))
            self._cells.append(idx)
            tgt = -1
            first = int(first_seen[d])
            if first >= 0:
                p = int(self._parent[first])
                if p >= 0:
                    tgt = int(self._dep_of[p])       # 平塘缺口外的格: 其归属即溢流去向
            self._spill_target.append(tgt)
        self._sizes = np.array([len(c) for c in self._cells], dtype=np.int64) \
            if self.n_dep else np.zeros(0, dtype=np.int64)

    # ---------- 步骤 4: 任意径流深 q 的反演 ----------
    def _level_for_volume(self, d, v):
        """洼地 d 蓄 v(单格面积=1)后的水面高程(≤溢口)。解析解, 免二分。

        v ∈ [cumv[k], cumv[k+1]] 时淹没 k+1 个最低格并均匀抬升:
        L = (v + Σ_{i<=k} zs[i]) / (k+1); v 超过 cumv[-1](塘内最高格)时 k=n-1 同式。
        """
        zs, cumv = self._zs[d], self._cumv[d]
        k = int(np.searchsorted(cumv, v, side="right")) - 1   # v ∈ [cumv[k], cumv[k+1])
        k = max(k, 0)
        return (v + float(np.sum(zs[:k + 1]))) / (k + 1)

    def flood(self, q_m, cell_area_m2, min_pool_cells=3, drain_depth_m=0.0):
        """给定径流深 q(m)与单格实地面积(m²), 反演逐洼地独立水面。

        min_pool_cells: 最小制图洼地(格数, 默认 3 ≈ 0.26ha@30m)。更小的微坑视为
        排水通畅不留蓄(其来水经溢流级联全部下泄)——30m GLO-30 上 1-2 格微坑多为
        DTM 噪声/已被市政管网消纳的浅洼, 逐坑注水会把淹没格数虚抬 40%+(实测)。

        drain_depth_m: 排水系统折减深度(m/场, 对标§四2 过渡版 SWMM/§四14 工程预案):
        泵站强排/管网改造按 24h 折算的削减能力, 从径流深中扣除(负不留蓄)。
        现状情景取 0(与设计暴雨口径连续); 工程方案 >0 演示"预演工程调度"。

        返回 (depth: float32 2D 水深(仅洼地内>0, 河道为 0),
              info: {"water_level_m": 主涝区(蓄水量最大洼地)水面, 展示口径,
                     "water_level_max_m": 最高洼地水面,
                     "levels": [{dep, W_m, cells, catchment_cells, volume_m3} 按水位降序],
                     "spilled_m3": 级联后最终出境水量,
                     "method": "multi-depression"})。
        """
        depth = np.zeros((self.rows, self.cols), dtype="float32")
        info = {"water_level_m": 0.0, "levels": [], "spilled_m3": 0.0,
                "method": "multi-depression"}
        if q_m <= 0 or self.n_dep == 0:
            return depth, info
        q_m = max(0.0, q_m - drain_depth_m)   # 排水折减: 削减后不足一格深度的洼地自然干涸
        if q_m <= 0:
            return depth, info
        flat_z = self.z.ravel()
        inflow = self.catchment.astype("float64") * (q_m * cell_area_m2)  # 各洼地初始来水(格)
        # 溢流级联: 出流树(洼地->溢口->下游洼地)无环, Kahn 拓扑序=上游先结算
        indeg = [0] * self.n_dep
        for t in self._spill_target:
            if 0 <= t < self.n_dep:
                indeg[t] += 1                          # 入边数: 有多少上游洼地会溢入 t
        dq = deque(d for d in range(self.n_dep) if indeg[d] == 0)
        wet = {}
        spilled_out = 0.0
        while dq:
            d = dq.popleft()
            v = inflow[d]
            if v > 0 and self._sizes[d] >= min_pool_cells:
                capacity = self._cap[d] * cell_area_m2
                v_eff = min(v, capacity)
                W = self._level_for_volume(d, v_eff / cell_area_m2)
                idx = self._cells[d]
                depth.reshape(-1)[idx] = np.clip(W - flat_z[idx], 0.0, None).astype("float32")
                wet[d] = (W, v_eff)
            excess = v - (wet[d][1] if d in wet else 0.0)
            t = self._spill_target[d]
            if excess > 0:
                if 0 <= t < self.n_dep:
                    inflow[t] += excess
                else:
                    spilled_out += excess            # 入河/出境(体积离开系统, 物理正确)
            if 0 <= t < self.n_dep:
                indeg[t] -= 1
                if indeg[t] == 0:
                    dq.append(t)
        info["levels"] = [{"dep": int(d), "W_m": round(W, 3),
                           "cells": int(len(self._cells[d])),
                           "catchment_cells": int(self.catchment[d]),
                           "volume_m3": round(v, 1)}
                          for d, (W, v) in sorted(wet.items(), key=lambda kv: -kv[1][0])]
        if wet:
            # 展示口径 water_level_m = 蓄水量最大洼地的水面(主涝区; 与旧全局水位量级连续,
            # 不被高位"悬空"小坑的绝对高程带偏); water_level_max_m 为最高洼地水面(诚实上界)
            lead = max(wet.values(), key=lambda wv: wv[1])
            info["water_level_m"] = round(lead[0], 3)
            info["water_level_max_m"] = round(max(W for W, _ in wet.values()), 3)
        info["spilled_m3"] = round(spilled_out, 1)
        return depth, info
