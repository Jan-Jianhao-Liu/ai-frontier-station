# -*- coding: utf-8 -*-
"""补修：对英文摘要但中文翻译过短/异常的条目，用加强 prompt 完整重翻（多行合并解析）。"""
import json, re, time, urllib.request

STORE = 'archive_store.json'
OLLAMA = 'http://127.0.0.1:11434/api/chat'

def has_cn(s):
    return bool(re.search(r'[\u4e00-\u9fff]', s or ''))

def translate(title, summary):
    body = json.dumps({
        'model': 'qwen3.5:4b', 'think': False, 'stream': False,
        'messages': [
            {'role': 'system', 'content': '你是专业学术翻译。把英文论文标题与摘要完整翻译成简体中文，必须逐句译完整个摘要，不得省略任何句子，禁止只输出链接或代码。只输出两行：\n标题：<中文标题>\n摘要：<中文摘要（完整）>'},
            {'role': 'user', 'content': f'标题：{title}\n摘要：{summary[:3500]}'}
        ]
    }).encode()
    for _ in range(3):
        try:
            req = urllib.request.Request(OLLAMA, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r).get('message', {}).get('content', '')
        except Exception as e:
            print(f'  retry {title[:16]} ({e})', flush=True)
            time.sleep(3)
    return ''

def parse(out):
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
    return tc, sc

def main():
    arch = json.load(open(STORE, encoding='utf-8'))
    fix = [(iid, p) for iid, p in arch['papers'].items()
           if p.get('kind') == 'paper' and p.get('abstract') and not has_cn(p.get('abstract'))
           and (not p.get('abstract_cn') or len(p.get('abstract_cn') or '') < max(60, len(p['abstract']) * 0.35))]
    print(f'[fix] 待重翻 {len(fix)} 条', flush=True)
    done = 0
    for iid, p in fix:
        out = translate(p.get('title', ''), p.get('abstract', ''))
        if out:
            tc, sc = parse(out)
            if tc and not has_cn(p.get('title', '')):
                p['title_cn'] = tc
            if sc:
                p['abstract_cn'] = sc
        done += 1
        if done % 10 == 0:
            print(f'  {done}/{len(fix)}', flush=True)
    json.dump(arch, open(STORE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    lens = [len(p.get('abstract_cn') or '') for iid, p in arch['papers'].items()
            if p.get('kind') == 'paper' and p.get('abstract') and not has_cn(p.get('abstract'))]
    print(f'[fix] 完成 {done} 条；英文摘要条目 abstract_cn avg {sum(lens) // max(1, len(lens))}，<200字 {sum(1 for l in lens if l < 200)}/{len(lens)}', flush=True)

if __name__ == '__main__':
    main()
