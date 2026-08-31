# -*- coding: utf-8 -*-
"""
AI 论文学术档案库 —— 累积式构建脚本
=================================
设计目标（区别于旧版"7天滑动窗口"）：
  - 本地 archive_store.json 按论文 id 持久化【全部】论文 + 五问阅读法要点；
  - 每次运行只抓取"距上次运行以来的新论文"（窗口 = max(3, 距上次天数+1)，上限7天，
    既能每日增量，也能在偶尔漏跑后自动追平）；
  - 新论文去重追加；HTML 从全量数据生成，时间轴覆盖所有历史日期，日积月累越来越长；
  - 首跑（store 为空）用 7d 窗口播种历史；旧 points_cache.json 的要点会被复用，
    避免重生成已完成的论文。

依赖：本机 Ollama（qwen3.5:4b）生成要点；AI HOT 公开检索池。
"""
import json, urllib.request, urllib.parse, re, os, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

SH = timezone(timedelta(hours=8))
base = 'https://aihot.virxact.com/api/v1/items'
ua = 'aihot-skill/1.2.1 (+https://aihot.virxact.com/aihot-skill/)'

DOMAINS = ['AI认知算法', 'LLM', 'AI Agent', '世界模型', '虚拟仿真社会', '具身智能']
QUERY = {'AI认知算法': '认知', 'LLM': '语言模型', 'AI Agent': '智能体',
         '世界模型': '世界模型', '虚拟仿真社会': '仿真', '具身智能': '具身'}
# arXiv API 英文检索词（按领域补严谨论文源）
ARXIV_API = 'https://export.arxiv.org/api/query'
ARXIV_QUERY = {
    'AI认知算法': 'all:"cognitive architecture" OR all:"cognitive AI" OR all:"cognitive reasoning"',
    'LLM': 'all:"large language model" OR all:"LLM"',
    'AI Agent': 'all:"AI agent" OR all:"LLM agent" OR all:"language agent"',
    '世界模型': 'all:"world model"',
    '虚拟仿真社会': 'all:"social simulation" OR all:"agent-based simulation" OR all:"simulated society"',
    '具身智能': 'all:"embodied AI" OR all:"embodied agent" OR all:"embodied intelligence" OR all:"embodied manipulation"',
}

WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
OLLAMA = 'http://127.0.0.1:11434/api/chat'
POINTS_MODEL = 'qwen3.5:4b'
STORE = 'archive_store.json'
OLD_CACHE = 'points_cache.json'   # 仅首跑播种时复用，迁移完成后不再需要

# ---------- Ollama 可用性探活与降级开关 ----------
OLLAMA_OK = True
def probe_ollama():
    """预热/探活：仅用于触发模型加载并打印状态，不因单次失败关闭生成
    （真实调用若失败会自行点亮降级开关；避免冷加载首请求超时被误判为不可用）。"""
    global OLLAMA_OK
    try:
        req = urllib.request.Request(OLLAMA, data=json.dumps({
            'model': POINTS_MODEL, 'think': False, 'stream': False,
            'messages': [{'role': 'user', 'content': 'ping'}]
        }).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        OLLAMA_OK = True
        print('[ollama] 探活成功，正常生成要点/解读', flush=True)
    except Exception as e:
        # 仅预热失败：不关闭生成，首次真实调用失败后再降级（避免冷加载误判）
        print(f'[ollama] 探活超时/失败（仍尝试生成，首次真实调用失败后再降级）：{e}', flush=True)

SKIP_HOSTS = ('x.com', 'twitter.com', 'www.x.com', 'mp.weixin.qq.com', 't.co')

# ---------- 来源类型判定（严谨性分级） ----------
# paper：可溯源到论文实体的来源（arXiv/OpenReview/官方研究页等）
# social：社媒帖（X 等），是转发/解读/观点，非论文本体
# news：行业资讯站 / 媒体 / 官方博客，非论文
SOCIAL_HOSTS = {'x.com', 'twitter.com', 't.co'}
NEWS_HOSTS = {'ithome.com', 'the-decoder.com', 'techcrunch.com', 'marktechpost.com',
              'news.ycombinator.com', 'hackernews.com', 'mp.weixin.qq.com',
              'huggingface.co', 'medium.com'}

def classify(link):
    h = urllib.parse.urlparse(link or '').netloc.lower().replace('www.', '')
    if h in SOCIAL_HOSTS:
        return 'social'
    if h in NEWS_HOSTS:
        return 'news'
    return 'paper'

# ---------- 拉取（按领域检索 + 打标签） ----------
def fetch_json(q, window):
    """拉取某领域论文；接口偶发抖动时重试，最终失败则返回空列表（不让单查询拖垮整次更新）。"""
    params = {'mode': 'all', 'window': window, 'category': 'paper', 'q': q, 'limit': '80'}
    out = []
    for attempt in range(3):
        try:
            while True:
                url = base + '?' + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={'User-Agent': ua})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                out.extend(data.get('items', []))
                nxt = data.get('page', {}).get('nextCursor')
                if not nxt or not data.get('page', {}).get('hasMore'):
                    break
                params['cursor'] = nxt
            return out
        except Exception as e:
            if attempt < 2:
                print(f'  fetch retry ({q}, {window}): {e}', flush=True)
                time.sleep(3)
            else:
                print(f'  fetch failed ({q}, {window}): {e} — 跳过该查询', flush=True)
                return out
    return out

