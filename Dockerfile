# 仅使用经 Phase 3 核验、目标主机已缓存的固定材料；构建不得联网。
ARG BIRDS_BASE_IMAGE
FROM ${BIRDS_BASE_IMAGE}
ARG BIRDS_SOURCE_SHA
ARG BIRDS_BASE_IMAGE_ID
ARG BIRDS_PYTHON_TREE_SHA
LABEL org.birds.source-sha=${BIRDS_SOURCE_SHA}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIRDS_SOURCE_SHA=${BIRDS_SOURCE_SHA} BIRDS_BASE_IMAGE_ID=${BIRDS_BASE_IMAGE_ID} BIRDS_PYTHON_TREE_SHA=${BIRDS_PYTHON_TREE_SHA}
WORKDIR /app
# python/ 是已安装基础依赖的核验副本，不是 wheel 或开发机 venv。
COPY materials/python/ /opt/birds-python/
COPY materials/python-manifest.json /opt/birds-python-manifest.json
ENV PYTHONPATH=/opt/birds-python
COPY archive/ /app/archive/
COPY config/ /app/config/
COPY manage.py requirements.in requirements.lock /app/
COPY deploy/ /app/deploy/
RUN python -I -S /app/deploy/materials.py
RUN python -c "import sys,sqlite3,importlib.metadata as m; assert sys.version_info[:2]==(3,12); assert sqlite3.connect(':memory:').execute('select sqlite_source_id()').fetchone()[0]=='2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9'; assert all(m.version(k)==v for k,v in {'Django':'5.2.18','Pillow':'12.3.0','gunicorn':'26.2.0','whitenoise':'6.12.0'}.items())"
# 仅装配源码快照中的公开CSS/JS；不联网、不复制图库，也不关闭WhiteNoise掩盖缺目录。
RUN BIRDS_SECRET_KEY=offline-static-build-only python manage.py collectstatic --noinput
ENTRYPOINT ["/bin/sh", "/app/deploy/entrypoint.sh"]
