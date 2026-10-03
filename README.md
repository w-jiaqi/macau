# 澳门一日游 · 路线地图

一张为手机设计的澳门一日游路线图：从 **澳门外港客运码头** 出发，按顺序经过你输入的地点，沿真实道路到达 **氹仔客运码头**。纯静态网页，无需构建，直接部署到 GitHub Pages 即可使用。

> 在线地址：<https://w-jiaqi.github.io/macau/>

## 它是怎么工作的

- **两张对齐的地图**：底下是一张“真实地图”（OpenStreetMap 数据绘制，负责位置、道路与路线）；上面叠着一张主题乐园风格的 **插画地图**——它就是一张图片，按 Web 墨卡托投影从同一份真实地理数据渲染出来，所以和真实地图严格对齐，加载快、也容易缓存。右上角的图层按钮可以随时切换查看真实地图。
- **输入地点**：面板里起点、终点固定，中间的站点点一下就能输入——可以直接选热门景点，也可以输入任何澳门地名、酒店、餐厅（通过 OpenStreetMap 搜索）。可以「添加一站」或用 × 删除，站数不限。
- **真实路线**：内置景点之间的路线是预先算好的（打开就有、离线可用）；其他地点会实时向 OSRM 路线服务请求，沿真实道路绘制（短距离走步行路线，其余走车行路线）。
- **手机优化**：底部抽屉式面板；电脑大屏上面板在左侧，可以收起；支持深色模式、「添加到主屏幕」与离线打开。

## 修改默认路线

编辑 [`data/route.json`](data/route.json)：

```json
{
  "title": "澳门一日游",
  "stops": [
    { "name": "保利美高梅博物馆", "lat": 22.186104, "lng": 113.547588 },
    "grand-lisboa",
    "大三巴牌坊"
  ]
}
```

- `stops`：按游览顺序列出默认站点（第一个就是第一站），数量不限。每一项可以是内置景点 ID（如 `"grand-lisboa"`）、内置景点名称（如 `"大三巴牌坊"`），或任意地点 `{ "name", "lat", "lng" }`（WGS-84 坐标）。起点（外港码头）和终点（氹仔码头）是固定的，不用写。
- `title`：页面标题。
- 改完后运行 `python3 tools/build_route.py`，把非内置地点相关路段的真实路线写进 `route.json`（`legs` 字段），这样默认路线打开即显示，不用联网计算。
- 访客在网页上修改过的路线只保存在他自己的浏览器里；没改过的访客总是看到这里的默认路线。

可用的景点 ID：

<!-- places:start -->
| ID | 景点 | 区域 |
|---|---|---|
| `ruins-st-paul` | 大三巴牌坊 | 澳门半岛 |
| `mount-fortress` | 大炮台（澳门博物馆） | 澳门半岛 |
| `senado-square` | 议事亭前地 | 澳门半岛 |
| `st-lazarus` | 疯堂斜巷（望德堂区） | 澳门半岛 |
| `tap-seac` | 塔石广场 | 澳门半岛 |
| `guia-fortress` | 东望洋炮台（松山灯塔） | 澳门半岛 |
| `three-lamps` | 三盏灯 | 澳门半岛 |
| `grand-prix-museum` | 澳门大赛车博物馆 | 澳门半岛 |
| `st-augustine` | 岗顶前地（岗顶剧院） | 澳门半岛 |
| `mandarins-house` | 郑家大屋 | 澳门半岛 |
| `a-ma-temple` | 妈阁庙 | 澳门半岛 |
| `penha-hill` | 主教山（西望洋圣堂） | 澳门半岛 |
| `macau-tower` | 澳门旅游塔 | 澳门半岛 |
| `grand-lisboa` | 新葡京 | 澳门半岛 |
| `fishermans-wharf` | 澳门渔人码头 | 澳门半岛 |
| `kun-iam` | 观音莲花苑 | 澳门半岛 |
| `rua-do-cunha` | 官也街 | 氹仔 |
| `taipa-houses` | 龙环葡韵 | 氹仔 |
| `venetian` | 澳门威尼斯人 | 路氹城 |
| `parisian` | 澳门巴黎人 | 路氹城 |
| `londoner` | 澳门伦敦人 | 路氹城 |
| `galaxy` | 澳门银河 | 路氹城 |
| `studio-city` | 新濠影汇 | 路氹城 |
| `wynn-palace` | 永利皇宫 | 路氹城 |
| `coloane-village` | 路环市区（圣方济各圣堂） | 路环 |
| `hac-sa-beach` | 黑沙海滩 | 路环 |
| `panda-pavilion` | 澳门大熊猫馆 | 路环 |
| `a-ma-statue` | 妈祖像（叠石塘山） | 路环 |
<!-- places:end -->