def tag_papers(window):
    tags = defaultdict(list)
    items = {}
    for dom in DOMAINS:
        for it in fetch_json(QUERY[dom], window):
            items[it['id']] = it
            if dom not in tags[it['id']]:
                tags[it['id']].append(dom)
    return items, tags

def to_sh(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(SH)

# ---------- 五问阅读法要点生成（本机 Ollama） ----------
LABELS = ['类别', '背景', '贡献', '合理性', '清晰度']
SYS_PROMPT = (
    '你是资深论文阅读助手，熟悉 Srinivasan Keshav《How to Read a Paper》的"五问(Cs)"阅读法。'
    '根据论文标题与摘要，用简体中文输出结构化要点，严格包含以下五行，每行以对应前缀开头：\n'
    '类别：论文类型（方法/系统/综述/实证研究等），一句话\n'
    '背景：研究问题来自哪里、与哪些方向相关，一句话\n'
    '贡献：论文的主要创新点/贡献，1-2句\n'
    '合理性：从摘要看假设与方法是否可信、有无明显局限，一句话\n'
    '清晰度：从摘要看写作与结构是否清晰易读，一句话\n'
    '硬性要求：每项 20-80 字、1-2 句，简洁专业，禁止空泛长段，也禁止过短的敷衍内容。\n'
    '只输出这五行，不要序号、不要代码块、不要多余解释。'
)

def parse_points(text):
    fields = {l: '' for l in LABELS}
    cur = None
    for line in text.split('\n'):
        s = line.strip()
        s = re.sub(r'^[\*\-\d\.\、\s]+', '', s).strip('*').strip()
        matched = None
        for lab in LABELS:
            if s.startswith(lab):
                rest = s[len(lab):].lstrip('*').strip().lstrip(':：').strip()
                if rest and not fields[lab]:
                    fields[lab] = rest
                cur = lab
                matched = lab
                break
        if not matched and cur and s:
            fields[cur] = (fields[cur] + ' ' + s).strip() if fields[cur] else s
    return {k: v for k, v in fields.items() if v}

def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; aihot-archive/1.0)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')

def extract_text(html):
    html = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    html = re.sub(r'<style[\s\S]*?</style>', ' ', html, flags=re.I)
    m = re.search(r'<blockquote class="abstract[^"]*">(.*?)</blockquote>', html, re.S | re.I)
    if m:
        txt = re.sub(r'<[^>]+>', ' ', m.group(1))
    else:
        paras = re.findall(r'<p[\s\S]*?</p>', html, re.I)
        txt = ' '.join(re.sub(r'<[^>]+>', ' ', p) for p in paras) if paras else re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', txt).strip()

def ollama_points(title, text):
    global OLLAMA_OK
    if not OLLAMA_OK:
        return {}
    if not text or len(text) < 30:
        return {}
    text = text[:3000]
    body = json.dumps({
        'model': POINTS_MODEL, 'think': False, 'stream': False,
        'messages': [
            {'role': 'system', 'content': SYS_PROMPT},
            {'role': 'user', 'content': f'标题：{title}\n内容：{text}'}
        ]
    }).encode('utf-8')
    req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
    except Exception as e:
        OLLAMA_OK = False
        print(f'  ollama 调用失败，降级跳过：{e}', flush=True)
        return {}
    out = d.get('message', {}).get('content', '').strip()
    return parse_points(out) if out else {}

def generate_points(title, url, fallback_summary, tries=2):
    """尝试抓取原文（arXiv 等）生成要点；失败则用 AI HOT 中文摘要兜底。
    tries>1 时遇网络异常自动重试，避免偶发 SSL 超时导致永久留空。"""
    last_err = None
    for attempt in range(tries):
        try:
            host = urllib.parse.urlparse(url).netloc.lower().replace('www.', '')
            text = None
            if 'arxiv.org' in host:
                txt = extract_text(http_get(url))
                if len(txt) >= 60:
                    text = txt
            if not text:
                s = (fallback_summary or '').strip()
                text = s if len(s) >= 20 else None
            if text:
                return ollama_points(title, text)
            return {}
        except Exception as e:
            last_err = e
            if attempt < tries - 1:
                print(f'  retry {title[:16]} ({e})', flush=True)
                time.sleep(2)
    print(f'  points fail {title[:20]}: {last_err}', flush=True)
    return {}

