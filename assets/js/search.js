// Turning typed text into a location: the built-in attraction list first (instant, offline),
// then OpenStreetMap's Nominatim search limited to Macau.

const NOMINATIM = 'https://nominatim.openstreetmap.org/search';
const VIEWBOX = '113.515,22.225,113.615,22.100'; // left,top,right,bottom

// Common Traditional → Simplified characters seen in Macau names (OSM names are mostly Traditional), plus 凼 → 氹.
const T2S = {'門':'门','媽':'妈','閣':'阁','廟':'庙','議':'议','場':'场','廣':'广','東':'东','燈':'灯','盞':'盏','賽':'赛','車':'车','館':'馆','鄭':'郑','頂':'顶','漁':'渔','碼':'码','頭':'头','觀':'观','蓮':'莲','環':'环','龍':'龙','韻':'韵','銀':'银','匯':'汇','滙':'汇','宮':'宫','灘':'滩','貓':'猫','疊':'叠','倫':'伦','聖':'圣','瘋':'疯','臺':'台','砲':'炮','樂':'乐','橋':'桥','島':'岛','灣':'湾','遊':'游','機':'机','際':'际','國':'国','運':'运','葉':'叶','歷':'历','區':'区','舊':'旧','會':'会','檔':'档','藝':'艺','術':'术','劇':'剧','號':'号','鐘':'钟','樓':'楼','寶':'宝','華':'华','長':'长','學':'学','體':'体','醫':'医','藥':'药','鄉':'乡','鎮':'镇','縣':'县','廳':'厅','園':'园','廠':'厂','點':'点','飲':'饮','麵':'面','飯':'饭','雞':'鸡','魚':'鱼','蝦':'虾','鮮':'鲜','關':'关','閘':'闸','處':'处','辦':'办','務':'务','總':'总','統':'统','電':'电','話':'话','線':'线','鐵':'铁','軌':'轨','輕':'轻','廈':'厦','廂':'厢','墳':'坟','壇':'坛','奧':'奥','婦':'妇','娛':'娱','嬰':'婴','寧':'宁','專':'专','對':'对','屆':'届','層':'层','岡':'冈','嶺':'岭','峽':'峡','帶':'带','幣':'币','廢':'废','張':'张','彎':'弯','徑':'径','從':'从','復':'复','愛':'爱','慶':'庆','懷':'怀','戰':'战','戲':'戏','戶':'户','掃':'扫','換':'换','據':'据','擁':'拥','數':'数','斷':'断','時':'时','晉':'晋','書':'书','條':'条','來':'来','標':'标','樣':'样','樹':'树','橫':'横','歐':'欧','歡':'欢','氣':'气','漢':'汉','灑':'洒','為':'为','無':'无','煙':'烟','熱':'热','燒':'烧','獅':'狮','獎':'奖','現':'现','產':'产','畫':'画','當':'当','發':'发','盧':'卢','禮':'礼','種':'种','穀':'谷','窩':'窝','筆':'笔','簡':'简','紀':'纪','紅':'红','約':'约','紙':'纸','級':'级','細':'细','終':'终','組':'组','結':'结','給':'给','絲':'丝','經':'经','綠':'绿','維':'维','網':'网','緣':'缘','織':'织','繡':'绣','聯':'联','職':'职','膠':'胶','與':'与','莊':'庄','萬':'万','蓋':'盖','藍':'蓝','蘭':'兰','虛':'虚','衛':'卫','裝':'装','製':'制','見':'见','規':'规','視':'视','親':'亲','記':'记','許':'许','設':'设','診':'诊','詩':'诗','該':'该','認':'认','語':'语','說':'说','誠':'诚','課':'课','調':'调','請':'请','論':'论','謝':'谢','證':'证','譽':'誉','讀':'读','變':'变','豐':'丰','貝':'贝','財':'财','貨':'货','貴':'贵','費':'费','賓':'宾','賣':'卖','質':'质','購':'购','趙':'赵','跡':'迹','輪':'轮','輸':'输','轉':'转','農':'农','這':'这','連':'连','進':'进','過':'过','達':'达','遠':'远','適':'适','選':'选','遺':'遗','邊':'边','鄧':'邓','醬':'酱','釣':'钓','銅':'铜','錢':'钱','鋪':'铺','錦':'锦','鏡':'镜','開':'开','間':'间','陽':'阳','陳':'陈','陸':'陆','隊':'队','階':'阶','雙':'双','雜':'杂','雲':'云','靈':'灵','靜':'静','韓':'韩','頁':'页','順':'顺','預':'预','領':'领','題':'题','顏':'颜','風':'风','飛':'飞','餅':'饼','馬':'马','駐':'驻','騎':'骑','髮':'发','鬥':'斗','魯':'鲁','鳥':'鸟','鳳':'凤','鴻':'鸿','鵝':'鹅','麗':'丽','黃':'黄','齊':'齐','齋':'斋','龜':'龟','凼':'氹'};

