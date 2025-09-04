#!/bin/bash
# 停止容器
docker stop dst

# 获取当前日期 (YYMMDD)
DATE=$(date +%y%m%d)
DST_DIR="/opt/klei/DoNotStarveTogether"
CLUSTER="Cluster_1"

cd "$DST_DIR" || { echo "找不到DST目录 $DST_DIR"; exit 1; }

echo "====== 今日已有备份 ======"
ls -1 Cluster_${DATE}*.zip 2>/dev/null || echo "暂无备份"
echo "========================"

# 找到当天最大编号
LAST_INDEX=$(ls Cluster_${DATE}*.zip 2>/dev/null \
  | sed -E "s/.*Cluster_${DATE}([0-9]{2})\.zip/\1/" \
  | sort -n | tail -n 1)

if [[ -z "$LAST_INDEX" ]]; then
  NEXT_INDEX="01"
else
  NEXT_INDEX=$(printf "%02d" $((10#$LAST_INDEX + 1)))
fi

BACKUP_NAME="Cluster_${DATE}${NEXT_INDEX}.zip"
echo "准备创建新备份: $BACKUP_NAME"

# 压缩
zip -r "$BACKUP_NAME" "$CLUSTER"

echo "备份完成: $DST_DIR/$BACKUP_NAME"

# 重新启动容器
docker start dst

echo "DST 容器已重启"