## 添加新的内置景点

在网页上可以直接搜索任何地点，不必修改数据。若想把某个地点加入「热门景点」并预先计算路线：

1. 在 [`data/places.json`](data/places.json) 的 `places` 数组里加一项，字段与其他景点相同（坐标用 WGS-84 经纬度）。
2. 重新计算路线（只补算新景点相关的路段）：

   ```bash
   pip install -r tools/requirements.txt
   python3 tools/build_legs.py
   ```

3. 如果希望插画地图上也画出它，在 `tools/cartoon/icons/` 放一个同名 SVG 图标，然后重新渲染插画地图（见下文）。

## 重新渲染插画地图

插画地图由 [`tools/cartoon/render.py`](tools/cartoon/render.py) 根据 `data/basemap.geojson`、`data/labels.json`、`data/places.json` 和 `tools/cartoon/icons/*.svg` 生成，输出 `assets/map/cartoon.webp`（3000×4211）、`assets/map/cartoon-sm.webp` 和 `data/cartoon.json`（地理范围）：

```bash
pip install -r tools/requirements.txt
cd tools && npm install && cd ..
python3 tools/cartoon/render.py
```

底图数据可用 `python3 tools/build_basemap.py` 从 OpenStreetMap 重新抓取。

## 本地预览

```bash
python3 -m http.server 8000
```

然后打开 <http://localhost:8000>。（需要通过本地服务器打开，直接双击 `index.html` 无法读取数据文件。）

## 部署到 GitHub Pages

1. 把代码推送到 GitHub 仓库的 `main` 分支。
2. 打开仓库 **Settings → Pages**，在 *Build and deployment* 中选择 **Source: Deploy from a branch**，分支选 `main`，目录选 `/ (root)`，保存。
3. 等一两分钟，访问 `https://<用户名>.github.io/<仓库名>/`。

所有路径都是相对路径，放在任何子目录下都能正常工作；地图库 Leaflet 已随仓库提供，不依赖外部 CDN（在中国大陆也能正常打开）。

## 目录结构

```
index.html                 页面
assets/css/app.css         样式（含深色模式）
assets/js/                 main.js 控制器 · mapview.js 双层地图 · router.js 路线 · search.js 地点搜索 · sheet.js 底部面板 · geo.js 工具函数
assets/map/                插画地图图片
data/route.json            默认路线（改这里）
data/places.json           内置景点（名称、坐标、介绍）
data/legs.json             预计算的路段
data/cartoon.json          插画地图的地理范围
data/basemap*.geojson      真实地图数据
data/labels.json           真实地图上的中文地名
tools/                     数据与插画地图的生成脚本、字体（SIL OFL）、图标
vendor/leaflet/            Leaflet 1.9.4
sw.js                      离线缓存
```

## 数据来源与许可

- 地图与道路数据 © [OpenStreetMap](https://www.openstreetmap.org/copyright) 贡献者，按 ODbL 许可使用。
- 路线计算：[OSRM](https://project-osrm.org/)（routing.openstreetmap.de）。
- 地点搜索：[Nominatim](https://nominatim.org/)。
- 地图库：[Leaflet](https://leafletjs.com/)（BSD-2-Clause）。
- 插画字体：站酷快乐体、站酷庆科黄油体（SIL Open Font License 1.1），以及只含「氹」一个字的思源黑体子集（Apache 2.0），见 `tools/fonts/`。
- 路线仅供参考，请以现场交通情况为准。