# ---------- 中文标题/摘要（页面展示用，保留原文供要点生成） ----------
def has_cn(s):
    return bool(re.search(r'[\u4e00-\u9fff]', s or ''))

def translate_cn(title, summary, tries=2):
    """把英文标题+摘要翻成简体中文；已是中文则原样返回；失败返回空（下次自愈）。"""
    if has_cn(title) and has_cn(summary):
        return title, summary
    t_in = title if not has_cn(title) else '(已有中文标题)'
    s_in = summary if not has_cn(summary) else '(已有中文摘要)'
    text = f'标题：{t_in}\n摘要：{s_in}'[:3500]
    body = json.dumps({
        'model': POINTS_MODEL, 'think': False, 'stream': False,
        'messages': [
            {'role': 'system', 'content': '你是专业学术翻译。把英文论文标题与摘要翻译成简体中文，保持学术准确、通顺自然，禁止添加任何解释或补充。只输出两行：\n标题：<中文标题>\n摘要：<中文摘要>'},
            {'role': 'user', 'content': text}
        ]
    }).encode('utf-8')
    for attempt in range(tries):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            out = (d.get('message', {}).get('content', '') or '').strip()
            # 解析：标题行 + 摘要行及其后所有续行（长摘要翻译成多行时全部并入，避免截断）
            tc, sc, in_sum = '', '', False
            for line in out.split('\n'):
                s = line.strip()
                if not s:
                    continue
                if s.startswith('标题'):
                    tc = s.split('：', 1)[-1].split(':', 1)[-1].strip()
                    in_sum = False
                elif s.startswith('摘要'):
                    sc = s.split('：', 1)[-1].split(':', 1)[-1].strip()
                    in_sum = True
                elif in_sum:
                    sc = (sc + ' ' + s).strip()
            tc_out = tc if not has_cn(title) else title  # 标题已中文则直接用原文
            sc_out = sc or (summary if has_cn(summary) else '')
            return tc_out, sc_out
        except Exception as e:
            if attempt < tries - 1:
                print(f'  translate retry {title[:16]} ({e})', flush=True)
                time.sleep(2)
    print(f'  translate fail {title[:20]}', flush=True)
    return '', ''

# ---------- 完整摘要修复（abstract：arXiv 完整英文摘要 / AI HOT items / summary 兜底） ----------
def arxiv_ok(a):
    """arXiv 条目的 abstract 是否有效：非空、够长（>=300）、纯英文。"""
    return bool(a) and len(a) >= 300 and not re.search(r'[\u4e00-\u9fff]', a)

def fix_abstracts(papers, items):
    """为每条 arxiv 链接的论文拉取完整英文摘要（小批量 id_list + 重试 + 单篇兜底）；
    非 arxiv 条目用 AI HOT items 完整 summary，兜底 summary。"""
    import xml.etree.ElementTree as ET
    ns = {'a': 'http://www.w3.org/2005/Atom'}

    def fetch_summaries(id_chunk):
        url = 'https://export.arxiv.org/api/query?id_list=' + ','.join(id_chunk)
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': ua}), timeout=45) as r:
            data = r.read().decode('utf-8', 'ignore')
        root = ET.fromstring(data)
        out = {}
        for entry in root.findall('a:entry', ns):
            aid = (entry.findtext('a:id', default='', namespaces=ns) or '')
            m2 = re.search(r'abs/([\d\.]+)', aid)
            if m2:
                out[m2.group(1)] = re.sub(r'\s+', ' ', entry.findtext('a:summary', default='', namespaces=ns) or '').strip()
        return out

    # 1) arxiv 链接且摘要无效的条目：小批量(15个/批)拉取，失败重试
    need = {}
    for iid, rec in papers.items():
        if arxiv_ok(rec.get('abstract', '')):
            continue
        m = re.search(r'arxiv\.org/abs/([\d\.]+)', rec.get('link', '') or '')
        if m:
            need[m.group(1)] = iid
    ids = list(need)
    for k in range(0, len(ids), 15):
        chunk = ids[k:k + 15]
        for attempt in range(3):
            try:
                got = fetch_summaries(chunk)
                for aid, s in got.items():
                    if aid in need and len(s) >= 300:
                        papers[need[aid]]['abstract'] = s
                break
            except Exception as e:
                print(f'  arxiv abstract batch fail ({attempt + 1}): {e}', flush=True)
                time.sleep(3)
    # 2) 仍无效的 arxiv 条目：单篇再拉一次（网络抖动小步重试）
    for iid, rec in papers.items():
        if arxiv_ok(rec.get('abstract', '')):
            continue
        m = re.search(r'arxiv\.org/abs/([\d\.]+)', rec.get('link', '') or '')
        if not m:
            continue
        try:
            got = fetch_summaries([m.group(1)])
            s = got.get(m.group(1), '')
            if len(s) >= 300:
                rec['abstract'] = s
        except Exception:
            pass
    # 3) 非 arxiv 条目：AI HOT items 完整 summary / 兜底 summary
    for iid, rec in papers.items():
        if rec.get('abstract'):
            continue
        it = items.get(iid)
        if it and it.get('summary'):
            rec['abstract'] = re.sub(r'\s+', ' ', it['summary']).strip()
    for iid, rec in papers.items():
        rec.setdefault('abstract', rec.get('summary', ''))
    ok = sum(1 for r in papers.values() if arxiv_ok(r.get('abstract', '')))
    total = sum(1 for r in papers.values() if 'arxiv.org' in (r.get('link') or ''))
    print(f'[abstract] arxiv 完整英文摘要 {ok}/{total}；其余条目用 AI HOT 摘要', flush=True)

