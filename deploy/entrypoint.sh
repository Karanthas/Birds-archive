#!/bin/sh
set -eu
umask 077
[ "$#" -ge 1 ] || { echo 'entrypoint_mode_invalid' >&2; exit 2; }
mode="$1"
shift
case "$mode" in init|web|worker|offline|backuparchive|restorearchive) ;; *) echo 'entrypoint_mode_invalid' >&2; exit 2;; esac
case "$mode" in init|web|worker) [ "$#" = 0 ] || { echo 'entrypoint_arguments_invalid' >&2; exit 2; };; esac
export BIRDS_DEPLOYMENT_MODE=1 BIRDS_RUNTIME_ROLE="$mode"
# -I -S先隔离PYTHONPATH/sitecustomize；精确核材料后才允许常规Django导入。
python -I -S /app/deploy/materials.py
if [ "$mode" = init ]; then
    python /app/manage.py checkruntime --deployment-init
    python /app/manage.py migrate --noinput
    python /app/manage.py checkruntime --deployment-finalize
    python /app/manage.py checkruntime --deployment-start
    exit 0
fi
if [ "$mode" = offline ]; then
    [ "$#" -ge 2 ] || { echo 'offline_arguments_invalid' >&2; exit 2; }
    session_id="$1"
    verb="$2"
    shift 2
    case "$verb" in
        importcatalog|loadguidebundle|preparemodel) ;;
        migrate) [ "$#" = 0 ] || { echo 'offline_arguments_invalid' >&2; exit 2; } ;;
        *) echo 'offline_command_not_allowed' >&2; exit 2 ;;
    esac
    exec python -m archive.services.maintenance "$session_id" "$verb" "$@"
fi
case "$mode" in
    # 备份核验可在源库不可用时运行；恢复从新根开始，由命令自行校验和持锁。
    backuparchive|restorearchive) exec python /app/manage.py "$mode" "$@" ;;
esac
python /app/manage.py checkruntime --deployment-start
case "$mode" in
    web) exec python -m gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 2 --timeout 60 --access-logfile - --no-control-socket ;;
    worker) exec python /app/manage.py runrecognitionworker ;;
esac