// Extra names people type for the built-in places.
const ALIASES = {
  'outer-harbour': ['外港码头', '澳门外港', '外港', '外港客运码头', '港澳码头'],
  'taipa-ferry': ['氹仔码头', '氹仔客运码头', '北安码头', '氹仔渡轮码头'],
  'ruins-st-paul': ['大三巴', '圣保禄教堂遗址', '牌坊'],
  'senado-square': ['议事亭', '喷水池', '市政署'],
  'mount-fortress': ['澳门博物馆', '炮台'],
  'st-lazarus': ['望德堂', '疯堂'],
  'guia-fortress': ['松山灯塔', '东望洋灯塔', '松山'],
  'grand-prix-museum': ['赛车博物馆', '葡萄酒博物馆'],
  'st-augustine': ['岗顶剧院', '岗顶'],
  'a-ma-temple': ['妈阁', '妈祖阁'],
  'penha-hill': ['西望洋圣堂', '主教山小堂'],
  'macau-tower': ['旅游塔', '观光塔', '澳门塔'],
  'grand-lisboa': ['葡京', '新葡京酒店'],
  'fishermans-wharf': ['渔人码头'],
  'kun-iam': ['观音像', '观音'],
  'rua-do-cunha': ['官也街', '氹仔旧城区'],
  'taipa-houses': ['龙环葡韵住宅式博物馆'],
  venetian: ['威尼斯人'],
  parisian: ['巴黎人', '巴黎铁塔'],
  londoner: ['伦敦人', '大笨钟'],
  galaxy: ['银河', '银河度假城'],
  'studio-city': ['新濠影汇', '8字摩天轮'],
  'wynn-palace': ['永利', '永利缆车'],
  'coloane-village': ['路环', '安德鲁', '路环圣方济各圣堂'],
  'hac-sa-beach': ['黑沙'],
  'panda-pavilion': ['大熊猫', '熊猫馆', '石排湾郊野公园'],
  'a-ma-statue': ['妈祖像', '妈祖文化村', '叠石塘山'],
};

/** Traditional → Simplified for display (covers the characters common in Macau place names). */
export function toSimplified(s) {
  return String(s || '').replace(/[\u4e00-\u9fff]/g, (c) => (c === '凼' ? c : T2S[c] || c));
}

export function normalize(s) {
  return String(s || '')
    .replace(/[一-鿿]/g, (c) => T2S[c] || c)
    .toLowerCase()
    .replace(/[\s·・•()（）【】\[\]\-_,，。.、'"“”]/g, '');
}

export function createSearch(places) {
  const index = places.map((p) => ({
    p,
    keys: [p.name, p.short, p.en, p.id, ...(ALIASES[p.id] || [])].filter(Boolean).map(normalize),
  }));

  /** Ranked attraction matches for a query (empty query → []). */
  function match(query, limit = 8) {
    const q = normalize(query);
    if (!q) return [];
    const scored = [];
    for (const { p, keys } of index) {
      let best = 0;
      for (const k of keys) {
        if (k === q) best = Math.max(best, 100);
        else if (k.startsWith(q)) best = Math.max(best, 80 - (k.length - q.length) * 0.1);
        else if (k.includes(q)) best = Math.max(best, 60);
        else if (q.length >= 2 && q.includes(k) && k.length >= 2) best = Math.max(best, 50);
        else if (isSubsequence(q, k)) best = Math.max(best, 25);
      }
      if (best > 0) scored.push({ p, score: best });
    }
    return scored.sort((a, b) => b.score - a.score).slice(0, limit).map((x) => x.p);
  }

  /** Best single attraction for free text, only when it is a confident match. */
  function resolveLocal(query) {
    const q = normalize(query);
    if (!q) return null;
    const hits = match(query, 2);
    if (!hits.length) return null;
    const keys = index.find((e) => e.p === hits[0]).keys;
    // exact, a prefix of a known name, or a known name with at most two extra characters (e.g. 澳门大三巴)
    return keys.some((k) => k === q || (k.startsWith(q) && q.length >= 2) || (q.includes(k) && k.length >= 2 && q.length - k.length <= 2)) ? hits[0] : null;
  }

  return { match, resolveLocal };
}

function isSubsequence(q, k) {
  let i = 0;
  for (const c of k) if (c === q[i]) i++;
  return i === q.length && q.length >= 2;
}

/** "22.19, 113.54" style input → {lat, lng} or null. */
export function parseCoords(text) {
  const m = /^\s*(-?\d{1,3}\.\d+)\s*[,，\s]\s*(-?\d{1,3}\.\d+)\s*$/.exec(text || '');
  if (!m) return null;
  let lat = +m[1], lng = +m[2];
  if (Math.abs(lat) > 90) [lat, lng] = [lng, lat];
  return { lat, lng };
}

const geoCache = new Map();

/** Search OpenStreetMap (Nominatim) inside Macau. Resolves to [{name, sub, lat, lng}]. */
export async function geocode(query, { signal } = {}) {
  const q = query.trim();
  if (!q) return [];
  if (geoCache.has(q)) return geoCache.get(q);
  const url = `${NOMINATIM}?format=jsonv2&limit=8&bounded=1&viewbox=${VIEWBOX}&accept-language=zh-Hans,zh-CN,zh&q=${encodeURIComponent(q)}`;
  const res = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`Nominatim ${res.status}`);
  const rows = await res.json();
  const out = [];
  for (const r of rows) {
    const parts = String(r.display_name || '').split(/[,，]\s*/);
    let name = toSimplified((r.name || parts[0] || q).trim());
    // Keep the visitor's own wording when it names the same place.
    if (normalize(name) === normalize(q)) name = q;
    const item = { name, sub: toSimplified(parts.slice(1, 4).join('，')), lat: +r.lat, lng: +r.lon };
    // Drop duplicates (same name within ~150 m, e.g. a building and its entrance node).
    if (out.some((o) => o.name === item.name && Math.abs(o.lat - item.lat) < 0.0014 && Math.abs(o.lng - item.lng) < 0.0014)) continue;
    out.push(item);
    if (out.length >= 6) break;
  }
  geoCache.set(q, out);
  return out;
}