# ---------- 今日精选论文（每日 5 篇，arXiv 权威源，领域均衡） ----------
def select_featured(papers, today, limit=5):
    """每天从 arXiv 论文池（非 HF 源）按领域均衡选 5 篇，打 featured_date；已选过的优先不重复。"""
    cur = [r for r in papers.values() if r.get('featured_date') == today]
    if len(cur) >= limit:
        return cur[:limit]
    cands = [r for r in papers.values()
             if r.get('kind') == 'paper'
             and 'HuggingFace' not in (r.get('source') or '')
             and 'arxiv.org' in (r.get('link') or '')]
    cands.sort(key=lambda r: r.get('ts', ''), reverse=True)
    # 优先未精选过的
    fresh = [r for r in cands if not r.get('featured_date')]
    pool = fresh if len(fresh) >= limit else cands
    seen = set()
    pick = []
    for r in pool:
        d = r.get('domain', '')
        if d in seen:
            continue
        seen.add(d)
        pick.append(r)
        if len(pick) >= limit:
            break
    # 领域去重后不足，允许补足（同领域）
    for r in pool:
        if len(pick) >= limit:
            break
        if r in pick:
            continue
        pick.append(r)
    for r in pick:
        r['featured_date'] = today
    if pick:
        print(f'[featured] 今日精选 {len(pick)} 篇: ' + ', '.join(r.get('domain', '') for r in pick), flush=True)
    return pick

# ---------- 科研社区热门解读（专业客观文风，150-300 字） ----------
def gen_community_interpret(title, abstract, tries=2):
    text = f'标题：{title}\n内容：{(abstract or title or "")[:900]}'
    body = json.dumps({
        'model': POINTS_MODEL, 'think': False, 'stream': False,
        'messages': [
            {'role': 'system', 'content': '你是科研内容编辑。为科研论文/资讯写 150-300 字中文解读：客观概括核心内容、说明研究背景与意义、点出关键发现与影响。保持专业准确，同时用通俗易懂的表述照顾非专业读者——必要术语可以保留但要顺带解释，不堆砌晦涩词汇，不刻意大白话。直接输出解读正文，不要标题、不要任何前缀。'},
            {'role': 'user', 'content': text}
        ]
    }).encode('utf-8')
    for attempt in range(tries):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r).get('message', {}).get('content', '').strip()
        except Exception as e:
            if attempt < tries - 1:
                print(f'  interpret retry {title[:16]} ({e})', flush=True)
                time.sleep(2)
    print(f'  interpret fail {title[:20]}', flush=True)
    return ''

# ---------- 精选论文解读（专业客观文风，150-300 字） ----------
def gen_featured_interpret(title, abstract, tries=2):
    text = f'标题：{title}\n内容：{(abstract or title or "")[:1500]}'
    body = json.dumps({
        'model': POINTS_MODEL, 'think': False, 'stream': False,
        'messages': [
            {'role': 'system', 'content': '你是科研内容编辑。把这篇论文写成 150-300 字中文解读：客观概括研究问题、方法与贡献，说明其意义。保持专业准确，同时用通俗易懂的表述照顾非专业读者——必要术语可以保留但要顺带解释，不堆砌晦涩词汇，不刻意大白话。直接输出解读正文，不要标题、不要任何前缀。'},
            {'role': 'user', 'content': text}
        ]
    }).encode('utf-8')
    for attempt in range(tries):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r).get('message', {}).get('content', '').strip()
        except Exception as e:
            if attempt < tries - 1:
                print(f'  feat interpret retry {title[:16]} ({e})', flush=True)
                time.sleep(2)
    print(f'  feat interpret fail {title[:20]}', flush=True)
    return ''

# ---------- 每日热门 GitHub 项目（按 6 领域） ----------
GITHUB_QUERIES = {
    'AI认知算法': 'cognitive architecture OR cognitive computing',
    'LLM': 'large language model',
    'AI Agent': 'AI agent OR LLM agent',
    '世界模型': 'world model',
    '虚拟仿真社会': 'agent-based simulation OR social simulation',
    '具身智能': 'embodied AI OR embodied intelligence',
}

