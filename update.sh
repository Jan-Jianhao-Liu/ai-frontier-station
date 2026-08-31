#!/usr/bin/env bash
# 每日更新脚本：增量追加当日新论文并累积，生成 index.html 并提交推送（GitHub Pages 自动发布）
set -e
cd "$(dirname "$0")"

PY="/c/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"

echo "[$(date +%F\ %T)] 增量更新档案库（累积模式）..."
"$PY" build_domains.py

echo "复制为 index.html ..."
cp ai_paper_archive.html index.html

git add index.html archive_store.json
if git diff --cached --quiet; then
  echo "无变化，跳过提交。"
else
  git commit -m "auto-update archive $(date +%F)"
  git push
  echo "已推送，GitHub Pages 将自动更新。"
fi

echo "同步到微信云开发数据库（papers/github/science/news）..."
"$PY" ../ai-miniapp/scripts/sync_to_cloud.py || echo "[warn] 云库同步失败，下次运行会自动重试"
