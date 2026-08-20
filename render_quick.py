# -*- coding: utf-8 -*-
"""快速重渲染：从 archive_store.json 直接生成 ai_paper_archive.html / index.html（不调 Ollama，不重新生成要点）。"""
import json, os
from datetime import datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
DOMAINS = ['AI认知算法', 'LLM', 'AI Agent', '世界模型', '虚拟仿真社会', '具身智能']

store = json.load(open(os.path.join(HERE, 'archive_store.json'), encoding='utf-8'))
papers = store['papers']
recs_all = sorted(papers.values(), key=lambda r: r.get('ts', ''), reverse=True)
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
# 今日精选数（最近一天 featured_date）
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
html = open(os.path.join(HERE, 'archive_template.html'), encoding='utf-8').read()
html = html.replace('/*__DATA__*/', json.dumps(payload, ensure_ascii=False))
open(os.path.join(HERE, 'ai_paper_archive.html'), 'w', encoding='utf-8').write(html)
open(os.path.join(HERE, 'index.html'), 'w', encoding='utf-8').write(html)
print(f'rendered papers {len(recs)} / community {len(community_all)} / gh {len(gh)}, {len(by_day)} days')