def ollama_complete(prompt, timeout=120):
    """通用 Ollama 补全（GitHub 中文一句话介绍等）；失败返回空，不关闭全局生成开关。"""
    body = json.dumps({
        'model': POINTS_MODEL, 'think': False, 'stream': False,
        'messages': [{'role': 'user', 'content': prompt}]
    }).encode('utf-8')
    req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get('message', {}).get('content', '').strip()
    except Exception as e:
        print(f'  ollama 调用失败（github intro）：{e}', flush=True)
        return ''

def fetch_github_projects(top_n=5, exclude=None):
    """当日 Top-N AI 领域仓库：每领域取 stars 最高若干，合并去重后按 stars 取前 N。
    生成中文一句话介绍(cn_intro)：类型 + 用途；无翻译则留空（不阻塞）。
    exclude: 已收录的 full_name 集合，传入后优先挑选「尚未收录」的仓库，
             使每日追加的都是新仓库，多日累积后板块内容持续充实。"""
    exclude = exclude or set()
    merged = {}
    for dom, q in GITHUB_QUERIES.items():
        url = ('https://api.github.com/search/repositories?q='
               + urllib.parse.quote(q) + '&sort=stars&order=desc&per_page=4')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua, 'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            for it in d.get('items', [])[:4]:
                fn = it.get('full_name', '')
                if not fn or fn in merged or fn in exclude:
                    continue
                merged[fn] = {
                    'domain': dom, 'name': it.get('name', ''), 'full_name': fn,
                    'url': it.get('html_url', ''), 'stars': it.get('stargazers_count', 0),
                    'lang': it.get('language'), 'desc': (it.get('description') or '')[:160],
                    'cn_intro': '',
                }
            time.sleep(6)  # 未认证 GitHub Search API 限流 10 次/分钟，逐域减速
        except Exception as e:
            print(f'  github fetch fail ({dom}): {e}', flush=True)
            time.sleep(3)
    ranked = sorted(merged.values(), key=lambda x: x.get('stars', 0), reverse=True)[:top_n]
    for r in ranked:
        if not r['cn_intro']:
            prompt = (f'请用一句话（不超过40字）中文介绍这个GitHub开源仓库：'
                      f'先说明它是什么类型（如 框架/工具/课程/数据集/应用），再说明主要用途。'
                      f'只输出这句话本身，不要引号、序号、解释。\n'
                      f'仓库名：{r["name"]}\n简介：{r["desc"]}\n地址：{r["url"]}')
            out = ollama_complete(prompt)
            r['cn_intro'] = out.strip().strip('"').strip("'").strip() if out else ''
    return ranked

# ---------- 持久化存储 ----------
def load_store():
    if os.path.exists(STORE):
        try:
            return json.load(open(STORE, encoding='utf-8'))
        except Exception:
            pass
    return {'meta': {}, 'papers': {}}

def save_store(s):
    json.dump(s, open(STORE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

def short_source(name):
    n = name
    if '（' in n:
        n = n.split('（')[0].strip()
    if n.startswith('X：'):
        rest = n[2:]
        if '(@' in rest:
            rest = rest.split('(@')[0].strip()
        n = 'X · ' + rest
    return n

def badge_hue(name):
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xffffffff
    return 200 + (h % 30)

def primary(dom_list, freq):
    return min(dom_list, key=lambda d: (freq[d], DOMAINS.index(d)))

def dedupe_papers(papers):
    """按规范键(arxiv id / 归一化标题)去重，保留信息更完整者。返回移除条数。
    解决「精选文章」重复的根因：同一论文以不同来源(id)入库时只保留一条。"""
    def canon(rec):
        link = rec.get('link', '') or ''
        m = re.search(r'arxiv\.org/abs/([\d\.]+)', link)
        if m:
            return 'arxiv:' + m.group(1)
        t = (rec.get('title_cn') or rec.get('title', '') or '').lower()
        t = re.sub(r'[^\w\u4e00-\u9fff]+', '', t)
        return 't:' + t if t else None
    def rich(rec):
        return sum([bool(rec.get('abstract_cn')), bool(rec.get('points')),
                    bool(rec.get('interpret')), bool(rec.get('title_cn')),
                    len(rec.get('summary', '') or '')])
    kept = {}
    removed = 0
    for pid, rec in list(papers.items()):
        k = canon(rec)
        if not k:
            kept[pid] = rec
            continue
        if k in kept:
            removed += 1
            old = kept[k]
            if rich(rec) > rich(old):
                rec.setdefault('featured_date', old.get('featured_date'))
                kept[k] = rec
            else:
                old.setdefault('featured_date', rec.get('featured_date'))
        else:
            kept[k] = rec
    papers.clear()
    for pid, rec in kept.items():
        papers[pid] = rec
    return removed

# ---------- arXiv 补充源（严谨论文源，按领域检索） ----------
def fetch_arxiv(domain, n=10):
    """arXiv API 按领域英文关键词检索近期论文，返回结构化列表；失败返回空。"""
    import xml.etree.ElementTree as ET
    q = ARXIV_QUERY.get(domain, '')
    if not q:
        return []
    params = {'search_query': q, 'start': 0, 'max_results': n,
              'sortBy': 'submittedDate', 'sortOrder': 'descending'}
    url = ARXIV_API + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': ua})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read().decode('utf-8', 'ignore')
    except Exception as e:
        print(f'  arxiv fetch failed ({domain}): {e}', flush=True)
        return []
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    out = []
    try:
        root = ET.fromstring(data)
        for entry in root.findall('a:entry', ns):
            aid = (entry.findtext('a:id', default='', namespaces=ns) or '').strip()
            m = re.search(r'abs/([\d\.]+)', aid)
            arxiv_id = m.group(1) if m else ''
            if not arxiv_id:
                continue
            title = re.sub(r'\s+', ' ', entry.findtext('a:title', default='', namespaces=ns) or '').strip()
            summary = re.sub(r'\s+', ' ', entry.findtext('a:summary', default='', namespaces=ns) or '').strip()
            published = (entry.findtext('a:published', default='', namespaces=ns) or '').strip()
            out.append({'arxiv_id': arxiv_id, 'title': title, 'summary': summary,
                        'published': published, 'url': f'https://arxiv.org/abs/{arxiv_id}'})
    except Exception as e:
        print(f'  arxiv parse failed ({domain}): {e}', flush=True)
    return out

# ---------- 主流程 ----------
def main():
    probe_ollama()
    store = load_store()
    papers = store.setdefault('papers', {})
    meta = store.setdefault('meta', {})
    last_run = meta.get('last_run')
    now = datetime.now(SH)

    # 领域变更清理：领域列表已更换（AI心理学 -> 具身智能），移除旧领域条目
    old_domains = [k for k, v in papers.items() if v.get('domain') not in DOMAINS]
    for k in old_domains:
        del papers[k]
    if old_domains:
        print(f'[cleanup] 移除旧领域条目 {len(old_domains)} 条（领域列表更换）', flush=True)

    # 增量与播种统一使用 7d 窗口（接口仅支持预设窗口值）；靠 id 去重追加，重叠无害，
    # 且天然支持在偶尔漏跑后自动追平（7 天内出现的论文都会被重新抓到并去重入库）。
    if not papers:
        print('[seed] store 为空，使用 7d 窗口播种历史...', flush=True)
    else:
        print('[update] 使用 7d 窗口增量追平（按 id 去重）...', flush=True)
    window = '7d'

    items, tags = tag_papers(window)

    # arXiv 补充：按领域检索，按 arxiv id 与存量去重后并入（保证论文源严谨）
    arxiv_skip = set()
    for rec in papers.values():
        mm = re.search(r'arxiv\.org/abs/([\d\.]+)', rec.get('link', '') or '')
        if mm:
            arxiv_skip.add(mm.group(1))
    arxiv_added = 0
    for dom in DOMAINS:
        for it in fetch_arxiv(dom):
            aid = it['arxiv_id']
            if aid in arxiv_skip:
                continue
            arxiv_skip.add(aid)
            key = 'arxiv:' + aid
            items[key] = {
                'id': key, 'title': it['title'], 'summary': it['summary'],
                'publishedAt': it['published'], 'discoveredAt': it['published'],
                'source': {'name': 'arXiv'},
                'links': {'original': it['url']},
            }
            tags[key] = [dom]
            arxiv_added += 1
    if arxiv_added:
        print(f'[arxiv] 补充 {arxiv_added} 篇论文（按领域检索去重）', flush=True)

    freq = defaultdict(int)
    for dl in tags.values():
        for d in dl:
            freq[d] += 1

    # 复用旧要点缓存（仅首跑有意义）
    old = {}
    if os.path.exists(OLD_CACHE):
        try:
            old = json.load(open(OLD_CACHE, encoding='utf-8'))
        except Exception:
            old = {}

    # 仅对"store 中不存在"的 id 入库（要点与中文摘要由后续统一流程生成）
    need = [iid for iid in tags if iid not in papers]

    added = 0
    done = 0
    for iid, dom_list in tags.items():
        if iid in papers:
            continue
        it = items[iid]
        ts = it.get('publishedAt') or it.get('discoveredAt')
        dt = to_sh(ts)
        dom = primary(dom_list, freq)
        url = it.get('links', {}).get('original') or it.get('links', {}).get('aihot') or ''
        title = it.get('title') or it.get('originalTitle') or '(无标题)'
        summary = re.sub(r'\s+', ' ', (it.get('summary') or '')).strip()  # 存完整摘要，不再 200 字截断
        papers[iid] = {
            'title': title, 'source': it['source']['name'],
            'sourceShort': short_source(it['source']['name']), 'hue': badge_hue(it['source']['name']),
            'domain': dom, 'ts': dt.isoformat(), 'date': dt.strftime('%Y-%m-%d'),
            'weekday': WEEK[dt.weekday()], 'summary': summary, 'link': url, 'points': {},
            'kind': classify(url),
        }
        added += 1
        done += 1
        if done % 10 == 0:
            print(f'  progress {done}/{len(need)}', flush=True)

    # 存量补齐 kind（历史入库的条目没有该字段）
    for rec in papers.values():
        rec.setdefault('kind', classify(rec.get('link', '')))

    # 完整摘要修复：arxiv 批量拉取 / AI HOT items 匹配（页面中文摘要基于完整原文）
    fix_abstracts(papers, items)

    # 中文化：abstract_cn（完整摘要中文）；abstract 为英文且翻译缺失/明显偏短（旧解析 bug 产物）则重翻
    # 阈值 0.25（中文摘要通常为原文 0.3-0.45 长度）；已达标不重翻，避免每次构建全量重翻
    need_cn = [iid for iid, rec in papers.items()
               if rec.get('abstract')
               and not has_cn(rec.get('abstract', ''))
               and not (rec.get('abstract_cn')
                        and len(rec.get('abstract_cn', '')) >= max(100, len(rec.get('abstract', '')) * 0.25))]
    if need_cn:
        print(f'[cn] 翻译完整摘要中文 for {len(need_cn)} 篇（Ollama）...', flush=True)
        ollama_points('预热', 'Warm-up sentence to load the model into GPU memory.')
        cdone = 0
        for iid in need_cn:
            rec = papers[iid]
            tc, ac = translate_cn(rec.get('title', ''), rec.get('abstract', '') or rec.get('summary', ''))
            if tc and not rec.get('title_cn'):
                rec['title_cn'] = tc
            if ac:
                rec['abstract_cn'] = ac
            cdone += 1
            if cdone % 10 == 0:
                print(f'  cn {cdone}/{len(need_cn)}', flush=True)
        print(f'[cn] 完成 {cdone} 篇摘要中文化', flush=True)
    # 摘要本身已是中文的条目直接作为 abstract_cn
    for iid, rec in papers.items():
        if not rec.get('abstract_cn') and has_cn(rec.get('abstract', '')):
            rec['abstract_cn'] = rec['abstract']

    # 要点统一重生成：完整摘要 + 严格格式 prompt（每项 20-80 字，风格一致）
    papers_p = [r for r in papers.values() if r.get('kind') == 'paper']
    if papers_p:
        print(f'[points] 统一重生成 {len(papers_p)} 篇论文要点...', flush=True)
        ollama_points('预热', 'Warm-up sentence to load the model into GPU memory.')
        pd = 0
        for iid, rec in papers.items():
            if rec.get('kind') != 'paper':
                continue
            rec['points'] = ollama_points(rec.get('title', ''), rec.get('abstract') or rec.get('summary') or '') or {}
            pd += 1
            if pd % 10 == 0:
                print(f'  points {pd}/{len(papers_p)}', flush=True)
        print(f'[points] 完成 {pd} 篇', flush=True)

    # 科研社区热门通俗解读：为 HF Daily Papers / X / 资讯 条目生成 interpret（缺失才生成）
    community_items = [r for r in papers.values()
                       if r.get('kind') != 'paper'
                       or 'HuggingFace Daily Papers' in (r.get('source') or '')]
    # 论文/社区解读：为全部论文（含普通论文）与社区资讯生成 interpret（缺失或版本不符才生成）
    need_int = [r for r in papers.values() if not r.get('interpret') or r.get('interpret_v') != 3]
    if need_int:
        print(f'[community] 为 {len(need_int)} 条社区热门生成解读（通俗易读版 v3）...', flush=True)
        ollama_points('预热', 'Warm-up sentence to load the model into GPU memory.')
        idn = 0
        for r in need_int:
            r['interpret'] = gen_community_interpret(r.get('title', ''), r.get('abstract') or r.get('summary') or '')
            r['interpret_v'] = 3
            idn += 1
            if idn % 10 == 0:
                print(f'  interpret {idn}/{len(need_int)}', flush=True)
        print(f'[community] 完成 {idn} 条解读', flush=True)

    # 精选文章去重（根因修复：同一论文多来源入库只保留一条）
    removed = dedupe_papers(papers)
    if removed:
        print(f'[dedupe] 移除重复精选文章 {removed} 条', flush=True)

    meta['last_run'] = now.isoformat()
    if 'first_run' not in meta:
        meta['first_run'] = now.isoformat()

    # 今日精选论文：每日 5 篇（arXiv 领域均衡），featured_date 标记入库
    today = now.strftime('%Y-%m-%d')
    select_featured(papers, today)
    # 今日精选生成"入门向通俗解读"（缺失才生成，适用于入门者）
    fdates_t = sorted({r.get('featured_date') for r in papers.values() if r.get('featured_date')}, reverse=True)
    feat_today = [r for r in papers.values() if r.get('featured_date') == (fdates_t[0] if fdates_t else '')][:5]
    need_fi = [r for r in feat_today if not r.get('interpret') or r.get('interpret_v') != 3]
    if need_fi:
        print(f'[featured] 为 {len(need_fi)} 篇精选生成解读（通俗易读版 v3）...', flush=True)
        ollama_points('预热', 'Warm-up sentence to load the model into GPU memory.')
        for r in need_fi:
            r['interpret'] = gen_featured_interpret(r.get('title', ''), r.get('abstract') or r.get('summary') or '')
            r['interpret_v'] = 3
        print(f'[featured] 精选解读完成', flush=True)
    # 每日热门 GitHub：累积式快照（保留历史，板块按多日累积展示）。
    # 仅覆盖当日；当日优先挑选「其他日尚未收录」的仓库，使每日自动追加新内容，
    # 多日累积后板块自然呈现约 15 条且不重复。保留近 60 天防止无限增长。
    gh_store = store.setdefault('github', {})
    cutoff = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y-%m-%d')
    for k in list(gh_store):
        if k < cutoff:
            del gh_store[k]
    others = set()
    for d, items in gh_store.items():
        if d == today:
            continue
        for p in items:
            fn = p.get('full_name') or p.get('name', '')
            if fn:
                others.add(fn)
    print(f'[github] 生成当日 Top-5（已收录 {len(others)} 个，优先追加新仓库）...', flush=True)
    gh_store[today] = fetch_github_projects(5, exclude=others)
    print(f'[github] 写入 {len(gh_store[today])} 个仓库（含中文介绍）', flush=True)
    save_store(store)

    # 全量排序（最新在前）
    recs_all = sorted(papers.values(), key=lambda r: r['ts'], reverse=True)
    # 论文板块：全部可溯源论文（含 HF Daily Papers 与每日精选，用徽章区分）
    recs = [r for r in recs_all if r.get('kind') == 'paper']
    # 科研社区热门：X 帖 + 资讯
    community_all = [r for r in recs_all if r.get('kind') != 'paper']
    community = []
    for day in sorted({r['date'] for r in community_all}, reverse=True):
        items = [r for r in community_all if r['date'] == day]
        items.sort(key=lambda r: r.get('ts', ''), reverse=True)
        community.append({'date': day, 'weekday': WEEK[datetime.strptime(day, '%Y-%m-%d').weekday()],
                          'items': items})
    # 今日精选数（最近一天 featured_date 的条目数，用于统计）
    fdates = sorted({r.get('featured_date') for r in recs_all if r.get('featured_date')}, reverse=True)
    feat_today_n = sum(1 for r in recs_all if r.get('featured_date') == (fdates[0] if fdates else ''))
    # 每日热门 GitHub 项目（最近一天）
    gh_dates = sorted(store.get('github', {}).keys(), reverse=True)
    gh = store.get('github', {}).get(gh_dates[0], []) if gh_dates else []
    for i, r in enumerate(recs, 1):
        r['num'] = i

    by_day = defaultdict(list)
    for r in recs:
        by_day[r['date']].append(r)
    timeline = []
    for day in sorted(by_day, reverse=True):
        d0 = datetime.strptime(day, '%Y-%m-%d')
        timeline.append({'date': day, 'weekday': WEEK[d0.weekday()], 'count': len(by_day[day])})

    payload = {
        'records': recs, 'community': community,
        'gh': gh, 'gh_date': gh_dates[0] if gh_dates else '',
        'timeline': timeline, 'domains': DOMAINS,
        'start_date': recs[-1]['date'] if recs else '', 'end_date': recs[0]['date'] if recs else '',
        'days': len(by_day), 'total': len(recs),
        'featured_count': feat_today_n, 'community_count': len(community_all),
        'points_count': sum(1 for r in recs if r.get('points')),
    }

    html = open('archive_template.html', encoding='utf-8').read()
    html = html.replace('/*__DATA__*/', json.dumps(payload, ensure_ascii=False))
    open('ai_paper_archive.html', 'w', encoding='utf-8').write(html)

    kinds = Counter(r['kind'] for r in recs_all)
    print(f'Built 论文 {len(recs)} / 社区热门 {len(community_all)} / GitHub {len(gh)} (新增 {added})；周期 {payload["start_date"]} -> {payload["end_date"]}，{payload["days"]} 天')
    print('kind balance:', dict(kinds))
    print('domain balance:', dict(Counter(r['domain'] for r in recs)))
    print('points:', payload['points_count'], '/', len(recs))

if __name__ == '__main__':
    main()
